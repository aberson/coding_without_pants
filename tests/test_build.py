"""build.py tests (Step 9): the generate -> verify -> repair -> commit reliability core.

The claude boundary is ALWAYS mocked (tests/test_drafting.py house style) -- scripted in-process
replies via a fake ``drafting.call_claude`` (build calls it through the module attribute, so the
patch lands). ``verify.py`` is the REAL calibrated instrument (real Chromium): the fake claude
returns the golden fixture as the "good toy", so a full verify pass is exercised end-to-end. The
broken toys are STATIC-failing (a forbidden pattern), so their verify short-circuits before a
browser launches -- the suite stays bounded (only the golden-passing verifies spin up Chromium).

Every path from the plan's Done-when + §3.2 is covered: (a) golden -> commit + pass log;
(b) broken x2 then golden -> repair on the EXACT evidence; (c) broken always -> needs_human,
exit 2, existing toy untouched; (d) same broken twice -> near-identical abort before the last
slot; (e) timeout -> one same-slot retry, no repair-attempt consumed; (f) missing brief -> exit 1;
(g) clobber with/without --force. Plus fence-extraction (issue #17) unit tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cwp import brief as brief_module
from cwp import build, drafting, episodes, templates, verify
from cwp.brief import PANTSLESS_CRITERIA, Brief
from cwp.cli import EXIT_ENV_ERROR, EXIT_OK, EXIT_USER_ERROR, main

WORKTREE_ROOT = Path(__file__).parents[1]
REAL_CONTRACT = WORKTREE_ROOT / "build-contract.md"
GOLDEN = WORKTREE_ROOT / "tests" / "fixtures" / "golden.html"

EPISODE_TITLE = "FizzBuzz, But It's a Dinosaur"
GOAL = "A giant dinosaur button that roars and counts every roar."

# Three DISTINCT static-failing toys (each trips one FORBIDDEN_PATTERN + the size floor, so
# verify short-circuits before Chromium). Distinct so consecutive attempts are NOT near-identical
# (the (b)/(c) repair paths must not trip the near-identical abort meant for (d)).
BROKEN_ALERT = """\
<!DOCTYPE html>
<html><head><title>Alpha toy</title></head>
<body>
<button data-testid="main-action" data-action-count="0">A</button>
<script>alert("boom in alpha"); var alpha = 1;</script>
</body></html>"""

BROKEN_FETCH = """\
<!DOCTYPE html>
<html><head><title>Beta gadget</title></head>
<body>
<section><button data-testid="main-action" data-action-count="0">B</button></section>
<script>fetch("/beta-endpoint"); let beta = 2;</script>
</body></html>"""

BROKEN_XHR = """\
<!DOCTYPE html>
<html><head><title>Gamma widget</title></head>
<body>
<main><button data-testid="main-action" data-action-count="0">C</button></main>
<script>var r = new XMLHttpRequest(); var gamma = 3;</script>
</body></html>"""


def make_brief() -> Brief:
    """A schema-valid brief whose must_haves the GOLDEN fixture satisfies (mirrors test_verify)."""
    return Brief(
        one_sentence_goal=GOAL,
        single_action="smash the big roar button",
        visual_motif="dinosaur",
        must_haves=(
            "visible:\U0001f996",  # the T-rex emoji sprite golden renders
            'element:[data-testid="main-action"]',
            "sound_on_action",
            "state_change:data-mood",
        ),
        kid_quote="make the dinosaur go woah weally woud",
        kid_nickname="the kid",
        pantsless={name: True for name in PANTSLESS_CRITERIA},
    )


def _html_reply(html: str) -> str:
    """Wrap a toy in the single ```html fence a real claude reply carries."""
    return f"```html\n{html.strip()}\n```\n"


def _golden_reply() -> str:
    return _html_reply(GOLDEN.read_text(encoding="utf-8"))


# --- fixtures ---


@pytest.fixture(autouse=True)
def cold_preflight_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with a cold per-process preflight cache."""
    monkeypatch.setattr(drafting, "_preflight_passed", False)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repo root: pyproject marker + the REAL build-contract.md, cwd inside it."""
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "build-contract.md").write_text(
        REAL_CONTRACT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def episode_dir(repo: Path) -> Path:
    """Episode 001 with a real, loadable brief.md (its placeholder index.html still in place)."""
    created = episodes.create_episode(
        repo / "episodes", EPISODE_TITLE, hook="Five clean lines, then a roaring counter."
    )
    brief_module.write_brief(created.directory, make_brief())
    return created.directory


def _seam(monkeypatch: pytest.MonkeyPatch, replies: list[object]) -> dict[str, object]:
    """Scripted in-process seam: reply k answers call k (a str is returned, an Exception raised).

    Returns a ``calls`` record: ``calls["prompts"]`` is every prompt seen (so call COUNT and the
    repair-prompt contents are assertable). A call past the end of ``replies`` raises IndexError,
    which loudly fails any test that made more claude calls than it scripted.
    """
    calls: dict[str, object] = {"prompts": [], "n": 0}

    def fake_ready(*, timeout: float | None = None) -> None:
        return None

    def fake_call(prompt: str, *, timeout: float, partial_path: Path | None = None) -> str:
        prompts: list[str] = calls["prompts"]  # type: ignore[assignment]
        prompts.append(prompt)
        index = calls["n"]
        assert isinstance(index, int)
        calls["n"] = index + 1
        item = replies[index]
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, str)
        return item

    monkeypatch.setattr(drafting, "ensure_claude_ready", fake_ready)
    monkeypatch.setattr(drafting, "call_claude", fake_call)
    return calls


def _prompts(calls: dict[str, object]) -> list[str]:
    prompts = calls["prompts"]
    assert isinstance(prompts, list)
    return prompts


def _needs_human_flag(episode_dir: Path) -> bool:
    _directory, episode = episodes.load_episode(episode_dir.parent, "001")
    return episode.needs_human


# --- (a) golden -> commit + pass log --------------------------------------------------------------


def test_golden_reply_commits_and_logs_a_pass(
    episode_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _seam(monkeypatch, [_golden_reply()])
    assert main(["build", "001"]) == EXIT_OK
    assert len(_prompts(calls)) == 1  # one generate call; placeholder overwritten without --force

    # The generate prompt is the contract with the brief substituted in (no leftover placeholder).
    prompt = _prompts(calls)[0]
    assert GOAL in prompt
    assert "smash the big roar button" in prompt
    assert "{one_sentence_goal}" not in prompt

    index = episode_dir / "project" / "index.html"
    content = index.read_text(encoding="utf-8")
    assert "Dino Roar Button" in content  # the golden toy is now committed
    assert templates.INDEX_HTML_PLACEHOLDER_SENTINEL not in content  # a real toy, not the scaffold

    log = episode_dir / "project" / ".repair" / "log.jsonl"
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert rows and all(row["passed"] for row in rows)  # a pass logged
    assert all(row["attempt"] == 1 for row in rows)  # committed on the first shot
    assert (episode_dir / "project" / ".repair" / "attempt-1.png").is_file()  # screenshot saved


# --- (b) broken x2 then golden -> repair succeeds on the EXACT verify evidence -------------------


def test_repair_succeeds_and_carries_the_exact_verify_evidence(
    episode_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _seam(
        monkeypatch, [_html_reply(BROKEN_ALERT), _html_reply(BROKEN_FETCH), _golden_reply()]
    )
    assert main(["build", "001"]) == EXIT_OK
    assert len(_prompts(calls)) == 3  # generate + 2 repairs

    index = episode_dir / "project" / "index.html"
    assert "Dino Roar Button" in index.read_text(encoding="utf-8")  # golden committed at the end

    # THE load-bearing assertion: the FIRST repair prompt carries verify's EXACT findings for
    # attempt-1 (BROKEN_ALERT), verbatim. Independently re-verify the exact file build verified
    # (.repair/attempt-1.html) and assert every failure's evidence string is embedded literally.
    repair_prompt = _prompts(calls)[1]
    assert "Repair" in repair_prompt
    attempt1 = episode_dir / "project" / ".repair" / "attempt-1.html"
    independent = verify.verify_toy(attempt1, make_brief())
    assert not independent.ok
    assert independent.failures()  # BROKEN_ALERT trips the static gate
    for finding in independent.failures():
        assert finding.evidence in repair_prompt, f"evidence for {finding.check} not fed to repair"
    assert "alert(" in repair_prompt  # the alert defect specifically reached the model


# --- (c) broken always -> needs_human, exit 2, existing toy NEVER clobbered -----------------------


def test_broken_every_time_needs_human_and_never_clobbers(
    episode_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    index = episode_dir / "project" / "index.html"
    real_toy = "<!doctype html>\n<html><body>MY REAL HAND-BUILT TOY</body></html>\n"
    index.write_text(real_toy, encoding="utf-8")  # a pre-existing REAL toy (sentinel gone)

    calls = _seam(
        monkeypatch,
        [_html_reply(BROKEN_ALERT), _html_reply(BROKEN_FETCH), _html_reply(BROKEN_XHR)],
    )
    # --force gets past the clobber gate; exhaustion must STILL leave the real toy untouched.
    assert main(["build", "001", "--force"]) == EXIT_ENV_ERROR
    assert len(_prompts(calls)) == 3  # all three shots spent

    assert index.read_text(encoding="utf-8") == real_toy  # untouched despite --force (item 7)
    assert _needs_human_flag(episode_dir) is True
    err = capsys.readouterr().err
    assert "needs human" in err
    assert "repair budget" in err  # the exhaustion reason, not a timeout/near-identical one


# --- (d) same broken twice -> near-identical abort BEFORE the last slot ---------------------------


def test_near_identical_repair_aborts_before_the_last_slot(
    episode_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Only TWO replies scripted: a third claude call would IndexError and fail the test loudly.
    calls = _seam(monkeypatch, [_html_reply(BROKEN_ALERT), _html_reply(BROKEN_ALERT)])
    assert main(["build", "001"]) == EXIT_ENV_ERROR
    assert len(_prompts(calls)) == 2  # aborted after the repeat, NOT after a third shot

    assert _needs_human_flag(episode_dir) is True
    assert "near-identical" in capsys.readouterr().err


# --- (e) timeout -> one same-slot retry, timeout does NOT consume a repair attempt ----------------


def test_double_timeout_needs_human_with_a_timeout_message(
    episode_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _seam(
        monkeypatch,
        [drafting.ClaudeTimeoutError("timeout 1"), drafting.ClaudeTimeoutError("timeout 2")],
    )
    assert main(["build", "001"]) == EXIT_ENV_ERROR
    assert len(_prompts(calls)) == 2  # one call + one same-slot retry, never a third

    assert _needs_human_flag(episode_dir) is True
    assert "timed out" in capsys.readouterr().err


def test_timeout_then_success_does_not_consume_a_repair_attempt(
    episode_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout at the initial slot is retried once; the retry's golden commits on SHOT 1 --
    proving the timeout burned no repair attempt (a consumed shot would log attempt > 1)."""
    calls = _seam(monkeypatch, [drafting.ClaudeTimeoutError("transient"), _golden_reply()])
    assert main(["build", "001"]) == EXIT_OK
    assert len(_prompts(calls)) == 2  # timeout + same-slot retry

    index = episode_dir / "project" / "index.html"
    assert "Dino Roar Button" in index.read_text(encoding="utf-8")
    log = episode_dir / "project" / ".repair" / "log.jsonl"
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert rows and all(row["attempt"] == 1 for row in rows)  # committed on shot 1, not a repair


# --- (f) missing / placeholder brief -> user error (exit 1) ---------------------------------------


def test_missing_brief_is_a_user_error_exit_1(
    episode_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (episode_dir / "brief.md").unlink()
    _seam(monkeypatch, [])  # no claude call may happen (any would IndexError)
    assert main(["build", "001"]) == EXIT_USER_ERROR
    assert "brief" in capsys.readouterr().err.lower()


def test_placeholder_brief_is_a_user_error_exit_1(
    episode_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The cwp new placeholder brief.md has no TOML fence -- not a distilled brief."""
    (episode_dir / "brief.md").write_text(
        templates.render_brief_md(title="T", episode_id="001-x"), encoding="utf-8"
    )
    _seam(monkeypatch, [])
    assert main(["build", "001"]) == EXIT_USER_ERROR
    assert "frontmatter fence" in capsys.readouterr().err


# --- (g) clobber protection ----------------------------------------------------------------------


def test_existing_toy_without_force_is_refused_before_any_call(
    episode_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    index = episode_dir / "project" / "index.html"
    real_toy = "<!doctype html>\n<html><body>REAL TOY, DO NOT CLOBBER</body></html>\n"
    index.write_text(real_toy, encoding="utf-8")

    calls = _seam(monkeypatch, [])  # NO claude call -- the clobber gate fails fast
    assert main(["build", "001"]) == EXIT_USER_ERROR
    assert len(_prompts(calls)) == 0  # refused before generating anything
    assert index.read_text(encoding="utf-8") == real_toy  # untouched
    assert "--force" in capsys.readouterr().err


def test_existing_toy_with_force_is_overwritten_on_a_verified_pass(
    episode_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = episode_dir / "project" / "index.html"
    index.write_text("<!doctype html>\n<html><body>OLD TOY</body></html>\n", encoding="utf-8")

    _seam(monkeypatch, [_golden_reply()])
    assert main(["build", "001", "--force"]) == EXIT_OK
    content = index.read_text(encoding="utf-8")
    assert "Dino Roar Button" in content
    assert "OLD TOY" not in content


# --- regression: a later successful build clears the stale needs_human flag (item 1) -------------


def test_a_later_forced_build_clears_needs_human(
    episode_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exhaust to needs_human, then a --force build that returns golden must commit AND reset
    needs_human -- else `cwp show` reports needs_human=yes on a freshly-verified toy forever."""
    _seam(
        monkeypatch,
        [_html_reply(BROKEN_ALERT), _html_reply(BROKEN_FETCH), _html_reply(BROKEN_XHR)],
    )
    assert main(["build", "001"]) == EXIT_ENV_ERROR
    assert _needs_human_flag(episode_dir) is True  # the give-up flag was set

    _seam(monkeypatch, [_golden_reply()])  # a fresh scripted seam for the retry
    assert main(["build", "001", "--force"]) == EXIT_OK
    index = episode_dir / "project" / "index.html"
    assert "Dino Roar Button" in index.read_text(encoding="utf-8")  # the verified toy committed
    assert _needs_human_flag(episode_dir) is False  # ...and the flag was reset, not left stale


# --- headless-failure exhaustion: the screenshot path is saved AND forwarded (item 3) ------------


def test_headless_failure_exhaustion_saves_and_forwards_a_screenshot(
    episode_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every OTHER failure test fails STATICALLY, so verify returns before the browser and no
    screenshot is ever saved. Here three toys PASS static but FAIL headless -- a genuine 3-shot
    headless exhaustion that proves the build.py -> cli.py screenshot-path forwarding.

    audio != button (not near-identical), so all three shots run; shot 3 reuses the audio fixture
    (not the immediate predecessor, so the near-identical guard stays quiet)."""
    fixtures = WORKTREE_ROOT / "tests" / "fixtures"
    audio = (fixtures / "garbage_audio.html").read_text(encoding="utf-8")
    button = (fixtures / "garbage_button.html").read_text(encoding="utf-8")
    calls = _seam(monkeypatch, [_html_reply(audio), _html_reply(button), _html_reply(audio)])
    assert main(["build", "001"]) == EXIT_ENV_ERROR
    assert len(_prompts(calls)) == 3  # a real 3-shot headless exhaustion, not a static short-cut
    assert _needs_human_flag(episode_dir) is True

    shot = episode_dir / "project" / ".repair" / "attempt-3.png"
    assert shot.is_file() and shot.stat().st_size > 0  # a real post-load screenshot was saved
    err = capsys.readouterr().err
    assert "screenshot" in err and "attempt-3.png" in err  # ...and its path reached the operator


# --- extraction discipline (issue #17) + prompt assembly -- fast unit tests, no browser -----------


def test_extract_html_keeps_an_internal_fence_before_close_issue_17() -> None:
    """A bare ``` inside a JS template literal (BEFORE </html>) must NOT truncate the toy."""
    inner = (
        "<!DOCTYPE html>\n<html><body>\n"
        "<script>const t = `\n```\nstill inside the toy`;</script>\n"
        "</body></html>\n"
    )
    extracted = build.extract_html(f"```html\n{inner}```\n")
    assert extracted is not None
    assert "still inside the toy" in extracted  # nothing before </html> was truncated
    assert "```" in extracted  # the internal bare fence survived (it precedes </html>)
    assert extracted.rstrip().endswith("</html>")


def test_extract_html_drops_a_trailing_aside_after_close() -> None:
    """The inverse of #17: a courtesy fenced aside AFTER the real close is NOT committed."""
    inner = "<!DOCTYPE html>\n<html><body><h1>toy</h1></body></html>"
    reply = f"```html\n{inner}\n```\n\nAll done! One tweak idea:\n```js\n// change the color\n```\n"
    extracted = build.extract_html(reply)
    assert extracted is not None
    assert extracted.rstrip().endswith("</html>")  # ends exactly at the document terminator
    assert "```" not in extracted  # trailing closing fence + js aside dropped as literal bytes
    assert "change the color" not in extracted


@pytest.mark.parametrize(
    "response",
    [
        "no code fence here at all",  # 0 openings
        "```html\n<a>x</a>\n```\n```html\n<b>y</b>\n```\n",  # >1 openings
        "```html\n<a>x</a>\n",  # opening but no closing fence
        "```html\n\n```\n",  # empty body
    ],
)
def test_extract_html_rejects_zero_multiple_and_unterminated_fences(response: str) -> None:
    assert build.extract_html(response) is None


def test_no_fence_reply_triggers_the_fence_specific_repair(
    episode_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _seam(monkeypatch, ["Sure! Here is your toy, but with no code fence.", _golden_reply()])
    assert main(["build", "001"]) == EXIT_OK
    assert len(_prompts(calls)) == 2  # fence failure consumed shot 1, golden committed on shot 2
    assert "```html fences" in _prompts(calls)[1]  # the fence-specific evidence template


def test_assemble_prompt_substitutes_every_field_and_leaves_no_placeholder() -> None:
    brief = make_brief()
    prompt = build.assemble_prompt(REAL_CONTRACT.read_text(encoding="utf-8"), brief)
    for field in build._CONTRACT_FIELDS:
        assert "{" + field + "}" not in prompt
    assert brief.one_sentence_goal in prompt
    assert brief.kid_quote in prompt
    assert ", ".join(brief.must_haves) in prompt  # must_haves rendered as a comma-joined list


def test_near_identical_flags_a_stuck_repair_but_not_a_real_change() -> None:
    assert build.near_identical(BROKEN_ALERT, BROKEN_ALERT)  # identical -> stuck
    assert build.near_identical(BROKEN_ALERT, BROKEN_ALERT + "\n\n\n")  # only whitespace differs
    assert not build.near_identical(BROKEN_ALERT, BROKEN_FETCH)  # genuinely different toys


def test_index_placeholder_sentinel_stays_in_the_scaffold() -> None:
    """Drift guard: build's clobber gate keys on the sentinel templates.py renders."""
    rendered = templates.render_index_html_placeholder(title="T", episode_id="001-t")
    assert templates.INDEX_HTML_PLACEHOLDER_SENTINEL in rendered
