"""AI drafting (plan.md §14 Step 4): prompt assembly + the ONE ``claude -p`` subprocess seam.

Powers ``cwp draft <id> <outline|script|title|description> [--dry-run]`` and exports the
claude-call seam that ``brief.py`` (Step 7) and ``build.py`` (Step 9) import — one
subprocess wrapper, three callers, each pinning its own timeout (drafts default to
``DRAFT_TIMEOUT`` ≈ 60s; the build engine passes its own ~300s).

The seam contract (verified live against claude v2.1.x; plan.md §3.2 + §14 Step 1):

- The prompt travels via **stdin**, never argv — Windows' ~32K argv ceiling. ``claude -p``
  with NO positional prompt reads the piped prompt and prints the response (verified:
  ``'Reply with exactly: ok' | claude -p`` → ``ok``, exit 0).
- ``cwd`` is a **neutral temp directory** (created/reused per user) so the repo's CLAUDE.md
  never leaks into the prompt: ``claude -p`` auto-discovers cwd CLAUDE.md by default, and
  ``--bare`` (which disables discovery) also restricts auth to ``ANTHROPIC_API_KEY`` — so
  neutral-cwd is the chosen mechanism, with ``--safe-mode`` as the verified fallback flag.
- Text mode, UTF-8, ``errors="replace"`` on both sides.
- **Timeout = process-TREE kill.** The PATH-resolved ``claude`` is usually the npm
  ``claude.cmd`` shim, so the tracked process is cmd.exe while a node grandchild does the
  work and inherits the stdout pipe — a plain ``kill()`` reaps only cmd.exe and the
  follow-up pipe read blocks until the orphan exits on its own. On timeout the seam kills
  the whole tree FIRST (Windows: ``taskkill /T /F``; non-Windows: ``kill()``), then reaps.
- **Partial-write idempotency:** on timeout or a failed call, whatever stdout was captured
  is flushed atomically to a caller-named PARTIAL file (never the target) so a retry can
  inspect or discard it; the raised error names that file. The target is written only on
  success, via ``episodes.atomic_write_bytes`` (same-dir temp file + ``os.replace``).
- **Preflight:** :func:`preflight` is the seam-testable cheap ``claude -p`` "ok" probe;
  :func:`ensure_claude_ready` runs it once per process before the first real call. A
  missing binary or auth failure raises with fix-it text (CLI exit 2).

Draft targets — the DECIDE from plan.md §14 Step 4, recorded here:

- ``outline`` and ``script`` both REPLACE ``script.md`` wholesale. An outline is just an
  earlier-fidelity script; one target keeps ONE writer for the file.
- ``title`` and ``description`` always print to stdout, AND are appended as a marked block
  to ``publish.md`` while that file still carries its Step-1 placeholder sentinel
  (``templates.PUBLISH_PLACEHOLDER_SENTINEL``). Step 5's ``cwp publish`` WHOLESALE-
  regenerates publish.md from meta.toml + script.md, so the appended blocks are
  pre-publish operator scratch — never inputs ``cwp publish`` reads. Once regeneration
  removes the sentinel, those drafts are stdout-only; publish owns the file from then on.
- Every block of content this module writes opens with the literal ``<!-- AI DRAFT -->``
  marker line (file-first line for script.md, block-first line for publish.md appends);
  Step 5's publish warns on markers still present at publish time.

Exit-code mapping (cli.py): :class:`cwp.episodes.EpisodeError` → 1 (user error, e.g. a bad
id); :class:`DraftEnvError` → 2 (environment: claude missing / unauthed / timed out).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from cwp import episodes, templates
from cwp.episodes import Episode

CLAUDE_BINARY = "claude"
DRAFT_TIMEOUT = 60.0  # seconds — this module's callers; build.py (Step 9) pins ~300s instead
PREFLIGHT_TIMEOUT = 30.0  # seconds — the cheap "ok" probe
AI_DRAFT_MARKER = "<!-- AI DRAFT -->"

KINDS = ("outline", "script", "title", "description")
FILE_KINDS = ("outline", "script")  # both replace script.md (module docstring)
# DERIVED, never restated (single source of truth): the non-file kinds print to stdout
# (+ the best-effort publish.md append). A drift test asserts the partition stays exact.
STDOUT_KINDS = tuple(kind for kind in KINDS if kind not in FILE_KINDS)

_PREFLIGHT_PROMPT = "Reply with exactly: ok"
_INSTALL_FIX_IT = (
    "Claude CLI not found — install/log in: `npm install -g @anthropic-ai/claude-code`, "
    "then run `claude` once interactively to log in, then retry"
)
_AUTH_FIX_IT = (
    "Not signed in? Run `claude` once interactively to log in "
    "(or set ANTHROPIC_API_KEY), then retry"
)

# The per-variant instruction is the ONLY thing that differs between the four kinds —
# everything else (voice, context, call, marker) is one shared code path.
_INSTRUCTIONS: dict[str, str] = {
    "outline": (
        "Draft a beat-by-beat OUTLINE for this episode's video as a Markdown bullet list "
        "(8-12 beats): the hook, the build in small steps, and the on-camera Pantsless "
        "Test payoff at the end."
    ),
    "script": (
        "Draft the full read-aloud SCRIPT for this episode, in Markdown with exactly these "
        "sections: '## Hook' (the first 15 seconds), '## Script' (the read-aloud "
        "narration), and '## On-screen actions' (what the camera shows, beat by beat)."
    ),
    "title": (
        "Draft 5 candidate YouTube TITLES for this episode, one per line, each 70 "
        "characters or fewer. Just the five lines — no numbering, no commentary."
    ),
    "description": (
        "Draft the YouTube DESCRIPTION for this episode: 2-3 short paragraphs in the "
        'channel voice, then one plain-language "What you\'ll learn:" line. '
        "No hashtags, no links."
    ),
}

_preflight_passed = False  # per-process cache; ensure_claude_ready() flips it on success


class DraftEnvError(Exception):
    """Base for environment failures around the claude call (CLI maps to exit 2)."""


class ClaudeNotFoundError(DraftEnvError):
    """The ``claude`` binary is not on PATH (or could not be spawned)."""


class ClaudeAuthError(DraftEnvError):
    """The preflight probe failed — most commonly not logged in."""


class ClaudeCallError(DraftEnvError):
    """A real call exited nonzero or produced no output."""


class ClaudeTimeoutError(DraftEnvError):
    """A real call exceeded its caller-pinned timeout."""


class UnknownDraftKindError(episodes.EpisodeError):
    """The draft kind is not one of :data:`KINDS` (CLI maps to exit 1, user error)."""


@dataclass(frozen=True)
class DraftResult:
    """What :func:`run_draft` did (the CLI renders this).

    ``text is None`` means ``--dry-run`` (print ``prompt``). ``to_stdout`` marks the
    title/description kinds whose draft text the CLI prints; ``target`` is the file the
    draft landed in (``None`` for a stdout-only draft — publish.md regenerated/missing).
    """

    kind: str
    prompt: str
    text: str | None
    target: Path | None
    to_stdout: bool


def neutral_cwd() -> Path:
    """Create/reuse the neutral temp directory the claude subprocess runs in.

    A fixed name under the system temp dir — empty of CLAUDE.md by construction, reused
    across calls so we don't litter %TEMP% with one dir per call.
    """
    directory = Path(tempfile.gettempdir()) / "cwp-neutral-cwd"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _resolve_claude() -> str:
    """PATH-resolve the claude binary; ``shutil.which`` also matches Windows ``.cmd``."""
    exe = shutil.which(CLAUDE_BINARY)
    if exe is None:
        raise ClaudeNotFoundError(_INSTALL_FIX_IT)
    return exe


def _coerce_text(captured: object) -> str:
    """Normalize captured stdout: ``TimeoutExpired.stdout`` can be bytes even in text mode."""
    if captured is None:
        return ""
    if isinstance(captured, bytes):
        return captured.decode("utf-8", "replace")
    return str(captured)


def _flush_partial(partial_path: Path | None, captured: str) -> Path | None:
    """Idempotency valve: flush partial stdout to the PARTIAL file, never the target."""
    if partial_path is None or not captured:
        return None
    episodes.atomic_write_bytes(partial_path, captured.encode("utf-8"))
    return partial_path


def _stderr_excerpt(stderr: str) -> str:
    collapsed = " ".join(stderr.split())
    return collapsed[:300]


def _taskkill_exe() -> str:
    """Absolute path to taskkill.exe — PATH-independent (cwp may run PATH-shadowed)."""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    return os.path.join(system_root, "System32", "taskkill.exe")


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    """Kill the WHOLE claude process tree, not just the tracked root.

    On Windows the PATH-resolved ``claude`` is typically ``claude.cmd`` (the npm shim):
    the tracked process is cmd.exe while the real node grandchild does the work AND
    inherits the stdout pipe. A plain ``kill()`` (TerminateProcess) reaps only cmd.exe,
    and the follow-up pipe read then blocks until the orphan exits on its own — so the
    tree must die first (``taskkill /T /F``, the workspace-proven pattern). Non-Windows
    falls back to ``kill()``.
    """
    if sys.platform == "win32":
        try:
            subprocess.run(
                [_taskkill_exe(), "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
        except OSError:
            pass
    try:
        proc.kill()  # reap the root too (a no-op if taskkill already got it)
    except OSError:
        pass


def _execute(exe: str, prompt: str, timeout: float) -> subprocess.CompletedProcess[str]:
    """The ONE subprocess touchpoint: run ``exe -p`` with *prompt* on stdin, neutral cwd.

    On timeout the process TREE is killed FIRST (:func:`_kill_tree` — killing only the
    tracked root would leave a pipe-holding orphan and the reap would block), then a
    final ``communicate()`` reaps the dead tree and drains the pipes; the re-raised
    ``TimeoutExpired`` carries that partial stdout for the callers' temp-flush path.
    """
    proc = subprocess.Popen(
        [exe, "-p"],  # prompt via stdin, NEVER argv (Windows ~32K argv ceiling)
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=neutral_cwd(),
    )
    try:
        stdout, stderr = proc.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_tree(proc)  # MUST precede the reap, or it blocks on surviving orphans
        stdout, stderr = proc.communicate()  # returns everything captured before the kill
        raise subprocess.TimeoutExpired(
            cmd=[exe, "-p"], timeout=timeout, output=stdout, stderr=stderr
        ) from exc
    except BaseException:  # e.g. KeyboardInterrupt mid-call: don't leak the tree
        _kill_tree(proc)
        proc.communicate()
        raise
    return subprocess.CompletedProcess([exe, "-p"], proc.wait(), stdout, stderr)


def call_claude(
    prompt: str, *, timeout: float = DRAFT_TIMEOUT, partial_path: Path | None = None
) -> str:
    """THE subprocess seam: run ``claude -p`` (prompt on stdin, neutral cwd), return stdout.

    Callers pin their own *timeout* (drafts ~60s, build ~300s) and may name a
    *partial_path* — on timeout/failure the captured stdout is flushed there atomically
    so a retry can inspect or discard it; the target file is never touched by this
    function. Raises the :class:`DraftEnvError` family only.
    """
    exe = _resolve_claude()
    try:
        result = _execute(exe, prompt, timeout)
    except subprocess.TimeoutExpired as exc:
        flushed = _flush_partial(partial_path, _coerce_text(exc.stdout))
        where = f" — partial stdout flushed to {flushed}" if flushed else ""
        raise ClaudeTimeoutError(
            f"Claude call timed out after {timeout:g}s{where}; the draft target was not touched"
        ) from exc
    except OSError as exc:
        raise ClaudeNotFoundError(f"Could not run {exe!r}: {exc}. {_INSTALL_FIX_IT}") from exc
    if result.returncode != 0:
        flushed = _flush_partial(partial_path, result.stdout)
        where = f" — partial stdout flushed to {flushed}" if flushed else ""
        detail = _stderr_excerpt(result.stderr) or "no stderr"
        raise ClaudeCallError(
            f"Claude call failed (exit {result.returncode}): {detail}{where} — {_AUTH_FIX_IT}"
        )
    if not result.stdout.strip():
        raise ClaudeCallError("Claude call exited 0 but returned no output — retry, or check auth")
    return result.stdout


def preflight(*, timeout: float = PREFLIGHT_TIMEOUT) -> None:
    """The cheap auth probe (seam-testable on its own): ``claude -p`` must answer at all.

    Raises :class:`ClaudeNotFoundError` (binary missing) or :class:`ClaudeAuthError`
    (probe exited nonzero / timed out — most commonly not logged in).
    """
    exe = _resolve_claude()
    try:
        result = _execute(exe, _PREFLIGHT_PROMPT, timeout)
    except subprocess.TimeoutExpired as exc:
        raise ClaudeAuthError(
            f"Claude preflight timed out after {timeout:g}s — {_AUTH_FIX_IT}"
        ) from exc
    except OSError as exc:
        raise ClaudeNotFoundError(f"Could not run {exe!r}: {exc}. {_INSTALL_FIX_IT}") from exc
    if result.returncode != 0:
        detail = _stderr_excerpt(result.stderr) or "no stderr"
        raise ClaudeAuthError(
            f"Claude preflight failed (exit {result.returncode}): {detail} — {_AUTH_FIX_IT}"
        )


def ensure_claude_ready(*, timeout: float = PREFLIGHT_TIMEOUT) -> None:
    """Run :func:`preflight` once per process, before the first real call; cache success."""
    global _preflight_passed
    if _preflight_passed:
        return
    preflight(timeout=timeout)
    _preflight_passed = True


def read_voice(voice_path: Path) -> str:
    """Read the frozen channel voice (repo-root ``voice.md``, seeded in Step 1)."""
    try:
        return voice_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DraftEnvError(
            f"{voice_path} not found — restore the frozen channel voice "
            "(plan.md Appendix A): git checkout -- voice.md"
        ) from exc
    except OSError as exc:
        raise DraftEnvError(f"{voice_path} unreadable: {exc}") from exc


def build_prompt(voice_text: str, episode: Episode, kind: str) -> str:
    """Assemble the draft prompt: full voice.md + episode context + per-kind instruction.

    ONE code path for all four kinds — only the instruction text varies.
    """
    instruction = _INSTRUCTIONS.get(kind)
    if instruction is None:
        raise UnknownDraftKindError(
            f"Unknown draft kind {kind!r} (expected one of: {', '.join(KINDS)})"
        )
    context = "\n".join(
        [
            f"- id: {episode.id}",
            f"- title: {episode.title}",
            f"- hook: {episode.hook or '(none yet)'}",
            f"- teaches: {episode.teaches or '(none yet)'}",
            f"- ingredient: {episode.ingredient}",
            f"- tags: {', '.join(episode.tags) or '(none)'}",
        ]
    )
    return (
        'You draft copy for the YouTube channel "Coding without Pants". Write in the\n'
        "channel voice below — it is the single source of truth for tone.\n"
        "\n"
        "## Channel voice (voice.md, verbatim)\n"
        "\n"
        f"{voice_text.strip()}\n"
        "\n"
        "## Episode context (meta.toml)\n"
        "\n"
        f"{context}\n"
        "\n"
        "## Task\n"
        "\n"
        f"{instruction}\n"
        "\n"
        "Return ONLY the drafted content — no preamble, no code fences, no sign-off.\n"
    )


def _marked(text: str) -> str:
    """Draft content always opens with the literal marker line (Step 5 warns on it)."""
    return f"{AI_DRAFT_MARKER}\n\n{text.strip()}\n"


def _append_to_publish(publish_path: Path, kind: str, text: str, partial_path: Path) -> Path | None:
    """Append a marker-first draft block while publish.md is still the Step-1 placeholder.

    Once ``cwp publish`` regenerates the file (sentinel gone) — or the file is missing —
    the draft is stdout-only and publish.md is left alone (``None``). A hand-edited
    publish.md with invalid UTF-8 raises a clean :class:`episodes.EpisodeError` (exit 1,
    consistent with ``episodes.read_meta``'s handling of the same class) — the draft text
    is saved to *partial_path* first, so the already-burned claude call is not lost.
    """
    try:
        current = publish_path.read_text(encoding="utf-8")
    except OSError:
        return None
    except UnicodeDecodeError as exc:
        episodes.atomic_write_bytes(partial_path, text.encode("utf-8"))
        raise episodes.EpisodeError(
            f"{publish_path} is not valid UTF-8 ({exc}) — fix or delete it; "
            f"the draft was saved to {partial_path}"
        ) from exc
    if templates.PUBLISH_PLACEHOLDER_SENTINEL not in current:
        return None
    block = f"\n{AI_DRAFT_MARKER}\n## {kind.capitalize()} draft (cwp draft)\n\n{text.strip()}\n"
    payload = current.rstrip("\n") + "\n" + block
    episodes.atomic_write_bytes(publish_path, payload.encode("utf-8"))
    return publish_path


def _write_draft(directory: Path, kind: str, text: str, partial_path: Path) -> Path | None:
    """Land the draft per the module-docstring target decision; atomic writes only."""
    if kind in FILE_KINDS:
        target = directory / "script.md"
        episodes.atomic_write_bytes(target, _marked(text).encode("utf-8"))
        return target
    return _append_to_publish(directory / "publish.md", kind, text, partial_path)


def run_draft(
    episodes_dir: Path,
    voice_path: Path,
    id_or_seq: str,
    kind: str,
    *,
    dry_run: bool = False,
    timeout: float | None = None,
) -> DraftResult:
    """The full ``cwp draft`` flow behind the CLI — all four kinds, one shared code path.

    ``--dry-run`` returns after prompt assembly: no preflight, no claude call. Otherwise
    preflight runs once per process, then the seam is called with the draft timeout and a
    per-kind PARTIAL file (``draft-<kind>.partial.txt`` in the episode folder) for the
    idempotency flush.
    """
    directory, episode = episodes.load_episode(episodes_dir, id_or_seq)
    prompt = build_prompt(read_voice(voice_path), episode, kind)
    if dry_run:
        return DraftResult(kind=kind, prompt=prompt, text=None, target=None, to_stdout=False)
    ensure_claude_ready()
    effective_timeout = DRAFT_TIMEOUT if timeout is None else timeout
    partial_path = directory / f"draft-{kind}.partial.txt"
    text = call_claude(prompt, timeout=effective_timeout, partial_path=partial_path)
    target = _write_draft(directory, kind, text, partial_path)
    return DraftResult(
        kind=kind, prompt=prompt, text=text, target=target, to_stdout=kind in STDOUT_KINDS
    )
