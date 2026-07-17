"""Build engine (plan.md §14 Step 9 + §3.2): the generate -> verify -> repair -> commit loop.

This is the RELIABILITY CORE. ``cwp build <id> [--force]`` turns a distilled ``brief.md``
into a verified ``project/index.html`` -- or refuses. The two invariants (§3.2 item 6):
**never ship a broken toy; never clobber an existing one.**

The loop (§3.2 items 1-7 / research §3), all through seams this project already owns:

1. **Generate.** Assemble the prompt = ``build-contract.md`` (full text, the one-shot
   generation contract) with the brief's fields substituted into its ``{placeholder}``
   tokens, then call the drafting.py claude seam (:func:`drafting.call_claude`) with build's
   OWN :data:`BUILD_TIMEOUT` (~300s, NOT drafting's 60s -- the same mechanism, a longer
   number). Prompt via stdin, neutral cwd, tree-kill timeout: all inherited from the seam.
   Preflight auth runs once (:func:`drafting.ensure_claude_ready`) before the first call.
2. **Extract** the single ```` ```html ```` block. Fence discipline (issue #17, BOTH ways):
   count ```` ```html ```` OPENINGS -- exactly one -- then end the toy at its own last
   ``</html>``. A bare ```` ``` ```` inside a JS template literal BEFORE ``</html>`` is kept
   (issue #17); a trailing closing fence + courtesy prose / a second fenced aside AFTER it is
   dropped (the inverse over-extraction case). 0 or >1 openings => a repair-triggering failure
   carrying the fence-specific evidence template.
3. **Verify.** Write the extracted HTML to a TEMP file under ``.repair/`` (NEVER
   ``project/index.html`` directly) and run :func:`verify.verify_toy` -- the calibrated
   instrument -- with the brief and a per-attempt ``.repair/attempt-N.png`` screenshot.
4. **Repair** (<=2 retries, 3 shots total). On any verify failure, a second call with the
   original contract + brief + the FULL failing HTML + the EXACT verify findings verbatim
   (each check id + its offending line/selector/console text -- never a paraphrase). A repair
   response near-identical to the previous attempt (normalized-whitespace difflib ratio >
   :data:`NEAR_IDENTICAL_RATIO`) aborts straight to ``needs_human`` -- no burning the last slot
   on a stuck model.
5. **Timeout != repair attempt.** A :class:`drafting.ClaudeTimeoutError` (vs a content/verify
   failure) is retried ONCE at the SAME slot and does NOT consume a repair attempt; a second
   consecutive timeout -> ``needs_human`` with a timeout-specific message.
6. **Commit** only on a full verify pass: atomic ``os.replace`` onto ``project/index.html``
   (via :func:`episodes.atomic_write_bytes`) + a pass logged to ``.repair/log.jsonl``, and any
   prior ``needs_human`` flag in ``meta.toml`` is cleared (a stale give-up flag is a wrong
   operator signal). CLOBBER PROTECTION: an existing NON-placeholder ``index.html`` is refused
   without ``--force`` (user error, exit 1) BEFORE any claude call; ``--force`` allows the
   overwrite-on-pass.
7. **Exhaustion.** All 3 shots fail -> ``needs_human=true`` in ``meta.toml`` + the last
   evidence + the screenshot path printed, exit 2. An existing ``index.html`` is NEVER touched
   on any failure path (clobber protection is senior to ``--force``).

Exit-code mapping (cli.py): :class:`cwp.episodes.EpisodeError` -- missing episode, missing/
invalid brief (:class:`cwp.brief.BriefError`), or :class:`ClobberError` -- => 1 (user error);
:class:`cwp.drafting.DraftEnvError` (claude missing/unauthed, contract missing) and
:class:`cwp.verify.HeadlessEnvError` (chromium missing) and a ``needs_human`` result => 2.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from time import monotonic

from cwp import brief as brief_module
from cwp import drafting, episodes, templates, verify
from cwp.brief import Brief
from cwp.episodes import Episode

# Build's OWN timeout (§3.2 item 1): share drafting's mechanism, not its 60s number. One-shot
# HTML generation + a repair carrying full HTML is far heavier than a copy draft.
BUILD_TIMEOUT = 300.0
MAX_REPAIRS = 2  # 3 shots total (initial + 2 repairs)
# Normalized-whitespace difflib ratio above which a repair response counts as "the model is
# stuck" -- abort to needs_human rather than burn the last slot (§3.2 item 5).
NEAR_IDENTICAL_RATIO = 0.98

# The six brief fields build-contract.md substitutes per build (its own header lists them). A
# TARGETED replace per token -- never str.format on the whole contract, whose CSS/JS bodies are
# full of literal { } braces that would raise/mis-substitute.
_CONTRACT_FIELDS = (
    "one_sentence_goal",
    "single_action",
    "visual_motif",
    "must_haves",
    "kid_quote",
    "kid_nickname",
)

# The fence-specific repair evidence (§3.2 item 2 / research §3 item 5) -- verbatim.
_FENCE_EVIDENCE = (
    "your previous response had 0 or >1 ```html fences (or no closing fence) -- return"
    " EXACTLY one ```html fenced block containing the complete index.html, and no prose"
)

# A ```html opening fence alone on its line; and a bare ``` closing fence alone on its line.
# Counting the ```html OPENINGS (not bare ```) is what dodges issue #17: a stray ``` inside a
# JS template literal is a bare fence, never a ```html opening, so it cannot inflate the count.
_FENCE_OPEN_RE = re.compile(r"(?m)^[ \t]*```html[ \t]*$")
_FENCE_CLOSE_RE = re.compile(r"(?m)^[ \t]*```[ \t]*$")
# The document terminator. The toy is ALWAYS a complete HTML document, so the extraction ends at
# its last </html> -- which (a) keeps a bare ``` inside a JS template literal that PRECEDES it
# (issue #17), and (b) drops a trailing closing fence + any courtesy prose / second fenced aside
# the model appends AFTER it (the inverse over-extraction case).
_HTML_END_RE = re.compile(r"</html\s*>", re.IGNORECASE)


class BuildError(episodes.EpisodeError):
    """Base for build-domain USER errors (CLI maps EpisodeError -> exit 1)."""


class ClobberError(BuildError):
    """An existing non-placeholder ``project/index.html`` would be overwritten without
    ``--force`` (user error, exit 1) -- the never-clobber invariant."""


class ContractNotFoundError(drafting.DraftEnvError):
    """``build-contract.md`` (a repo SoT authored in Step 1) is missing/unreadable.

    Mirrors :func:`drafting.read_voice`'s treatment of a missing ``voice.md``: a broken repo
    is an ENVIRONMENT failure (CLI exit 2), not a user error."""


class BuildOutcome(Enum):
    """What :func:`run_build` concluded -- the CLI picks the exit code from this."""

    COMMITTED = "committed"
    NEEDS_HUMAN = "needs_human"


class NeedsHumanReason(Enum):
    """Why a build gave up -- distinguishes the three ``needs_human`` paths in the message."""

    EXHAUSTED = "exhausted the 3-shot repair budget"
    NEAR_IDENTICAL = "the repair returned a near-identical toy (model is stuck)"
    TIMEOUT = "claude timed out twice in a row (a timeout is retried once)"


@dataclass(frozen=True)
class BuildResult:
    """The outcome of one ``cwp build`` (the CLI renders this).

    On ``COMMITTED``: ``index_path`` holds the verified toy, ``log_path`` the pass log. On
    ``NEEDS_HUMAN``: ``evidence`` is the last failure evidence and ``screenshot_path`` the last
    saved attempt PNG (``None`` when every attempt failed the static gate before a browser ran).
    """

    outcome: BuildOutcome
    index_path: Path
    attempts: int
    evidence: str = ""
    screenshot_path: Path | None = None
    reason: NeedsHumanReason | None = None
    log_path: Path | None = None


def _read_contract(contract_path: Path) -> str:
    """Read ``build-contract.md`` (the one-shot generation contract, a repo SoT)."""
    try:
        return contract_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ContractNotFoundError(
            f"{contract_path} not found -- the build contract is a repo SoT (plan.md §5.2);"
            " restore it: git checkout -- build-contract.md"
        ) from exc
    except OSError as exc:
        raise ContractNotFoundError(f"{contract_path}: unreadable: {exc}") from exc


def assemble_prompt(contract: str, brief: Brief) -> str:
    """Substitute the brief's fields into build-contract.md's ``{placeholder}`` tokens.

    A per-token ``str.replace`` (never ``str.format``): the contract embeds literal CSS/JS
    braces that ``format`` would choke on. ``must_haves`` renders as a comma-joined list.
    """
    values = {
        "one_sentence_goal": brief.one_sentence_goal,
        "single_action": brief.single_action,
        "visual_motif": brief.visual_motif,
        "must_haves": ", ".join(brief.must_haves),
        "kid_quote": brief.kid_quote,
        "kid_nickname": brief.kid_nickname,
    }
    prompt = contract
    for field in _CONTRACT_FIELDS:
        prompt = prompt.replace("{" + field + "}", values[field])
    return prompt


def render_evidence(failures: tuple[verify.Finding, ...]) -> str:
    """The EXACT verify findings, one per line -- fed verbatim into the repair prompt.

    Each line carries the check id AND its structured evidence (offending line/selector/console
    text) with no paraphrase, so the model addresses the real defect (§3.2 item 4).
    """
    return "\n".join(f"- [{finding.check}] {finding.evidence}" for finding in failures)


def build_repair_prompt(base_prompt: str, evidence: str, previous_content: str) -> str:
    """The repair call: the ORIGINAL contract+brief prompt, verbatim, + the exact evidence
    + the FULL previous response (§3.2 item 4 / build-contract.md "Repair retries")."""
    return (
        f"{base_prompt}\n"
        "## Repair -- your previous attempt FAILED verification\n"
        "\n"
        "Your previous response is reproduced below, followed by the EXACT automated checks it\n"
        "failed. Fix EXACTLY these problems and change nothing else. Return the FULL corrected\n"
        "index.html as a single ```html fenced block and nothing else -- do not describe the\n"
        "changes.\n"
        "\n"
        "### Failing checks (verbatim evidence)\n"
        "\n"
        f"{evidence}\n"
        "\n"
        "### Your previous response\n"
        "\n"
        f"{previous_content}\n"
    )


def extract_html(response: str) -> str | None:
    """Pull the single ```html document, ending at the toy's own ``</html>`` (issue #17, both ways).

    Requires EXACTLY one ```` ```html ```` opening (0 or >1 -> ``None``, a fence-specific repair).
    From just after that opening, the toy ends at the LAST case-insensitive ``</html>``: an
    internal bare ```` ``` ```` (a JS template literal) BEFORE it is kept, while a trailing closing
    fence + courtesy prose / a second fenced aside AFTER it is dropped. A reply with the opening
    but no ``</html>`` falls back to the last bare closing fence (still bounded to the fenced
    region, never the whole reply); an empty body -> ``None``. Every ``None`` is a repair-triggering
    fence failure carrying :data:`_FENCE_EVIDENCE`.
    """
    opens = list(_FENCE_OPEN_RE.finditer(response))
    if len(opens) != 1:
        return None
    body = response[opens[0].end() :]  # everything after the single ```html opening line
    end_matches = list(_HTML_END_RE.finditer(body))
    if end_matches:
        html = body[: end_matches[-1].end()].strip("\r\n")  # end AT the last </html>
        return html or None
    closes = list(_FENCE_CLOSE_RE.finditer(body))  # no </html>: fall back to the closing fence
    if not closes:
        return None
    html = body[: closes[-1].start()].strip("\r\n")
    return html or None


def _normalize_ws(text: str) -> str:
    """Collapse ALL whitespace away -- so reformatting/reindentation alone can't dodge the
    near-identical guard (only real content differences move the ratio)."""
    return re.sub(r"\s+", "", text)


def near_identical(new_html: str, previous_html: str) -> bool:
    """True when a repair barely changed the toy (§3.2 item 5) -- normalized-ws difflib ratio."""
    ratio = difflib.SequenceMatcher(None, _normalize_ws(new_html), _normalize_ws(previous_html))
    return ratio.ratio() > NEAR_IDENTICAL_RATIO


def _check_clobber(index_path: Path, *, force: bool) -> None:
    """The never-clobber gate: refuse an existing NON-placeholder toy without ``--force``.

    The ``cwp new`` scaffold (:func:`templates.render_index_html_placeholder`, carrying
    :data:`templates.INDEX_HTML_PLACEHOLDER_SENTINEL`) is safe to overwrite. A real toy -- or
    any hand-edit that dropped the sentinel -- is protected. Runs BEFORE any claude call so a
    refusal never burns a generation.
    """
    if force or not index_path.exists():
        return
    try:
        content = index_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        content = ""  # unreadable/binary -> treat as real content worth protecting
    if templates.INDEX_HTML_PLACEHOLDER_SENTINEL in content:
        return
    raise ClobberError(
        f"{index_path} already holds a built toy -- refusing to overwrite it. Re-run with"
        " --force to regenerate (the existing toy is only replaced on a verified pass)."
    )


def _call_with_timeout_retry(prompt: str, partial_path: Path) -> str:
    """One claude call at :data:`BUILD_TIMEOUT`, with a SINGLE same-slot retry on timeout.

    A first :class:`drafting.ClaudeTimeoutError` is retried once (§3.2 item 5 -- does NOT
    consume a repair attempt); a second consecutive timeout propagates for the caller to convert
    into a timeout-specific ``needs_human``. Non-timeout :class:`drafting.DraftEnvError`
    (auth/not-found/nonzero-exit) propagate unchanged -> CLI exit 2.
    """
    try:
        return drafting.call_claude(prompt, timeout=BUILD_TIMEOUT, partial_path=partial_path)
    except drafting.ClaudeTimeoutError:
        return drafting.call_claude(prompt, timeout=BUILD_TIMEOUT, partial_path=partial_path)


def _append_log(
    log_path: Path, attempt: int, result: verify.VerifyResult, duration_ms: int
) -> None:
    """Append one JSONL row per check of the committing attempt (§3.2 item 6 schema)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = episodes.utc_now_iso()
    with log_path.open("a", encoding="utf-8") as handle:
        for finding in result.findings:
            handle.write(
                json.dumps(
                    {
                        "attempt": attempt,
                        "timestamp": timestamp,
                        "check": finding.check,
                        "passed": finding.passed,
                        "duration_ms": duration_ms,
                        "evidence": finding.evidence,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def _needs_human(
    directory: Path,
    episode: Episode,
    index_path: Path,
    *,
    reason: NeedsHumanReason,
    evidence: str,
    screenshot_path: Path | None,
    attempts: int,
) -> BuildResult:
    """Set ``needs_human=true`` in meta.toml and return the give-up result (exit 2).

    NEVER touches ``index_path`` -- clobber protection holds on every failure path.
    """
    episode.needs_human = True
    episodes.write_meta(directory, episode)
    return BuildResult(
        outcome=BuildOutcome.NEEDS_HUMAN,
        index_path=index_path,
        attempts=attempts,
        evidence=evidence,
        screenshot_path=screenshot_path,
        reason=reason,
    )


def run_build(
    episodes_dir: Path,
    build_contract_md: Path,
    id_or_seq: str,
    *,
    force: bool = False,
) -> BuildResult:
    """The full ``cwp build`` flow: generate -> extract -> verify -> repair -> commit.

    Raises :class:`cwp.episodes.EpisodeError` (missing episode), :class:`cwp.brief.BriefError`
    (missing/invalid brief), or :class:`ClobberError` -- all user-class (exit 1) -- and
    :class:`cwp.drafting.DraftEnvError` / :class:`cwp.verify.HeadlessEnvError` (environment,
    exit 2). A ``needs_human`` outcome is RETURNED (not raised); the CLI maps it to exit 2.
    """
    directory, episode = episodes.load_episode(episodes_dir, id_or_seq)
    brief = brief_module.load_brief(directory)  # BriefError (missing/invalid) -> exit 1
    index_path = directory / "project" / "index.html"
    _check_clobber(index_path, force=force)  # refuse a real toy w/o --force BEFORE any call
    base_prompt = assemble_prompt(_read_contract(build_contract_md), brief)

    repair_dir = directory / "project" / ".repair"
    repair_dir.mkdir(parents=True, exist_ok=True)
    partial_path = repair_dir / "build.partial.txt"

    drafting.ensure_claude_ready()  # preflight once, before the first call

    previous_html: str | None = None  # last SUCCESSFULLY-extracted HTML (near-identical anchor)
    previous_content = ""  # the previous response the repair prompt reproduces (HTML or raw)
    last_evidence = ""
    last_screenshot: Path | None = None

    for attempt in range(MAX_REPAIRS + 1):  # shots 0 (initial), 1, 2 (repairs)
        shot = attempt + 1
        if attempt == 0:
            prompt = base_prompt
        else:
            prompt = build_repair_prompt(base_prompt, last_evidence, previous_content)
        try:
            response = _call_with_timeout_retry(prompt, partial_path)
        except drafting.ClaudeTimeoutError:
            return _needs_human(
                directory,
                episode,
                index_path,
                reason=NeedsHumanReason.TIMEOUT,
                evidence=f"claude timed out twice at shot {shot} ({BUILD_TIMEOUT:g}s each)",
                screenshot_path=last_screenshot,
                attempts=attempt,  # the timeout consumed NO repair attempt
            )

        html = extract_html(response)
        if html is None:
            last_evidence = _FENCE_EVIDENCE
            previous_content = response  # reproduced in the next repair prompt
            continue  # a fence failure consumes this shot and triggers a repair

        if attempt > 0 and previous_html is not None and near_identical(html, previous_html):
            return _needs_human(
                directory,
                episode,
                index_path,
                reason=NeedsHumanReason.NEAR_IDENTICAL,
                evidence=last_evidence,
                screenshot_path=last_screenshot,
                attempts=attempt,  # abort BEFORE spending this slot on verification
            )

        candidate_path = repair_dir / f"attempt-{shot}.html"
        screenshot_path = repair_dir / f"attempt-{shot}.png"
        candidate_path.write_bytes(html.encode("utf-8"))  # TEMP file -- never index.html
        started = monotonic()
        result = verify.verify_toy(candidate_path, brief, screenshot_path=screenshot_path)
        duration_ms = int((monotonic() - started) * 1000)
        if result.screenshot_path is not None:
            last_screenshot = result.screenshot_path

        if result.ok:
            episodes.atomic_write_bytes(index_path, html.encode("utf-8"))  # commit (os.replace)
            if episode.needs_human:
                episode.needs_human = False  # a verified toy clears a prior give-up flag
                episodes.write_meta(directory, episode)  # full-record save: no field dropped
            log_path = repair_dir / "log.jsonl"
            _append_log(log_path, shot, result, duration_ms)
            return BuildResult(
                outcome=BuildOutcome.COMMITTED,
                index_path=index_path,
                attempts=shot,
                log_path=log_path,
            )

        last_evidence = render_evidence(result.failures())
        previous_content = html  # reproduced in the next repair prompt
        previous_html = html

    return _needs_human(
        directory,
        episode,
        index_path,
        reason=NeedsHumanReason.EXHAUSTED,
        evidence=last_evidence,
        screenshot_path=last_screenshot,
        attempts=MAX_REPAIRS + 1,
    )
