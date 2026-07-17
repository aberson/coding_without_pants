"""capture.py tests (Step 6): seam mock, redaction scan, confidence heuristic, exit codes.

House style: real tmp_path repos, the production ``cwp.cli.main`` entry point, and a
subprocess integration test through ``python -m cwp``. The ONLY mock is the Step 6
transcription seam (:func:`cwp.capture.transcribe_audio`) — everything downstream of it
(redaction, heuristic, atomic write, CLI rendering) is the REAL code path. The real
model runs only in the opt-in ``CWP_RUN_REAL_WHISPER=1`` test at the bottom.

The fixture ``tests/fixtures/hello.wav`` is a generated 1s 440 Hz beep (16 kHz mono
s16, stdlib ``wave``) — committed via a ``.gitignore`` negation; it contains no child
speech. Mocked-seam tests use it as a real existing ``--audio`` path.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from cwp import capture, episodes
from cwp.capture import (
    LOW_CONFIDENCE_MEAN_LOGPROB,
    RERECORD_HINT,
    UNSCANNED_NOTICE,
    WHISPER_MODELS,
    Segment,
    TranscriptResult,
    load_redact_names,
    redact_text,
)
from cwp.cli import EXIT_ENV_ERROR, EXIT_OK, EXIT_USER_ERROR, main
from cwp.config import DEFAULT_WHISPER_MODEL

FIXTURE_WAV = Path(__file__).parent / "fixtures" / "hello.wav"

KID_TEXT = "I want a dinosaur named Kai that goes roar and eats seventeen cookies"
GOOD_LOGPROB = -0.3  # comfortably above the -1.0 threshold
BAD_LOGPROB = -1.8  # comfortably below it


def _canned(text: str = KID_TEXT, *logprobs: float) -> TranscriptResult:
    """A canned seam result; default = one healthy segment."""
    values = logprobs or (GOOD_LOGPROB,)
    return TranscriptResult(
        text=text, segments=tuple(Segment(text=text, avg_logprob=lp) for lp in values)
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repo root (pyproject marker) with cwd inside it — the CLI resolves
    ``episodes/`` and ``private/`` from cwd; the real repo is never hit."""
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def episode_dir(repo: Path) -> Path:
    created = episodes.create_episode(repo / "episodes", "The Dinosaur Cookie Counter")
    return created.directory


@pytest.fixture
def seam(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace the transcription seam; record the (audio_path, model_size) call."""
    calls: dict[str, object] = {"result": _canned()}

    def fake(audio_path: Path, model_size: str) -> TranscriptResult:
        calls["audio_path"] = audio_path
        calls["model_size"] = model_size
        result = calls["result"]
        assert isinstance(result, TranscriptResult)
        return result

    monkeypatch.setattr(capture, "transcribe_audio", fake)
    return calls


def _transcript(episode_dir: Path) -> str:
    return (episode_dir / "capture" / "transcript.txt").read_text(encoding="utf-8")


def _write_redact_file(repo: Path, content: str) -> Path:
    private = repo / "private"
    private.mkdir(exist_ok=True)
    path = private / "redact-names.txt"
    path.write_text(content, encoding="utf-8")
    return path


# --- happy path ---


def test_capture_writes_transcript_via_cli(
    repo: Path,
    episode_dir: Path,
    seam: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Acceptance: ``cwp capture 001 --audio tests/fixtures/hello.wav`` writes the file."""
    assert main(["capture", "001", "--audio", str(FIXTURE_WAV)]) == EXIT_OK
    assert _transcript(episode_dir) == KID_TEXT + "\n"
    assert seam["audio_path"] == FIXTURE_WAV
    out = capsys.readouterr().out
    assert "transcribed ->" in out
    assert "transcript.txt" in out


def test_capture_creates_missing_capture_dir(
    repo: Path, episode_dir: Path, seam: dict[str, object]
) -> None:
    """capture/ is git-ignored — a fresh clone won't have it; capture must recreate it."""
    (episode_dir / "capture").rmdir()  # cwp new creates it empty
    assert main(["capture", "001", "--audio", str(FIXTURE_WAV)]) == EXIT_OK
    assert _transcript(episode_dir) == KID_TEXT + "\n"


def test_default_model_is_small_and_medium_escalates(
    repo: Path, episode_dir: Path, seam: dict[str, object]
) -> None:
    assert main(["capture", "001", "--audio", str(FIXTURE_WAV)]) == EXIT_OK
    assert seam["model_size"] == "small"
    assert main(["capture", "001", "--audio", str(FIXTURE_WAV), "--model", "medium"]) == EXIT_OK
    assert seam["model_size"] == "medium"


def test_default_model_is_a_member_of_the_model_choices() -> None:
    """Drift guard: the config default must stay a member of the --model choices."""
    assert DEFAULT_WHISPER_MODEL in WHISPER_MODELS


# --- redact-names scan (§4.3) ---


def test_redacted_name_never_appears_in_written_transcript(
    repo: Path,
    episode_dir: Path,
    seam: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Acceptance: a name in the redact list never appears in the written transcript —
    any casing (whole-word, case-insensitive)."""
    _write_redact_file(repo, "# my kid\nKai = the kid\n\nMcAllister = our family\n")
    seam["result"] = _canned("KAI said Kai wants kai cookies from McAllister")
    assert main(["capture", "001", "--audio", str(FIXTURE_WAV)]) == EXIT_OK
    written = _transcript(episode_dir)
    assert re.search(r"\bkai\b", written, re.IGNORECASE) is None
    assert "McAllister" not in written
    assert written == "the kid said the kid wants the kid cookies from our family\n"
    assert "4 name(s) redacted" in capsys.readouterr().out


def test_redaction_is_whole_word_only() -> None:
    """'Kai' must not fire inside 'Kaiju' (unit-level: the scan function itself)."""
    redacted, count = redact_text("Kai loves the Kaiju roar", (("Kai", "Bud"),))
    assert redacted == "Bud loves the Kaiju roar"
    assert count == 1


def test_nickname_with_backslashes_is_inserted_literally() -> None:
    """Regression (review F1a): a backslash-bearing nickname must not raise re.error —
    the nickname is a literal replacement, never a regex template."""
    redacted, count = redact_text("Kai went home", (("Kai", r"C:\Users\Kai"),))
    assert redacted == r"C:\Users\Kai went home"
    assert count == 1


def test_nickname_with_template_token_cannot_reinsert_real_name() -> None:
    """Regression (review F1b): a ``\\g<0>`` nickname must stay literal — template
    interpretation re-inserted the matched REAL name while counting it as redacted."""
    redacted, count = redact_text("Kai said hi", (("Kai", r"redacted (was \g<0>)"),))
    assert redacted == r"redacted (was \g<0>) said hi"
    assert "Kai" not in redacted
    assert count == 1


def test_bom_prefixed_redact_file_first_pair_still_matches(
    repo: Path,
    episode_dir: Path,
    seam: dict[str, object],
) -> None:
    """Regression (review F2): PowerShell ``Set-Content -Encoding utf8`` / Notepad emit
    a UTF-8 BOM; utf-8-sig reading keeps the FIRST pair matching (a bare utf-8 read glued
    U+FEFF onto the first real name, which then silently never redacted)."""
    private = repo / "private"
    private.mkdir()
    (private / "redact-names.txt").write_bytes(b"\xef\xbb\xbfKai = the kid\n")
    seam["result"] = _canned("Kai wants a rocket")
    assert main(["capture", "001", "--audio", str(FIXTURE_WAV)]) == EXIT_OK
    assert _transcript(episode_dir) == "the kid wants a rocket\n"


@pytest.mark.parametrize(
    "content",
    [
        "Kai = Buddy\nKai Long = KL\n",  # shorter name listed FIRST
        "Kai Long = KL\nKai = Buddy\n",  # longer name listed first
    ],
)
def test_longest_real_name_wins_regardless_of_file_order(
    repo: Path,
    episode_dir: Path,
    seam: dict[str, object],
    content: str,
) -> None:
    """Regression (review F3): 'Kai Long' must become 'KL' (never 'Buddy Long' with the
    surname leaking) no matter which order the operator listed the two pairs."""
    _write_redact_file(repo, content)
    seam["result"] = _canned("Kai Long stomped while Kai counted cookies")
    assert main(["capture", "001", "--audio", str(FIXTURE_WAV)]) == EXIT_OK
    assert _transcript(episode_dir) == "KL stomped while Buddy counted cookies\n"


def test_hyphenated_real_name_redacts_whole_compound(
    repo: Path,
    episode_dir: Path,
    seam: dict[str, object],
) -> None:
    """Regression (review F4): a hyphenated real name is matched as one whole name."""
    _write_redact_file(repo, "Kai-Lan = KL\n")
    seam["result"] = _canned("Kai-Lan waved and kai-lan giggled")
    assert main(["capture", "001", "--audio", str(FIXTURE_WAV)]) == EXIT_OK
    assert _transcript(episode_dir) == "KL waved and KL giggled\n"


def test_short_name_does_not_fire_inside_hyphenated_compound() -> None:
    """Regression (review F4): hyphens are name-internal — a listed 'Mary' must not
    corrupt the distinct compound 'Mary-Jane' (list the compound to redact it)."""
    redacted, count = redact_text("Mary-Jane waved to Mary", (("Mary", "Buddy"),))
    assert redacted == "Mary-Jane waved to Buddy"
    assert count == 1


def test_multi_word_real_name_redacts_end_to_end() -> None:
    """Review F6: multi-word pairs asserted through the scan itself, not just the parser."""
    redacted, count = redact_text("Mary Jane wants socks, MARY JANE roars", (("Mary Jane", "MJ"),))
    assert redacted == "MJ wants socks, MJ roars"
    assert count == 2


def test_allow_names_skips_redaction(
    repo: Path,
    episode_dir: Path,
    seam: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_redact_file(repo, "Kai = the kid\n")
    assert main(["capture", "001", "--audio", str(FIXTURE_WAV), "--allow-names"]) == EXIT_OK
    assert "Kai" in _transcript(episode_dir)
    captured = capsys.readouterr()
    assert "redaction skipped (--allow-names)" in captured.out
    assert UNSCANNED_NOTICE not in captured.err  # skipped-by-flag is not "unscanned"


def test_absent_redact_file_writes_unscanned_plus_one_warning(
    repo: Path,
    episode_dir: Path,
    seam: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ABSENT file → no-op + the one-time (once per invocation) stderr warning."""
    assert main(["capture", "001", "--audio", str(FIXTURE_WAV)]) == EXIT_OK
    assert _transcript(episode_dir) == KID_TEXT + "\n"
    err = capsys.readouterr().err
    assert err.count(UNSCANNED_NOTICE) == 1
    assert "cwp capture: warning:" in err  # the established warning prefix vocabulary


def test_present_redact_file_prints_no_warning(
    repo: Path,
    episode_dir: Path,
    seam: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_redact_file(repo, "Kai = the kid\n")
    assert main(["capture", "001", "--audio", str(FIXTURE_WAV)]) == EXIT_OK
    assert UNSCANNED_NOTICE not in capsys.readouterr().err


def test_malformed_redact_line_is_a_loud_user_error(
    repo: Path,
    episode_dir: Path,
    seam: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A silently skipped pair line could leak a real name — parse failures exit 1."""
    _write_redact_file(repo, "Kai = the kid\njust-a-name-no-equals\n")
    assert main(["capture", "001", "--audio", str(FIXTURE_WAV)]) == EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert "line 2" in err
    assert "real-name = nickname" in err
    assert not (episode_dir / "capture" / "transcript.txt").exists()  # nothing written


def test_load_redact_names_parses_comments_blanks_and_pairs(tmp_path: Path) -> None:
    path = tmp_path / "redact-names.txt"
    path.write_text("# comment\n\nKai = the kid\nMary Jane = MJ\n", encoding="utf-8")
    assert load_redact_names(path) == (("Kai", "the kid"), ("Mary Jane", "MJ"))
    assert load_redact_names(tmp_path / "absent.txt") is None


def test_inline_trailing_comment_is_stripped_from_pair_lines(tmp_path: Path) -> None:
    """Regression (review F5): a trailing ``# comment`` on a pair line must not become
    part of the nickname (which then got substituted into the transcript verbatim)."""
    path = tmp_path / "redact-names.txt"
    path.write_text("Robert = Bobby  # uncle's nickname\nKai = the kid #tail\n", encoding="utf-8")
    assert load_redact_names(path) == (("Robert", "Bobby"), ("Kai", "the kid"))


def test_inline_comment_that_empties_a_nickname_is_loud(tmp_path: Path) -> None:
    """'name = # comment' leaves an empty nickname — malformed-line error, not a skip."""
    path = tmp_path / "redact-names.txt"
    path.write_text("Kai = # oops, no nickname\n", encoding="utf-8")
    with pytest.raises(capture.CaptureError, match="line 1"):
        load_redact_names(path)


# --- low-confidence heuristic ---


def test_low_mean_logprob_prints_rerecord_hint_but_still_writes(
    repo: Path,
    episode_dir: Path,
    seam: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Acceptance: the low-confidence heuristic prints the re-record hint (exit stays 0)."""
    seam["result"] = _canned(KID_TEXT, BAD_LOGPROB, BAD_LOGPROB)
    assert main(["capture", "001", "--audio", str(FIXTURE_WAV)]) == EXIT_OK
    assert _transcript(episode_dir) == KID_TEXT + "\n"
    assert RERECORD_HINT in capsys.readouterr().err


def test_two_word_transcript_prints_rerecord_hint(
    repo: Path,
    episode_dir: Path,
    seam: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    seam["result"] = _canned("uh dinosaur", GOOD_LOGPROB)  # ≤ 2 words, healthy logprob
    assert main(["capture", "001", "--audio", str(FIXTURE_WAV)]) == EXIT_OK
    assert RERECORD_HINT in capsys.readouterr().err


def test_healthy_transcript_prints_no_hint(
    repo: Path,
    episode_dir: Path,
    seam: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["capture", "001", "--audio", str(FIXTURE_WAV)]) == EXIT_OK
    assert RERECORD_HINT not in capsys.readouterr().err


def test_mean_is_across_segments_not_any_single_one() -> None:
    """One bad segment must not trip the hint when the MEAN stays above threshold."""
    result = _canned(KID_TEXT, -0.1, -0.1, BAD_LOGPROB)  # mean ≈ -0.67 > -1.0
    assert capture.assess_confidence(result) is None
    result = _canned(KID_TEXT, BAD_LOGPROB, BAD_LOGPROB, -0.1)  # mean ≈ -1.23 < -1.0
    reason = capture.assess_confidence(result)
    assert reason is not None
    assert str(LOW_CONFIDENCE_MEAN_LOGPROB) in reason


def test_empty_segments_are_judged_on_length_alone() -> None:
    """Review F6: no segments (canned/edge result) → no mean to take, no crash — a long
    transcript passes, a short one still trips the word-count leg."""
    long_result = TranscriptResult(text="I want a big loud stompy dinosaur", segments=())
    assert capture.assess_confidence(long_result) is None
    short_result = TranscriptResult(text="uh dinosaur", segments=())
    assert capture.assess_confidence(short_result) is not None


# --- exit codes ---


def test_missing_audio_file_exits_1(
    repo: Path,
    episode_dir: Path,
    seam: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["capture", "001", "--audio", "nope.wav"]) == EXIT_USER_ERROR
    assert "Audio file not found" in capsys.readouterr().err
    assert "audio_path" not in seam  # the seam (model load!) is never reached


def test_missing_episode_exits_1(
    repo: Path, seam: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["capture", "999", "--audio", str(FIXTURE_WAV)]) == EXIT_USER_ERROR
    assert "No episode matching" in capsys.readouterr().err


def test_whisper_env_failure_exits_2(
    repo: Path,
    episode_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom(audio_path: Path, model_size: str) -> TranscriptResult:
        raise capture.CaptureEnvError("faster-whisper unavailable (fake)")

    monkeypatch.setattr(capture, "transcribe_audio", boom)
    assert main(["capture", "001", "--audio", str(FIXTURE_WAV)]) == EXIT_ENV_ERROR
    assert "faster-whisper unavailable" in capsys.readouterr().err


def test_audio_flag_is_required(repo: Path) -> None:
    """No --record in v1 — bare ``cwp capture <id>`` is a usage error (exit 1)."""
    with pytest.raises(SystemExit) as excinfo:
        main(["capture", "001"])
    assert excinfo.value.code == EXIT_USER_ERROR


# --- integration through the production entry points ---


def test_python_dash_m_cwp_capture_missing_audio_exits_1(repo: Path) -> None:
    """End-to-end through ``python -m cwp`` (no seam mock possible across the process
    boundary, so the asserted path is the pre-whisper user-error leg)."""
    episodes.create_episode(repo / "episodes", "Subprocess Episode")
    result = subprocess.run(
        [sys.executable, "-m", "cwp", "capture", "001", "--audio", "nope.wav"],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo,
    )
    assert result.returncode == EXIT_USER_ERROR
    assert "Audio file not found" in result.stderr


def test_fixture_wav_exists_and_is_small() -> None:
    """The committed fixture stays a tiny generated beep (no child speech, < 100 KB)."""
    assert FIXTURE_WAV.is_file()
    assert FIXTURE_WAV.stat().st_size < 100_000
    assert FIXTURE_WAV.read_bytes()[:4] == b"RIFF"


# --- the real model (opt-in) ---


@pytest.mark.skipif(
    os.environ.get("CWP_RUN_REAL_WHISPER") != "1",
    reason="real faster-whisper run is opt-in: set CWP_RUN_REAL_WHISPER=1"
    " (downloads the small model, ~460 MB, then runs offline)",
)
def test_real_whisper_pipeline_end_to_end(
    repo: Path, episode_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """REAL MODEL RUN — asserts the PIPELINE (decode → transcribe → scan → write) runs,
    NOT accuracy: the fixture is a sine beep, so empty/garbage text is expected (the
    empty transcript exercises the ≤2-words re-record hint, still exit 0)."""
    assert main(["capture", "001", "--audio", str(FIXTURE_WAV)]) == EXIT_OK
    transcript = episode_dir / "capture" / "transcript.txt"
    assert transcript.is_file()
    transcript.read_text(encoding="utf-8")  # valid UTF-8; content is noise by design
