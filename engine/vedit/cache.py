"""The shared, global, content-addressed render cache.

Three properties are non-negotiable:

- **Atomic.** Everything is written to a temp name and ``os.replace``d into
  place. A killed render must never leave a truncated file claiming a valid key;
  that is the one failure the key alone cannot detect.
- **Self-describing.** Every artifact gets a ``<key>.json`` sidecar holding the
  verbatim key inputs. When something re-renders that should not have, you diff
  two sidecars and see exactly which field moved. Without it you are guessing.
- **Never invalidated.** The key IS the identity. A changed input produces a
  different key and the old artifact simply ages out via ``gc()``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from typing import Any, Iterable

from .ffmpeg import VeditError
from .keys import canonical_json, default_cache_root

KINDS = ("seg", "ovl", "html", "transcript")

# seg/ovl/html get a 2-char shard because they run to thousands of files and
# Explorer plus NTFS both degrade on a flat directory that size. transcript is
# one file per source, so it stays flat and matches the path already written
# into project.json.
_SHARDED = frozenset({"seg", "ovl", "html"})

_KEY_RE = re.compile(r"^[0-9a-zA-Z._-]+$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    kind      TEXT NOT NULL,
    key       TEXT NOT NULL,
    ext       TEXT NOT NULL,
    path      TEXT NOT NULL,
    bytes     INTEGER NOT NULL DEFAULT 0,
    created   REAL NOT NULL,
    last_used REAL NOT NULL,
    frames    INTEGER,
    render_ms INTEGER,
    PRIMARY KEY (kind, key, ext)
);
CREATE INDEX IF NOT EXISTS artifacts_lru ON artifacts (last_used);
"""


class CacheError(VeditError):
    """The cache could not honour a request."""


class Cache:
    """Content-addressed artifact store with an sqlite index beside it."""

    def __init__(self, root: str | None = None) -> None:
        self.root = os.path.abspath(root or default_cache_root())
        self.index_path = os.path.join(self.root, "index.sqlite")
        os.makedirs(self.root, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    # --- plumbing -------------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self.index_path, timeout=10.0, isolation_level=None)
            # WAL so a long render holding a read does not block a sibling agent
            # writing its own commit.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.executescript(_SCHEMA)
            conn.row_factory = sqlite3.Row
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Cache":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @staticmethod
    def _safe_key(key: str) -> str:
        """Strip the ``sha256:`` prefix and refuse anything unsafe as a filename.

        A colon in a Windows filename does not error -- it silently opens an NTFS
        alternate data stream, so ``sha256:abc.mp4`` writes zero bytes to a file
        that appears to exist at 0 length. Strip it before it costs an afternoon.
        """
        k = key.split(":", 1)[1] if ":" in key else key
        if not k or not _KEY_RE.match(k) or k in (".", "..") or "/" in k or "\\" in k:
            raise CacheError(f"unsafe cache key {key!r}")
        return k

    @staticmethod
    def _norm_ext(ext: str) -> str:
        if not ext:
            return ""
        return ext if ext.startswith(".") else "." + ext

    def _dir_for(self, kind: str, key: str) -> str:
        if kind not in KINDS:
            raise CacheError(f"unknown cache kind {kind!r}, expected one of {KINDS}")
        k = self._safe_key(key)
        if kind in _SHARDED:
            return os.path.join(self.root, kind, k[:2])
        return os.path.join(self.root, kind)

    # --- paths ----------------------------------------------------------------

    def path_for(self, kind: str, key: str, ext: str) -> str:
        """Absolute path an artifact of (kind, key, ext) must live at."""
        return os.path.join(self._dir_for(kind, key), self._safe_key(key) + self._norm_ext(ext))

    def sidecar_for(self, kind: str, key: str) -> str:
        """The ``<key>.json`` that records why this key is this key."""
        return os.path.join(self._dir_for(kind, key), self._safe_key(key) + ".json")

    def tmp_for(self, kind: str, key: str, ext: str) -> str:
        """A scratch path on the same volume, so commit() is a rename not a copy.

        The real extension stays LAST. ffmpeg picks its muxer from the filename
        extension, so a scratch name like ``<key>.mp4.tmp`` dies with
        "Unable to choose an output format" before it writes a byte.

        Lives in ``<root>/tmp/`` rather than beside the artifact so a crashed
        render leaves no debris in a kind directory that reconcile() scans.
        """
        d = os.path.join(self.root, "tmp")
        os.makedirs(d, exist_ok=True)
        fd, path = tempfile.mkstemp(dir=d, prefix=f"{kind}.{self._safe_key(key)}.", suffix=self._norm_ext(ext))
        os.close(fd)
        os.unlink(path)  # ffmpeg wants to create the file itself
        return path

    # --- read -----------------------------------------------------------------

    def has(self, kind: str, key: str, ext: str, expect_frames: int | None = None) -> bool:
        """True only if the artifact exists, is non-empty, and has the right length.

        ``expect_frames`` triggers an ffprobe ``-count_frames``. Pass it for video
        artifacts: a truncated file from a killed render is otherwise
        indistinguishable from a good one at the key level.
        """
        path = self.path_for(kind, key, ext)
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        if size <= 0:
            return False
        if expect_frames is not None:
            from .probe import count_frames  # local import: probe pulls in ffprobe

            try:
                actual = count_frames(path)
            except (VeditError, OSError, ValueError):
                return False
            if actual != expect_frames:
                return False
        self.touch(kind, key, ext)
        return True

    def get(self, kind: str, key: str, ext: str) -> str | None:
        """Path if present (no integrity check), else None."""
        path = self.path_for(kind, key, ext)
        return path if os.path.exists(path) else None

    def meta(self, kind: str, key: str) -> dict[str, Any] | None:
        """The sidecar contents, for diffing two keys that should have matched."""
        try:
            with open(self.sidecar_for(kind, key), "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def touch(self, kind: str, key: str, ext: str) -> None:
        self.conn.execute(
            "UPDATE artifacts SET last_used=? WHERE kind=? AND key=? AND ext=?",
            (time.time(), kind, self._safe_key(key), self._norm_ext(ext)),
        )

    # --- write ----------------------------------------------------------------

    def commit(self, tmp_path: str, kind: str, key: str, ext: str, meta: dict) -> str:
        """Atomically publish ``tmp_path`` as (kind, key, ext). Returns final path.

        Sidecar first, artifact second: ``has()`` keys off the artifact, so the
        key is not claimed until the bytes are in place. The reverse order would
        let a crash between the two publish an artifact nobody can explain.
        """
        if not os.path.isfile(tmp_path):
            raise CacheError(f"nothing to commit at {tmp_path}")
        size = os.path.getsize(tmp_path)
        if size <= 0:
            raise CacheError(f"refusing to commit a zero-byte artifact: {tmp_path}")

        final = self.path_for(kind, key, ext)
        os.makedirs(os.path.dirname(final), exist_ok=True)

        sidecar = {
            "key": self._safe_key(key),
            "kind": kind,
            "ext": self._norm_ext(ext),
            "bytes": size,
            "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "inputs": meta,
        }
        self._atomic_write_text(self.sidecar_for(kind, key), canonical_json(sidecar))

        try:
            os.replace(tmp_path, final)
        except OSError:
            # Only happens across volumes (a caller's own scratch dir on another
            # drive). Stage beside the target so the publish itself stays atomic.
            staged = final + ".stage"
            shutil.copyfile(tmp_path, staged)
            os.replace(staged, final)
            os.unlink(tmp_path)

        now = time.time()
        self.conn.execute(
            "INSERT INTO artifacts (kind,key,ext,path,bytes,created,last_used,frames,render_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(kind,key,ext) DO UPDATE SET path=excluded.path, bytes=excluded.bytes, "
            "last_used=excluded.last_used, frames=excluded.frames, render_ms=excluded.render_ms",
            (
                kind,
                self._safe_key(key),
                self._norm_ext(ext),
                final,
                size,
                now,
                now,
                meta.get("frames") if isinstance(meta, dict) else None,
                meta.get("render_ms") if isinstance(meta, dict) else None,
            ),
        )
        return final

    @staticmethod
    def _atomic_write_text(path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # --- maintenance ----------------------------------------------------------

    def reconcile(self) -> int:
        """Adopt on-disk artifacts the index does not know about.

        The index is a convenience, not the truth -- the files are. Losing
        index.sqlite must not orphan 80 GB of renders.
        """
        added = 0
        now = time.time()
        for kind in KINDS:
            base = os.path.join(self.root, kind)
            if not os.path.isdir(base):
                continue
            for dirpath, _dirnames, filenames in os.walk(base):
                for name in filenames:
                    is_sidecar = name.endswith(".json") and not name.endswith(".words.json")
                    if name.endswith(".tmp") or is_sidecar:
                        continue
                    full = os.path.join(dirpath, name)
                    key, ext = _split_key_ext(name)
                    row = self.conn.execute(
                        "SELECT 1 FROM artifacts WHERE kind=? AND key=? AND ext=?", (kind, key, ext)
                    ).fetchone()
                    if row:
                        continue
                    try:
                        st = os.stat(full)
                    except OSError:
                        continue
                    self.conn.execute(
                        "INSERT OR REPLACE INTO artifacts (kind,key,ext,path,bytes,created,last_used) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (kind, key, ext, full, st.st_size, st.st_mtime, min(st.st_mtime, now)),
                    )
                    added += 1
        return added

    def prune_tmp(self, older_than_hours: float = 24.0) -> int:
        """Delete abandoned scratch files. A killed render leaves one behind."""
        d = os.path.join(self.root, "tmp")
        if not os.path.isdir(d):
            return 0
        cutoff = time.time() - older_than_hours * 3600
        removed = 0
        for name in os.listdir(d):
            full = os.path.join(d, name)
            try:
                if os.path.isfile(full) and os.path.getmtime(full) < cutoff:
                    os.unlink(full)
                    removed += 1
            except OSError:
                pass  # a render still holding the handle keeps its file
        return removed

    def stats(self) -> dict[str, Any]:
        row = self.conn.execute("SELECT COUNT(*) n, COALESCE(SUM(bytes),0) b FROM artifacts").fetchone()
        return {"files": int(row["n"]), "bytes": int(row["b"]), "gb": int(row["b"]) / 1e9}

    def gc(self, max_gb: float, keep_days: int, protect: set[str]) -> dict:
        """LRU eviction by ``last_used``. Keys in ``protect`` are never touched.

        Two passes: drop everything older than ``keep_days``, then, if still over
        ``max_gb``, evict least-recently-used until under budget. Protected keys
        count toward the total but are never candidates -- an over-budget cache
        that is entirely in use shrinks to nothing rather than breaking a render.
        """
        protected = {self._safe_key(p) for p in (protect or set())}
        self.reconcile()
        orphan_tmp = self.prune_tmp()

        max_bytes = int(max_gb * 1e9)
        cutoff = time.time() - keep_days * 86400
        rows = [dict(r) for r in self.conn.execute("SELECT * FROM artifacts ORDER BY last_used ASC")]

        total = sum(int(r["bytes"]) for r in rows)
        deleted: list[str] = []
        freed = 0

        def drop(row: dict) -> None:
            nonlocal freed
            self._delete_row(row)
            deleted.append(row["path"])
            freed_bytes = int(row["bytes"])
            freed += freed_bytes

        survivors: list[dict] = []
        for row in rows:
            if row["key"] in protected:
                survivors.append(row)
                continue
            if keep_days >= 0 and float(row["last_used"]) < cutoff:
                drop(row)
                total -= int(row["bytes"])
            else:
                survivors.append(row)

        for row in survivors:
            if total <= max_bytes:
                break
            if row["key"] in protected:
                continue
            drop(row)
            total -= int(row["bytes"])

        return {
            "scanned": len(rows),
            "orphan_tmp_removed": orphan_tmp,
            "deleted": len(deleted),
            "freed_bytes": freed,
            "freed_gb": freed / 1e9,
            "kept_bytes": total,
            "kept_gb": total / 1e9,
            "protected": len(protected),
            "deleted_paths": deleted,
        }

    def _delete_row(self, row: dict) -> None:
        for path in (row["path"], _sidecar_of(row["path"], row["ext"])):
            try:
                if path and os.path.isfile(path):
                    os.unlink(path)
            except OSError:
                pass  # a locked file just survives until the next gc
        self.conn.execute(
            "DELETE FROM artifacts WHERE kind=? AND key=? AND ext=?", (row["kind"], row["key"], row["ext"])
        )

    def protect_keys_from_project(self, project: Any) -> set[str]:
        """Collect every cache key the project currently references.

        Used as the ``protect`` argument to gc(): evicting a key the open project
        points at turns the next render from instant into a full re-encode.
        """
        keys: set[str] = set()
        data = getattr(project, "data", project)
        for seg in data.get("segments", []) or []:
            key = (seg.get("render") or {}).get("cache_key")
            if key:
                keys.add(key)
        for ovl in data.get("overlays", []) or []:
            key = (ovl.get("render") or {}).get("cache_key")
            if key:
                keys.add(key)
        return keys


def _split_key_ext(name: str) -> tuple[str, str]:
    """``abc.words.json`` -> (abc, .words.json); ``abc.mp4`` -> (abc, .mp4)."""
    head, _, _ = name.partition(".")
    return head, name[len(head) :]


def _sidecar_of(path: str, ext: str) -> str:
    return path[: len(path) - len(ext)] + ".json" if ext and path.endswith(ext) else ""


def open_cache(root: str | None = None) -> Cache:
    """Convenience for callers that do not want to remember the default root."""
    return Cache(root)


def iter_keys(paths: Iterable[str]) -> set[str]:
    return {os.path.splitext(os.path.basename(p))[0] for p in paths}
