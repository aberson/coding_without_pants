"""verify.py tests (Step 8): calibration anchors, static-gate units, drift guard, hardening paths.

Calibration (measurement-validity rule — anchor the instrument before it gates anything):

- ``golden.html`` passes EVERY check, including a brief whose must_haves reference its content,
  and pins the SVG-xmlns allowlist (it carries ``xmlns="http://www.w3.org/2000/svg"``).
- Each single-defect garbage fixture fails FOR ITS OWN defect — asserted on the structured
  findings' check ids (exact failed-set equality), never on bare ``ok=False``.
- The must_haves compiler is exercised on predicates ABSENT from every fixture (tmp_path toys
  built from a template), in both directions — it must generalize, not fixture-match.

Hardening (iteration 2):

- The FORBIDDEN_PATTERNS drift test parses ``build-contract.md``'s ``## NEVER`` section and fails
  if any machine-checkable forbidden construct it names has no enforcing pattern here.
- Whole-text URL detection (split/unquoted/backtick evasions), the narrowed SVG allowlist (N1),
  the network-block belt (B2), the hang timeout (B3), and the three previously-untested critical
  paths (B4: aborted-mid-run, invalid selector, mash-monotonic failure) each get a test.

Runtime note: headless tests launch a real Chromium (``playwright install chromium`` required —
the browser session is reused across checks within each ``verify_toy`` call); static-gate tests
are pure string checks and free. The hang test passes a SHORT ``step_timeout_ms`` so a regression
fails fast instead of stalling the suite.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from cwp.brief import MUST_HAVE_VOCABULARY, PANTSLESS_CRITERIA, Brief
from cwp.verify import (
    CHECK_ABORTED,
    CHECK_ACTION_COUNT,
    CHECK_AUDIO_AFTER_CLICK,
    CHECK_CONSOLE_CLEAN_LOAD,
    CHECK_DOCTYPE,
    CHECK_MAIN_ACTION_PRESENT,
    CHECK_MAIN_ACTION_SIZE,
    CHECK_MASH_CLEAN,
    CHECK_MASH_MONOTONIC,
    CHECK_NETWORK_BLOCKED,
    CHECK_NO_AUDIO_BEFORE_CLICK,
    CHECK_ONLY_MAIN_INTERACTIVE,
    CHECK_SIZE_FLOOR,
    CHECK_TIMEOUT,
    COMPILED_MUST_HAVE_PREFIXES,
    FORBIDDEN_PATTERNS,
    UNCOMPILED_VOCABULARY_PREFIXES,
    ToyNotFoundError,
    VerifyResult,
    forbidden_check_id,
    must_have_check_id,
    static_checks,
    verify_toy,
)

WORKTREE_ROOT = Path(__file__).parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = FIXTURES / "golden.html"
GARBAGE_BUTTON = FIXTURES / "garbage_button.html"
GARBAGE_AUDIO = FIXTURES / "garbage_audio.html"
GARBAGE_DIALOG = FIXTURES / "garbage_dialog.html"

# Short but generous enough for a responsive page to answer a bounded evaluate; the hang test
# relies on this being small so a regression trips CHECK_TIMEOUT in ~1-2s, not tens of seconds.
FAST_STEP_TIMEOUT_MS = 1500


def make_brief(*must_haves: str) -> Brief:
    """A schema-valid Brief carrying the given must_haves (Brief does not validate them)."""
    return Brief(
        one_sentence_goal="A giant button that makes a dinosaur roar and counts every roar.",
        single_action="smash the big roar button",
        visual_motif="dinosaur",
        must_haves=tuple(must_haves),
        kid_quote="make the dinosaur go woah weally woud",
        kid_nickname="the kid",
        pantsless={name: True for name in PANTSLESS_CRITERIA},
    )


def failed_checks(result: VerifyResult) -> set[str]:
    return {finding.check for finding in result.findings if not finding.passed}


def assert_structured(result: VerifyResult) -> None:
    """Every finding carries a check id + non-empty evidence; ok mirrors the findings."""
    assert result.ok == all(finding.passed for finding in result.findings)
    assert result.failures() == tuple(f for f in result.findings if not f.passed)
    for finding in result.findings:
        assert finding.check, "finding with an empty check id"
        assert finding.evidence, f"finding {finding.check} has empty evidence"


# --- static gate (no browser) -------------------------------------------------------------------


def static_page(extra: str = "", *, doctype: bool = True) -> str:
    """A minimal static-clean page: doctype, over the size floor, one own </script>."""
    head = "<!DOCTYPE html>\n" if doctype else ""
    padding = "x" * 1500  # comfortably clears MIN_TOY_BYTES on its own
    return (
        f"{head}<html><head><title>static probe</title>\n"
        f"<style>/* {padding} */</style></head>\n"
        '<body><button data-testid="main-action" data-action-count="0">go</button>\n'
        f"{extra}\n"
        "<script>var count = 0;</script>\n"
        "</body></html>\n"
    )


def static_failed(text: str) -> set[str]:
    return {finding.check for finding in static_checks(text) if not finding.passed}


def test_static_clean_page_passes_every_check() -> None:
    findings = static_checks(static_page())
    assert all(finding.passed for finding in findings)
    # the toy's OWN closing </script> tag is never a failure (count rule allows exactly one)
    script_rule = next(f for f in findings if f.check == forbidden_check_id("script-close-count"))
    assert script_rule.passed


@pytest.mark.parametrize(
    ("snippet", "pattern_name"),
    [
        ('<img src="https://cdn.example.com/dino.png">', "external-url"),
        ('<script src="https://unpkg.com/lib.js"></script>', "external-url"),
        ("var ws = new WebSocket('wss://live.example.com/feed');", "external-url-websocket"),
        ("var ws2 = new WebSocket(endpoint);", "network-websocket"),
        ("var stream = new EventSource(path);", "network-eventsource"),
        ("navigator.sendBeacon('/telemetry', payload);", "network-sendbeacon"),
        ('<img src="//cdn.example.com/dino.png">', "external-url-relative"),
        ('<img src="pic.png">', "img-non-data-src"),
        ("@import url('theme.css');", "css-import"),
        ("fetch('/api/toys')", "network-fetch"),
        ("var request = new XMLHttpRequest();", "network-xhr"),
        ("import('./extra.js')", "network-import"),
        ('<link rel="stylesheet" href="style.css">', "external-stylesheet"),
        ("alert('ROAR')", "dialog-alert"),
        ("confirm('again?')", "dialog-confirm"),
        ("prompt('name?')", "dialog-prompt"),
    ],
)
def test_forbidden_pattern_hits_with_offending_line_evidence(
    snippet: str, pattern_name: str
) -> None:
    findings = static_checks(static_page(snippet))
    check = forbidden_check_id(pattern_name)
    by_check = {finding.check: finding for finding in findings}
    assert not by_check[check].passed
    assert "line " in by_check[check].evidence  # the offending line is the evidence


@pytest.mark.parametrize(
    "evasion",
    [
        "<a data-x=https://evil.example/x>unquoted attr</a>",  # unquoted
        "<a\n  data-x=\n  'https://evil.example/x'>split across lines</a>",  # attr/URL split
        "<p>note: `https://evil.example/x` in a template literal</p>",  # backtick form
        "img.setAttribute('src', 'https://evil.example/x.png');",  # setAttribute
    ],
)
def test_whole_text_url_detection_catches_evasions(evasion: str) -> None:
    """A URL split across lines, unquoted, backtick-wrapped, or set via setAttribute still fails —
    static_checks scans the whole text and the scheme literal is always present."""
    assert forbidden_check_id("external-url") in static_failed(static_page(evasion))


def test_bare_identifier_mention_passes_but_construction_fails() -> None:
    """NIT: WebSocket/EventSource/XMLHttpRequest patterns require `new` — a mere mention in a
    comment/text must PASS (no false-FAIL burning a repair retry), while real construction FAILS."""
    mention = (
        "<!-- design note: no WebSocket, EventSource, or XMLHttpRequest needed for this toy -->"
    )
    assert static_failed(static_page(mention)) == set()
    for snippet, pattern_name in (
        ("var s = new WebSocket(u);", "network-websocket"),
        ("var e = new EventSource(u);", "network-eventsource"),
        ("var x = new XMLHttpRequest();", "network-xhr"),
    ):
        assert forbidden_check_id(pattern_name) in static_failed(static_page(snippet))


def test_script_close_count_rule_fails_only_above_one() -> None:
    # a SECOND </script> (here: a string-embedded literal) is the breaker
    embedded = 'var trap = "</script>";'
    findings = static_checks(static_page(embedded))
    check = forbidden_check_id("script-close-count")
    finding = next(f for f in findings if f.check == check)
    assert not finding.passed
    assert "occurrences" in finding.evidence


def test_svg_namespace_allowlist_exempts_w3_namespace_declarations() -> None:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"></svg>'
    )
    assert static_failed(static_page(svg)) == set()


def test_xlink_href_resource_is_not_allowlisted() -> None:
    """N1: xmlns/xmlns:xlink are inert namespace decls (exempt), but xlink:href="http…" is a
    fetched resource on <use>/<image> and must FAIL."""
    use = '<svg xmlns="http://www.w3.org/2000/svg"><use xlink:href="http://evil/x#i"/></svg>'
    assert forbidden_check_id("external-url") in static_failed(static_page(use))


def test_non_w3_namespace_url_still_fails() -> None:
    assert forbidden_check_id("external-url") in static_failed(
        static_page('<svg xmlns="https://evil.example/ns"></svg>')
    )


def test_missing_doctype_fails() -> None:
    findings = static_checks(static_page(doctype=False))
    by_check = {finding.check: finding for finding in findings}
    assert not by_check[CHECK_DOCTYPE].passed


def test_size_floor_fails_a_stub() -> None:
    findings = static_checks("<!DOCTYPE html>\n<html><body>hi</body></html>\n")
    by_check = {finding.check: finding for finding in findings}
    assert not by_check[CHECK_SIZE_FLOOR].passed
    assert "bytes" in by_check[CHECK_SIZE_FLOOR].evidence


def test_missing_toy_raises_toy_not_found(tmp_path: Path) -> None:
    with pytest.raises(ToyNotFoundError):
        verify_toy(tmp_path / "absent.html")


def test_no_heavy_imports_at_module_top() -> None:
    """Importing cwp.verify must not pull playwright (Step 9 imports it on every build)."""
    code = (
        "import sys\n"
        "import cwp.verify\n"
        "heavy = [m for m in ('playwright', 'faster_whisper')"
        " if any(k == m or k.startswith(m + '.') for k in sys.modules)]\n"
        "assert not heavy, f'heavy modules imported at cwp.verify import time: {heavy}'\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_compiler_covers_the_whole_vocabulary() -> None:
    """Drift guard: every brief.py vocabulary predicate has a compiled assertion here."""
    assert {p.prefix for p in MUST_HAVE_VOCABULARY} == set(COMPILED_MUST_HAVE_PREFIXES)
    assert UNCOMPILED_VOCABULARY_PREFIXES == frozenset()
    # and the forbidden-pattern names stay unique (they are Finding.check keys)
    names = [pattern.name for pattern in FORBIDDEN_PATTERNS]
    assert len(names) == len(set(names))


# --- FORBIDDEN_PATTERNS <-> build-contract.md drift guard ---------------------------------------

# Each row: a machine-checkable construct build-contract.md's NEVER list names, the token that
# must still appear in that section, a violating snippet, and the pattern that must catch it. If
# the contract renames/drops an item this test's `token in never` assertion fails; if a pattern is
# removed the `static_failed` assertion fails. New NEVER items must be added here (the tripwire).
CONTRACT_NEVER_COVERAGE = (
    ("fetch(", "fetch('/api/x')", "network-fetch"),
    ("XMLHttpRequest", "var r = new XMLHttpRequest();", "network-xhr"),
    ("WebSocket", "var s = new WebSocket(endpoint);", "network-websocket"),
    ("import()", "import('./mod.js')", "network-import"),
    ("alert(", "alert('hi')", "dialog-alert"),
    ("confirm(", "confirm('ok?')", "dialog-confirm"),
    ("prompt(", "prompt('name?')", "dialog-prompt"),
    ("@import", "@import url('theme.css');", "css-import"),
    (
        '<link href="http',
        '<link rel="stylesheet" href="https://cdn.example.com/s.css">',
        "external-stylesheet",
    ),
    ('<img src="http', '<img src="https://cdn.example.com/p.png">', "external-url"),
    ("</script>", 'var t = "</script>";', "script-close-count"),
)


def never_section() -> str:
    text = (WORKTREE_ROOT / "build-contract.md").read_text(encoding="utf-8")
    start = text.index("## NEVER")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def test_forbidden_patterns_cover_the_contract_never_list() -> None:
    section = never_section()
    known_names = {pattern.name for pattern in FORBIDDEN_PATTERNS}
    for token, snippet, pattern_name in CONTRACT_NEVER_COVERAGE:
        assert token in section, f"build-contract.md NEVER no longer names {token!r}"
        assert pattern_name in known_names, f"no FORBIDDEN_PATTERNS entry named {pattern_name!r}"
        assert forbidden_check_id(pattern_name) in static_failed(static_page(snippet)), (
            f"NEVER item {token!r} is not enforced by pattern {pattern_name!r}"
        )


# --- calibration anchors (headless Chromium) ----------------------------------------------------


def test_golden_passes_every_check_with_brief_and_screenshot(tmp_path: Path) -> None:
    shot = tmp_path / "shots" / "golden.png"
    brief = make_brief(
        "visible:\U0001f996",  # the T-rex emoji sprite
        'element:[data-testid="main-action"]',
        "sound_on_action",
        "state_change:data-mood",
    )
    result = verify_toy(GOLDEN, brief, screenshot_path=shot)
    assert_structured(result)
    assert failed_checks(result) == set()
    assert result.ok
    assert result.screenshot_path == shot
    assert shot.is_file() and shot.stat().st_size > 0
    checks = {finding.check for finding in result.findings}
    # the full headless battery ran (including the B2 network-block belt)...
    assert {
        CHECK_MAIN_ACTION_SIZE,
        CHECK_ACTION_COUNT,
        CHECK_AUDIO_AFTER_CLICK,
        CHECK_MASH_CLEAN,
        CHECK_MASH_MONOTONIC,
        CHECK_ONLY_MAIN_INTERACTIVE,
        CHECK_NETWORK_BLOCKED,
    } <= checks
    assert CHECK_TIMEOUT not in checks  # a responsive page emits no timeout finding
    assert CHECK_ABORTED not in checks
    # ...and every must_have compiled into its own finding
    for entry in brief.must_haves:
        assert must_have_check_id(entry) in checks


def test_garbage_button_fails_for_the_size_check_only() -> None:
    result = verify_toy(GARBAGE_BUTTON)
    assert_structured(result)
    assert not result.ok
    assert failed_checks(result) == {CHECK_MAIN_ACTION_SIZE}
    size = next(f for f in result.findings if f.check == CHECK_MAIN_ACTION_SIZE)
    assert "%" in size.evidence  # the measured coverage is the evidence


def test_garbage_audio_fails_for_the_shim_check_only() -> None:
    """Top-level AudioContext: silently suspended, NO console error — only the shim sees it."""
    result = verify_toy(GARBAGE_AUDIO)
    assert_structured(result)
    assert not result.ok
    assert failed_checks(result) == {CHECK_NO_AUDIO_BEFORE_CLICK}
    by_check = {finding.check: finding for finding in result.findings}
    assert by_check[CHECK_CONSOLE_CLEAN_LOAD].passed  # console errors could NOT have caught this
    assert by_check[CHECK_AUDIO_AFTER_CLICK].passed  # resume() in the handler — running after
    assert "before the first click" in by_check[CHECK_NO_AUDIO_BEFORE_CLICK].evidence


def test_garbage_dialog_fails_static_alert_and_never_reaches_the_browser() -> None:
    result = verify_toy(GARBAGE_DIALOG)
    assert_structured(result)
    assert not result.ok
    assert failed_checks(result) == {forbidden_check_id("dialog-alert")}
    dialog = next(f for f in result.findings if f.check == forbidden_check_id("dialog-alert"))
    assert "alert(" in dialog.evidence  # exact offending line
    # static failure short-circuits: no headless finding may exist, no screenshot possible
    assert not any(finding.check.startswith("headless:") for finding in result.findings)
    assert result.screenshot_path is None


# --- headless behaviour toys (B2/B3/B4) ---------------------------------------------------------

_SIZE_PAD = "z" * 1000  # a CSS comment that keeps every behaviour toy over the size floor


def behavior_toy(
    path: Path, *, handler_body: str = "", script_prelude: str = "", body_extra: str = ""
) -> Path:
    """A contract-clean big-button toy with pluggable JS — ONE <script> block only (a second
    would trip the </script>-count rule and short-circuit the headless phase). ``script_prelude``
    runs at load, ``handler_body`` on each click."""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>behaviour toy</title>
<style>
  html, body {{ height: 100%; margin: 0; }}
  body {{ display: flex; align-items: center; justify-content: center; }}
  button[data-testid="main-action"] {{ width: 70vw; height: 70vh; border: none; font-size: 8rem; }}
  /* size-floor padding: {_SIZE_PAD} */
</style>
</head>
<body>
{body_extra}
<button data-testid="main-action" data-action-count="0" aria-label="press">GO</button>
<script>
  {script_prelude}
  var button = document.querySelector('[data-testid="main-action"]');
  button.addEventListener("click", function () {{
    {handler_body}
  }});
</script>
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")
    return path


def test_network_block_belt_aborts_an_evasive_request(tmp_path: Path) -> None:
    """B2: a runtime network call whose URL evades the static grep (scheme built from char codes,
    no <img> tag, no fetch() literal) is still physically blocked and reported."""
    toy = tmp_path / "evasive_net.html"
    # String.fromCharCode(104,116,116,112,115) === "https"; assembled at runtime so no scheme
    # literal exists for static_checks to catch. new Image().src triggers a real resource load.
    evasion = (
        "var scheme = String.fromCharCode(104,116,116,112,115);\n"
        "  var probe = new Image();\n"
        '  probe.src = scheme + "://blocked.invalid/tracker.png";\n'
        "  document.body.appendChild(probe);"
    )
    behavior_toy(toy, script_prelude=evasion)
    # confirm it truly evades the static gate (else this would not exercise the belt)
    assert not any(
        f.check.startswith("static:forbidden") and not f.passed
        for f in static_checks(toy.read_text(encoding="utf-8"))
    )
    result = verify_toy(toy)
    assert_structured(result)
    assert CHECK_NETWORK_BLOCKED in failed_checks(result)
    blocked = next(f for f in result.findings if f.check == CHECK_NETWORK_BLOCKED)
    assert "blocked.invalid" in blocked.evidence


def test_websocket_belt_drops_an_evasive_handshake(tmp_path: Path) -> None:
    """B2 (WS hole): context.route rides the Fetch domain and does NOT intercept WebSocket
    handshakes — so a runtime-built WS (scheme AND constructor assembled from string-concat, to
    evade BOTH the wss:// URL pattern and the new-WebSocket construction pattern) must be dropped
    by the dedicated context.route_web_socket belt, not left to open a real outbound connection."""
    toy = tmp_path / "evasive_ws.html"
    # String.fromCharCode(119,115,115) === "wss"; window["Web"+"Socket"] dodges `new\s+WebSocket`.
    evasion = (
        "var scheme = String.fromCharCode(119,115,115);\n"
        '  var Ctor = window["Web" + "Socket"];\n'
        '  try { new Ctor(scheme + "://blocked.invalid/live"); } catch (e) {}'
    )
    behavior_toy(toy, script_prelude=evasion)
    # confirm it truly evades the static gate (else this would not exercise the runtime WS belt)
    assert not any(
        f.check.startswith("static:forbidden") and not f.passed
        for f in static_checks(toy.read_text(encoding="utf-8"))
    )
    result = verify_toy(toy)
    assert_structured(result)
    assert CHECK_NETWORK_BLOCKED in failed_checks(result)
    blocked = next(f for f in result.findings if f.check == CHECK_NETWORK_BLOCKED)
    assert "blocked.invalid" in blocked.evidence


def test_busy_loop_toy_times_out_and_does_not_hang(tmp_path: Path) -> None:
    """B3: a toy that busy-loops the JS event loop after load yields CHECK_TIMEOUT within a
    bounded wall-clock — page.evaluate/count are routed through a driver-timed bounded read."""
    toy = tmp_path / "hang.html"
    # Scheduled (not inline) so goto's load event fires first; then the callback busy-loops the
    # JS event loop, which would block any unbounded page.evaluate/count forever.
    behavior_toy(toy, script_prelude="setTimeout(function () { while (true) {} }, 50);")
    start = time.monotonic()
    result = verify_toy(toy, step_timeout_ms=FAST_STEP_TIMEOUT_MS)
    elapsed = time.monotonic() - start
    assert CHECK_TIMEOUT in failed_checks(result)
    assert elapsed < 15, f"verify_toy did not return promptly on a hanging toy ({elapsed:.1f}s)"
    timeout = next(f for f in result.findings if f.check == CHECK_TIMEOUT)
    assert "hung" in timeout.evidence


def test_navigate_away_toy_reports_aborted_with_partial_findings(tmp_path: Path) -> None:
    """B4: a handler that navigates the page away from itself mid-verification (a reload/redirect
    dead-end) surfaces CHECK_ABORTED, and the findings gathered before the abort are preserved."""
    toy = tmp_path / "abort.html"
    # deferred so the click itself completes cleanly; the navigation then destroys the toy the
    # subsequent read expects, which is the abort we want to observe.
    behavior_toy(
        toy,
        handler_body="setTimeout(function () { window.location.href = 'about:blank'; }, 10);",
    )
    result = verify_toy(toy, step_timeout_ms=2500)
    assert_structured(result)
    assert CHECK_ABORTED in failed_checks(result)
    by_check = {finding.check: finding for finding in result.findings}
    # pre-abort findings are preserved
    assert by_check[CHECK_MAIN_ACTION_PRESENT].passed
    assert CHECK_CONSOLE_CLEAN_LOAD in by_check


def test_mash_monotonic_fails_when_the_counter_plateaus(tmp_path: Path) -> None:
    """B4: a counter that increments once then plateaus fails CHECK_MASH_MONOTONIC (not
    CHECK_ACTION_COUNT) with the plateaued sequence in evidence."""
    toy = tmp_path / "plateau.html"
    behavior_toy(
        toy,
        # first press 0 -> 1 (action-count passes), every later press capped at 1 (plateau)
        handler_body=(
            "var current = parseInt(button.getAttribute('data-action-count'), 10);\n"
            "button.setAttribute('data-action-count', String(Math.min(current + 1, 1)));"
        ),
    )
    result = verify_toy(toy)
    assert_structured(result)
    by_check = {finding.check: finding for finding in result.findings}
    assert by_check[CHECK_ACTION_COUNT].passed  # the first click DID increment (0 -> 1)
    mono = by_check[CHECK_MASH_MONOTONIC]
    assert not mono.passed
    assert "1" in mono.evidence  # the plateaued sequence is the evidence
    assert "strictly increasing" in mono.evidence


def test_invalid_element_selector_degrades_to_a_failed_finding(tmp_path: Path) -> None:
    """B4: a malformed element: selector becomes a failed Finding carrying the JS error, never an
    uncaught exception that would crash verify_toy."""
    toy = tmp_path / "clean.html"
    behavior_toy(
        toy,
        handler_body=(
            "var c = parseInt(button.getAttribute('data-action-count'), 10) + 1;\n"
            "button.setAttribute('data-action-count', String(c));"
        ),
    )
    brief = make_brief("element:###bad[")
    result = verify_toy(toy, brief)  # must not raise
    assert_structured(result)
    finding = next(f for f in result.findings if f.check == must_have_check_id("element:###bad["))
    assert not finding.passed
    assert "invalid" in finding.evidence


# --- must_haves compiler generalization (predicates absent from every fixture) -------------------

_TOY_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>compiler probe toy</title>
<style>
  html, body {{ height: 100%; margin: 0; }}
  body {{ display: flex; align-items: center; justify-content: center; }}
  button[data-testid="main-action"] {{
    width: 70vw; height: 70vh; border: none; font-size: 8rem;
  }}
  /* size-floor padding: {padding} */
</style>
</head>
<body data-phase="{phase}">
  {body_extra}
  <button data-testid="main-action" data-action-count="0" aria-label="press">&#127880;</button>
  <script>
    var button = document.querySelector('[data-testid="main-action"]');
    button.addEventListener("click", function () {{
      var count = parseInt(button.getAttribute("data-action-count"), 10) + 1;
      button.setAttribute("data-action-count", String(count));
      {handler_extra}
    }});
  </script>
</body>
</html>
"""


def write_probe_toy(
    path: Path, *, phase: str, body_extra: str = "", handler_extra: str = ""
) -> Path:
    """A contract-clean toy (passes everything generic) with pluggable must_have targets."""
    path.write_text(
        _TOY_TEMPLATE.format(
            padding="p" * 900, phase=phase, body_extra=body_extra, handler_extra=handler_extra
        ),
        encoding="utf-8",
    )
    return path


def test_compiler_maps_fresh_predicates_positively(tmp_path: Path) -> None:
    """visible:/element:/state_change: pass against content no fixture carries; the
    sound_on_action tightening FAILS on a silent toy (audio is otherwise optional)."""
    toy = write_probe_toy(
        tmp_path / "cookie.html",
        phase="before",
        body_extra='<div id="cookie-jar">\U0001f36a</div>',
        handler_extra='document.body.setAttribute("data-phase", "after");',
    )
    brief = make_brief(
        "visible:\U0001f36a",
        "element:#cookie-jar",
        "state_change:data-phase",
        "sound_on_action",
    )
    result = verify_toy(toy, brief)
    assert_structured(result)
    by_check = {finding.check: finding for finding in result.findings}
    assert by_check[must_have_check_id("visible:\U0001f36a")].passed
    assert by_check[must_have_check_id("element:#cookie-jar")].passed
    assert by_check[must_have_check_id("state_change:data-phase")].passed
    sound = by_check[must_have_check_id("sound_on_action")]
    assert not sound.passed
    assert "no AudioContext" in sound.evidence
    # the GENERIC audio checks still pass — sound is optional unless the brief demands it
    assert by_check[CHECK_NO_AUDIO_BEFORE_CLICK].passed
    assert by_check[CHECK_AUDIO_AFTER_CLICK].passed


def test_compiler_maps_visible_via_pseudo_content(tmp_path: Path) -> None:
    """N3: an emoji placed via CSS ::before content is a contract-endorsed visual and must
    satisfy visible: — innerText alone would false-FAIL it."""
    toy = write_probe_toy(
        tmp_path / "pseudo.html",
        phase="idle",
        body_extra=(
            '<style>#badge::before { content: "\U0001f680"; }</style><div id="badge"></div>'
        ),
    )
    result = verify_toy(toy, make_brief("visible:\U0001f680"))
    assert_structured(result)
    by_check = {finding.check: finding for finding in result.findings}
    assert by_check[must_have_check_id("visible:\U0001f680")].passed


def test_compiler_maps_fresh_predicates_negatively(tmp_path: Path) -> None:
    """The same predicates fail with structured evidence when the toy lacks the content,
    and an out-of-vocabulary entry becomes a failed finding instead of a crash."""
    toy = write_probe_toy(tmp_path / "plain.html", phase="frozen")
    brief = make_brief(
        "visible:\U0001f9e6",  # sock emoji — nowhere in the toy
        "element:#sock-drawer",
        "state_change:data-phase",  # attribute exists but never changes
        "confetti_everywhere",  # out of vocabulary
    )
    result = verify_toy(toy, brief)
    assert_structured(result)
    by_check = {finding.check: finding for finding in result.findings}
    visible = by_check[must_have_check_id("visible:\U0001f9e6")]
    assert not visible.passed
    assert "not found" in visible.evidence
    element = by_check[must_have_check_id("element:#sock-drawer")]
    assert not element.passed
    assert "#sock-drawer" in element.evidence
    state = by_check[must_have_check_id("state_change:data-phase")]
    assert not state.passed
    assert "did not change" in state.evidence
    unknown = by_check[must_have_check_id("confetti_everywhere")]
    assert not unknown.passed
    assert "unknown predicate" in unknown.evidence
