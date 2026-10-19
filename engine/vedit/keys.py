"""Content identity: two tiers for source files, one canonical form for keys.

Measured on this machine: sha256 runs at ~1864 MB/s, a head+tail 8 MiB probe
takes ~10 ms. So a 20 GB source costs ~11 s to hash once and 10 ms to
re-validate. That gap is the whole reason for the two tiers.

mtime alone is NEVER identity: cloud sync and plain file copies change it
without changing content, and touch it without the content changing.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from typing import Any

_CHUNK = 4 * 1024 * 1024
_PROBE_BYTES = 8 * 1024 * 1024

_ALGO = "sha256"
_INDEX_VERSION = 2


def repo_root() -> str:
    """The ``Editing/`` directory. This file lives at engine/vedit/keys.py."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def default_cache_root() -> str:
    """The one shared, global, content-addressed cache (CONTRACT section 2).

    Global on purpose: two projects using the same B-roll clip share its renders.
    ``VEDIT_CACHE`` overrides it, which is how tests stay out of the real cache.
    """
    return os.environ.get("VEDIT_CACHE") or os.path.join(repo_root(), ".cache")


# --- canonical serialization --------------------------------------------------


def _canonicalize(obj: Any) -> Any:
    """Normalize a value so equal payloads always produce equal bytes.

    The rule that matters: a float holding an integral value collapses to an int.
    ``speed: 1.0`` read back from JSON and ``speed: 1`` written by Python must
    hash the same, or every round-trip through project.json is a silent 100%
    cache miss.
    """
    if obj is None or isinstance(obj, bool):
        # bool before int: bool IS an int subclass and True must stay `true`.
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            raise ValueError("NaN/Infinity cannot appear in a cache key")
        if obj.is_integer():
            return int(obj)
        return obj
    if isinstance(obj, str):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_canonicalize(x) for x in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if not isinstance(k, str):
                # Silent str() coercion here would let 1 and "1" collide.
                raise TypeError(f"cache-key dict keys must be str, got {type(k).__name__}")
            out[k] = _canonicalize(v)
        return out
    raise TypeError(f"cannot canonicalize {type(obj).__name__} in a cache key")


def canonical_json(obj: Any) -> str:
    """Stable JSON: sorted keys, no whitespace, integral floats as ints.

    Non-integral floats use Python's repr, which is the shortest round-tripping
    decimal and has been stable since 3.1. Keep floats out of keys anyway --
    that is why this pipeline stores integers.
    """
    return json.dumps(
        _canonicalize(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def cache_key(payload: dict) -> str:
    """Content-addressed key: first 32 hex chars of sha256(canonical_json)."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:32]


# --- file content keys --------------------------------------------------------


def fast_key(path: str) -> str:
    """sha256(first 8 MiB || last 8 MiB || size). ~10 ms. For REVALIDATION only.

    Never store this as a source's identity: two different 30-minute recordings
    from the same camera share a header and a size class far more often than is
    comfortable. It answers "did this file change?", not "which file is this?".
    """
    size = os.path.getsize(path)
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        if size <= 2 * _PROBE_BYTES:
            while chunk := fh.read(_CHUNK):
                h.update(chunk)
        else:
            h.update(fh.read(_PROBE_BYTES))
            fh.seek(size - _PROBE_BYTES)
            h.update(fh.read(_PROBE_BYTES))
    h.update(str(size).encode("ascii"))
    return f"{_ALGO}:{h.hexdigest()}"


def _hash_whole(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return f"{_ALGO}:{h.hexdigest()}"


def _index_path(cache_root: str | None) -> str:
    return os.path.join(cache_root or default_cache_root(), "source_index.json")


def _load_index(index_file: str) -> dict[str, Any]:
    try:
        with open(index_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"version": _INDEX_VERSION, "entries": {}}
    if data.get("version") != _INDEX_VERSION or not isinstance(data.get("entries"), dict):
        return {"version": _INDEX_VERSION, "entries": {}}
    return data


def _store_entry(index_file: str, ident: str, entry: dict[str, Any]) -> None:
    """Re-read, merge, atomically replace.

    Several agents may hash different sources at the same time; a blind
    read-modify-write from a stale in-memory copy would drop their entries.
    """
    os.makedirs(os.path.dirname(index_file), exist_ok=True)
    data = _load_index(index_file)
    data["entries"][ident] = entry
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(index_file), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, index_file)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def strong_key(path: str, *, cache_root: str | None = None) -> str:
    """sha256 of the whole file, memoized on (abspath, size, mtime_ns).

    Three tiers, cheapest first:
      1. (size, mtime_ns) match the index  -> free.
      2. they do not, but fast_key matches -> the file was touched or re-synced;
         reuse the strong key and refresh the stat. ~10 ms.
      3. fast_key differs                  -> pay the full hash.
    """
    abspath = os.path.abspath(path)
    st = os.stat(abspath)
    ident = os.path.normcase(abspath)
    index_file = _index_path(cache_root)
    index = _load_index(index_file)
    entry = index["entries"].get(ident)

    if entry and entry.get("size") == st.st_size and entry.get("mtime_ns") == st.st_mtime_ns:
        return entry["strong_key"]

    probe = fast_key(abspath)
    if entry and entry.get("fast_key") == probe and entry.get("strong_key"):
        key = entry["strong_key"]
    else:
        key = _hash_whole(abspath)

    _store_entry(
        index_file,
        ident,
        {
            "path": abspath,
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "fast_key": probe,
            "strong_key": key,
            "checked_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    return key
