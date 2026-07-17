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

from cwp import __version__, capture, drafting, episodes, lifecycle, publishing
from cwp.config import DEFAULT_WHISPER_MODEL, RepoRootNotFoundError, get_paths

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_ENV_ERROR = 2

Handler = Callable[[argparse.Namespace], int]

# Subcommands not yet implemented → the plan.md §14 step that implements each.
_STUB_STEPS: dict[str, int] = {
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


def _cmd_list(args: argparse.Namespace, episodes_dir: Path) -> int:
    result = episodes.scan_episodes(episodes_dir)
    for warning in result.warnings:
        print(f"cwp list: warning: {warning}", file=sys.stderr)
    if not result.episodes:
        print('no episodes yet — try: cwp new "<title>"')
        return EXIT_OK
    visible = [
        episode
        for episode in result.episodes
        if args.all or lifecycle.visible_in_default_list(episode)
    ]
    hidden = len(result.episodes) - len(visible)
    if hidden:
        print(f"cwp list: {hidden} cut hidden (cwp list --all shows them)", file=sys.stderr)
    if not visible:
        print(f"no episodes to list — {hidden} cut hidden (try: cwp list --all)")
        return EXIT_OK
    print(episodes.format_table(visible))
    return EXIT_OK


def _warn_unusual_jump(command: str, transition: lifecycle.Transition) -> None:
    """The one warn-but-never-block §5.3 line — shared by ``status`` and ``publish --url``."""
    if transition.unusual_reason is not None:
        print(
            f"cwp {command}: warning: unusual jump {transition.old_status} -> "
            f"{transition.new_status} ({transition.unusual_reason}) — allowed, recorded",
            file=sys.stderr,
        )


def _cmd_status(args: argparse.Namespace, episodes_dir: Path) -> int:
    transition = lifecycle.apply_status(episodes_dir, args.id, args.status)
    _warn_unusual_jump("status", transition)
    print(f"{transition.episode.id}: {transition.old_status} -> {transition.new_status}")
    if transition.published_at_stamped:
        print(f"published_at: {transition.episode.published_at}")
    return EXIT_OK


def _cmd_next(_args: argparse.Namespace, episodes_dir: Path) -> int:
    result = episodes.scan_episodes(episodes_dir)
    for warning in result.warnings:
        print(f"cwp next: warning: {warning}", file=sys.stderr)
    suggestion = lifecycle.pick_next(result.episodes)
    if suggestion is None:
        print(
            "nothing in flight — every episode is published, on-hold, or cut"
            ' (or none exist yet); try: cwp new "<title>"'
        )
        return EXIT_OK
    episode = suggestion.episode
    print(f"{episode.id}  [{episode.status}]  {episode.title}")
    print(f"next: {suggestion.action}")
    return EXIT_OK


def _cmd_show(args: argparse.Namespace, episodes_dir: Path) -> int:
    _directory, episode = episodes.load_episode(episodes_dir, args.id)
    print(episodes.format_detail(episode))
    return EXIT_OK


def _cmd_draft(args: argparse.Namespace, episodes_dir: Path) -> int:
    # _episode_command hands only episodes_dir; re-derive Paths for voice.md (the wrapper
    # already proved the root walk succeeds, so this cannot raise here).
    voice_md = get_paths().voice_md
    try:
        result = drafting.run_draft(
            episodes_dir, voice_md, args.id, args.kind, dry_run=args.dry_run
        )
    except drafting.DraftEnvError as exc:  # claude missing / unauthed / timed out
        print(f"cwp draft: {exc}", file=sys.stderr)
        return EXIT_ENV_ERROR
    text = result.text
    if text is None:  # --dry-run: the assembled prompt IS the output
        print(result.prompt)
        return EXIT_OK
    if result.to_stdout:  # title/description: the draft itself goes to stdout
        print(text.strip())
        if result.target is not None:
            print(
                f"cwp draft: also appended to {_display_path(result.target)}"
                f" (review, then remove the {drafting.AI_DRAFT_MARKER} marker)",
                file=sys.stderr,
            )
        else:
            print(
                "cwp draft: publish.md already regenerated (or missing) — "
                "draft printed to stdout only",
                file=sys.stderr,
            )
        return EXIT_OK
    assert result.target is not None  # file kinds always write on success
    print(f"drafted {result.kind} -> {_display_path(result.target)}")
    return EXIT_OK


def _cmd_capture(args: argparse.Namespace, episodes_dir: Path) -> int:
    """§4.3 output contract: the transcript path goes to stdout; the one-time unscanned
    warning and the low-confidence re-record hint go to stderr (exit stays 0 for both)."""
    # _episode_command hands only episodes_dir; re-derive Paths for private/ (the wrapper
    # already proved the root walk succeeds, so this cannot raise here).
    redact_path = get_paths().redact_names_txt
    try:
        result = capture.run_capture(
            episodes_dir,
            redact_path,
            args.id,
            Path(args.audio),
            model_size=args.model,
            allow_names=args.allow_names,
        )
    except capture.CaptureEnvError as exc:  # whisper import / model / decode failure
        print(f"cwp capture: {exc}", file=sys.stderr)
        return EXIT_ENV_ERROR
    if result.scan_state == "unscanned":  # absent redact file → one-time warning (§4.3)
        print(f"cwp capture: warning: {capture.UNSCANNED_NOTICE}", file=sys.stderr)
    if result.low_confidence_reason is not None:
        print(
            f"cwp capture: {capture.RERECORD_HINT} ({result.low_confidence_reason})",
            file=sys.stderr,
        )
    detail = f"{result.word_count} words"
    if result.redacted_count:
        detail += f", {result.redacted_count} name(s) redacted"
    elif result.scan_state == "skipped":
        detail += ", redaction skipped (--allow-names)"
    print(f"transcribed -> {_display_path(result.transcript_path)} ({detail})")
    return EXIT_OK


def _cmd_publish(args: argparse.Namespace, episodes_dir: Path) -> int:
    """Stdout carries ONLY the paste block + checklist (+ ``--url`` record lines) so a
    redirect stays paste-clean; warnings and the wrote-file note go to stderr."""
    result = publishing.run_publish(episodes_dir, args.id, url=args.url)
    for warning in result.warnings:
        print(f"cwp publish: warning: {warning}", file=sys.stderr)
    print(f"cwp publish: wrote {_display_path(result.publish_path)}", file=sys.stderr)
    print(result.block.rstrip("\n"))
    print()
    print(publishing.render_checklist().rstrip("\n"))
    transition = result.transition
    if transition is None:  # no --url: publish prep only, status untouched
        return EXIT_OK
    _warn_unusual_jump("publish", transition)
    print()
    print(f"{transition.episode.id}: {transition.old_status} -> {transition.new_status}")
    print(f"youtube_url: {transition.episode.youtube_url}")
    if transition.published_at_stamped:
        print(f"published_at: {transition.episode.published_at}")
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

    p = sub.add_parser("list", help="derived episode table: status + cycle time")
    p.add_argument(
        "--all",
        action="store_true",
        help="include cut episodes (hidden from the default list)",
    )

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
        choices=drafting.KINDS,
        help="what to draft",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the fully-assembled prompt and exit — no claude call, no preflight",
    )

    p = sub.add_parser("publish", help="paste-ready YouTube metadata / mark published")
    p.add_argument("id", help="episode id or seq")
    p.add_argument("--url", help="published YouTube URL (records it + sets published)")

    p = sub.add_parser("capture", help="transcribe a kid clip via local faster-whisper")
    p.add_argument("id", help="episode id or seq")
    p.add_argument("--audio", required=True, help="path to the recorded clip (--record is v3)")
    p.add_argument(
        "--model",
        choices=capture.WHISPER_MODELS,
        default=DEFAULT_WHISPER_MODEL,
        help=f"whisper model size (default: {DEFAULT_WHISPER_MODEL}; medium = escalation)",
    )
    p.add_argument(
        "--allow-names",
        action="store_true",
        help="skip the private/redact-names.txt scan (names stay verbatim)",
    )

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
    handlers["status"] = _episode_command("status", _cmd_status)
    handlers["next"] = _episode_command("next", _cmd_next)
    handlers["draft"] = _episode_command("draft", _cmd_draft)
    handlers["capture"] = _episode_command("capture", _cmd_capture)
    handlers["publish"] = _episode_command("publish", _cmd_publish)
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
