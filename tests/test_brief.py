"""brief.py tests (Step 7): vocabulary, the ONE parse/write pair, distill flow, CLI wiring.

The claude boundary is ALWAYS mocked (tests/test_drafting.py house style):

- **In-process:** monkeypatch the seam functions (``drafting.ensure_claude_ready`` /
  ``drafting.call_claude``) with scripted replies — brief.py calls both through the
  ``drafting`` module attribute, so the patch lands.
- **Subprocess:** a fake ``claude`` shim on PATH (mirrors test_drafting's
  ``_write_shim``) for the integration test through the production ``python -m cwp``
  entry point.

House style otherwise holds: real tmp_path repos, real episode folders.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from cwp import brief as brief_module
from cwp import capture, drafting, episodes
from cwp.brief import (
    MUST_HAVE_VOCABULARY,
    PANTSLESS_CRITERIA,
    Brief,
    BriefFormatError,
    BriefNotFoundError,
    load_brief,
    must_have_problem,
    split_must_have,
    validate_must_have,
    write_brief,
)
from cwp.cli import EXIT_ENV_ERROR, EXIT_OK, EXIT_USER_ERROR, main

EPISODE_TITLE = "FizzBuzz, But It's a Dinosaur"
TRANSCRIPT = (
    "I want the dinosaur to go woah weally woud when I push the big button"
    " and it counts all the woars"
)

# A realistic model reply: preamble before the fence (lenient extraction must cope),
# a TOML literal string for the selector entry, and a prose paragraph after the fence.
VALID_REPLY = """Here is the distilled brief:

```toml
one_sentence_goal = "A giant button that makes a dinosaur roar and counts every roar."
single_action = "smash the big roar button"
visual_motif = "dinosaur"
must_haves = [
    "visible:dinosaur",
    'element:[data-testid="main-action"]',
    "sound_on_action",
    "state_change:data-action-count",
]
kid_quote = "make the dinosaur go woah weally woud"
kid_nickname = "the kid"

[pantsless]
can_start_unaided = true
understands_goal = true
cant_break_it = true
enjoys_it = true
```

He wants one huge friendly button that roars back every time.
"""

# Same reply with ONE out-of-vocabulary must_have — the re-ask trigger.
INVALID_REPLY = VALID_REPLY.replace('"sound_on_action",', '"confetti_everywhere",')


def make_brief(**overrides: object) -> Brief:
    """A schema-valid Brief; keyword overrides poke individual fields."""
    values: dict[str, object] = {
        "one_sentence_goal": "A cookie splitter that is always fair.",
        "single_action": "press the split button",
        "visual_motif": "cookie 🍪",
        "must_haves": (
            "visible:🍪",
            "sound_on_action",
            "state_change:data-action-count",
        ),
        "kid_quote": "cut the tookie the fair way",
        "kid_nickname": "Buddy",
        "pantsless": {name: True for name in PANTSLESS_CRITERIA},
        "prose": "Two cookie halves.\nBoth provably equal — even with 🦖 supervision.",
    }
    values.update(overrides)
    return Brief(**values)  # type: ignore[arg-type]


# --- fixtures ---


@pytest.fixture(autouse=True)
def cold_preflight_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with a cold per-process preflight cache."""
    monkeypatch.setattr(drafting, "_preflight_passed", False)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repo root (pyproject marker) with cwd inside it."""
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def episode_dir(repo: Path) -> Path:
    """Episode 001 with a captured (already-redacted-at-capture-time) transcript."""
    created = episodes.create_episode(
        repo / "episodes",
        EPISODE_TITLE,
        hook="Five clean lines, then a counting toy that roars.",
        teaches="fizzbuzz",
    )
    (created.directory / "capture" / capture.TRANSCRIPT_FILENAME).write_text(
        TRANSCRIPT + "\n", encoding="utf-8"
    )
    return created.directory


def _seam(monkeypatch: pytest.MonkeyPatch, replies: list[str]) -> list[str]:
    """Scripted in-process seam: reply k answers call k; returns the prompts seen."""
    prompts: list[str] = []

    def fake_ready(*, timeout: float | None = None) -> None:
        return None

    def fake_call(prompt: str, *, timeout: float, partial_path: Path | None = None) -> str:
        prompts.append(prompt)
        return replies[len(prompts) - 1]

    monkeypatch.setattr(drafting, "ensure_claude_ready", fake_ready)
    monkeypatch.setattr(drafting, "call_claude", fake_call)
    return prompts


# --- the fake claude shim (mirrors tests/test_drafting.py's PATH-resolvable double) ---


def _write_shim(shim_dir: Path, body: str) -> None:
    """A fake ``claude`` on PATH: ``claude.cmd`` (Windows) + ``claude`` sh (POSIX)."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    script = shim_dir / "claude_shim.py"
    script.write_text(body, encoding="utf-8")
    (shim_dir / "claude.cmd").write_text(
        f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
    )
    posix = shim_dir / "claude"
    posix.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    posix.chmod(0o755)


def _reply_shim_body(reply: str) -> str:
    """A shim that answers EVERY call (preflight probe + distill) with *reply*."""
    return f"import sys\nsys.stdin.read()\nsys.stdout.write({reply!r})\n"


# --- the closed vocabulary (validator + structured constant) ---


@pytest.mark.parametrize(
    "entry",
    [
        "visible:🦖",
        "visible:dinosaur",
        'element:[data-testid="main-action"]',
        "element:#big-button",
        "element:a:hover",  # payload may itself contain colons (first-colon split)
        "sound_on_action",
        "state_change:data-action-count",
        "state_change:data-mood",
    ],
)
def test_vocabulary_accepts_every_appendix_c_form(entry: str) -> None:
    assert validate_must_have(entry)
    assert must_have_problem(entry) is None


@pytest.mark.parametrize(
    "entry",
    [
        "",
        "visible",  # payload required but missing
        "visible:",  # empty payload
        "visible:   ",  # whitespace-only payload
        "visible: x",  # padded payload
        " visible:x",  # padded prefix is not a known prefix
        "element:",
        "state_change",
        "sound_on_action:beep",  # takes no payload
        "sound_on_action:",
        "confetti_everywhere",  # not in the closed vocabulary
        ":payload",
        "VISIBLE:x",  # prefixes are case-sensitive (the closed vocabulary is exact)
    ],
)
def test_vocabulary_rejects_out_of_vocabulary_entries(entry: str) -> None:
    assert not validate_must_have(entry)
    assert must_have_problem(entry)  # and the problem string is non-empty


def test_split_must_have_splits_at_the_first_colon_only() -> None:
    assert split_must_have("element:a:hover") == ("element", "a:hover")
    assert split_must_have("sound_on_action") == ("sound_on_action", "")


def test_vocabulary_constant_shape_for_step_8_import() -> None:
    """Drift guard: verify.py (Step 8) imports this structured constant — pin its shape."""
    assert [predicate.prefix for predicate in MUST_HAVE_VOCABULARY] == [
        "visible",
        "element",
        "sound_on_action",
        "state_change",
    ]
    by_prefix = {predicate.prefix: predicate for predicate in MUST_HAVE_VOCABULARY}
    assert by_prefix["sound_on_action"].takes_payload is False
    for prefix in ("visible", "element", "state_change"):
        assert by_prefix[prefix].takes_payload is True
        assert by_prefix[prefix].payload_hint.startswith("<")


def test_pantsless_criteria_derive_from_the_meta_toml_gate() -> None:
    """Drift guard: the brief's [pantsless] keys ARE meta.toml's [pantsless_test] keys."""
    assert PANTSLESS_CRITERIA == (
        "can_start_unaided",
        "understands_goal",
        "cant_break_it",
        "enjoys_it",
    )


# --- the ONE parse/write pair ---


def test_write_then_load_round_trips_exactly(tmp_path: Path) -> None:
    original = make_brief()
    write_brief(tmp_path, original)
    content = (tmp_path / "brief.md").read_text(encoding="utf-8")
    assert content.splitlines()[0] == "```toml"  # Appendix C: the fence OPENS the file
    assert load_brief(tmp_path) == original
    assert not list(tmp_path.glob("*.tmp"))  # atomic write left no droppings


def test_write_then_load_round_trips_without_prose(tmp_path: Path) -> None:
    original = make_brief(prose="")
    write_brief(tmp_path, original)
    assert load_brief(tmp_path) == original


def test_write_brief_refuses_an_invalid_brief(tmp_path: Path) -> None:
    """The one writer can never produce a file its own loader rejects."""
    with pytest.raises(BriefFormatError, match="must_haves"):
        write_brief(tmp_path, make_brief(must_haves=("visible:a", "sound_on_action")))
    with pytest.raises(BriefFormatError, match="pantsless"):
        write_brief(tmp_path, make_brief(pantsless={"can_start_unaided": True}))
    assert not (tmp_path / "brief.md").exists()


def test_load_brief_missing_file_is_a_domain_error(tmp_path: Path) -> None:
    with pytest.raises(BriefNotFoundError, match="cwp brief"):
        load_brief(tmp_path)
    assert issubclass(BriefNotFoundError, episodes.EpisodeError)  # CLI maps it to exit 1


def test_load_brief_rejects_the_step_2_placeholder(episode_dir: Path) -> None:
    """cwp new seeds a placeholder brief.md — it has no fence, so it is not a brief."""
    with pytest.raises(BriefFormatError, match="frontmatter fence"):
        load_brief(episode_dir)


def test_load_brief_requires_the_fence_to_open_the_file(tmp_path: Path) -> None:
    valid = make_brief()
    write_brief(tmp_path, valid)
    shifted = "# prose first\n\n" + (tmp_path / "brief.md").read_text(encoding="utf-8")
    (tmp_path / "brief.md").write_text(shifted, encoding="utf-8")
    with pytest.raises(BriefFormatError, match="frontmatter fence"):
        load_brief(tmp_path)


def test_load_brief_invalid_toml_is_a_format_error(tmp_path: Path) -> None:
    (tmp_path / "brief.md").write_text("```toml\nthis is = = not toml\n```\n", encoding="utf-8")
    with pytest.raises(BriefFormatError, match="invalid TOML"):
        load_brief(tmp_path)


@pytest.mark.parametrize(
    ("mutate", "expect"),
    [
        (lambda text: text.replace('kid_quote = "cut the tookie the fair way"\n', ""), "kid_quote"),
        (lambda text: text.replace('"sound_on_action",', ""), "3-5 entries"),
        (
            lambda text: text.replace(
                '"sound_on_action",', '"sound_on_action", "a:1", "b:2", "c:3", "d:4",'
            ),
            "3-5 entries",
        ),
        (
            lambda text: text.replace('"sound_on_action",', '"confetti_everywhere",'),
            "confetti_everywhere",
        ),
        (lambda text: text.replace("enjoys_it = true\n", ""), "enjoys_it"),
        (lambda text: text.replace("enjoys_it = true", 'enjoys_it = "yes"'), "boolean"),
        (lambda text: text.replace("enjoys_it = true", "enjoys_it = true\nextra = true"), "extra"),
        (
            lambda text: text.replace(
                'kid_quote = "cut the tookie the fair way"', 'kid_quote = ""'
            ),
            "kid_quote",
        ),
    ],
)
def test_load_brief_reports_each_schema_violation(
    tmp_path: Path, mutate: Callable[[str], str], expect: str
) -> None:
    write_brief(tmp_path, make_brief(prose=""))
    text = (tmp_path / "brief.md").read_text(encoding="utf-8")
    mutated = mutate(text)
    assert mutated != text, "the mutation must actually change the file"
    (tmp_path / "brief.md").write_text(mutated, encoding="utf-8")
    with pytest.raises(BriefFormatError, match=expect):
        load_brief(tmp_path)


def test_load_brief_tolerates_unknown_top_level_keys(tmp_path: Path) -> None:
    """Forward compatibility: unknown TOP-LEVEL keys load; [pantsless] stays pinned."""
    write_brief(tmp_path, make_brief(prose=""))
    content = (tmp_path / "brief.md").read_text(encoding="utf-8")
    content = content.replace("```toml\n", '```toml\nfuture_field = "ok"\n')
    (tmp_path / "brief.md").write_text(content, encoding="utf-8")
    assert load_brief(tmp_path).one_sentence_goal == make_brief().one_sentence_goal


# --- the distill flow (in-process seam) ---


def test_run_brief_writes_a_brief_that_round_trips_via_its_own_loader(
    repo: Path,
    episode_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompts = _seam(monkeypatch, [VALID_REPLY])
    assert main(["brief", "001"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "brief.md" in captured.out
    assert "4 must-haves" in captured.out
    # No redact file → the §4.3 one-time unscanned notice, on stderr.
    assert capture.UNSCANNED_NOTICE in captured.err
    loaded = load_brief(episode_dir)
    assert loaded.must_haves  # non-empty, every entry vocabulary-form
    assert all(validate_must_have(entry) for entry in loaded.must_haves)
    assert loaded.kid_quote == "make the dinosaur go woah weally woud"
    assert loaded.kid_nickname == "the kid"  # no redact file → the default nickname
    assert all(loaded.pantsless[name] is True for name in PANTSLESS_CRITERIA)
    assert loaded.prose == "He wants one huge friendly button that roars back every time."
    # The distill prompt carried the transcript, the noise guidance, and the vocabulary.
    assert len(prompts) == 1
    prompt = prompts[0]
    assert TRANSCRIPT in prompt
    assert "25% word error rate" in prompt
    assert "verbatim" in prompt.lower()
    for predicate in MUST_HAVE_VOCABULARY:
        assert predicate.form in prompt
    assert "visible:🦖" in prompt  # a worked example made it in
    assert '"the kid"' in prompt


def test_run_brief_redacts_and_takes_nickname_from_the_redact_file(
    repo: Path,
    episode_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """First nickname in private/redact-names.txt wins (over the model's own value),
    and every free-text field is redact-scanned before the write."""
    private = repo / "private"
    private.mkdir()
    (private / "redact-names.txt").write_text("Kai = Buddy\n", encoding="utf-8")
    named_reply = VALID_REPLY.replace(
        'kid_quote = "make the dinosaur go woah weally woud"',
        'kid_quote = "Kai says make it go woah"',
    ).replace("He wants one huge", "Kai wants one huge")
    _seam(monkeypatch, [named_reply])
    assert main(["brief", "001"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "name(s) redacted" in captured.out
    assert capture.UNSCANNED_NOTICE not in captured.err
    written = (episode_dir / "brief.md").read_text(encoding="utf-8")
    assert "Kai" not in written  # the real name never reaches disk (§4.3)
    loaded = load_brief(episode_dir)
    assert loaded.kid_quote == "Buddy says make it go woah"
    assert loaded.prose.startswith("Buddy wants one huge")
    # The reply said "the kid"; the redact file's nickname is authoritative.
    assert loaded.kid_nickname == "Buddy"


def test_out_of_vocabulary_must_have_triggers_exactly_one_reask(
    repo: Path,
    episode_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompts = _seam(monkeypatch, [INVALID_REPLY, VALID_REPLY])
    assert main(["brief", "001"]) == EXIT_OK
    assert len(prompts) == 2
    # The re-ask is the original prompt + the exact validation errors appended.
    assert prompts[1].startswith(prompts[0])
    assert "Correction required" in prompts[1]
    assert "confetti_everywhere" in prompts[1]
    assert "re-asked once" in capsys.readouterr().err
    assert load_brief(episode_dir).kid_quote  # the valid second reply landed


@pytest.mark.parametrize(
    "bad_reply",
    [
        INVALID_REPLY,  # out-of-vocabulary must_have
        "I'm sorry, I can't produce TOML today.",  # no parseable TOML at all
    ],
)
def test_two_bad_replies_exit_2_and_leave_the_placeholder_untouched(
    repo: Path,
    episode_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bad_reply: str,
) -> None:
    before = (episode_dir / "brief.md").read_text(encoding="utf-8")
    prompts = _seam(monkeypatch, [bad_reply, bad_reply])
    assert main(["brief", "001"]) == EXIT_ENV_ERROR
    assert len(prompts) == 2  # one call + exactly one re-ask, never more
    err = capsys.readouterr().err
    assert "re-ask" in err
    # brief.md untouched; the rejected reply is flushed for inspection.
    assert (episode_dir / "brief.md").read_text(encoding="utf-8") == before
    partial = episode_dir / "brief.partial.txt"
    assert partial.read_text(encoding="utf-8") == bad_reply
    assert str(partial) in err


def test_fenceless_toml_reply_is_accepted_leniently(
    repo: Path, episode_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reply that is bare TOML (model dropped the fence) still parses — no re-ask."""
    start = VALID_REPLY.index("one_sentence_goal")
    end = VALID_REPLY.index("```", start)
    prompts = _seam(monkeypatch, [VALID_REPLY[start:end]])
    assert main(["brief", "001"]) == EXIT_OK
    assert len(prompts) == 1
    assert load_brief(episode_dir).prose == ""  # nothing after a fence that isn't there


def test_missing_transcript_is_a_user_error_exit_1(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    episodes.create_episode(repo / "episodes", "No Capture Yet")
    _seam(monkeypatch, [])  # any claude call would IndexError — none may happen
    assert main(["brief", "001"]) == EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert "transcript.txt" in err
    assert "cwp capture" in err  # the fix-it points at Step 6's command


def test_empty_transcript_is_a_user_error_exit_1(
    repo: Path,
    episode_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (episode_dir / "capture" / capture.TRANSCRIPT_FILENAME).write_text("   \n", encoding="utf-8")
    _seam(monkeypatch, [])
    assert main(["brief", "001"]) == EXIT_USER_ERROR
    assert "is empty" in capsys.readouterr().err


def test_unknown_episode_is_a_user_error_exit_1(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["brief", "999"]) == EXIT_USER_ERROR
    assert "No episode matching" in capsys.readouterr().err


def test_dry_run_prints_the_prompt_and_never_touches_claude(
    repo: Path,
    episode_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("claude must not be touched on --dry-run")

    monkeypatch.setattr(drafting, "preflight", boom)
    monkeypatch.setattr(drafting, "ensure_claude_ready", boom)
    monkeypatch.setattr(drafting, "call_claude", boom)
    before = (episode_dir / "brief.md").read_text(encoding="utf-8")
    assert main(["brief", "001", "--dry-run"]) == EXIT_OK
    out = capsys.readouterr().out
    assert TRANSCRIPT in out
    assert "must_haves vocabulary" in out
    assert (episode_dir / "brief.md").read_text(encoding="utf-8") == before


def test_claude_env_failures_map_to_exit_2(
    repo: Path,
    episode_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """drafting's own env errors (missing binary, timeout, ...) surface as exit 2."""

    def not_found(**kwargs: object) -> None:
        raise drafting.ClaudeNotFoundError("Claude CLI not found — install it")

    monkeypatch.setattr(drafting, "ensure_claude_ready", not_found)
    assert main(["brief", "001"]) == EXIT_ENV_ERROR
    assert "Claude CLI not found" in capsys.readouterr().err


# --- integration through the production entry point (fake shim on PATH) ---


def test_production_cli_with_fake_shim_writes_a_loadable_brief(
    repo: Path, episode_dir: Path, tmp_path: Path
) -> None:
    """The acceptance target: `cwp brief <id>` end-to-end with a fake claude shim —
    the written brief.md round-trips via brief.py's own loader."""
    shim_dir = tmp_path / "shims"
    _write_shim(shim_dir, _reply_shim_body(VALID_REPLY))
    env = dict(os.environ)
    env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
    result = subprocess.run(
        [sys.executable, "-m", "cwp", "brief", "001"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo,
        env=env,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "brief.md" in result.stdout
    loaded = load_brief(episode_dir)
    assert loaded.must_haves and loaded.kid_quote  # non-empty, per the acceptance target
    assert all(validate_must_have(entry) for entry in loaded.must_haves)


def test_module_exports_the_seam_step_8_and_9_import() -> None:
    """Drift guard: the names verify.py/build.py are promised (plan §7 module table)."""
    for name in ("load_brief", "write_brief", "MUST_HAVE_VOCABULARY", "validate_must_have"):
        assert hasattr(brief_module, name), f"brief.{name} missing"
