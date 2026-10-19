"""vedit -- the core data layer of the Editing pipeline.

Stdlib only. Nothing in this package may import a third-party module: the main
pipeline runs on the system Python 3.14 and the moment someone reaches for numpy
this becomes a wheel-availability tax forever. Heavy ML lives in .venv-asr
(Python 3.12) and is reached only through subprocess.

Import surface:

    from vedit import Timebase, Project, Cache
    from vedit import probe, count_frames, concat_compatible, is_vfr
    from vedit import fast_key, strong_key, cache_key, canonical_json
    from vedit import run, ffprobe_json, write_filtergraph, escape_filter_path

Note ``vedit.probe`` is the FUNCTION here (the contract's API). The module is
reachable as ``from vedit.probe import probe, count_frames``.
"""

from __future__ import annotations

__version__ = "1.0.0"

from .cache import KINDS, Cache, CacheError
from .ffmpeg import (
    FFMPEG,
    FFPROBE,
    FFmpegError,
    VeditError,
    escape_filter_path,
    escape_filter_path_nested,
    ffprobe_json,
    filter_complex_args,
    run,
    tool_versions,
    write_filtergraph,
)
from .keys import (
    cache_key,
    canonical_json,
    default_cache_root,
    fast_key,
    repo_root,
    strong_key,
)
from .probe import ProbeError, concat_compatible, count_audio_samples, count_frames, is_vfr, probe
from .project import SCHEMA_VERSION, Project, ProjectError
from .timebase import Timebase

# The pipeline lanes. Imported eagerly (+23 ms measured over the core, which is
# noise next to one ffprobe) so that every signature CONTRACT section 5 names is
# reachable from the package, not only from its own module. Three separate
# slices shipped assuming `from vedit import run_qc` works.
#
# Module objects are bound too -- `from vedit import cut, render` -- EXCEPT for
# probe, which stays the FUNCTION here because the contract's API lists it that
# way. `vedit.probe` the module is still `from vedit.probe import ...`.
from . import audio, captions, cut, fillers, gfx, qc, render, transcript  # noqa: E402
from .audio import build_audio_graph, loudnorm_two_pass, render_audio  # noqa: E402
from .captions import build_ass, estimate_text_width_px, group_words  # noqa: E402
from .cut import build_segments, detect_fillers, detect_retakes, detect_silence_cuts  # noqa: E402
from .gfx import chrome_screenshot, frames_to_alpha_movie, render_html_frames  # noqa: E402
from .qc import run_qc  # noqa: E402
from .render import concat_segments, mux, render_all, render_segment  # noqa: E402
from .transcript import flatten_text, load_words, normalize_whisperx  # noqa: E402

__all__ = [
    "__version__",
    "SCHEMA_VERSION",
    "KINDS",
    # errors
    "VeditError",
    "FFmpegError",
    "ProbeError",
    "ProjectError",
    "CacheError",
    # timebase
    "Timebase",
    # probe
    "probe",
    "count_frames",
    "count_audio_samples",
    "concat_compatible",
    "is_vfr",
    # keys
    "fast_key",
    "strong_key",
    "cache_key",
    "canonical_json",
    "repo_root",
    "default_cache_root",
    # ffmpeg
    "FFMPEG",
    "FFPROBE",
    "run",
    "ffprobe_json",
    "write_filtergraph",
    "filter_complex_args",
    "escape_filter_path",
    "escape_filter_path_nested",
    "tool_versions",
    # cache / project
    "Cache",
    "Project",
    # lane modules
    "transcript",
    "cut",
    "fillers",
    "audio",
    "render",
    "captions",
    "gfx",
    "qc",
    # transcript
    "normalize_whisperx",
    "load_words",
    "flatten_text",
    # cut
    "detect_silence_cuts",
    "detect_fillers",
    "detect_retakes",
    "build_segments",
    # audio
    "build_audio_graph",
    "render_audio",
    "loudnorm_two_pass",
    # render
    "render_segment",
    "concat_segments",
    "mux",
    "render_all",
    # captions
    "build_ass",
    "group_words",
    "estimate_text_width_px",
    # gfx
    "render_html_frames",
    "frames_to_alpha_movie",
    "chrome_screenshot",
    # qc
    "run_qc",
]
