"""drafting.py tests (Step 4): prompt assembly, the claude seam, ``cwp draft`` wiring.

The claude boundary is ALWAYS mocked — no real API calls in CI. Two mock styles:

- **In-process:** monkeypatch the seam functions (``ensure_claude_ready``/``call_claude``)
  or ``drafting._execute`` (the one subprocess touchpoint — the sanctioned surgical fault
  injection for the timeout/exit paths, mirroring test_episodes' ``os.replace`` exception).
- **Subprocess:** a fake ``claude`` shim on PATH — a ``claude.cmd`` (Windows resolves
  only .cmd/.bat/.exe from PATH; the .cmd wraps python) plus a ``claude`` sh wrapper
  (POSIX), both driving one tiny python script that records argv/stdin/cwd to JSON.

House style otherwise holds: real tmp_path repos, real episode folders, and an
integration test through the production ``python -m cwp`` entry point.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cwp import drafting, episodes, templates
from cwp.cli import EXIT_ENV_ERROR, EXIT_OK, EXIT_USER_ERROR, main
from cwp.drafting import (
    AI_DRAFT_MARKER,
    KINDS,
    ClaudeCallError,
    ClaudeTimeoutError,
    DraftEnvError,
    build_prompt,
    call_claude,
)

VOICE = "# Voice\n\nCalm. A little absurd. One small useful thing per video.\n"
CANNED = "Drafted by the fake seam.\nSecond drafted line."
EPISODE_TITLE = "The Number-Guessing Machine"


# --- fixtures ---


@pytest.fixture(autouse=True)
def cold_preflight_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with a cold per-process preflight cache."""
    monkeypatch.setattr(drafting, "_preflight_passed", False)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repo root (pyproject marker + voice.md) with cwd inside it."""
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "voice.md").write_text(VOICE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def episode_dir(repo: Path) -> Path:
    created = episodes.create_episode(
        repo / "episodes",
        EPISODE_TITLE,
        hook="It guesses your number. It never loses.",
        teaches="binary search",
        tags=["neetcode", "kids"],
    )
    return created.directory


@pytest.fixture
def fake_seam(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object]]:
    """Replace preflight + call seam in-process; records event order and call args."""
    events: list[tuple[str, object]] = []

    def fake_ready(*, timeout: float | None = None) -> None:
        events.append(("ready", timeout))

    def fake_call(prompt: str, *, timeout: float, partial_path: Path | None = None) -> str:
        events.append(("call", prompt))
        return CANNED

    monkeypatch.setattr(drafting, "ensure_claude_ready", fake_ready)
    monkeypatch.setattr(drafting, "call_claude", fake_call)
    return events


# --- the fake claude shim (PATH-resolvable subprocess double) ---

_NOOP_SHIM = "raise SystemExit(0)\n"

_AUTHFAIL_SHIM = """\
import sys
sys.stdin.read()
sys.stderr.write("Invalid API key. Please run /login\\n")
sys.exit(1)
"""

# The Finding-1 regression double: mimics the npm claude.cmd shim tree (cmd.exe -> python
# -> sleeping python grandchild), echoes a partial line, then hangs far past any timeout.
# Only a process-TREE kill unblocks the pipe reap in under the sleep duration.
_SLOW_TREE_SHIM = """\
import subprocess, sys, time
sys.stdin.read()
sys.stdout.write("PARTIAL BEFORE HANG")
sys.stdout.flush()
subprocess.Popen([sys.executable, "-c", "import time; time.sleep(15)"])
time.sleep(15)
"""

# __CALLS__ is replaced with a repr()'d directory path; records argv/stdin/cwd per call.
_OK_SHIM = """\
import json, os, sys
record = {"argv": sys.argv[1:], "stdin": sys.stdin.read(), "cwd": os.getcwd()}
calls_dir = __CALLS__
os.makedirs(calls_dir, exist_ok=True)
n = len(os.listdir(calls_dir))
with open(os.path.join(calls_dir, "call-%d.json" % n), "w", encoding="utf-8") as fh:
    json.dump(record, fh)
sys.stdout.write("A drafted line.\\nAnother drafted line.\\n")
"""


def _write_shim(shim_dir: Path, body: str) -> None:
    """A fake ``claude`` on PATH: ``claude.cmd`` (Windows) + ``claude`` sh (POSIX)."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    script = shim_dir / "claude_shim.py"
    script.write_text(body, encoding="utf-8")
    (shim_dir / "claude.cmd").write_text(
        f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
    )
    posix = shim_dir / "claude"
    posix.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    posix.chmod(0o755)


def _ok_shim_body(calls_dir: Path) -> str:
    return _OK_SHIM.replace("__CALLS__", repr(str(calls_dir)))


def _shim_on_path(monkeypatch: pytest.MonkeyPatch, shim_dir: Path) -> None:
    monkeypatch.setenv("PATH", str(shim_dir) + os.pathsep + os.environ.get("PATH", ""))


# --- prompt assembly (one shared code path; only the instruction varies) ---


def test_build_prompt_embeds_voice_context_and_per_kind_instruction() -> None:
    episode = episodes.Episode(
        id="001-t",
        seq=1,
        slug="t",
        title="T…itle with unicode",
        hook="the hook line",
        teaches="binary search",
        ingredient="hak",
        tags=["a", "b"],
    )
    prompts = {}
    for kind in KINDS:
        prompt = build_prompt(VOICE, episode, kind)
        assert VOICE.strip() in prompt
        for needle in ("T…itle with unicode", "the hook line", "binary search", "hak", "a, b"):
            assert needle in prompt, f"{needle!r} missing from the {kind} prompt"
        assert drafting._INSTRUCTIONS[kind] in prompt
        prompts[kind] = prompt
    assert len(set(prompts.values())) == len(KINDS)  # only the instruction differs — but it does


def test_build_prompt_unknown_kind_is_a_user_error() -> None:
    episode = episodes.Episode(id="001-t", seq=1, slug="t", title="T")
    with pytest.raises(episodes.EpisodeError, match="poem"):
        build_prompt(VOICE, episode, "poem")


def test_publish_placeholder_sentinel_stays_in_the_template() -> None:
    """Drift guard: drafting's append check keys on the sentinel templates.py renders."""
    rendered = templates.render_publish_md(title="T", episode_id="001-t")
    assert templates.PUBLISH_PLACEHOLDER_SENTINEL in rendered


def test_kind_partition_is_complete_and_disjoint() -> None:
    """Drift guard: FILE_KINDS/STDOUT_KINDS partition KINDS exactly (STDOUT is derived)."""
    assert set(drafting.FILE_KINDS) | set(drafting.STDOUT_KINDS) == set(KINDS)
    assert not set(drafting.FILE_KINDS) & set(drafting.STDOUT_KINDS)


# --- --dry-run: prompt only, no preflight, no call ---


def test_dry_run_prints_prompt_and_never_touches_claude(
    episode_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("claude must not be touched on --dry-run")

    monkeypatch.setattr(drafting, "preflight", boom)
    monkeypatch.setattr(drafting, "ensure_claude_ready", boom)
    monkeypatch.setattr(drafting, "call_claude", boom)
    assert main(["draft", "001", "title", "--dry-run"]) == EXIT_OK
    out = capsys.readouterr().out
    assert VOICE.strip() in out
    assert drafting._INSTRUCTIONS["title"] in out
    assert EPISODE_TITLE in out
    # No draft landed anywhere.
    assert AI_DRAFT_MARKER not in (episode_dir / "script.md").read_text(encoding="utf-8")
    assert AI_DRAFT_MARKER not in (episode_dir / "publish.md").read_text(encoding="utf-8")


# --- all four variants through the one shared code path (one test each) ---


@pytest.mark.parametrize("kind", KINDS)
def test_each_kind_lands_marked_content_through_the_shared_path(
    kind: str,
    episode_dir: Path,
    fake_seam: list[tuple[str, object]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    publish_before = (episode_dir / "publish.md").read_text(encoding="utf-8")
    assert main(["draft", "001", kind]) == EXIT_OK
    # Preflight ran before the one call, and the prompt went through build_prompt.
    assert [event[0] for event in fake_seam] == ["ready", "call"]
    prompt = str(fake_seam[1][1])
    assert VOICE.strip() in prompt
    assert drafting._INSTRUCTIONS[kind] in prompt
    captured = capsys.readouterr()
    if kind in drafting.FILE_KINDS:  # outline and script both replace script.md
        content = (episode_dir / "script.md").read_text(encoding="utf-8")
        assert content.splitlines()[0] == AI_DRAFT_MARKER
        assert "Drafted by the fake seam." in content
        assert "script.md" in captured.out
    else:  # title/description: stdout + append under publish.md (placeholder present)
        assert "Drafted by the fake seam." in captured.out
        publish = (episode_dir / "publish.md").read_text(encoding="utf-8")
        # Positional, not membership: the appended block's FIRST line is the marker,
        # mirroring the FILE_KINDS first-line check. Exact-content equality proves it.
        expected_block = (
            f"\n{AI_DRAFT_MARKER}\n## {kind.capitalize()} draft (cwp draft)\n\n{CANNED}\n"
        )
        assert publish == publish_before.rstrip("\n") + "\n" + expected_block
        assert "appended to" in captured.err
    # Atomic writes leave no temp droppings behind.
    assert not list(episode_dir.glob("*.tmp"))


def test_title_is_stdout_only_once_publish_md_is_regenerated(
    episode_dir: Path,
    fake_seam: list[tuple[str, object]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    regenerated = "# real publish metadata (no placeholder sentinel)\n"
    (episode_dir / "publish.md").write_text(regenerated, encoding="utf-8")
    assert main(["draft", "001", "title"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "Drafted by the fake seam." in captured.out
    assert "stdout only" in captured.err
    assert (episode_dir / "publish.md").read_text(encoding="utf-8") == regenerated


def test_title_survives_a_missing_publish_md(
    episode_dir: Path,
    fake_seam: list[tuple[str, object]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    (episode_dir / "publish.md").unlink()
    assert main(["draft", "001", "title"]) == EXIT_OK
    assert "Drafted by the fake seam." in capsys.readouterr().out


def test_invalid_utf8_publish_md_is_a_clean_user_error_and_saves_the_draft(
    episode_dir: Path,
    fake_seam: list[tuple[str, object]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A hand-edited publish.md with invalid UTF-8 must NOT traceback after the claude
    call burned — clean exit 1 (read_meta's contract for the same class), draft saved."""
    (episode_dir / "publish.md").write_bytes(b"# publish\n<!-- PLACEHOLDER \xff\xfe garbage")
    assert main(["draft", "001", "title"]) == EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert "not valid UTF-8" in err
    assert "saved to" in err
    partial = episode_dir / "draft-title.partial.txt"
    assert partial.read_text(encoding="utf-8") == CANNED


# --- user errors (exit 1) ---


def test_unknown_episode_id_is_a_user_error(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["draft", "999", "script", "--dry-run"]) == EXIT_USER_ERROR
    assert "No episode matching" in capsys.readouterr().err


def test_unknown_kind_is_an_argparse_user_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["draft", "001", "poem"])
    assert excinfo.value.code == EXIT_USER_ERROR


def test_missing_voice_md_is_an_environment_error(
    episode_dir: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "voice.md").unlink()
    assert main(["draft", "001", "script", "--dry-run"]) == EXIT_ENV_ERROR
    assert "voice.md" in capsys.readouterr().err


# --- environment errors (exit 2): missing binary, auth failure, timeout ---


def test_missing_claude_prints_fix_it_text_and_exits_2(
    episode_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))  # nothing named claude anywhere on PATH
    before = (episode_dir / "script.md").read_text(encoding="utf-8")
    assert main(["draft", "001", "script"]) == EXIT_ENV_ERROR
    err = capsys.readouterr().err
    assert "Claude CLI not found" in err
    assert "log in" in err
    assert (episode_dir / "script.md").read_text(encoding="utf-8") == before


def test_unauthed_claude_shim_prints_fix_it_text_and_exits_2(
    episode_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Auth-fail path through the REAL subprocess seam: the preflight probe exits 1."""
    shim_dir = tmp_path / "shims"
    _write_shim(shim_dir, _AUTHFAIL_SHIM)
    _shim_on_path(monkeypatch, shim_dir)
    assert main(["draft", "001", "description"]) == EXIT_ENV_ERROR
    err = capsys.readouterr().err
    assert "preflight failed" in err
    assert "Invalid API key" in err  # the probe's stderr is surfaced
    assert "log in" in err


def test_timeout_flushes_partial_to_temp_and_leaves_target_untouched(
    episode_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    shim_dir = tmp_path / "shims"
    _write_shim(shim_dir, _NOOP_SHIM)  # only needed so PATH resolution succeeds
    _shim_on_path(monkeypatch, shim_dir)
    monkeypatch.setattr(drafting, "ensure_claude_ready", lambda **kwargs: None)

    def timing_out_execute(exe: str, prompt: str, timeout: float) -> None:
        raise subprocess.TimeoutExpired(
            cmd="claude -p", timeout=timeout, output=b"PARTIAL DRAFT SO FAR"
        )

    monkeypatch.setattr(drafting, "_execute", timing_out_execute)
    before = (episode_dir / "script.md").read_text(encoding="utf-8")
    assert main(["draft", "001", "script"]) == EXIT_ENV_ERROR
    err = capsys.readouterr().err
    assert "timed out" in err
    partial = episode_dir / "draft-script.partial.txt"
    assert str(partial) in err  # the error names the flush file
    assert partial.read_text(encoding="utf-8") == "PARTIAL DRAFT SO FAR"
    assert (episode_dir / "script.md").read_text(encoding="utf-8") == before


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="regression is specific to the Windows .cmd shim tree; non-Windows uses kill()",
)
def test_timeout_tree_kill_unblocks_reap_and_flushes_partial(
    episode_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """REAL-subprocess regression (review Finding 1): the claude.cmd shim spawns a
    sleeping python child and hangs 15s; the tracked cmd.exe dying must not leave the
    pipe-holding tree alive, or the reap blocks until the sleep ends. The tree-kill
    bounds the whole timeout path well under the shim's hang."""
    shim_dir = tmp_path / "shims"
    _write_shim(shim_dir, _SLOW_TREE_SHIM)
    monkeypatch.setenv("PATH", str(shim_dir))  # fully shadow PATH — no real claude reachable
    monkeypatch.setattr(drafting, "ensure_claude_ready", lambda **kwargs: None)
    monkeypatch.setattr(drafting, "DRAFT_TIMEOUT", 2.0)  # run_draft reads it at call time
    before = (episode_dir / "script.md").read_text(encoding="utf-8")
    start = time.monotonic()
    assert main(["draft", "001", "script"]) == EXIT_ENV_ERROR
    elapsed = time.monotonic() - start
    assert elapsed < 6.0, f"timeout path took {elapsed:.1f}s — the shim tree survived the kill"
    err = capsys.readouterr().err
    assert "timed out" in err
    partial = episode_dir / "draft-script.partial.txt"
    assert partial.read_text(encoding="utf-8") == "PARTIAL BEFORE HANG"
    assert (episode_dir / "script.md").read_text(encoding="utf-8") == before


# --- the seam itself ---


def test_call_claude_contract_stdin_argv_and_neutral_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing seam contract: prompt on stdin (never argv), cwd = neutral dir.

    The prompt is >32K chars — over the Windows argv ceiling — to prove stdin carries it.
    """
    shim_dir = tmp_path / "shims"
    calls_dir = tmp_path / "shim-calls"
    _write_shim(shim_dir, _ok_shim_body(calls_dir))
    _shim_on_path(monkeypatch, shim_dir)
    prompt = "x" * 40_000
    text = call_claude(prompt, timeout=60.0)
    assert text == "A drafted line.\nAnother drafted line.\n"
    record = json.loads((calls_dir / "call-0.json").read_text(encoding="utf-8"))
    assert record["argv"] == ["-p"]  # NO prompt in argv
    assert record["stdin"] == prompt  # the whole 40K arrived via stdin
    used_cwd = Path(record["cwd"]).resolve()
    assert used_cwd == drafting.neutral_cwd().resolve()
    assert used_cwd != tmp_path.resolve()


def test_call_claude_nonzero_exit_flushes_partial_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim_dir = tmp_path / "shims"
    _write_shim(shim_dir, _NOOP_SHIM)
    _shim_on_path(monkeypatch, shim_dir)
    completed = subprocess.CompletedProcess(
        args=["claude", "-p"], returncode=3, stdout="half a draft", stderr="kaboom"
    )
    monkeypatch.setattr(drafting, "_execute", lambda *a, **k: completed)
    partial = tmp_path / "partial.txt"
    with pytest.raises(ClaudeCallError, match="exit 3") as excinfo:
        call_claude("prompt", timeout=5.0, partial_path=partial)
    assert "kaboom" in str(excinfo.value)
    assert partial.read_text(encoding="utf-8") == "half a draft"


def test_call_claude_timeout_coerces_bytes_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TimeoutExpired.stdout can be bytes even in text mode — the seam must decode it."""
    shim_dir = tmp_path / "shims"
    _write_shim(shim_dir, _NOOP_SHIM)
    _shim_on_path(monkeypatch, shim_dir)

    def timing_out_execute(exe: str, prompt: str, timeout: float) -> None:
        raise subprocess.TimeoutExpired(
            cmd="claude -p", timeout=timeout, output=b"bytes partial \xe2\x80\xa6"
        )

    monkeypatch.setattr(drafting, "_execute", timing_out_execute)
    partial = tmp_path / "partial.txt"
    with pytest.raises(ClaudeTimeoutError, match="timed out after 2"):
        call_claude("prompt", timeout=2.0, partial_path=partial)
    assert partial.read_text(encoding="utf-8") == "bytes partial …"


def test_call_claude_empty_output_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim_dir = tmp_path / "shims"
    _write_shim(shim_dir, _NOOP_SHIM)
    _shim_on_path(monkeypatch, shim_dir)
    completed = subprocess.CompletedProcess(
        args=["claude", "-p"], returncode=0, stdout="  \n", stderr=""
    )
    monkeypatch.setattr(drafting, "_execute", lambda *a, **k: completed)
    with pytest.raises(DraftEnvError, match="no output"):
        call_claude("prompt", timeout=5.0)


def test_ensure_claude_ready_runs_preflight_once_per_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(drafting, "preflight", lambda **kwargs: calls.append(1))
    drafting.ensure_claude_ready()
    drafting.ensure_claude_ready()
    assert len(calls) == 1


# --- integration through the production entry point (fake shim on PATH) ---


def test_production_cli_with_fake_shim_writes_marked_script(
    repo: Path, episode_dir: Path, tmp_path: Path
) -> None:
    shim_dir = tmp_path / "shims"
    calls_dir = tmp_path / "shim-calls"
    _write_shim(shim_dir, _ok_shim_body(calls_dir))
    env = dict(os.environ)
    env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
    result = subprocess.run(
        [sys.executable, "-m", "cwp", "draft", "001", "script"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo,
        env=env,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    content = (episode_dir / "script.md").read_text(encoding="utf-8")
    assert content.splitlines()[0] == AI_DRAFT_MARKER
    assert "A drafted line." in content
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(calls_dir.glob("call-*.json"))
    ]
    assert len(records) == 2  # exactly: the preflight probe, then the one draft call
    preflight_record, draft_record = records
    assert preflight_record["argv"] == ["-p"]
    assert draft_record["argv"] == ["-p"]  # prompt NEVER in argv
    assert "ok" in preflight_record["stdin"]
    assert "Channel voice" in draft_record["stdin"]
    assert EPISODE_TITLE in draft_record["stdin"]
    for record in records:
        used_cwd = Path(record["cwd"]).resolve()
        assert used_cwd != repo.resolve()  # neutral cwd — repo CLAUDE.md can't leak in
        assert used_cwd.name == "cwp-neutral-cwd"


def test_production_cli_dry_run_prints_prompt_without_any_call(
    repo: Path, episode_dir: Path, tmp_path: Path
) -> None:
    shim_dir = tmp_path / "shims"
    calls_dir = tmp_path / "shim-calls"
    _write_shim(shim_dir, _ok_shim_body(calls_dir))
    env = dict(os.environ)
    env["PATH"] = str(shim_dir)  # the shim is the ONLY claude candidate on PATH
    result = subprocess.run(
        [sys.executable, "-m", "cwp", "draft", "001", "title", "--dry-run"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo,
        env=env,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "Channel voice" in result.stdout
    assert "TITLES" in result.stdout
    assert not calls_dir.exists() or not list(calls_dir.glob("call-*.json"))
