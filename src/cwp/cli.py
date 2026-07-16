"""Thin argparse dispatch for the ``cwp`` CLI.

Exit codes: 0 ok, 1 user error, 2 environment/quality-gate failure.

Heavy dependencies (``faster_whisper``, ``playwright``) are NEVER imported at module
top anywhere in this package — subcommand handlers import them lazily so ``cwp --help``
and the Channel Loop commands stay fast (tests/test_cli.py enforces this).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from typing import NoReturn

from cwp import __version__

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_ENV_ERROR = 2

Handler = Callable[[argparse.Namespace], int]

# Subcommands not yet implemented → the plan.md §14 step that implements each.
_STUB_STEPS: dict[str, int] = {
    "new": 2,
    "idea": 2,
    "list": 2,
    "show": 2,
    "status": 3,
    "next": 3,
    "draft": 4,
    "publish": 5,
    "capture": 6,
    "brief": 7,
    "build": 9,
}


class _Parser(argparse.ArgumentParser):
    """ArgumentParser whose usage errors exit 1 (user error), not argparse's default 2.

    Exit code 2 is reserved for environment/quality-gate failures (module docstring).
    Sub-parsers inherit this class via ``add_subparsers`` (argparse defaults
    ``parser_class`` to ``type(self)``).
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USER_ERROR, f"{self.prog}: error: {message}\n")


def _reconfigure_utf8() -> None:
    """Force UTF-8 with ``errors="replace"`` on stdout/stderr.

    Windows cp1252 consoles and captured/piped output choke on episode titles
    (they contain … and – and emoji). Runs FIRST in ``main()``. Streams without
    ``reconfigure`` (test doubles, detached streams) are skipped.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def _cmd_version(_args: argparse.Namespace) -> int:
    print(f"cwp {__version__}")
    return EXIT_OK


def _make_stub(command: str, step: int) -> Handler:
    def handler(_args: argparse.Namespace) -> int:
        print(f"cwp {command}: not implemented yet (Step {step})", file=sys.stderr)
        return EXIT_USER_ERROR

    return handler


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="cwp",
        description=(
            "Coding without Pants — one CLI, two loops: the Channel Loop and the Pantsless Build."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("new", help="create an episode folder from a title (idea status)")
    p.add_argument("title", help="episode title")

    p = sub.add_parser("idea", help="fast idea capture (minimal idea episode)")
    p.add_argument("thought", help="the idea, in one line")

    sub.add_parser("list", help="derived episode table: status + cycle time")

    p = sub.add_parser("show", help="detail for one episode")
    p.add_argument("id", help="episode id or seq (e.g. 001)")

    p = sub.add_parser("status", help="advance/change an episode's lifecycle state")
    p.add_argument("id", help="episode id or seq")
    p.add_argument(
        "status",
        help="target status (idea|scripted|built|recorded|edited|published|on-hold|cut)",
    )

    sub.add_parser("next", help="which episode to work on + its next action")

    p = sub.add_parser("draft", help="AI-draft episode copy in the channel voice")
    p.add_argument("id", help="episode id or seq")
    p.add_argument(
        "kind",
        choices=("outline", "script", "title", "description"),
        help="what to draft",
    )

    p = sub.add_parser("publish", help="paste-ready YouTube metadata / mark published")
    p.add_argument("id", help="episode id or seq")
    p.add_argument("--url", help="published YouTube URL (records it + sets published)")

    p = sub.add_parser("capture", help="transcribe a kid clip via local faster-whisper")
    p.add_argument("id", help="episode id or seq")
    p.add_argument("--audio", help="path to the recorded clip")

    p = sub.add_parser("brief", help="distill the noisy transcript into brief.md")
    p.add_argument("id", help="episode id or seq")

    p = sub.add_parser("build", help="one-shot generate + verify + repair the episode toy")
    p.add_argument("id", help="episode id or seq")
    p.add_argument("--force", action="store_true", help="overwrite an existing project/index.html")

    sub.add_parser("version", help="print the cwp version")

    return parser


def _handlers() -> dict[str, Handler]:
    handlers: dict[str, Handler] = {
        name: _make_stub(name, step) for name, step in _STUB_STEPS.items()
    }
    handlers["version"] = _cmd_version
    return handlers


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``cwp`` console script and ``python -m cwp``."""
    _reconfigure_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    command: str | None = args.command
    if command is None:
        parser.print_help()
        return EXIT_USER_ERROR
    return _handlers()[command](args)
