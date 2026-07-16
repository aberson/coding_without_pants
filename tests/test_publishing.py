"""publishing.py tests (Step 5): Studio ordering, folding, validation, --url, checklist.

House style: real tmp_path repos, real TOML round trips, and a subprocess integration
test through the production ``python -m cwp`` entry point. The ONLY mock is Step 4's
claude seam — drafted content is produced by the REAL ``cwp draft`` code path with the
seam monkeypatched, so the fold tests are a true producer → consumer round trip
(code-quality: the bug lives in the relationship, not in either endpoint).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cwp import drafting, episodes, templates
from cwp.cli import EXIT_OK, EXIT_USER_ERROR, main
from cwp.drafting import AI_DRAFT_MARKER, publish_draft_heading
from cwp.episodes import read_meta
from cwp.publishing import (
    STUDIO_FIELDS,
    THUMBNAIL_MAX_CHARS,
    derive_thumbnail_text,
    extract_drafted_sections,
)

TITLE = "The Number-Guessing Machine (Binary Search, No Cheating)"
HOOK = "It guesses your number. It never loses."
TEACHES = "binary search"
URL = "https://youtu.be/abc123XYZ00"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repo root (pyproject marker + voice.md for the draft producer) with
    cwd inside it — the CLI resolves ``episodes/`` from cwd; the real repo is never hit."""
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "voice.md").write_text("# Voice\n\nCalm, a little absurd.\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def episode_dir(repo: Path) -> Path:
    created = episodes.create_episode(
        repo / "episodes", TITLE, hook=HOOK, teaches=TEACHES, tags=["neetcode", "kids"]
    )
    return created.directory


def _draft(monkeypatch: pytest.MonkeyPatch, kind: str, text: str) -> None:
    """Run the REAL ``cwp draft`` producer with only the claude seam faked."""
    monkeypatch.setattr(drafting, "ensure_claude_ready", lambda **kwargs: None)
    monkeypatch.setattr(drafting, "call_claude", lambda prompt, *, timeout, partial_path=None: text)
    assert main(["draft", "001", kind]) == EXIT_OK


def _headings(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("## ")]


def _section(text: str, heading: str) -> str:
    """Body of one ``## <heading>`` section (to the next ``##`` heading or ``---`` rule)."""
    lines = text.splitlines()
    start = lines.index(f"## {heading}") + 1  # ValueError = section missing, loud
    body: list[str] = []
    for line in lines[start:]:
        if line.startswith("## ") or line.strip() == "---":
            break
        body.append(line)
    return "\n".join(body).strip()


def _publish_text(episode_dir: Path) -> str:
    return (episode_dir / "publish.md").read_text(encoding="utf-8")


STUDIO_HEADINGS = [f"## {name}" for name in STUDIO_FIELDS]


# --- ordering + assembly (cwp publish without --url) ---


def test_publish_writes_studio_ordered_block_and_prints_it(
    episode_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["publish", "001"]) == EXIT_OK
    content = _publish_text(episode_dir)
    assert _headings(content)[: len(STUDIO_HEADINGS)] == STUDIO_HEADINGS  # file order
    out = capsys.readouterr().out
    assert _headings(out)[: len(STUDIO_HEADINGS)] == STUDIO_HEADINGS  # stdout block order
    assert _section(content, "Title") == TITLE
    assert HOOK in _section(content, "Description")
    assert f"What you'll learn: {TEACHES}" in _section(content, "Description")
    assert _section(content, "Tags") == "neetcode, kids"
    assert _section(content, "Thumbnail text") == "The Number-Guessing Machine"
    assert "wrote" not in out  # stdout stays paste-clean; file note goes to stderr


def test_publish_without_url_does_not_transition_status(episode_dir: Path) -> None:
    assert main(["publish", "001"]) == EXIT_OK
    episode = read_meta(episode_dir / "meta.toml")
    assert episode.status == "idea"
    assert episode.published_at == ""
    assert episode.youtube_url == ""
    assert [entry.status for entry in episode.history] == ["idea"]


def test_regeneration_removes_sentinel_so_draft_goes_stdout_only(
    episode_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Producer/consumer handoff: once publish owns the file, cwp draft stops appending."""
    assert main(["publish", "001"]) == EXIT_OK
    content = _publish_text(episode_dir)
    assert templates.PUBLISH_PLACEHOLDER_SENTINEL not in content
    capsys.readouterr()
    _draft(monkeypatch, "title", "A new candidate")
    assert "stdout only" in capsys.readouterr().err
    assert _publish_text(episode_dir) == content  # untouched


# --- the unconditional "Before you publish" checklist ---


def test_checklist_prints_after_the_block_and_is_embedded(
    episode_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["publish", "001"]) == EXIT_OK
    captured = capsys.readouterr()
    for needle in ("Before you publish", "Made for Kids", "Real-name scan"):
        assert needle in captured.out
        assert needle in _publish_text(episode_dir)
    assert captured.out.index("## Title") < captured.out.index("Before you publish")


def test_checklist_prints_even_when_fields_are_missing(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["idea", "Just a thought"]) == EXIT_OK
    capsys.readouterr()
    assert main(["publish", "001"]) == EXIT_OK
    out = capsys.readouterr().out
    for needle in ("Before you publish", "Made for Kids", "Real-name scan"):
        assert needle in out


# --- validation: warn, don't block ---


def test_missing_fields_warn_but_publish_still_writes(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["idea", "Just a thought"]) == EXIT_OK  # no hook/teaches/tags
    capsys.readouterr()
    assert main(["publish", "001"]) == EXIT_OK
    err = capsys.readouterr().err
    assert "missing description" in err
    assert "missing tags" in err
    assert "missing title" not in err
    assert "missing thumbnail" not in err
    content = (repo / "episodes" / "001-just-a-thought" / "publish.md").read_text(encoding="utf-8")
    assert _headings(content)[: len(STUDIO_HEADINGS)] == STUDIO_HEADINGS  # every section written
    assert _section(content, "Title") == "Just a thought"
    assert _section(content, "Description") == ""
    assert _section(content, "Tags") == ""
    assert _section(content, "Thumbnail text") == "Just a thought"


def test_every_field_missing_warns_each_and_still_exits_0(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    created = episodes.create_episode(repo / "episodes", "Temp")
    episode = created.episode
    episode.title = ""  # hand-edit: §4.1 permissive reads make this real
    episodes.write_meta(created.directory, episode)
    assert main(["publish", "001"]) == EXIT_OK
    err = capsys.readouterr().err
    for needle in ("missing title", "missing description", "missing tags", "missing thumbnail"):
        assert needle in err
    assert (created.directory / "publish.md").exists()


# --- folding drafted content (real producer → real consumer round trip) ---


def test_drafted_description_wins_over_derived_and_marker_warns(
    episode_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    drafted = "We build a guessing machine.\n\nWhat you'll learn: halving a haystack."
    _draft(monkeypatch, "description", drafted)
    capsys.readouterr()
    assert main(["publish", "001"]) == EXIT_OK
    captured = capsys.readouterr()
    content = _publish_text(episode_dir)
    assert _section(content, "Description") == drafted
    assert HOOK not in _section(content, "Description")  # drafted WINS, not merged
    assert "review AI-drafted content" in captured.err
    # The drafted source block is preserved (marker + heading) below the paste block.
    assert publish_draft_heading("description") in content
    assert AI_DRAFT_MARKER in content


def test_drafted_title_first_candidate_becomes_the_title(
    episode_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _draft(monkeypatch, "title", "Guess My Number Forever\nSecond Candidate\nThird One")
    assert main(["publish", "001"]) == EXIT_OK
    assert _section(_publish_text(episode_dir), "Title") == "Guess My Number Forever"


def test_latest_draft_block_wins(
    episode_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _draft(monkeypatch, "description", "First draft.")
    _draft(monkeypatch, "description", "Second, better draft.")
    assert main(["publish", "001"]) == EXIT_OK
    content = _publish_text(episode_dir)
    assert _section(content, "Description") == "Second, better draft."
    assert "First draft." not in content


def test_drafted_content_with_rule_and_subheading_survives_fold_and_republish(
    episode_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression (review iteration 2, Finding 1): generic ``---`` rules and ``##``
    sub-headings are CONTENT, not block terminators — a drafted description containing
    both must survive the fold byte-for-byte, and the wholesale rewrite must not destroy
    anything on a second run."""
    drafted = (
        "First paragraph before a divider.\n"
        "\n"
        "---\n"
        "\n"
        "## What's inside\n"
        "\n"
        "Second paragraph, after a markdown rule AND a sub-heading."
    )
    _draft(monkeypatch, "description", drafted)
    assert main(["publish", "001"]) == EXIT_OK
    content = _publish_text(episode_dir)
    # Byte-preserved in BOTH places: the folded Description section + the preserved block.
    assert content.count(drafted) == 2
    # Re-extraction from the REGENERATED file still returns the full drafted text.
    assert extract_drafted_sections(content)["description"].text == drafted
    assert main(["publish", "001"]) == EXIT_OK
    assert _publish_text(episode_dir) == content  # second run: idempotent, no further loss


def test_republish_is_idempotent_and_keeps_drafted_content(
    episode_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Wholesale regeneration must not lose the folded draft on the second run."""
    _draft(monkeypatch, "description", "Drafted description body.")
    assert main(["publish", "001"]) == EXIT_OK
    first = _publish_text(episode_dir)
    assert main(["publish", "001"]) == EXIT_OK
    second = _publish_text(episode_dir)
    assert second == first  # byte-for-byte stable
    assert _section(second, "Description") == "Drafted description body."


def test_operator_removing_marker_silences_the_review_warning(
    episode_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _draft(monkeypatch, "description", "Reviewed description.")
    assert main(["publish", "001"]) == EXIT_OK
    publish_path = episode_dir / "publish.md"
    reviewed = publish_path.read_text(encoding="utf-8").replace(AI_DRAFT_MARKER + "\n", "")
    publish_path.write_text(reviewed, encoding="utf-8")
    capsys.readouterr()
    assert main(["publish", "001"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "review AI-drafted content" not in captured.err
    content = _publish_text(episode_dir)
    assert _section(content, "Description") == "Reviewed description."  # content survives
    assert AI_DRAFT_MARKER not in content


# --- --url: record + transition (reusing lifecycle/episodes semantics) ---


def test_url_records_and_transitions_to_published(
    episode_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["status", "001", "edited"]) == EXIT_OK  # park one step before published
    capsys.readouterr()
    assert main(["publish", "001", "--url", URL]) == EXIT_OK
    captured = capsys.readouterr()
    episode = read_meta(episode_dir / "meta.toml")
    assert episode.youtube_url == URL
    assert episode.status == "published"
    assert episode.published_at != ""
    assert episode.history[-1].status == "published"
    assert "edited -> published" in captured.out
    assert f"youtube_url: {URL}" in captured.out
    assert f"published_at: {episode.published_at}" in captured.out
    assert "unusual jump" not in captured.err  # edited -> published is the happy path
    assert "Before you publish" in captured.out  # checklist still printed with --url


def test_url_republish_updates_url_but_never_clobbers_published_at(
    episode_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["publish", "001", "--url", URL]) == EXIT_OK
    first = read_meta(episode_dir / "meta.toml")
    assert first.published_at != ""
    capsys.readouterr()
    assert main(["publish", "001", "--url", "https://youtu.be/fixed-link0"]) == EXIT_OK
    second = read_meta(episode_dir / "meta.toml")
    assert second.youtube_url == "https://youtu.be/fixed-link0"  # last write wins
    assert second.published_at == first.published_at  # stamped once, never clobbered
    assert [entry.status for entry in second.history] == ["idea", "published", "published"]


def test_url_from_idea_warns_unusual_jump_but_succeeds(
    episode_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["publish", "001", "--url", URL]) == EXIT_OK
    err = capsys.readouterr().err
    assert "unusual jump idea -> published" in err
    assert "allowed, recorded" in err


def test_empty_url_is_a_user_error_and_touches_nothing(
    episode_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["publish", "001", "--url", "  "]) == EXIT_USER_ERROR
    assert "non-empty" in capsys.readouterr().err
    episode = read_meta(episode_dir / "meta.toml")
    assert episode.status == "idea"
    assert episode.youtube_url == ""
    # The guard fires before regeneration: publish.md is still the Step-1 placeholder.
    assert templates.PUBLISH_PLACEHOLDER_SENTINEL in _publish_text(episode_dir)


# --- user errors + degraded inputs ---


def test_unknown_episode_is_a_user_error(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["publish", "999"]) == EXIT_USER_ERROR
    assert "No episode matching" in capsys.readouterr().err


def test_invalid_utf8_publish_md_is_a_clean_user_error(
    episode_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (episode_dir / "publish.md").write_bytes(b"\xff\xfe broken bytes")
    assert main(["publish", "001", "--url", URL]) == EXIT_USER_ERROR
    assert "not valid UTF-8" in capsys.readouterr().err
    episode = read_meta(episode_dir / "meta.toml")
    assert episode.status == "idea"  # nothing was recorded
    assert episode.youtube_url == ""


def test_missing_publish_md_regenerates_from_meta(
    episode_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (episode_dir / "publish.md").unlink()
    assert main(["publish", "001"]) == EXIT_OK
    assert _section(_publish_text(episode_dir), "Title") == TITLE


# --- unit: thumbnail derivation + draft-block parsing ---


def test_derive_thumbnail_text_takes_the_leading_title_clause() -> None:
    assert derive_thumbnail_text(TITLE, HOOK) == "The Number-Guessing Machine"


def test_derive_thumbnail_text_trims_to_whole_words_within_limit() -> None:
    text = derive_thumbnail_text("word " * 20, "")
    assert 0 < len(text) <= THUMBNAIL_MAX_CHARS
    assert set(text.split()) == {"word"}  # whole words only, never mid-word


def test_derive_thumbnail_text_falls_back_to_hook_then_empty() -> None:
    assert derive_thumbnail_text("", "A hook: with a subtitle") == "A hook"
    assert derive_thumbnail_text("", "") == ""


def test_extract_drafted_sections_marker_detection_and_last_block_wins() -> None:
    heading = publish_draft_heading("description")
    text = "\n".join(
        [
            "# placeholder",
            "",
            AI_DRAFT_MARKER,
            heading,
            "",
            "old body",
            "",
            heading,
            "",
            "new body",
        ]
    )
    sections = extract_drafted_sections(text)
    assert sections["description"].text == "new body"
    assert sections["description"].marked is False  # the winning block carries no marker
    marked_only = "\n".join([AI_DRAFT_MARKER, heading, "", "body"])
    assert extract_drafted_sections(marked_only)["description"].marked is True
    assert extract_drafted_sections("no blocks here") == {}


# --- integration through the production entry point ---


def test_production_cli_publish_url_records_and_prints_ordered_block(
    repo: Path, episode_dir: Path
) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "cwp", "publish", "001", "--url", URL],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=repo,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert _headings(result.stdout)[: len(STUDIO_HEADINGS)] == STUDIO_HEADINGS
    assert "Before you publish" in result.stdout
    assert "Made for Kids" in result.stdout
    assert "wrote" in result.stderr  # the file note stays off the paste surface
    episode = read_meta(episode_dir / "meta.toml")
    assert episode.youtube_url == URL
    assert episode.status == "published"
    assert episode.published_at != ""
    content = _publish_text(episode_dir)
    assert _section(content, "Tags") == "neetcode, kids"
