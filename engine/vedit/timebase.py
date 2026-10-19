"""The one authority for frame math.

Integer frames on ONE project timebase are the only authority in this pipeline
(CONTRACT section 0.2). Seconds exist only as an emission format for ffmpeg and
inside ``project.json._computed``; nothing downstream reads them back.

The rounding asymmetry below is the single most load-bearing detail in the repo.
``out`` is EXCLUSIVE because ffmpeg implements ``trim=end`` as a strict ``<``.
Rounding OUT the same way as IN makes every segment one frame long, which at 200
cuts is ~6.7 s of drift and audio that ends visibly out of sync.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Any


@dataclass(frozen=True)
class Timebase:
    """A constant, rational frame rate plus the audio sample rate it rides on.

    Frozen because a timebase change invalidates every integer in the project;
    it must be a new object, not a mutation nobody noticed.
    """

    fps_num: int
    fps_den: int
    sample_rate: int = 48000

    def __post_init__(self) -> None:
        if self.fps_num <= 0 or self.fps_den <= 0:
            raise ValueError(f"invalid frame rate {self.fps_num}/{self.fps_den}")
        if self.sample_rate <= 0:
            raise ValueError(f"invalid sample rate {self.sample_rate}")

    # --- rate -----------------------------------------------------------------

    @property
    def fps(self) -> float:
        return self.fps_num / self.fps_den

    def as_fraction(self) -> Fraction:
        """Exact rate, for arithmetic that must not accumulate float error."""
        return Fraction(self.fps_num, self.fps_den)

    # --- snapping -------------------------------------------------------------

    def snap_in(self, seconds: float) -> int:
        """Seconds -> INCLUSIVE start frame. Round half UP: ties land later."""
        # CONTRACT section 4, verbatim. Do not "simplify" to round().
        return math.floor(seconds * self.fps + 0.5)

    def snap_out(self, seconds: float) -> int:
        """Seconds -> EXCLUSIVE end frame. Round half DOWN: ties land earlier."""
        # CONTRACT section 4, verbatim.
        return math.ceil(seconds * self.fps - 0.5)

    def snap_pair(self, t_in: float, t_out: float) -> tuple[int, int]:
        """Snap both ends and apply the non-empty guard from CONTRACT section 4.

        snap_out() alone cannot apply the guard: it does not know the IN frame.
        Anything building a segment should use this, not the two halves.
        """
        in_frame = self.snap_in(t_in)
        out_frame = self.snap_out(t_out)
        if out_frame <= in_frame:
            out_frame = in_frame + 1
        return in_frame, out_frame

    def seconds_to_frames_delta(self, seconds: float) -> int:
        """A DURATION in seconds -> a whole number of frames ("200 ms earlier").

        Sign-preserving round-half-up on the magnitude, so -0.2 s and +0.2 s move
        the same distance. At 29.97: 0.200 * 29.97 = 5.994 -> 6 frames.
        """
        sign = -1 if seconds < 0 else 1
        return sign * math.floor(abs(seconds) * self.fps + 0.5)

    # --- conversion out -------------------------------------------------------

    def frames_to_seconds(self, frames: int) -> float:
        return frames * self.fps_den / self.fps_num

    def secs(self, frames: int) -> str:
        """Frames -> the exact string handed to ffmpeg.

        Fixed 6 decimals, culture-invariant by construction (Python floats never
        render a comma). Anything that formats times for an argv list uses this.
        """
        return f"{frames * self.fps_den / self.fps_num:.6f}"

    def frames_to_samples(self, frames: int) -> int:
        """Frames -> audio samples, exactly.

        Rational arithmetic, not float: at 30000/1001 the float path drifts and
        adelay=<n>S wants an exact integer sample index.
        """
        exact = Fraction(frames * self.sample_rate * self.fps_den, self.fps_num)
        return int(exact.numerator // exact.denominator) if exact.denominator == 1 else _round_half_up(exact)

    def samples_to_frames(self, samples: int) -> int:
        """Inverse of frames_to_samples, rounded half up. For QC only."""
        return _round_half_up(Fraction(samples * self.fps_num, self.sample_rate * self.fps_den))

    # --- serialization --------------------------------------------------------

    def to_dict(self) -> dict[str, int]:
        return {"fps_num": self.fps_num, "fps_den": self.fps_den, "sample_rate": self.sample_rate}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Timebase":
        return cls(
            fps_num=int(d["fps_num"]),
            fps_den=int(d["fps_den"]),
            sample_rate=int(d.get("sample_rate", 48000)),
        )


def _round_half_up(f: Fraction) -> int:
    return math.floor(f + Fraction(1, 2))
