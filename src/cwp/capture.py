"""Capture (plan.md §14 Step 6): local faster-whisper transcription → ``capture/transcript.txt``.

Powers ``cwp capture <id> --audio <path>`` (import-only; live mic ``--record`` is v3).
The child's voice never leaves the machine: Whisper runs locally (research doc §4), and
the WHOLE ``capture/`` dir is git-ignored (§4.3) so verbatim child speech never reaches
the public repo.

The seam: :func:`transcribe_audio` is the ONE faster-whisper touchpoint — tests replace
it with a canned :class:`TranscriptResult` (text + per-segment ``avg_logprob``), so the
redaction scan and the confidence heuristic are testable without the real model. The
``faster_whisper`` import is LAZY (inside the function) — ``cwp --help`` must never pay
model-library startup cost (tests/test_cli.py enforces this). Transcribe kwargs are
pinned per the plan: ``language="en"``, ``vad_filter=True``, ``beam_size=5``, and the
toy-vocabulary :data:`TOY_VOCABULARY_PROMPT` — real WER levers for ~25%-WER child speech.
Default model ``small``; ``--model medium`` is the escalation flag.

Redact-names scan (§4.3): ``private/redact-names.txt`` (git-ignored; one
``real-name = nickname`` pair per line; ``#`` comments allowed, whole-line or trailing;
read as ``utf-8-sig`` so a BOM'd Windows file cannot silently poison the first name) is
applied to the transcript BEFORE writing — case-insensitive WHOLE-NAME replace,
longest real name first, nickname inserted literally (never a regex template),
redact-by-default. Hyphens are name-internal: "Kai" never fires inside "Kai-Lan" — a
hyphenated compound is a distinct identity, so list it as its own pair.
``--allow-names`` skips the scan; an ABSENT file is a no-op plus a one-time (per
invocation) stderr warning (:data:`UNSCANNED_NOTICE`). A malformed pair line raises
:class:`CaptureError` — a silent parse-skip could leak a real name into a text artifact.

Low-confidence heuristic: mean segment ``avg_logprob`` < :data:`LOW_CONFIDENCE_MEAN_LOGPROB`
OR transcript ≤ :data:`LOW_CONFIDENCE_MAX_WORDS` words → the CLI prints
:data:`RERECORD_HINT` on stderr. The transcript is STILL written and the exit code stays
0 — the transcript is noisy input by design; re-record is the operator's call.

ffmpeg (the Step 6 verify, resolved): system ffmpeg is NOT required — faster-whisper
decodes via bundled PyAV (``faster_whisper/audio.py`` sources ``av``, whose wheel ships
the FFmpeg DLLs; verified by decoding an MP3 with ffmpeg stripped from PATH). No
``shutil.which("ffmpeg")`` preflight is needed.

Exit-code mapping (cli.py): :class:`CaptureError` (missing audio file, bad redact file —
an :class:`cwp.episodes.EpisodeError`) → 1; :class:`CaptureEnvError` (whisper
import/model/decode failure) → 2.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cwp import episodes
from cwp.config import DEFAULT_WHISPER_MODEL

TRANSCRIPT_FILENAME = "transcript.txt"
WHISPER_MODELS = ("small", "medium")  # default + the one escalation step (plan §2; no tiny/base)

# Low-confidence heuristic thresholds — TUNE AFTER M2, when real kid clips exist to
# calibrate against (these are provisional, pinned in plan.md §14 Step 6).
LOW_CONFIDENCE_MEAN_LOGPROB = -1.0  # mean segment avg_logprob below this → likely garbled
LOW_CONFIDENCE_MAX_WORDS = 2  # a transcript this short → probably a failed take

# Toy-vocabulary initial prompt — a real WER lever for child speech (research doc §4):
# biases decoding toward the words a 4-year-old actually uses when wishing up a toy.
TOY_VOCABULARY_PROMPT = (
    "A little kid describes a toy he wants: dinosaurs, socks, cookies, buttons, roar, "
    "stomp, robot, rocket, colors, counting, guessing, matching, silly, big, tiny."
)

# The one-time (per invocation) absent-redact-file line the CLI prints on stderr (§4.3),
# under the established `cwp <cmd>: warning:` prefix vocabulary.
UNSCANNED_NOTICE = (
    "text artifacts are unscanned — create private/redact-names.txt to enable redaction"
)
# The low-confidence hint the CLI prints on stderr (transcript still written, exit 0).
RERECORD_HINT = "transcript looks garbled — consider re-recording"

ScanState = Literal["scanned", "skipped", "unscanned"]


class CaptureError(episodes.EpisodeError):
    """User-input failures around capture (CLI maps to exit 1, like all EpisodeErrors)."""


class CaptureEnvError(Exception):
    """Environment failures around faster-whisper — import, model download/load, audio
    decode (CLI maps to exit 2)."""


@dataclass(frozen=True)
class Segment:
    """One transcription segment: its text + Whisper's ``avg_logprob`` confidence signal."""

    text: str
    avg_logprob: float


@dataclass(frozen=True)
class TranscriptResult:
    """What :func:`transcribe_audio` returns — everything the heuristic + scan need.

    ``text`` is the joined, stripped transcript; ``segments`` carry per-segment
    ``avg_logprob`` so the confidence heuristic is testable with canned values.
    """

    text: str
    segments: tuple[Segment, ...]


@dataclass(frozen=True)
class CaptureResult:
    """What :func:`run_capture` did (the CLI renders this)."""

    transcript_path: Path
    text: str  # the FINAL written text (post-redaction)
    word_count: int
    redacted_count: int  # whole-word replacements made (0 unless scan_state == "scanned")
    scan_state: ScanState
    low_confidence_reason: str | None  # None = looks fine; else feeds RERECORD_HINT


def transcribe_audio(audio_path: Path, model_size: str) -> TranscriptResult:
    """THE transcription seam — the only faster-whisper touchpoint; tests replace this.

    Lazy-imports ``faster_whisper`` (module docstring: CLI startup latency). Model runs
    on CPU with int8 compute — no GPU-driver surprises on the target laptop; ``medium``
    on CPU is the supported escalation. First run downloads the model, then offline.
    Raises :class:`CaptureEnvError` for every environment failure (CLI exit 2).
    """
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:  # ImportError, or a broken native dep chain (ctranslate2 DLLs)
        raise CaptureEnvError(
            f"faster-whisper unavailable ({type(exc).__name__}: {exc}) — reinstall: uv sync"
        ) from exc
    try:
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        # transcribe() returns a LAZY generator — transcription happens during iteration,
        # so materializing the tuple must stay inside this try.
        segment_iter, _info = model.transcribe(
            str(audio_path),
            language="en",
            vad_filter=True,
            beam_size=5,
            initial_prompt=TOY_VOCABULARY_PROMPT,
        )
        segments = tuple(
            Segment(text=str(segment.text), avg_logprob=float(segment.avg_logprob))
            for segment in segment_iter
        )
    except Exception as exc:  # model download/load or PyAV decode — env boundary, exit 2
        raise CaptureEnvError(
            f"faster-whisper transcription failed ({type(exc).__name__}: {exc}) — the model"
            " downloads once on first run (needs network); also check the audio file decodes"
        ) from exc
    text = "".join(segment.text for segment in segments).strip()
    return TranscriptResult(text=text, segments=segments)


# A trailing comment on a pair line: whitespace then '#' to end of line. A nickname
# therefore cannot contain ' #' — the loud malformed-line error catches the fallout
# (an emptied nickname), never a silent one.
_INLINE_COMMENT_RE = re.compile(r"\s+#.*$")

# Name boundaries: like \b, but hyphens count as name-INTERNAL characters, so a listed
# "Kai" never fires inside the distinct compound name "Kai-Lan" (bare \b treats the
# hyphen as a boundary and would corrupt "Kai-Lan" → "<nickname>-Lan"). The residual
# gap is deliberate: a compound that ISN'T listed keeps its embedded name — list the
# compound as its own pair to redact it (longest-first ordering makes both coexist).
_NAME_BOUNDARY_START = r"(?<![\w-])"
_NAME_BOUNDARY_END = r"(?![\w-])"


def load_redact_names(redact_path: Path) -> tuple[tuple[str, str], ...] | None:
    """Parse ``private/redact-names.txt`` → ``(real_name, nickname)`` pairs (§4.3).

    ``None`` means the file is ABSENT (caller prints the one-time unscanned warning).
    One ``real-name = nickname`` pair per line; blank lines and ``#`` comments (whole-line
    or trailing) allowed. Read as ``utf-8-sig``: PowerShell's ``Set-Content -Encoding
    utf8`` and Notepad emit a BOM, which would otherwise glue U+FEFF onto the first real
    name and make it silently never match (a documented workspace landmine). A malformed
    or empty-sided line raises :class:`CaptureError` (exit 1) — redaction is privacy
    enforcement, so a silently skipped line is worse than a loud stop.
    """
    try:
        raw = redact_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise CaptureError(f"{redact_path} unreadable: {exc}") from exc
    pairs: list[tuple[str, str]] = []
    for lineno, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stripped = _INLINE_COMMENT_RE.sub("", stripped).strip()
        real, sep, nickname = stripped.partition("=")
        real, nickname = real.strip(), nickname.strip()
        if not sep or not real or not nickname:
            raise CaptureError(
                f"{redact_path} line {lineno}: expected 'real-name = nickname', got"
                f" {stripped!r} — fix the line so redaction cannot silently miss a name"
            )
        pairs.append((real, nickname))
    return tuple(pairs)


def _literal_replacement(nickname: str) -> Callable[[re.Match[str]], str]:
    """A replacement CALLABLE so the nickname is inserted literally, never interpreted
    as a regex template: a backslash nickname (``C:\\Users\\Kai``) must not crash the
    scan (uncaught ``re.error``), and a template token (``\\g<0>``) must not re-insert
    the matched real name into the transcript while counting as a redaction."""

    def replace(_match: re.Match[str]) -> str:
        return nickname

    return replace


def redact_text(text: str, pairs: tuple[tuple[str, str], ...]) -> tuple[str, int]:
    """Case-insensitive WHOLE-NAME replace of each real name with its nickname (§4.3).

    Returns ``(redacted_text, total_replacements)``.

    - **Longest real name first** (regardless of file order): with both "Kai = Buddy"
      and "Kai Long = KL" configured, "Kai Long" always becomes "KL" — never
      "Buddy Long" with the surname leaking.
    - **Nickname is literal** — see :func:`_literal_replacement`.
    - **Name boundaries, hyphen-aware** — "Kai" never fires inside "Kaiju" OR "Kai-Lan";
      multi-word and hyphenated real names match as written (spaces/hyphens literal).
    """
    total = 0
    for real, nickname in sorted(pairs, key=lambda pair: len(pair[0]), reverse=True):
        pattern = re.compile(
            rf"{_NAME_BOUNDARY_START}{re.escape(real)}{_NAME_BOUNDARY_END}", re.IGNORECASE
        )
        text, count = pattern.subn(_literal_replacement(nickname), text)
        total += count
    return text, total


def assess_confidence(result: TranscriptResult) -> str | None:
    """The low-confidence heuristic (module docstring); returns the reason or ``None``.

    Word count uses the transcript text; the logprob leg needs at least one segment
    (a canned/empty result with no segments is judged on length alone).
    """
    word_count = len(result.text.split())
    if word_count <= LOW_CONFIDENCE_MAX_WORDS:
        return f"only {word_count} word(s) transcribed"
    if result.segments:
        mean = sum(segment.avg_logprob for segment in result.segments) / len(result.segments)
        if mean < LOW_CONFIDENCE_MEAN_LOGPROB:
            return f"mean segment avg_logprob {mean:.2f} < {LOW_CONFIDENCE_MEAN_LOGPROB}"
    return None


def run_capture(
    episodes_dir: Path,
    redact_path: Path,
    id_or_seq: str,
    audio_path: Path,
    *,
    model_size: str = DEFAULT_WHISPER_MODEL,
    allow_names: bool = False,
) -> CaptureResult:
    """The full ``cwp capture`` flow behind the CLI: resolve → transcribe → scan → write.

    The scan runs BEFORE the write (§4.3 — the unredacted transcript never touches disk).
    ``capture/`` is created if missing (the whole dir is git-ignored, so a clone or a
    hand-deleted folder must not break capture). The transcript is written atomically
    (UTF-8, trailing newline) even when low-confidence — re-record is a hint, not a gate.
    """
    directory, _episode = episodes.load_episode(episodes_dir, id_or_seq)
    if not audio_path.is_file():
        raise CaptureError(f"Audio file not found: {audio_path}")
    result = transcribe_audio(audio_path, model_size)

    text = result.text.strip()
    redacted_count = 0
    scan_state: ScanState
    if allow_names:
        scan_state = "skipped"
    else:
        pairs = load_redact_names(redact_path)
        if pairs is None:
            scan_state = "unscanned"
        else:
            scan_state = "scanned"
            text, redacted_count = redact_text(text, pairs)

    capture_dir = directory / "capture"
    capture_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = capture_dir / TRANSCRIPT_FILENAME
    episodes.atomic_write_bytes(transcript_path, (text + "\n").encode("utf-8"))
    return CaptureResult(
        transcript_path=transcript_path,
        text=text,
        word_count=len(text.split()),
        redacted_count=redacted_count,
        scan_state=scan_state,
        low_confidence_reason=assess_confidence(result),
    )
