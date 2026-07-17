"""Toy verifier (plan.md §14 Step 8): the deterministic gate every generated toy must pass.

``build.py`` (Step 9) feeds each extracted candidate through :func:`verify_toy` and repairs from
the returned evidence, so EVERY check reports a structured finding — ``{check, passed, evidence}``
with the exact offending line / selector / console text — never a bare boolean. There is no LLM
anywhere in this module (measurement-validity: the instrument must not share failure modes with
the generator it gates), and the calibration fixtures under ``tests/fixtures/`` anchor it: a
golden toy passes every check; each single-defect garbage fixture fails FOR its own defect.

**Static checks** (fast, no browser). :data:`FORBIDDEN_PATTERNS` is THE machine-enforced list
of ``build-contract.md``'s NEVER items — the contract cites this constant as the one source of
truth and never restates it. The mapping is not asserted by eyeball: ``test_verify.py``'s drift
test parses the contract's ``## NEVER`` section and fails if any forbidden construct it names has
no enforcing pattern here, so the two cannot silently drift. Coverage: every absolute
``http(s)://`` (and ``ws(s)://``) URL — scanned over the WHOLE text (not per line), so unquoted
(``src=https://…``), backtick/template-literal, and attr-name/URL-split-across-lines forms are
all caught, since each still contains the literal scheme — plus ``fetch(`` / ``XMLHttpRequest`` /
``WebSocket`` / ``EventSource`` / ``sendBeacon(`` / dynamic ``import(`` (network calls),
``<link rel="stylesheet"`` / ``@import`` / CSS ``url("http…")`` / ``<img>`` with a non-``data:``
``src`` / protocol-relative ``src=``/``href=`` (external resource loads), and
``alert(`` / ``confirm(`` / ``prompt(`` (blocking dialogs — they freeze the page AND would hang
the headless phase, which is why ANY static failure short-circuits the browser phase entirely).
The allowlist exempts ONLY inline-SVG namespace DECLARATIONS (``xmlns="http://www.w3.org/…"`` /
``xmlns:xlink="…"``) — ``xlink:href="http…"`` is a REAL fetched resource on ``<use>``/``<image>``
and is NOT exempt. The ``</script>`` rule is a COUNT (>1 occurrences = a string-embedded literal
prematurely closing the toy's script tag; exactly one is the toy's own closing tag and NEVER a
failure). Plus ``<!DOCTYPE html>`` (case-insensitive) and the :data:`MIN_TOY_BYTES` size floor.
Each hit carries the exact offending line as evidence.

**Headless checks** (Playwright Chromium, imported LAZILY — ``import cwp.verify`` must stay
heavy-free, mirroring cli.py's contract). Chromium is launched with
``--autoplay-policy=user-gesture-required`` and Playwright's own relaxing default flag stripped
(the relaxed default would mask exactly the gesture-gating bugs hunted here). **Defense in depth:
the browsing context routes EVERY request through an abort handler** — ``context.route`` aborts
http(s)/EventSource/beacon/img/font/etc. (only ``file:``/``data:``/``blob:``/``about:`` continue),
and ``context.route_web_socket`` drops WebSocket handshakes (which ride a separate CDP domain
``context.route`` does NOT cover — microsoft/playwright#31969), so untrusted LLM HTML physically
cannot reach the network during verification even if a static URL pattern is missed, and any
attempted outbound request (http OR ws) becomes a :data:`CHECK_NETWORK_BLOCKED` failure (the belt
to FORBIDDEN_PATTERNS' suspenders). Console +
pageerror listeners are registered and the AudioContext shim injected BEFORE ``page.goto``; the
shim wraps ``window.AudioContext``/``webkitAudioContext`` as a NON-CONFIGURABLE, NON-WRITABLE
global (a toy reassigning ``window.AudioContext`` cannot defeat it) and records construction time,
whether it happened after the verifier's first click, and state transitions into a JS-side log
(``window.__cwpAudio``) — the ONLY way to catch a top-level ``new AudioContext()`` (it just sits
silently suspended; no console error ever fires). One browser session is reused across all checks
within a :func:`verify_toy` call. Every in-page read (``page.evaluate`` and ``Locator.count`` take
NO ``timeout=`` and ignore ``set_default_timeout``) is routed through a driver-timed
:func:`_bounded_evaluate`, so a toy that busy-loops in a timer/rAF callback after load cannot hang
the verifier — it yields a :data:`CHECK_TIMEOUT` failure within the step budget. Clicks use
``force=True``: the contract MANDATES an idle animation on the main action, which defeats
Playwright's actionability stability-wait. The no-other-interactive-elements sweep runs AFTER the
mash on the happy path, so a dead-end screen that materializes only after interaction (a game-over
restart link) is still caught.

**must_haves compiler** (deterministic mapper — no LLM). Brief entries — validated against
:data:`cwp.brief.MUST_HAVE_VOCABULARY`, which is imported and never re-declared — compile into
keypoint assertions: ``visible:<x>`` → the text/emoji is present in the RENDERED page — body
``innerText`` OR ``::before``/``::after`` generated content (a contract-endorsed emoji technique),
so a CSS-pseudo sprite is not false-FAILed; ``element:<sel>`` → the selector matches (a malformed
selector degrades to a failed finding carrying the JS error, never an uncaught exception);
``sound_on_action`` → the shim log must show a context created and running by the first click (the
tightened, REQUIRED form of the generic gesture-gating check); ``state_change:<attr>`` → the named
attribute's values change across the first click.

API: ``verify_toy(html_path, brief=None, *, screenshot_path=None, step_timeout_ms=None)`` →
:class:`VerifyResult` (``ok`` + findings + the saved screenshot path). ``step_timeout_ms`` bounds
every in-page operation (default :data:`DEFAULT_STEP_TIMEOUT_MS`; tests pass a small value so a
hang regression fails fast). The screenshot captures the FINAL page state (post-mash) so Step 9
can save per-attempt evidence PNGs; on a static failure no browser runs and no screenshot is
taken. Domain errors: :class:`ToyNotFoundError` / :class:`VerifyError` are user-class (exit 1);
:class:`HeadlessEnvError` (playwright/chromium unavailable) is environment-class (exit 2) —
requires ``playwright install chromium`` once per machine.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cwp import episodes
from cwp.brief import MUST_HAVE_VOCABULARY, Brief, must_have_problem, split_must_have

if TYPE_CHECKING:
    from playwright.sync_api import ConsoleMessage, Error, Page


class VerifyError(episodes.EpisodeError):
    """Base for verifier-domain failures (CLI maps EpisodeError to exit 1, user error)."""


class ToyNotFoundError(VerifyError):
    """The toy HTML file does not exist."""


class HeadlessEnvError(Exception):
    """Playwright/Chromium unavailable or failed to launch (environment failure, exit 2).

    Mirrors ``drafting.DraftEnvError``'s role: the toy is not at fault — the machine is
    missing ``playwright install chromium`` (or the driver failed outside the page).
    """


@dataclass(frozen=True)
class Finding:
    """One structured check result — the repair loop's unit of evidence."""

    check: str
    passed: bool
    evidence: str  # offending line / selector / console text; measurement detail on a pass


@dataclass(frozen=True)
class VerifyResult:
    """What :func:`verify_toy` concluded: the verdict plus every finding behind it."""

    ok: bool
    findings: tuple[Finding, ...]
    screenshot_path: Path | None = None  # set iff the caller asked and the capture succeeded

    def failures(self) -> tuple[Finding, ...]:
        """The failed findings only — what Step 9 feeds back as repair evidence."""
        return tuple(finding for finding in self.findings if not finding.passed)


# --- thresholds & knobs (each with its rationale) ---------------------------------------------

# The contract's skeleton (doctype + viewport meta + centered-button CSS + a count-incrementing
# handler) cannot fit under ~1.5 KB: anything below the floor is a stub, a truncation, or an
# extraction accident — not a toy.
MIN_TOY_BYTES = 1500

# Intentional buffer under the contract's 25% generation target: measurement jitter (animation
# mid-frame scaling, rounding, scrollbar theft) must never flunk a toy that honestly aimed at 25%.
MAIN_ACTION_MIN_VIEWPORT_PCT = 20

# The contract's ONE mandated hook (build-contract.md MUST §2): the single primary action
# element carries data-testid="main-action" — every headless check locates the toy through it.
MAIN_ACTION_SELECTOR = '[data-testid="main-action"]'
# The contract's machine-checkable activation counter (build-contract.md MUST §2): the handler
# increments this attribute on every press — the pixel-diff-free signal that a click registered.
ACTION_COUNT_ATTR = "data-action-count"

# Anything a toddler could poke besides the one main action. Enumerated in the page and reported
# with a per-element descriptor as evidence.
INTERACTIVE_SELECTOR = (
    "button, a, input, select, textarea, [onclick], [role='button'], "
    "details, summary, [contenteditable], audio[controls], video[controls], embed, iframe"
)

# A fixed desktop viewport so the main-action ≥20%-coverage math (below) is deterministic across
# machines — the percentage is area/(WIDTH*HEIGHT), so both must be pinned, not environment-drawn.
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 720

SETTLE_MS = 1000  # post-load window in which load-time console errors must surface
POST_CLICK_SETTLE_MS = 300  # lets the click handler + audio state transition land
AUDIO_RUNNING_TIMEOUT_MS = 2000  # AudioContext resume() settles asynchronously
# Bounds goto/clicks/get_attribute AND (via _bounded_evaluate) every in-page read, so a toy that
# hangs the JS event loop cannot hang verify_toy — it becomes a CHECK_TIMEOUT within this budget.
DEFAULT_STEP_TIMEOUT_MS = 10_000
MASH_CLICKS = 15  # the "can't break it" rapid-mash budget

# Schemes a self-contained toy may legitimately resolve during verification; everything else is
# an outbound network request the route handler aborts (B2 defense-in-depth).
_ALLOWED_REQUEST_SCHEMES = ("file:", "data:", "blob:", "about:")

# Playwright's default Chromium args RELAX autoplay, which would mask gesture-gating bugs —
# strip the relaxing flag and pin the strict policy a real kid-facing browser applies.
AUTOPLAY_POLICY_FLAG = "--autoplay-policy=user-gesture-required"
_PLAYWRIGHT_RELAXED_AUTOPLAY_FLAG = "--autoplay-policy=no-user-gesture-required"

# --- check ids (stable strings tests + Step 9's evidence templates key on) ---------------------

CHECK_DOCTYPE = "static:doctype"
CHECK_SIZE_FLOOR = "static:size-floor"
CHECK_PAGE_LOAD = "headless:page-load"
CHECK_CONSOLE_CLEAN_LOAD = "headless:console-clean-load"
CHECK_MAIN_ACTION_PRESENT = "headless:main-action-present"
CHECK_MAIN_ACTION_SIZE = "headless:main-action-size"
CHECK_NO_AUDIO_BEFORE_CLICK = "headless:no-audio-before-click"
CHECK_CLICK_CLEAN = "headless:click-clean"
CHECK_ACTION_COUNT = "headless:action-count-increments"
CHECK_AUDIO_AFTER_CLICK = "headless:audio-running-after-click"
CHECK_MASH_CLEAN = "headless:mash-clean"
CHECK_MASH_MONOTONIC = "headless:mash-count-monotonic"
CHECK_ONLY_MAIN_INTERACTIVE = "headless:only-main-action-interactive"
CHECK_NETWORK_BLOCKED = "headless:no-network-request"
CHECK_TIMEOUT = "headless:page-responsive"
CHECK_ABORTED = "headless:aborted"


def forbidden_check_id(pattern_name: str) -> str:
    """The Finding.check id for one :data:`FORBIDDEN_PATTERNS` entry."""
    return f"static:forbidden:{pattern_name}"


def must_have_check_id(entry: str) -> str:
    """The Finding.check id for one compiled must_have entry (the entry itself is the key)."""
    return f"must-have:{entry}"


# --- FORBIDDEN_PATTERNS: the ONE source of truth build-contract.md cites -----------------------


@dataclass(frozen=True)
class ForbiddenPattern:
    """One machine-enforced NEVER rule: a name, a whole-text regex, and an occurrence budget.

    ``max_count=0`` means any (non-allowlisted) hit fails; ``max_count=1`` implements the
    ``</script>`` COUNT rule (the toy's own closing tag is free; a second occurrence is a
    string-embedded breaker). ``svg_namespace_exempt`` applies the inline-SVG allowlist below.
    Regexes run over the WHOLE document (static_checks scans the full text, not per line) so a
    URL split across lines or an unquoted/backtick URL cannot slip through a per-line anchor.
    """

    name: str
    regex: re.Pattern[str]
    why: str
    max_count: int = 0
    svg_namespace_exempt: bool = False


# The ONLY legitimate absolute-URL-in-an-attribute a self-contained toy carries: inline-SVG
# namespace DECLARATIONS (xmlns / xmlns:xlink → w3.org). N1: xlink:href="http…" is a fetched
# resource on <use>/<image>, NOT a namespace decl — deliberately NOT matched here, so it fails.
_SVG_NAMESPACE_ALLOW_RE = re.compile(
    r"""xmlns(?::[\w-]+)?\s*=\s*["']\s*https?://www\.w3\.org/""",
    re.IGNORECASE,
)

FORBIDDEN_PATTERNS: tuple[ForbiddenPattern, ...] = (
    ForbiddenPattern(
        name="external-url",
        # ANY absolute http(s) URL literal, anywhere in the document. A self-contained toy loads
        # zero external resources, so every such literal is forbidden regardless of context —
        # this single whole-text pattern subsumes quoted/unquoted/backtick/split src=, href=,
        # <link href>, img.src=, setAttribute('src','http…'), CSS url("http…"), and @import
        # url(http…) (each still contains the literal scheme). Only the SVG-namespace allowlist
        # is exempt. Bare URLs in comments are rare in generated toys and (if present) cost only
        # a repair retry — a false-positive here is far cheaper than a missed live fetch.
        regex=re.compile(r"https?://", re.IGNORECASE),
        why="an absolute http(s) URL — a self-contained toy loads no external resources",
        svg_namespace_exempt=True,
    ),
    ForbiddenPattern(
        name="external-url-websocket",
        regex=re.compile(r"wss?://", re.IGNORECASE),
        why="a ws(s):// URL opens an external WebSocket",
    ),
    ForbiddenPattern(
        name="external-url-relative",
        regex=re.compile(r"""\b(?:src|href)\s*=\s*["']?\s*//\w""", re.IGNORECASE),
        why="a protocol-relative src=/href= loads an external resource",
    ),
    ForbiddenPattern(
        name="network-fetch",
        regex=re.compile(r"\bfetch\s*\("),
        why="fetch( makes a network call",
    ),
    ForbiddenPattern(
        # `new\s+X` (not a bare `\bX\b`): these globals are constructors — a real use is always
        # `new XMLHttpRequest`/`new WebSocket`/`new EventSource`, so requiring `new` catches
        # construction without false-FAILing a mere mention in a comment/text ("no WebSocket
        # needed here"). The wss:// URL pattern above complements this for the literal-URL form.
        name="network-xhr",
        regex=re.compile(r"\bnew\s+XMLHttpRequest\b"),
        why="new XMLHttpRequest makes a network call",
    ),
    ForbiddenPattern(
        name="network-websocket",
        regex=re.compile(r"\bnew\s+WebSocket\b"),
        why="new WebSocket opens a network connection",
    ),
    ForbiddenPattern(
        name="network-eventsource",
        regex=re.compile(r"\bnew\s+EventSource\b"),
        why="new EventSource opens a server-sent-events network stream",
    ),
    ForbiddenPattern(
        name="network-sendbeacon",
        regex=re.compile(r"\bsendBeacon\s*\("),
        why="navigator.sendBeacon( makes a network call",
    ),
    ForbiddenPattern(
        name="network-import",
        regex=re.compile(r"\bimport\s*\("),
        why="dynamic import( loads external code",
    ),
    ForbiddenPattern(
        name="external-stylesheet",
        regex=re.compile(r"""<link[^>]*\brel\s*=\s*["']?stylesheet""", re.IGNORECASE),
        why='<link rel="stylesheet"> loads an external stylesheet (all CSS must be inline)',
    ),
    ForbiddenPattern(
        name="css-import",
        regex=re.compile(r"@import\b", re.IGNORECASE),
        why="@import pulls in an external stylesheet (all CSS must be inline)",
    ),
    ForbiddenPattern(
        name="img-non-data-src",
        # <img> whose src is present and does NOT start with data: — a non-inline image the
        # contract forbids (No <img> at all unless its src is a data: URI). Absolute-URL srcs
        # also trip external-url; this additionally catches relative srcs (pic.png) that have no
        # scheme literal to match.
        regex=re.compile(r"""<img\b[^>]*\bsrc\s*=\s*["']?(?!data:)[^"'\s>]""", re.IGNORECASE),
        why="an <img> loads a non-data: src (inline images must use a data: URI)",
    ),
    ForbiddenPattern(
        name="dialog-alert",
        regex=re.compile(r"\balert\s*\("),
        why="alert( freezes the page on first interaction (and hangs the headless verifier)",
    ),
    ForbiddenPattern(
        name="dialog-confirm",
        regex=re.compile(r"\bconfirm\s*\("),
        why="confirm( freezes the page on first interaction (and hangs the headless verifier)",
    ),
    ForbiddenPattern(
        name="dialog-prompt",
        regex=re.compile(r"\bprompt\s*\("),
        why="prompt( freezes the page on first interaction (and hangs the headless verifier)",
    ),
    ForbiddenPattern(
        name="script-close-count",
        regex=re.compile(r"</script", re.IGNORECASE),
        why=(
            "more than one </script> occurrence — a string-embedded literal prematurely"
            " closes the toy's script tag"
        ),
        max_count=1,
    ),
)

# The doctype the contract's skeleton opens with (build-contract.md MUST §1): case-insensitive
# because a generated toy may emit <!doctype html>; the whole-text search tolerates a BOM/preamble.
_DOCTYPE_RE = re.compile(r"<!doctype\s+html", re.IGNORECASE)


def _svg_namespace_allow_spans(html_text: str) -> list[tuple[int, int]]:
    """Whole-text spans of allowlisted SVG-namespace declarations (for hit-overlap checks)."""
    return [(match.start(), match.end()) for match in _SVG_NAMESPACE_ALLOW_RE.finditer(html_text)]


def _line_at(html_text: str, line_starts: list[int], offset: int) -> tuple[int, str]:
    """Map a whole-text match *offset* to ``(1-based line number, stripped line text)``."""
    lineno = bisect.bisect_right(line_starts, offset)  # 1-based: line_starts[0] == 0
    start = line_starts[lineno - 1]
    end = html_text.find("\n", start)
    line = html_text[start:] if end == -1 else html_text[start:end]
    return lineno, line.strip()


def static_checks(html_text: str) -> tuple[Finding, ...]:
    """The full static gate (no browser): doctype, size floor, every FORBIDDEN_PATTERNS rule.

    Scans the WHOLE text (never per line), so a forbidden URL split across lines or written in an
    unquoted/backtick form still fails. Public seam: Step 9 can static-check extracted fence text
    before ever touching a temp file. Returns EVERY finding (passes included) so the evidence
    trail is complete either way.
    """
    findings: list[Finding] = []
    # Line-start offsets for O(log n) offset->line mapping (line_starts[0] == 0).
    line_starts = [0] + [match.end() for match in re.finditer(r"\n", html_text)]
    first_line = next((line.strip() for line in html_text.splitlines() if line.strip()), "")
    has_doctype = _DOCTYPE_RE.search(html_text) is not None
    findings.append(
        Finding(
            check=CHECK_DOCTYPE,
            passed=has_doctype,
            evidence=(
                "found <!DOCTYPE html>"
                if has_doctype
                else f"no <!DOCTYPE html> declaration; the file starts with: {first_line[:80]!r}"
            ),
        )
    )
    size = len(html_text.encode("utf-8"))
    findings.append(
        Finding(
            check=CHECK_SIZE_FLOOR,
            passed=size >= MIN_TOY_BYTES,
            evidence=f"{size} bytes (floor: {MIN_TOY_BYTES} — smaller is a stub, not a toy)",
        )
    )
    allow_spans = _svg_namespace_allow_spans(html_text)
    for pattern in FORBIDDEN_PATTERNS:
        hits: list[tuple[int, str]] = []
        for match in pattern.regex.finditer(html_text):
            if pattern.svg_namespace_exempt and any(
                start <= match.start() < end for start, end in allow_spans
            ):
                continue
            hits.append(_line_at(html_text, line_starts, match.start()))
        check = forbidden_check_id(pattern.name)
        if len(hits) > pattern.max_count:
            if pattern.max_count == 0:
                lineno, offending = hits[0]
                evidence = f"{pattern.why} — line {lineno}: {offending}"
            else:
                # A COUNT violation has no single offending occurrence (document order puts
                # the toy's own closing tag among the hits) — list every line as evidence.
                listed = "; ".join(f"line {lineno}: {line}" for lineno, line in hits[:4])
                evidence = f"{pattern.why} — {len(hits)} occurrences: {listed}"
            findings.append(Finding(check=check, passed=False, evidence=evidence))
        elif hits:
            findings.append(
                Finding(
                    check=check,
                    passed=True,
                    evidence=f"{len(hits)} occurrence(s), within the allowed budget"
                    f" of {pattern.max_count}",
                )
            )
        else:
            findings.append(Finding(check=check, passed=True, evidence="no occurrences"))
    return tuple(findings)


# --- must_haves compiler (deterministic; vocabulary imported from brief.py) --------------------

# Every MUST_HAVE_VOCABULARY prefix must have a compiled assertion below — tests assert this set
# equals the vocabulary so the two modules cannot drift (one-source-of-truth discipline).
COMPILED_MUST_HAVE_PREFIXES: frozenset[str] = frozenset(
    {"visible", "element", "sound_on_action", "state_change"}
)

# Import-time drift surface, derived from brief.py's vocabulary constant (imported, never
# re-declared): predicates the vocabulary knows but this compiler cannot assert yet. Kept as
# data rather than an assert (importing must never crash) — tests pin it EMPTY, and
# _plan_must_haves turns any runtime straggler into a failed finding instead of a pass-by-gap.
UNCOMPILED_VOCABULARY_PREFIXES: frozenset[str] = (
    frozenset(predicate.prefix for predicate in MUST_HAVE_VOCABULARY) - COMPILED_MUST_HAVE_PREFIXES
)


@dataclass(frozen=True)
class _MustHavePlan:
    """brief.must_haves bucketed by predicate, ready for the check sequence to consume."""

    visible: tuple[tuple[str, str], ...] = ()  # (entry, payload)
    elements: tuple[tuple[str, str], ...] = ()
    state_changes: tuple[tuple[str, str], ...] = ()
    sound: tuple[str, ...] = ()  # entries (payload-less)
    invalid: tuple[tuple[str, str], ...] = ()  # (entry, problem)

    def click_dependent_entries(self) -> tuple[str, ...]:
        """Entries whose assertion needs the first click (skip-failed when no click happens)."""
        return self.sound + tuple(entry for entry, _attr in self.state_changes)


def _plan_must_haves(brief: Brief | None) -> _MustHavePlan:
    """Bucket the brief's vocabulary-form entries; anything non-compilable lands in invalid."""
    if brief is None:
        return _MustHavePlan()
    visible: list[tuple[str, str]] = []
    elements: list[tuple[str, str]] = []
    state_changes: list[tuple[str, str]] = []
    sound: list[str] = []
    invalid: list[tuple[str, str]] = []
    for entry in brief.must_haves:
        problem = must_have_problem(entry)
        if problem is not None:
            invalid.append((entry, f"not a compilable must_have: {problem}"))
            continue
        prefix, payload = split_must_have(entry)
        if prefix == "visible":
            visible.append((entry, payload))
        elif prefix == "element":
            elements.append((entry, payload))
        elif prefix == "state_change":
            state_changes.append((entry, payload))
        elif prefix == "sound_on_action":
            sound.append(entry)
        else:  # a future vocabulary predicate this compiler doesn't know — surface, never crash
            invalid.append(
                (
                    entry,
                    f"vocabulary predicate {prefix!r} has no compiled assertion in verify.py"
                    f" (compiled: {sorted(COMPILED_MUST_HAVE_PREFIXES)})",
                )
            )
    return _MustHavePlan(
        visible=tuple(visible),
        elements=tuple(elements),
        state_changes=tuple(state_changes),
        sound=tuple(sound),
        invalid=tuple(invalid),
    )


# --- public entry point -------------------------------------------------------------------------


def verify_toy(
    html_path: Path,
    brief: Brief | None = None,
    *,
    screenshot_path: Path | None = None,
    step_timeout_ms: int | None = None,
) -> VerifyResult:
    """Run the full gate on one toy: static checks, then (only if clean) the headless checks.

    A static failure short-circuits the browser phase — a toy carrying ``alert(`` would hang a
    headless click, and there is nothing a browser can add to an exact-offending-line grep hit.
    ``brief`` adds the compiled must_haves assertions; ``screenshot_path`` captures the FINAL
    page state PNG (parent dirs created; skipped on the static-fail path — no page exists).
    ``step_timeout_ms`` (default :data:`DEFAULT_STEP_TIMEOUT_MS`) bounds every in-page operation
    so a hanging toy yields a :data:`CHECK_TIMEOUT` failure instead of blocking the verifier.

    Raises :class:`ToyNotFoundError` / :class:`VerifyError` for unreadable input (user-class)
    and :class:`HeadlessEnvError` when Playwright/Chromium is unavailable (environment-class).
    """
    try:
        raw = html_path.read_bytes()
    except FileNotFoundError as exc:
        raise ToyNotFoundError(f"{html_path} not found — nothing to verify") from exc
    except OSError as exc:
        raise VerifyError(f"{html_path}: unreadable: {exc}") from exc
    # errors="replace": a mojibake toy must still produce static evidence, never a crash.
    findings = list(static_checks(raw.decode("utf-8", errors="replace")))
    if any(not finding.passed for finding in findings):
        return VerifyResult(ok=False, findings=tuple(findings), screenshot_path=None)
    step_timeout = step_timeout_ms if step_timeout_ms is not None else DEFAULT_STEP_TIMEOUT_MS
    headless_findings, saved = _headless_checks(html_path, brief, screenshot_path, step_timeout)
    findings.extend(headless_findings)
    return VerifyResult(
        ok=all(finding.passed for finding in findings),
        findings=tuple(findings),
        screenshot_path=saved,
    )


# --- headless internals (playwright imported lazily below this line only) ----------------------


class _PageTimeout(Exception):
    """A bounded in-page read exceeded the step budget — the toy hung the JS event loop."""


# Injected BEFORE page.goto on every navigation: wraps AudioContext/webkitAudioContext and
# records construction time, whether the verifier's first click had already started, and every
# statechange into window.__cwpAudio. A top-level `new AudioContext()` produces NO console error
# (it just sits suspended) — this log is the only way to catch it. N2: the wrapper is installed as
# a NON-CONFIGURABLE, NON-WRITABLE global, so a toy reassigning window.AudioContext cannot swap
# our wrapper back out and silently defeat the audio checks (the reassignment no-ops / throws).
_AUDIO_SHIM_JS = """\
(() => {
  const log = { constructed: [], contexts: [], transitions: [] };
  Object.defineProperty(window, "__cwpAudio", { value: log, configurable: false });
  const wrap = (name) => {
    const Original = window[name];
    if (typeof Original !== "function") {
      return;
    }
    const Wrapped = function (...args) {
      const context = new Original(...args);
      log.constructed.push({
        at: performance.now(),
        afterClick: window.__cwpClickStarted === true,
      });
      log.contexts.push(context);
      try {
        context.addEventListener("statechange", () => {
          log.transitions.push({ at: performance.now(), state: context.state });
        });
      } catch (error) {
        /* transition logging is best-effort */
      }
      return context;
    };
    Wrapped.prototype = Original.prototype;
    try {
      Object.defineProperty(window, name, {
        value: Wrapped,
        writable: false,
        configurable: false,
      });
    } catch (error) {
      window[name] = Wrapped; /* fall back if the global was already non-configurable */
    }
  };
  wrap("AudioContext");
  wrap("webkitAudioContext");
})();
"""

_MARK_CLICK_STARTED_JS = "() => { window.__cwpClickStarted = true; return true; }"
_AUDIO_CONSTRUCTED_JS = "() => window.__cwpAudio.constructed"
_AUDIO_CONTEXT_COUNT_JS = "() => window.__cwpAudio.contexts.length"
_AUDIO_STATES_JS = "() => window.__cwpAudio.contexts.map((context) => context.state)"
_AUDIO_ALL_RUNNING_JS = (
    "() => window.__cwpAudio.contexts.length > 0"
    " && window.__cwpAudio.contexts.every((context) => context.state === 'running')"
)
# N3: presence in the RENDERED page — body innerText OR ::before/::after generated content, a
# contract-endorsed way to place an emoji sprite. Raw textContent is deliberately NOT used (it
# would match the toy's own <script> source). Bounded by _bounded_evaluate like every read.
_VISIBLE_TEXT_JS = """\
(needle) => {
  const body = document.body;
  if (!body) {
    return false;
  }
  if ((body.innerText || "").includes(needle)) {
    return true;
  }
  for (const el of document.querySelectorAll("*")) {
    for (const pseudo of ["::before", "::after"]) {
      const content = window.getComputedStyle(el, pseudo).content;
      if (content && content !== "none" && content.includes(needle)) {
        return true;
      }
    }
  }
  return false;
}
"""
_SELECTOR_EXISTS_JS = """\
(selector) => {
  try {
    return { found: document.querySelector(selector) !== null, error: "" };
  } catch (error) {
    return { found: false, error: String(error) };
  }
}
"""
_ATTR_VALUES_JS = """\
(attr) => {
  try {
    const selector = "[" + CSS.escape(attr) + "]";
    return Array.from(document.querySelectorAll(selector)).map((el) => el.getAttribute(attr));
  } catch (error) {
    return null;
  }
}
"""
_INTERACTIVE_ENUM_JS = """\
(selector) => {
  const main = document.querySelector('[data-testid="main-action"]');
  const offenders = [];
  for (const el of document.querySelectorAll(selector)) {
    if (main && (el === main || main.contains(el))) {
      continue;
    }
    const parts = [el.tagName.toLowerCase()];
    if (el.id) {
      parts.push("#" + el.id);
    }
    const testid = el.getAttribute("data-testid");
    if (testid) {
      parts.push('[data-testid="' + testid + '"]');
    }
    offenders.push(parts.join(""));
  }
  return offenders;
}
"""

_QUERY_COUNT_JS = "(selector) => document.querySelectorAll(selector).length"

_CLICK_DEPENDENT_CHECKS = (
    CHECK_ACTION_COUNT,
    CHECK_AUDIO_AFTER_CLICK,
    CHECK_MASH_CLEAN,
    CHECK_MASH_MONOTONIC,
)


def _bounded_evaluate(page: Page, fn: str, arg: object = None, *, timeout_ms: int) -> Any:
    """Driver-timed ``page.evaluate`` — the ONLY safe way to read from untrusted-toy JS.

    ``page.evaluate`` / ``Locator.count`` take no ``timeout=`` and ignore
    ``set_default_timeout`` (confirmed against the playwright signatures), so a toy that
    busy-loops the JS event loop would block them forever. ``page.wait_for_function`` IS
    driver-timed: even against a wedged page its timeout fires. We wrap the reader ``fn`` so it
    returns a truthy envelope ``{v: fn(arg)}`` (always truthy → resolves on the first poll) and
    read the value back off the handle. On expiry we raise :class:`_PageTimeout` (→ CHECK_TIMEOUT);
    any other page error propagates (→ CHECK_ABORTED).
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    wrapped = "(a) => { const f = (" + fn + "); return { v: f(a) }; }"
    try:
        handle = page.wait_for_function(wrapped, arg=arg, timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise _PageTimeout(str(exc)) from exc
    try:
        envelope: dict[str, Any] = handle.json_value()
    finally:
        handle.dispose()
    return envelope["v"]


def _skipped(check: str, reason: str) -> Finding:
    """A dependent check that could not run is a FAILURE with the reason as evidence."""
    return Finding(check=check, passed=False, evidence=f"not run — {reason}")


def _parse_count(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _console_finding(check: str, errors: list[str], baseline: int, context: str) -> Finding:
    """Pass/fail on NEW console/page errors accumulated since *baseline*."""
    new = errors[baseline:]
    if not new:
        return Finding(check=check, passed=True, evidence=f"no console or page errors {context}")
    extra = f" (+{len(new) - 1} more)" if len(new) > 1 else ""
    return Finding(
        check=check,
        passed=False,
        evidence=f"{len(new)} console/page error(s) {context}: {new[0]}{extra}",
    )


def _register_error_listeners(page: Page, errors: list[str]) -> None:
    """Console + pageerror collectors — wired BEFORE page.goto so load-time errors count."""

    def on_console(message: ConsoleMessage) -> None:
        if message.type == "error":
            errors.append(f"console error: {message.text}")

    def on_page_error(error: Error) -> None:
        errors.append(f"pageerror: {error}")

    page.on("console", on_console)
    page.on("pageerror", on_page_error)


def _interactive_finding(page: Page, timeout_ms: int) -> Finding:
    """Enumerate interactive elements beyond the main action (or its descendants)."""
    offenders: list[str] = _bounded_evaluate(
        page, _INTERACTIVE_ENUM_JS, INTERACTIVE_SELECTOR, timeout_ms=timeout_ms
    )
    if offenders:
        return Finding(
            check=CHECK_ONLY_MAIN_INTERACTIVE,
            passed=False,
            evidence="interactive elements besides the main action: " + ", ".join(offenders),
        )
    return Finding(
        check=CHECK_ONLY_MAIN_INTERACTIVE,
        passed=True,
        evidence="no interactive elements beyond the main action",
    )


def _append_click_dependent_skips(
    findings: list[Finding], plan: _MustHavePlan, reason: str
) -> None:
    for check in _CLICK_DEPENDENT_CHECKS:
        findings.append(_skipped(check, reason))
    for entry in plan.click_dependent_entries():
        findings.append(_skipped(must_have_check_id(entry), reason))


def _run_page_checks(
    page: Page, brief: Brief | None, findings: list[Finding], errors: list[str], timeout_ms: int
) -> None:
    """The in-page check sequence (§3.2 item 4), in execution order.

    Every in-page read goes through :func:`_bounded_evaluate` (``timeout_ms``), so a toy that
    hangs the event loop raises :class:`_PageTimeout` here and is reported as CHECK_TIMEOUT by
    the caller — the sequence never blocks indefinitely.
    """
    from playwright.sync_api import Error as PlaywrightError

    page.wait_for_timeout(SETTLE_MS)
    findings.append(_console_finding(CHECK_CONSOLE_CLEAN_LOAD, errors, 0, "during load + settle"))

    plan = _plan_must_haves(brief)
    for entry, problem in plan.invalid:
        findings.append(Finding(check=must_have_check_id(entry), passed=False, evidence=problem))

    # DOM-only must-haves: "visible in the rendered page after load" / selector existence.
    for entry, payload in plan.visible:
        seen = bool(_bounded_evaluate(page, _VISIBLE_TEXT_JS, payload, timeout_ms=timeout_ms))
        findings.append(
            Finding(
                check=must_have_check_id(entry),
                passed=seen,
                evidence=(
                    f"{payload!r} is visible in the rendered page (text or ::before/::after)"
                    if seen
                    else f"{payload!r} not found in rendered text or ::before/::after content"
                ),
            )
        )
    for entry, payload in plan.elements:
        outcome: dict[str, Any] = _bounded_evaluate(
            page, _SELECTOR_EXISTS_JS, payload, timeout_ms=timeout_ms
        )
        found = bool(outcome.get("found"))
        selector_error = str(outcome.get("error") or "")
        if selector_error:
            evidence = f"selector {payload!r} is invalid: {selector_error}"
        elif found:
            evidence = f"selector {payload!r} matches an element"
        else:
            evidence = f"no element matches selector {payload!r}"
        findings.append(
            Finding(
                check=must_have_check_id(entry),
                passed=found and not selector_error,
                evidence=evidence,
            )
        )

    # No AudioContext may exist before the first click — the shim log is the only witness.
    constructed: list[dict[str, Any]] = _bounded_evaluate(
        page, _AUDIO_CONSTRUCTED_JS, timeout_ms=timeout_ms
    )
    if constructed:
        findings.append(
            Finding(
                check=CHECK_NO_AUDIO_BEFORE_CLICK,
                passed=False,
                evidence=(
                    f"AudioContext constructed {len(constructed)}x before the first click"
                    f" (first at {constructed[0]['at']:.0f} ms after load) — Web Audio must be"
                    " created inside the click handler"
                ),
            )
        )
    else:
        findings.append(
            Finding(
                check=CHECK_NO_AUDIO_BEFORE_CLICK,
                passed=True,
                evidence="no AudioContext constructed before the first click",
            )
        )

    locator = page.locator(MAIN_ACTION_SELECTOR)
    # Locator.count() is NOT timeout-governed — read the match count via a bounded evaluate.
    matches: int = _bounded_evaluate(
        page, _QUERY_COUNT_JS, MAIN_ACTION_SELECTOR, timeout_ms=timeout_ms
    )
    findings.append(
        Finding(
            check=CHECK_MAIN_ACTION_PRESENT,
            passed=matches == 1,
            evidence=(
                f"exactly one {MAIN_ACTION_SELECTOR} element"
                if matches == 1
                else f"{matches} elements match {MAIN_ACTION_SELECTOR} (need exactly one)"
            ),
        )
    )
    if matches != 1:
        reason = f"{matches} main-action elements (need exactly one to click)"
        findings.append(_skipped(CHECK_MAIN_ACTION_SIZE, reason))
        findings.append(_skipped(CHECK_CLICK_CLEAN, reason))
        _append_click_dependent_skips(findings, plan, reason)
        findings.append(_interactive_finding(page, timeout_ms))
        return

    box = locator.bounding_box()
    if box is None:
        findings.append(
            Finding(
                check=CHECK_MAIN_ACTION_SIZE,
                passed=False,
                evidence="the main action has no bounding box (hidden or not rendered)",
            )
        )
    else:
        pct = 100.0 * (box["width"] * box["height"]) / (VIEWPORT_WIDTH * VIEWPORT_HEIGHT)
        findings.append(
            Finding(
                check=CHECK_MAIN_ACTION_SIZE,
                passed=pct >= MAIN_ACTION_MIN_VIEWPORT_PCT,
                evidence=(
                    f"main action covers {pct:.1f}% of the {VIEWPORT_WIDTH}x{VIEWPORT_HEIGHT}"
                    f" viewport (floor: {MAIN_ACTION_MIN_VIEWPORT_PCT}%)"
                ),
            )
        )

    state_before: dict[str, list[str] | None] = {}
    for _entry, attr in plan.state_changes:
        state_before[attr] = _bounded_evaluate(page, _ATTR_VALUES_JS, attr, timeout_ms=timeout_ms)

    # First click. force=True: the contract-mandated idle animation defeats Playwright's
    # stability-wait; a forced click is still a real, trusted input event (user activation).
    errors_before_click = len(errors)
    count_before = _parse_count(locator.get_attribute(ACTION_COUNT_ATTR))
    _bounded_evaluate(page, _MARK_CLICK_STARTED_JS, timeout_ms=timeout_ms)
    try:
        locator.click(force=True)
    except PlaywrightError as exc:
        findings.append(
            Finding(
                check=CHECK_CLICK_CLEAN,
                passed=False,
                evidence=f"clicking the main action failed: {exc}",
            )
        )
        _append_click_dependent_skips(findings, plan, "the first click failed")
        findings.append(_interactive_finding(page, timeout_ms))
        return
    page.wait_for_timeout(POST_CLICK_SETTLE_MS)
    findings.append(
        _console_finding(CHECK_CLICK_CLEAN, errors, errors_before_click, "on the first click")
    )

    count_after = _parse_count(locator.get_attribute(ACTION_COUNT_ATTR))
    if count_before is None:
        count_ok = False
        count_evidence = f"{ACTION_COUNT_ATTR} missing or non-numeric before the click"
    elif count_after is None:
        count_ok = False
        count_evidence = f"{ACTION_COUNT_ATTR} missing or non-numeric after the click"
    elif count_after > count_before:
        count_ok = True
        count_evidence = f"{ACTION_COUNT_ATTR}: {count_before} -> {count_after} on the first click"
    else:
        count_ok = False
        count_evidence = (
            f"{ACTION_COUNT_ATTR} did not increment on the click ({count_before} -> {count_after})"
        )
    findings.append(Finding(check=CHECK_ACTION_COUNT, passed=count_ok, evidence=count_evidence))

    # Gesture-gated audio: any constructed context must reach state === "running" post-click.
    total_contexts: int = _bounded_evaluate(page, _AUDIO_CONTEXT_COUNT_JS, timeout_ms=timeout_ms)
    if total_contexts == 0:
        audio_running = True
        audio_evidence = "no AudioContext constructed — sound is optional for the generic check"
    else:
        try:
            page.wait_for_function(_AUDIO_ALL_RUNNING_JS, timeout=AUDIO_RUNNING_TIMEOUT_MS)
            audio_running = True
            audio_evidence = (
                f"{total_contexts} AudioContext(s) reached state 'running' after the first click"
            )
        except PlaywrightError:
            states: list[str] = _bounded_evaluate(page, _AUDIO_STATES_JS, timeout_ms=timeout_ms)
            audio_running = False
            audio_evidence = f"AudioContext state(s) after the click: {states} (expected 'running')"
    findings.append(
        Finding(check=CHECK_AUDIO_AFTER_CLICK, passed=audio_running, evidence=audio_evidence)
    )
    # sound_on_action tightens the generic check: a context is REQUIRED, not optional.
    for entry in plan.sound:
        if total_contexts == 0:
            findings.append(
                Finding(
                    check=must_have_check_id(entry),
                    passed=False,
                    evidence="no AudioContext was constructed by the first main-action click",
                )
            )
        else:
            findings.append(
                Finding(
                    check=must_have_check_id(entry),
                    passed=audio_running,
                    evidence=audio_evidence,
                )
            )

    for entry, attr in plan.state_changes:
        before = state_before[attr]
        after: list[str] | None = _bounded_evaluate(
            page, _ATTR_VALUES_JS, attr, timeout_ms=timeout_ms
        )
        check = must_have_check_id(entry)
        if before is None or after is None:
            findings.append(
                Finding(
                    check=check,
                    passed=False,
                    evidence=f"{attr!r} is not usable in a DOM attribute query",
                )
            )
        elif not before and not after:
            findings.append(
                Finding(
                    check=check,
                    passed=False,
                    evidence=f"no element carries the {attr!r} attribute",
                )
            )
        elif before != after:
            findings.append(
                Finding(
                    check=check,
                    passed=True,
                    evidence=f"{attr!r} changed on the main-action click: {before} -> {after}",
                )
            )
        else:
            findings.append(
                Finding(
                    check=check,
                    passed=False,
                    evidence=f"{attr!r} did not change on the main-action click"
                    f" (stuck at {before})",
                )
            )

    # The "can't break it" mash: rapid clicks, zero new errors, strictly monotonic count.
    errors_before_mash = len(errors)
    mash_counts: list[int | None] = []
    mash_error: str | None = None
    for _ in range(MASH_CLICKS):
        try:
            locator.click(force=True)
        except PlaywrightError as exc:
            mash_error = str(exc)
            break
        mash_counts.append(_parse_count(locator.get_attribute(ACTION_COUNT_ATTR)))
    if mash_error is not None:
        findings.append(
            Finding(
                check=CHECK_MASH_CLEAN,
                passed=False,
                evidence=f"mash click #{len(mash_counts) + 1} failed: {mash_error}",
            )
        )
    else:
        findings.append(
            _console_finding(
                CHECK_MASH_CLEAN, errors, errors_before_mash, f"during the {MASH_CLICKS}-click mash"
            )
        )
    numeric = [value for value in mash_counts if value is not None]
    strictly_increasing = all(
        later > earlier for earlier, later in zip(numeric, numeric[1:], strict=False)
    )
    # N4: continuity from the first click can only be certified when count_after is a real number.
    # If it is None (attribute missing/non-numeric after the click), we CANNOT vouch the mash
    # continued strictly from it — so monotonic fails and the evidence says why, rather than
    # letting `anchored` read trivially True. (CHECK_ACTION_COUNT has already failed here too.)
    continuity_known = count_after is not None
    anchored = count_after is not None and bool(numeric) and numeric[0] > count_after
    monotonic = (
        mash_error is None and len(numeric) == MASH_CLICKS and strictly_increasing and anchored
    )
    sequence_evidence = (
        f"{ACTION_COUNT_ATTR} over the mash (after first click: {count_after}): {mash_counts}"
    )
    if monotonic:
        detail = ""
    elif not continuity_known:
        detail = " — first-click count was unavailable, so mash continuity could not be established"
    else:
        detail = " — expected strictly increasing numbers continuing from the first click"
    findings.append(
        Finding(
            check=CHECK_MASH_MONOTONIC,
            passed=monotonic,
            evidence=sequence_evidence + detail,
        )
    )

    # Run the interactive sweep LAST: a dead-end screen (game-over + restart link) only
    # materializes after interaction, and the mash is our best approximation of a kid.
    findings.append(_interactive_finding(page, timeout_ms))


def _save_screenshot(page: Page, screenshot_path: Path) -> Path | None:
    """Best-effort final-state PNG — evidence capture must never change a verdict."""
    from playwright.sync_api import Error as PlaywrightError

    try:
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot_path))
    except (PlaywrightError, OSError):
        return None
    return screenshot_path


def _network_block_finding(blocked: list[str]) -> Finding:
    """B2 belt: any outbound request the route handler aborted is a hard failure with evidence."""
    if not blocked:
        return Finding(
            check=CHECK_NETWORK_BLOCKED,
            passed=True,
            evidence="the toy made no outbound network request during verification",
        )
    listed = ", ".join(blocked[:5])
    extra = f" (+{len(blocked) - 5} more)" if len(blocked) > 5 else ""
    return Finding(
        check=CHECK_NETWORK_BLOCKED,
        passed=False,
        evidence=(
            f"{len(blocked)} outbound network request(s) blocked mid-verification"
            f" (a static URL pattern was evaded): {listed}{extra}"
        ),
    )


def _headless_checks(
    html_path: Path, brief: Brief | None, screenshot_path: Path | None, step_timeout: int
) -> tuple[list[Finding], Path | None]:
    """One reused Chromium session for the whole in-browser phase (playwright imported here).

    The browsing context routes EVERY request through an abort handler (B2): only local schemes
    continue, so untrusted toy HTML cannot reach the network even if a static pattern is missed.
    ``step_timeout`` governs goto/clicks (``set_default_timeout``) AND every bounded in-page read.
    """
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import Route, WebSocketRoute, sync_playwright
    except ImportError as exc:  # pragma: no cover — playwright is a hard runtime dep
        raise HeadlessEnvError(
            "playwright is not importable — uv sync, then: uv run playwright install chromium"
        ) from exc

    findings: list[Finding] = []
    errors: list[str] = []
    blocked: list[str] = []
    saved: Path | None = None

    def block_network(route: Route) -> None:
        request_url = route.request.url
        if request_url.startswith(_ALLOWED_REQUEST_SCHEMES):
            route.continue_()
        else:
            blocked.append(request_url)
            route.abort()

    def block_websocket(ws: WebSocketRoute) -> None:
        # context.route rides Playwright's Fetch domain, which does NOT intercept WebSocket
        # handshakes (microsoft/playwright#31969) — a runtime-built new WebSocket(wss://…) would
        # otherwise open a real outbound connection during verification of untrusted HTML. This
        # dedicated WS route records the URL and returns WITHOUT calling connect_to_server(), so
        # Playwright never opens the real handshake — the connection is dropped, nothing leaves the
        # machine. (We deliberately do NOT call ws.close() here: it deadlocks the sync route
        # handler; simply not connecting is what drops the socket.)
        blocked.append(ws.url)

    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(
                    args=[AUTOPLAY_POLICY_FLAG],
                    ignore_default_args=[_PLAYWRIGHT_RELAXED_AUTOPLAY_FLAG],
                )
            except PlaywrightError as exc:
                raise HeadlessEnvError(
                    f"chromium failed to launch — run: uv run playwright install chromium ({exc})"
                ) from exc
            try:
                context = browser.new_context(
                    viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT}
                )
                context.set_default_timeout(step_timeout)
                context.route("**/*", block_network)  # B2: abort every non-local http(s) request
                context.route_web_socket("**/*", block_websocket)  # B2: drop every WS handshake
                page = context.new_page()
                _register_error_listeners(page, errors)  # BEFORE goto (§14 Step 8)
                page.add_init_script(_AUDIO_SHIM_JS)  # BEFORE goto: wraps AudioContext
                url = html_path.resolve().as_uri()
                loaded = False
                try:
                    page.goto(url)
                    loaded = True
                except PlaywrightError as exc:
                    findings.append(
                        Finding(
                            check=CHECK_PAGE_LOAD, passed=False, evidence=f"failed to load: {exc}"
                        )
                    )
                if loaded:
                    findings.append(
                        Finding(check=CHECK_PAGE_LOAD, passed=True, evidence=f"loaded {url}")
                    )
                    try:
                        _run_page_checks(page, brief, findings, errors, step_timeout)
                    except _PageTimeout as exc:
                        findings.append(
                            Finding(
                                check=CHECK_TIMEOUT,
                                passed=False,
                                evidence=f"a toy script did not yield within {step_timeout} ms —"
                                f" the page hung the JS event loop: {exc}"
                                " (earlier findings are partial)",
                            )
                        )
                    except PlaywrightError as exc:
                        findings.append(
                            Finding(
                                check=CHECK_ABORTED,
                                passed=False,
                                evidence=f"headless checks aborted mid-run: {exc}"
                                " (earlier findings are partial)",
                            )
                        )
                    findings.append(_network_block_finding(blocked))
                if screenshot_path is not None:
                    saved = _save_screenshot(page, screenshot_path)
            finally:
                browser.close()
    except HeadlessEnvError:
        raise
    except PlaywrightError as exc:
        raise HeadlessEnvError(
            f"the playwright session failed outside the toy's control: {exc}"
        ) from exc
    return findings, saved
