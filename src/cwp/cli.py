"""Thin argparse dispatch for the ``cwp`` CLI.

Exit codes: 0 ok, 1 user error, 2 environment/quality-gate failure.

Heavy dependencies (``faster_whisper``, ``playwright``) are NEVER imported at module
top anywhere in this package — subcommand handlers import them lazily so ``cwp --help``
and the Channel Loop commands stay fast (tests/test_cli.py enforces this).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NoReturn

from cwp import __version__, episodes
from cwp.config import RepoRootNotFoundError, get_paths

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_ENV_ERROR = 2

Handler = Callable[[argparse.Namespace], int]

# Subcommands not yet implemented → the plan.md §14 step that implements each.
_STUB_STEPS: dict[str, int] = {
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


def _display_path(path: Path) -> str:
    """Path relative to cwd when possible (readable), absolute otherwise."""
    try:
        return os.path.relpath(path)
    except ValueError:  # e.g. a different drive on Windows
        return str(path)


def _episode_command(command: str, fn: Callable[[argparse.Namespace, Path], int]) -> Handler:
    """Wrap an episode handler: resolve ``episodes/`` from cwd + map domain errors.

    Exit-code contract (module docstring): no repo root → 2 (environment);
    any :class:`episodes.EpisodeError` (bad title/id, missing episode) → 1 (user).
    """

    def handler(args: argparse.Namespace) -> int:
        try:
            episodes_dir = get_paths().episodes_dir
        except RepoRootNotFoundError as exc:
            print(f"cwp {command}: {exc}", file=sys.stderr)
            return EXIT_ENV_ERROR
        try:
            return fn(args, episodes_dir)
        except episodes.EpisodeError as exc:
            print(f"cwp {command}: {exc}", file=sys.stderr)
            return EXIT_USER_ERROR

    return handler


def _report_created(command: str, created: episodes.CreatedEpisode, label: str) -> int:
    for warning in created.warnings:
        print(f"cwp {command}: warning: {warning}", file=sys.stderr)
    print(f"{label} {created.episode.id} at {_display_path(created.directory)}")
    return EXIT_OK


def _cmd_new(args: argparse.Namespace, episodes_dir: Path) -> int:
    created = episodes.create_episode(
        episodes_dir,
        args.title,
        ingredient=args.ingredient,
        effort=args.effort,
        hook=args.hook,
        teaches=args.teaches,
        tags=episodes.parse_tags(args.tags),
    )
    return _report_created("new", created, "created")


def _cmd_idea(args: argparse.Namespace, episodes_dir: Path) -> int:
    created = episodes.create_episode(episodes_dir, args.thought)
    return _report_created("idea", created, "captured idea")


def _cmd_list(_args: argparse.Namespace, episodes_dir: Path) -> int:
    result = episodes.scan_episodes(episodes_dir)
    for warning in result.warnings:
        print(f"cwp list: warning: {warning}", file=sys.stderr)
    if not result.episodes:
        print('no episodes yet — try: cwp new "<title>"')
        return EXIT_OK
    print(episodes.format_table(result.episodes))
    return EXIT_OK


def _cmd_show(args: argparse.Namespace, episodes_dir: Path) -> int:
    _directory, episode = episodes.load_episode(episodes_dir, args.id)
    print(episodes.format_detail(episode))
    return EXIT_OK


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
    p.add_argument(
        "--ingredient",
        choices=episodes.INGREDIENTS,
        default=episodes.DEFAULT_INGREDIENT,
        help=f"content ingredient (default: {episodes.DEFAULT_INGREDIENT})",
    )
    p.add_argument(
        "--effort",
        choices=episodes.EFFORTS,
        default=episodes.DEFAULT_EFFORT,
        help=f"effort size (default: {episodes.DEFAULT_EFFORT})",
    )
    p.add_argument("--hook", default="", help="one-line hook (the first-15-seconds pitch)")
    p.add_argument("--teaches", default="", help="what the episode teaches")
    p.add_argument("--tags", default="", help="comma-separated tags")

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
    handlers["new"] = _episode_command("new", _cmd_new)
    handlers["idea"] = _episode_command("idea", _cmd_idea)
    handlers["list"] = _episode_command("list", _cmd_list)
    handlers["show"] = _episode_command("show", _cmd_show)
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
