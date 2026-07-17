"""End-to-end smoke gate (plan.md §14 Step 11): both loops through the production CLI.

This is the producer->consumer + external-subprocess smoke gate the plan calls for
(audio -> whisper -> brief -> claude -> html -> playwright). Per-module mocked tests
cannot see cross-module drift (a producer/consumer format mismatch, an exit-code
inconsistency); driving the WHOLE pipeline through the real ``cwp`` entry point on a
throwaway ``tmp_path`` repo, with ONLY the two external boundaries mocked, can.

Both boundaries are the ones the plan names, and nothing else is faked:

- **claude** — the fake-claude that returns the RIGHT artifact per call site (a real
  ``draft`` reply is an in-voice script; a ``brief`` reply is a valid TOML brief; a
  ``build`` reply is the golden toy in a ```` ```html ```` fence). The Channel Loop drives
  it through the REAL subprocess seam via a ``claude`` shim on PATH (``test_drafting``'s
  ``_write_shim`` shape); the Pantsless Build drives it through the in-process seam
  (``drafting.call_claude`` / ``ensure_claude_ready``) because capture's whisper seam
  cannot be mocked across a subprocess boundary (``test_capture`` documents this).
- **whisper** — ``capture.transcribe_audio`` returns a canned :class:`TranscriptResult`.

Everything else is REAL: episodes/lifecycle/publishing/brief/verify (+ real Chromium),
the atomic writes, and the derived index. Two production entry points are exercised:
``python -m cwp`` (Channel Loop) and ``cwp.cli.main`` (Pantsless Build).

The brief's ``must_haves`` are derived HONESTLY from what ``tests/fixtures/golden.html``
actually renders (so build's REAL verify passes on the golden toy given that brief), and
they are exactly the set ``test_build.make_brief`` proves the golden satisfies — the
brief->verify handoff is honest, not rigged. The existing golden fixture is reused, never
forked.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import tomli_w

from cwp import brief as brief_module
from cwp import capture, drafting, episodes, templates
from cwp.brief import PANTSLESS_CRITERIA, validate_must_have
from cwp.capture import Segment, TranscriptResult
from cwp.cli import EXIT_OK, main
from cwp.drafting import AI_DRAFT_MARKER
from cwp.lifecycle import PUBLISHED_STATUS

WORKTREE_ROOT = Path(__file__).parents[1]
REAL_CONTRACT = WORKTREE_ROOT / "build-contract.md"
GOLDEN = WORKTREE_ROOT / "tests" / "fixtures" / "golden.html"
FIXTURE_WAV = WORKTREE_ROOT / "tests" / "fixtures" / "hello.wav"

# --- the fake-claude artifacts (right shape per call site) --------------------------------------

# A minimal channel voice.md — draft reads it; the fake claude ignores its content.
VOICE = "# Voice\n\nCalm. A little absurd. One small useful thing per video.\n"

# The in-voice script the fake claude returns for `cwp draft <id> script` (drafting.py
# prepends the AI-draft marker and writes it wholesale to script.md).
CHANNEL_SCRIPT = (
    "## Hook\n\n"
    "It guesses your number. It never loses. Watch.\n\n"
    "## Script\n\n"
    "Calm as ever, we build one small useful thing: a number-guessing machine that\n"
    "wins by halving the range every time.\n\n"
    "## On-screen actions\n\n"
    "- Type a number; the machine narrows in.\n"
    "- The pantsless co-star pushes the big button at the end.\n"
)

# A realistic local-whisper transcript of a kid clip (canned; ASCII, >2 words, healthy
# logprob so no re-record hint noise).
CANNED_TRANSCRIPT = (
    "I want a giant dinosaur button that roars really loud and counts every single roar"
)

# The brief must_haves, DERIVED from what golden.html actually renders (read the fixture):
#   - visible:<T-rex>  -> golden's button sprite is &#129430; == U+1F996 (fixtures/golden.html)
#   - element:[data-testid="main-action"] -> golden's one primary action element
#   - sound_on_action  -> golden constructs Web Audio INSIDE the click handler
#   - state_change:data-mood -> golden toggles data-mood calm<->roar on each click
# This is exactly the set tests/test_build.make_brief proves the golden verify-passes.
GOLDEN_MUST_HAVES = (
    "visible:\U0001f996",
    'element:[data-testid="main-action"]',
    "sound_on_action",
    "state_change:data-mood",
)

# The TOML brief the fake claude returns for `cwp brief`. Built via tomli_w so the emoji
# and the quote-bearing selector serialize correctly; pantsless is the only table, so it
# lands last (brief.py's parser requires scalars before the table).
_BRIEF_DOCUMENT: dict[str, object] = {
    "one_sentence_goal": "A giant dinosaur button that roars and counts every roar.",
    "single_action": "smash the big roar button",
    "visual_motif": "dinosaur",
    "must_haves": list(GOLDEN_MUST_HAVES),
    "kid_quote": "make the dinosaur go woah weally woud",
    "kid_nickname": "the kid",
    "pantsless": {name: True for name in PANTSLESS_CRITERIA},
}


def _brief_reply() -> str:
    """A realistic model reply: preamble, the fenced TOML brief, then a prose paragraph."""
    return (
        "Here is the distilled brief:\n\n"
        f"```toml\n{tomli_w.dumps(_BRIEF_DOCUMENT)}```\n\n"
        "One giant friendly dinosaur that roars back on every single press.\n"
    )


def _golden_html_reply() -> str:
    """The golden toy in the single ```html fence a real claude build reply carries."""
    return f"```html\n{GOLDEN.read_text(encoding='utf-8').strip()}\n```\n"


# --- fixtures -----------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def cold_preflight_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with a cold per-process preflight cache (house style)."""
    monkeypatch.setattr(drafting, "_preflight_passed", False)


# --- Channel Loop: new -> draft -> status -> publish, through `python -m cwp` --------------------

# The fake claude shim body: dispatch on the piped prompt. The preflight probe
# ("Reply with exactly: ok") gets "ok"; any other prompt is the draft prompt -> the script.
_SHIM_BODY = """\
import sys
prompt = sys.stdin.read()
if "Reply with exactly: ok" in prompt:
    sys.stdout.write("ok\\n")
else:
    sys.stdout.write(__SCRIPT__)
"""


def _write_shim(shim_dir: Path) -> None:
    """A fake ``claude`` on PATH: ``claude.cmd`` (Windows) + ``claude`` sh (POSIX)."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    script = shim_dir / "claude_shim.py"
    script.write_text(_SHIM_BODY.replace("__SCRIPT__", repr(CHANNEL_SCRIPT)), encoding="utf-8")
    (shim_dir / "claude.cmd").write_text(
        f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
    )
    posix = shim_dir / "claude"
    posix.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    posix.chmod(0o755)


def _run_cwp(args: list[str], repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cwp", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=180,
    )


@pytest.mark.timeout(180)
def test_channel_loop_end_to_end_through_python_dash_m_cwp(tmp_path: Path) -> None:
    """Acceptance bar 1: ``new -> draft(fake claude) -> status -> publish`` through the real
    ``python -m cwp`` process, with the claude subprocess seam exercised via a PATH shim."""
    repo = tmp_path / "channel"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (repo / "voice.md").write_text(VOICE, encoding="utf-8")
    shim_dir = tmp_path / "shims"
    _write_shim(shim_dir)
    env = dict(os.environ)
    env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")

    # new: episode folder + valid meta.toml (idea status).
    created = _run_cwp(
        [
            "new",
            "The Number-Guessing Machine",
            "--hook",
            "It guesses your number. It never loses.",
            "--teaches",
            "binary search",
            "--tags",
            "neetcode,kids",
        ],
        repo,
        env,
    )
    assert created.returncode == EXIT_OK, created.stderr
    episode_dir = repo / "episodes" / "001-the-number-guessing-machine"
    assert (episode_dir / "meta.toml").is_file()
    _directory, episode = episodes.load_episode(repo / "episodes", "001")  # valid meta round-trips
    assert episode.status == "idea"
    assert episode.tags == ["neetcode", "kids"]

    # draft(script): the fake claude writes marked content into script.md.
    drafted = _run_cwp(["draft", "001", "script"], repo, env)
    assert drafted.returncode == EXIT_OK, drafted.stderr
    script = (episode_dir / "script.md").read_text(encoding="utf-8")
    assert script.splitlines()[0] == AI_DRAFT_MARKER  # marker is the FIRST line
    assert "## Hook" in script and "number-guessing machine" in script

    # status: advance the lifecycle (a history row is recorded).
    advanced = _run_cwp(["status", "001", "scripted"], repo, env)
    assert advanced.returncode == EXIT_OK, advanced.stderr
    _d2, episode = episodes.load_episode(repo / "episodes", "001")
    assert episode.status == "scripted"
    assert [entry.status for entry in episode.history] == ["idea", "scripted"]

    # publish --url: ordered Studio paste block + youtube_url + published_at + published.
    url = "https://youtu.be/dQw4w9WgXcQ"
    published = _run_cwp(["publish", "001", "--url", url], repo, env)
    assert published.returncode == EXIT_OK, published.stderr
    publish_md = (episode_dir / "publish.md").read_text(encoding="utf-8")
    order = [publish_md.index(f"## {field}") for field in ("Title", "Description", "Tags")]
    order.append(publish_md.index("## Thumbnail text"))
    assert order == sorted(order), f"Studio paste block out of order: {order}"
    _d3, episode = episodes.load_episode(repo / "episodes", "001")
    assert episode.status == PUBLISHED_STATUS
    assert episode.youtube_url == url
    assert episode.published_at  # stamped on the transition to published

    # The DERIVED index (`cwp list`) reflects the final state — no folder/index drift.
    listed = _run_cwp(["list"], repo, env)
    assert listed.returncode == EXIT_OK, listed.stderr
    assert "001-the-number-guessing-machine" in listed.stdout
    assert PUBLISHED_STATUS in listed.stdout

    # Clean throughout: atomic writes leave no temp/partial droppings behind, in any subdir
    # (mkstemp writes beside the target — script.md/publish.md sit a dir deep in the episode).
    assert not list(episode_dir.rglob("*.tmp"))
    assert not list(episode_dir.rglob("*.partial.txt"))


# --- Pantsless Build: capture -> brief -> build (+ REAL verify), through cli.main ----------------


# Timing tradeoff (documented, not a test bug): this half runs verify.py's REAL headless
# checks under Chromium, gated by verify's PRODUCTION timeout budgets (the 2s/10s in-page
# step limits that gate real kid-facing toys). Those budgets are deliberately NOT loosened
# here — weakening them would weaken what the gate proves. The happy path is engineered so a
# transient timing hiccup CANNOT convert into a deterministic red via build.py's near-identical
# repair guard: the fake claude returns a passing golden on the FIRST build attempt, so a single
# verify pass commits with NO repair cycle at all (asserted below — exactly one build-generate
# call). A repair (and thus the byte-identical-golden near-identical trap) can only fire if the
# FIRST verify genuinely fails. Therefore a red in this half means either real drift (the gate
# working) OR severe CPU starvation blowing verify's real production budget — in the latter case,
# re-run on an unloaded machine; it is not a test defect.
@pytest.mark.timeout(180)
def test_pantsless_build_end_to_end_through_cli_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance bar 2: ``capture(canned) -> brief(fake claude) -> build(fake claude -> golden)``
    with build's verify.py + REAL Chromium actually running on the golden toy and passing.

    Only the two external boundaries are mocked: the whisper seam (canned transcript) and
    the in-process claude seam (dispatched to the right artifact per call site). Everything
    downstream — the transcript->brief->build->verify producer/consumer chain — is real.
    """
    repo = tmp_path / "pantsless"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (repo / "build-contract.md").write_text(
        REAL_CONTRACT.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.chdir(repo)

    # Boundary 1 — whisper: a canned transcript (healthy confidence, >2 words).
    def fake_transcribe(audio_path: Path, model_size: str) -> TranscriptResult:
        return TranscriptResult(
            text=CANNED_TRANSCRIPT,
            segments=(Segment(text=CANNED_TRANSCRIPT, avg_logprob=-0.3),),
        )

    monkeypatch.setattr(capture, "transcribe_audio", fake_transcribe)

    # Boundary 2 — claude: dispatch to the right artifact per call site.
    claude_prompts: list[str] = []

    def fake_ready(*, timeout: float | None = None) -> None:
        return None

    def fake_call(prompt: str, *, timeout: float, partial_path: Path | None = None) -> str:
        claude_prompts.append(prompt)
        if "You distill what a 4-year-old asked for" in prompt:  # brief distill prompt
            return _brief_reply()
        if "Build ONE self-contained browser toy" in prompt:  # build-contract.md prompt
            return _golden_html_reply()
        raise AssertionError(f"unexpected claude prompt (first 120 chars): {prompt[:120]!r}")

    monkeypatch.setattr(drafting, "ensure_claude_ready", fake_ready)
    monkeypatch.setattr(drafting, "call_claude", fake_call)

    # new (production entry) — the episode to build into.
    assert main(["new", "Dino Roar Button", "--hook", "One button. It roars."]) == EXIT_OK
    episode_dir = repo / "episodes" / "001-dino-roar-button"
    assert episode_dir.is_dir()

    # capture: the canned transcript is written (redaction-scanned — no redact file => no-op).
    assert main(["capture", "001", "--audio", str(FIXTURE_WAV)]) == EXIT_OK
    transcript = (episode_dir / "capture" / "transcript.txt").read_text(encoding="utf-8")
    assert transcript == CANNED_TRANSCRIPT + "\n"

    # brief: a valid brief.md whose frontmatter round-trips via load_brief, with
    # vocabulary-form must_haves + a kid_quote.
    assert main(["brief", "001"]) == EXIT_OK
    loaded = brief_module.load_brief(episode_dir)
    assert loaded.must_haves == GOLDEN_MUST_HAVES
    assert all(validate_must_have(entry) for entry in loaded.must_haves)
    assert loaded.kid_quote == "make the dinosaur go woah weally woud"
    assert loaded.kid_nickname == "the kid"  # force-set from the (absent) redact file default

    # build: verify.py + REAL Chromium run on the golden toy and PASS; the verified golden
    # commits to project/index.html and a pass is logged to .repair/log.jsonl.
    assert main(["build", "001"]) == EXIT_OK
    index = episode_dir / "project" / "index.html"
    committed = index.read_text(encoding="utf-8")
    assert committed.strip() == GOLDEN.read_text(encoding="utf-8").strip()  # the VERIFIED golden
    assert templates.INDEX_HTML_PLACEHOLDER_SENTINEL not in committed  # not the scaffold

    log = episode_dir / "project" / ".repair" / "log.jsonl"
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert rows and all(row["passed"] for row in rows)  # a full pass logged
    assert all(row["attempt"] == 1 for row in rows)  # committed on the first shot
    checks = {row["check"] for row in rows}
    # These check ids exist ONLY if the real browser ran and the brief compiled — proof
    # that verify.py (real Chromium) actually gated the golden toy, not a stubbed pass.
    assert "headless:page-load" in checks
    assert "headless:audio-running-after-click" in checks
    assert any(check.startswith("must-have:") for check in checks)
    assert (episode_dir / "project" / ".repair" / "attempt-1.png").is_file()  # real screenshot

    # Exactly two claude calls (brief distill + build generate), no re-ask, no extra shots —
    # and EXACTLY ONE build-generate call, so the golden committed on attempt 1 with NO repair
    # cycle. This is what keeps the byte-identical-golden near-identical trap unreachable in the
    # green case (a repair would be a 2nd build-contract prompt, failing this assertion loudly).
    assert len(claude_prompts) == 2
    build_calls = sum("Build ONE self-contained browser toy" in p for p in claude_prompts)
    assert build_calls == 1, f"expected a single first-shot build call, saw {build_calls} (repair?)"

    # No folder/index drift: the derived scan sees the one built episode, no temp droppings in
    # ANY subdir (atomic writes drop temp files a dir deeper — capture/, project/, .repair/).
    assert not list(episode_dir.rglob("*.tmp"))
    scan = episodes.scan_episodes(repo / "episodes")
    assert [ep.id for ep in scan.episodes] == ["001-dino-roar-button"]
