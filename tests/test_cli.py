"""CLI dispatch tests (Step 1): help, version, stubs, exit codes, UTF-8 output, lazy imports."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from cwp import __version__
from cwp.cli import _STUB_STEPS, EXIT_OK, EXIT_USER_ERROR, _reconfigure_utf8, main

ALL_COMMANDS = [
    "new",
    "idea",
    "list",
    "show",
    "status",
    "next",
    "draft",
    "publish",
    "capture",
    "brief",
    "build",
    "version",
]

# One valid invocation per stub subcommand (must reach the handler, not an argparse error).
# new/idea/list/show became real handlers in Step 2 (tests/test_episodes.py);
# status/next became real handlers in Step 3 (tests/test_lifecycle.py);
# draft became a real handler in Step 4 (tests/test_drafting.py);
# publish became a real handler in Step 5 (tests/test_publishing.py).
STUB_ARGV: dict[str, list[str]] = {
    "capture": ["capture", "001", "--audio", "clip.wav"],
    "brief": ["brief", "001"],
    "build": ["build", "001"],
}


def test_version_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == EXIT_OK
    assert __version__ in capsys.readouterr().out


def test_help_exits_zero_and_lists_all_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for command in ALL_COMMANDS:
        assert command in out, f"{command!r} missing from cwp --help"


def test_no_command_prints_help_and_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == EXIT_USER_ERROR
    assert "usage: cwp" in capsys.readouterr().out


def test_stub_argv_covers_every_stub_command() -> None:
    """A stub added to _STUB_STEPS without a test invocation here must fail loud."""
    assert set(STUB_ARGV) == set(_STUB_STEPS)


@pytest.mark.parametrize(("command", "step"), sorted(_STUB_STEPS.items()))
def test_stubs_exit_1_with_specific_step_pointer(
    command: str, step: int, capsys: pytest.CaptureFixture[str]
) -> None:
    """Parametrized from cwp.cli._STUB_STEPS itself so the test and the map cannot drift."""
    assert main(STUB_ARGV[command]) == EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert f"not implemented yet (Step {step})" in err


def test_unknown_command_is_a_user_error_exit_1(capsys: pytest.CaptureFixture[str]) -> None:
    """Usage errors exit 1 (user error), not argparse's default 2 (reserved for env failures)."""
    with pytest.raises(SystemExit) as excinfo:
        main(["definitely-not-a-command"])
    assert excinfo.value.code == EXIT_USER_ERROR


def test_no_heavy_imports_at_module_top() -> None:
    """Importing cwp.cli must not pull faster_whisper/playwright (keeps --help fast)."""
    code = (
        "import sys\n"
        "import cwp.cli\n"
        "heavy = [m for m in ('faster_whisper', 'playwright', 'ctranslate2')"
        " if m in sys.modules]\n"
        "assert not heavy, f'heavy modules imported at CLI import time: {heavy}'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_non_ascii_title_prints_under_captured_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Seed episode titles contain … and – ; printing them must not raise under capture."""
    _reconfigure_utf8()
    print("The Number-Guessing Machine guesses… – test 🦖")
    assert "guesses… – test" in capsys.readouterr().out


def test_non_ascii_title_survives_piped_output_after_reconfigure() -> None:
    """The real cp1252 landmine: piped stdout on Windows defaults to the ANSI codepage.

    An astral emoji is unencodable in cp1252 — without ``_reconfigure_utf8`` this print
    raises UnicodeEncodeError. Env encoding overrides are stripped so the default applies.
    """
    code = (
        "from cwp.cli import _reconfigure_utf8\n"
        "_reconfigure_utf8()\n"
        "print('guesses\\u2026 \\u2013 \\U0001f996')\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, check=False)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert "\U0001f996" in result.stdout.decode("utf-8")


def test_python_dash_m_cwp_version() -> None:
    """Integration: the ``python -m cwp`` production entry point reaches cli.main()."""
    result = subprocess.run(
        [sys.executable, "-m", "cwp", "version"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert __version__ in result.stdout


def test_console_script_lists_all_subcommands() -> None:
    """Integration: the installed ``cwp`` console script (pyproject [project.scripts])."""
    exe = shutil.which("cwp")
    if exe is None:
        pytest.skip("cwp console script not on PATH (run via `uv run pytest`)")
    result = subprocess.run([exe, "--help"], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    for command in ALL_COMMANDS:
        assert command in result.stdout
