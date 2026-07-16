"""episodes.py tests (Step 2): slug/id gen, atomic meta I/O, derived scan, CLI new/idea/list/show.

House style: no mocks — real tmp_path episode roots, real TOML round trips, and a
subprocess integration test through the production ``python -m cwp`` entry point.
NEVER touches the repo's real ``episodes/`` dir.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from cwp import episodes
from cwp.cli import EXIT_ENV_ERROR, EXIT_OK, EXIT_USER_ERROR, main
from cwp.episodes import (
    Episode,
    EpisodeError,
    EpisodeNotFoundError,
    HistoryEntry,
    PantslessTest,
    SlugError,
    create_episode,
    cycle_time_days,
    format_seq,
    next_seq,
    parse_tags,
    read_meta,
    resolve_episode_dir,
    scan_episodes,
    slugify,
    write_episode_files,
    write_meta,
)

PER_EPISODE_FILES = ("meta.toml", "script.md", "publish.md", "brief.md")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repo root (pyproject marker) with cwd inside it — the CLI resolves
    ``episodes/`` from cwd, so tests never touch the real repo's episodes dir."""
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def episodes_dir(repo: Path) -> Path:
    return repo / "episodes"


# --- slugify (§4.1) ---


def test_slugify_basic_punctuation_stripped() -> None:
    assert slugify("The Sock-Matching Machine (Two Sum, But Socks)") == (
        "the-sock-matching-machine-two-sum-but-so"
    )


def test_slugify_preserves_word_joining_hyphens() -> None:
    assert slugify("The Number-Guessing Machine") == "the-number-guessing-machine"


def test_slugify_collapses_whitespace_and_trims() -> None:
    assert slugify("  A   --  B  ") == "a-b"


def test_slugify_strips_non_ascii() -> None:
    assert slugify("Pants… – 🦖 Optional") == "pants-optional"


def test_slugify_truncates_to_40_without_trailing_hyphen() -> None:
    slug = slugify("The Number-Guessing Machine (Binary Search, No Cheating)")
    assert len(slug) <= episodes.MAX_SLUG_LENGTH
    assert not slug.endswith("-")
    assert slug.startswith("the-number-guessing-machine-binary")


def test_slugify_truncation_landing_on_hyphen_boundary() -> None:
    """Truncation at exactly 40 chars can EXPOSE a trailing hyphen — the rstrip branch."""
    title = "x" * 39 + " " + "y" * 10  # collapsed slug = 39 x's + "-" + y's; char 40 IS the hyphen
    slug = slugify(title)
    assert slug == "x" * 39
    assert not slug.endswith("-")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a, b", ["a", "b"]),
        ("algorithms,modulo", ["algorithms", "modulo"]),
        ("solo", ["solo"]),
        ("", []),
        (" , ,, ", []),
        ("  spaced  ,  out  ", ["spaced", "out"]),
    ],
)
def test_parse_tags(raw: str, expected: list[str]) -> None:
    assert parse_tags(raw) == expected


@pytest.mark.parametrize("bad_title", ["", "!!!", "…—…", "🦖🦖🦖", "   "])
def test_slugify_empty_result_raises(bad_title: str) -> None:
    with pytest.raises(SlugError):
        slugify(bad_title)


@pytest.mark.parametrize(
    "title",
    [
        "FizzBuzz, But It's a Dinosaur",
        "Scream-to-Watts: Could Bath-Time Meltdowns Power the House?",
        "Are We There Yet? (An Honest Answer, Powered by Math)",
        "A + B = C",
        "under_scores_and-hyphens  mixed",
    ],
)
def test_slugify_output_matches_pinned_regex(title: str) -> None:
    assert episodes.SLUG_RE.fullmatch(slugify(title))


# --- seq (§4.1) ---


def test_format_seq_zero_pads_to_three() -> None:
    assert format_seq(1) == "001"
    assert format_seq(42) == "042"
    assert format_seq(999) == "999"


def test_format_seq_widens_past_999() -> None:
    assert format_seq(1000) == "1000"


@pytest.mark.parametrize("bad_seq", [0, -1])
def test_format_seq_rejects_non_positive(bad_seq: int) -> None:
    with pytest.raises(ValueError):
        format_seq(bad_seq)


def test_next_seq_starts_at_one(episodes_dir: Path) -> None:
    assert next_seq(episodes_dir) == 1  # dir doesn't even exist yet


def test_next_seq_is_max_plus_one_with_gaps(episodes_dir: Path) -> None:
    (episodes_dir / "001-a").mkdir(parents=True)
    (episodes_dir / "005-b").mkdir()
    assert next_seq(episodes_dir) == 6


def test_next_seq_ignores_non_episode_entries(episodes_dir: Path) -> None:
    (episodes_dir / "001-a").mkdir(parents=True)
    (episodes_dir / "notes").mkdir()  # not id-shaped
    (episodes_dir / "01-bad").mkdir()  # seq too short
    (episodes_dir / "003-file").write_text("a file, not a dir", encoding="utf-8")
    assert next_seq(episodes_dir) == 2


def test_next_seq_crosses_into_four_digits(episodes_dir: Path) -> None:
    (episodes_dir / "999-last").mkdir(parents=True)
    assert next_seq(episodes_dir) == 1000
    created = create_episode(episodes_dir, "Test")
    assert created.episode.id == "1000-test"


# --- create_episode (§4.1 + §4.2) ---


def test_create_episode_full_layout_and_valid_meta(episodes_dir: Path) -> None:
    created = create_episode(episodes_dir, "Test")
    directory = episodes_dir / "001-test"
    assert created.directory == directory
    for name in PER_EPISODE_FILES:
        assert (directory / name).is_file(), f"missing {name}"
    capture = directory / "capture"
    assert capture.is_dir()
    assert list(capture.iterdir()) == []  # created EMPTY — the whole dir is git-ignored
    assert (directory / "project" / "index.html").is_file()

    with (directory / "meta.toml").open("rb") as handle:
        data = tomllib.load(handle)
    assert data["schema_version"] == 1
    assert data["id"] == "001-test"
    assert data["seq"] == 1
    assert data["slug"] == "test"
    assert data["title"] == "Test"
    assert data["status"] == "idea"
    assert data["ingredient"] in episodes.INGREDIENTS
    assert data["effort"] in episodes.EFFORTS
    assert data["published_at"] == ""
    assert data["needs_human"] is False
    assert set(data["pantsless_test"]) == {
        "can_start_unaided",
        "understands_goal",
        "cant_break_it",
        "enjoys_it",
        "notes",
    }
    assert data["history"] == [{"status": "idea", "at": data["created_at"]}]
    assert data["created_at"].endswith("Z")


def test_create_episode_assigns_sequential_ids(episodes_dir: Path) -> None:
    first = create_episode(episodes_dir, "Alpha")
    second = create_episode(episodes_dir, "Beta")
    assert first.episode.id == "001-alpha"
    assert second.episode.id == "002-beta"
    assert first.warnings == () and second.warnings == ()


def test_create_episode_duplicate_slug_allowed_with_warning(episodes_dir: Path) -> None:
    create_episode(episodes_dir, "Test")
    dup = create_episode(episodes_dir, "Test!")  # same slug, new seq
    assert dup.episode.id == "002-test"
    assert dup.directory.is_dir()
    assert any("001-test" in warning for warning in dup.warnings)


def test_create_episode_rejects_bad_enum(episodes_dir: Path) -> None:
    with pytest.raises(EpisodeError, match="ingredient"):
        create_episode(episodes_dir, "Test", ingredient="cheese")


def test_write_episode_files_never_clobbers(episodes_dir: Path) -> None:
    """§4.2 clobber protection: re-running layout creation must not touch existing files."""
    created = create_episode(episodes_dir, "Test")
    index = created.directory / "project" / "index.html"
    index.write_text("THE REAL TOY", encoding="utf-8")
    script = created.directory / "script.md"
    script.write_text("my hand-written script", encoding="utf-8")
    write_episode_files(created.directory, created.episode)
    assert index.read_text(encoding="utf-8") == "THE REAL TOY"
    assert script.read_text(encoding="utf-8") == "my hand-written script"


def test_index_html_placeholder_escapes_title(episodes_dir: Path) -> None:
    created = create_episode(episodes_dir, "Socks & Pants <optional>")
    html_text = (created.directory / "project" / "index.html").read_text(encoding="utf-8")
    assert "Socks &amp; Pants &lt;optional&gt;" in html_text
    assert "<optional>" not in html_text


# --- meta.toml round trip + atomic write (§4.1 + §4.3) ---


def _full_episode() -> Episode:
    return Episode(
        id="003-pants-index",
        seq=3,
        slug="pants-index",
        title="The Precise Moment Pants Become Optional… – 🦖",
        status="published",
        ingredient="hak",
        kid_usable=True,
        effort="M",
        hook="20 Questions turned into an app…",
        teaches="formula/heat-index modeling",
        tags=["hawaii", "modeling"],
        created_at="2026-07-01T00:00:00Z",
        published_at="2026-07-05T12:00:00Z",
        youtube_url="https://youtu.be/XXXX",
        needs_human=True,
        notes="non-ASCII survives: naïve — ✓",
        pantsless_test=PantslessTest(
            can_start_unaided=True,
            understands_goal=True,
            cant_break_it=False,
            enjoys_it=True,
            notes="almost",
        ),
        history=[
            HistoryEntry(status="idea", at="2026-07-01T00:00:00Z"),
            HistoryEntry(status="published", at="2026-07-05T12:00:00Z"),
        ],
    )


def test_meta_round_trip_preserves_everything(tmp_path: Path) -> None:
    original = _full_episode()
    episode_dir = tmp_path / original.id
    write_meta(episode_dir, original)
    assert read_meta(episode_dir / "meta.toml") == original


def test_write_meta_overwrites_and_leaves_no_temp_files(tmp_path: Path) -> None:
    episode = _full_episode()
    episode_dir = tmp_path / episode.id
    write_meta(episode_dir, episode)
    episode.title = "Retitled (id/folder stays immutable)"
    write_meta(episode_dir, episode)
    assert read_meta(episode_dir / "meta.toml").title == episode.title
    assert [entry.name for entry in episode_dir.iterdir()] == ["meta.toml"]


def test_write_meta_failure_at_replace_leaves_original_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§4.3 crash-safety: a failure at the replace boundary must leave the OLD file
    byte-identical and no stray temp file. (House style is no mocks; a monkeypatched
    os-level fault injection is the sanctioned surgical exception — there is no other
    way to simulate a kill at this boundary.)"""
    episode = _full_episode()
    episode_dir = tmp_path / episode.id
    write_meta(episode_dir, episode)
    original_bytes = (episode_dir / "meta.toml").read_bytes()

    def exploding_replace(src: object, dst: object) -> None:
        raise OSError("simulated kill at the replace boundary")

    monkeypatch.setattr(os, "replace", exploding_replace)
    episode.title = "this write never lands"
    with pytest.raises(OSError, match="simulated kill"):
        write_meta(episode_dir, episode)
    monkeypatch.undo()

    assert (episode_dir / "meta.toml").read_bytes() == original_bytes
    assert [entry.name for entry in episode_dir.iterdir()] == ["meta.toml"]


def test_read_meta_normalizes_native_toml_datetimes(tmp_path: Path) -> None:
    """§4.1 pins STRING timestamps, but unquoted datetime literals are valid TOML a
    hand-editor may plausibly type — they must normalize to the pinned Z shape on
    read (and thus round-trip deterministically), not bake in str(datetime)."""
    meta = tmp_path / "meta.toml"
    meta.write_text(
        'id = "001-x"\nseq = 1\nslug = "x"\ntitle = "X"\nstatus = "published"\n'
        "created_at = 2026-07-15T00:00:00Z\n"  # unquoted offset-datetime (UTC)
        "published_at = 2026-07-20T02:00:00+02:00\n"  # non-UTC offset -> converted
        "[[history]]\nstatus = 'idea'\nat = 2026-07-15\n",  # local-date literal
        encoding="utf-8",
    )
    episode = read_meta(meta)
    assert episode.created_at == "2026-07-15T00:00:00Z"
    assert episode.published_at == "2026-07-20T00:00:00Z"
    assert episode.history[0].at == "2026-07-15T00:00:00Z"

    write_meta(tmp_path, episode)  # the very next write must persist the pinned shape
    reread = (tmp_path / "meta.toml").read_text(encoding="utf-8")
    assert 'created_at = "2026-07-15T00:00:00Z"' in reread
    assert 'published_at = "2026-07-20T00:00:00Z"' in reread


_BASE_META = 'id = "001-x"\nseq = 1\nslug = "x"\ntitle = "X"\n'


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (_BASE_META + 'tags = "oops"', "tags must be an array"),
        (_BASE_META + 'pantsless_test = "oops"', "pantsless_test must be a table"),
        (_BASE_META + 'history = "oops"', "history must be an array"),
        (_BASE_META + "history = [1, 2]", "history entries must be tables"),
        ('id = "001-x"\nseq = "NaN"\nslug = "x"\ntitle = "X"\n', "NaN"),
    ],
)
def test_read_meta_hand_edited_type_mismatch_raises(tmp_path: Path, body: str, match: str) -> None:
    """The permissive-read TypeError/ValueError branches: hand-edited-but-valid TOML
    with wrong shapes degrades to MetaFormatError (which scan turns into a warning)."""
    meta = tmp_path / "meta.toml"
    meta.write_text(body + "\n", encoding="utf-8")
    with pytest.raises(episodes.MetaFormatError, match=match):
        read_meta(meta)


def test_read_meta_missing_file_raises_meta_format_error(tmp_path: Path) -> None:
    with pytest.raises(episodes.MetaFormatError, match="unreadable"):
        read_meta(tmp_path / "meta.toml")


def test_read_meta_missing_required_key_raises(tmp_path: Path) -> None:
    meta = tmp_path / "meta.toml"
    meta.write_text('id = "001-x"\nseq = 1\nslug = "x"\n', encoding="utf-8")  # no title
    with pytest.raises(episodes.MetaFormatError, match="title"):
        read_meta(meta)


def test_read_meta_invalid_toml_raises(tmp_path: Path) -> None:
    meta = tmp_path / "meta.toml"
    meta.write_text("this is [not toml", encoding="utf-8")
    with pytest.raises(episodes.MetaFormatError, match="invalid TOML"):
        read_meta(meta)


# --- scan (derived index, §4) ---


def test_scan_returns_episodes_sorted_by_seq(episodes_dir: Path) -> None:
    create_episode(episodes_dir, "Alpha")
    create_episode(episodes_dir, "Beta")
    create_episode(episodes_dir, "Gamma")
    result = scan_episodes(episodes_dir)
    assert [episode.seq for episode in result.episodes] == [1, 2, 3]
    assert result.warnings == ()


def test_scan_missing_dir_is_empty(episodes_dir: Path) -> None:
    result = scan_episodes(episodes_dir)
    assert result.episodes == () and result.warnings == ()


def test_scan_skips_stray_entries_silently(episodes_dir: Path) -> None:
    create_episode(episodes_dir, "Alpha")
    (episodes_dir / "notes").mkdir()
    (episodes_dir / "README.md").write_text("hi", encoding="utf-8")
    result = scan_episodes(episodes_dir)
    assert len(result.episodes) == 1
    assert result.warnings == ()


def test_scan_warns_on_corrupt_meta_and_keeps_going(episodes_dir: Path) -> None:
    create_episode(episodes_dir, "Alpha")
    broken = create_episode(episodes_dir, "Beta")
    (broken.directory / "meta.toml").write_text("corrupt [", encoding="utf-8")
    result = scan_episodes(episodes_dir)
    assert [episode.slug for episode in result.episodes] == ["alpha"]
    assert any("002-beta" in warning for warning in result.warnings)


def test_scan_warns_on_invalid_utf8_meta_and_keeps_going(episodes_dir: Path) -> None:
    """tomllib raises plain UnicodeDecodeError (not TOMLDecodeError) on bad bytes —
    a wrong-encoding hand-edit or a truncated multibyte tail must warn, never crash."""
    create_episode(episodes_dir, "Alpha")
    broken = create_episode(episodes_dir, "Beta")
    (broken.directory / "meta.toml").write_bytes(
        b'id = "002-beta"\nseq = 2\nslug = "beta"\ntitle = "bad \xff byte"\n'
    )
    result = scan_episodes(episodes_dir)
    assert [episode.slug for episode in result.episodes] == ["alpha"]
    assert any("002-beta" in warning and "UTF-8" in warning for warning in result.warnings)


def test_scan_warns_on_hand_edited_type_mismatch(episodes_dir: Path) -> None:
    create_episode(episodes_dir, "Alpha")
    broken = create_episode(episodes_dir, "Beta")
    meta = broken.directory / "meta.toml"
    meta.write_text(
        'id = "002-beta"\nseq = 2\nslug = "beta"\ntitle = "B"\ntags = "oops"\n',
        encoding="utf-8",
    )
    result = scan_episodes(episodes_dir)
    assert [episode.slug for episode in result.episodes] == ["alpha"]
    assert any("tags must be an array" in warning for warning in result.warnings)


def test_scan_warns_on_missing_meta(episodes_dir: Path) -> None:
    create_episode(episodes_dir, "Alpha")
    (episodes_dir / "002-ghost").mkdir()
    result = scan_episodes(episodes_dir)
    assert len(result.episodes) == 1
    assert any("002-ghost" in warning for warning in result.warnings)


def test_scan_warns_on_id_folder_mismatch(episodes_dir: Path) -> None:
    created = create_episode(episodes_dir, "Alpha")
    episode = created.episode
    episode.id = "099-hand-edited"
    write_meta(created.directory, episode)
    result = scan_episodes(episodes_dir)
    assert len(result.episodes) == 1
    assert any("folder name" in warning for warning in result.warnings)


# --- resolve (full id or bare seq) ---


def test_resolve_accepts_bare_and_padded_seq(episodes_dir: Path) -> None:
    created = create_episode(episodes_dir, "Test")
    assert resolve_episode_dir(episodes_dir, "1") == created.directory
    assert resolve_episode_dir(episodes_dir, "001") == created.directory


def test_resolve_accepts_full_id(episodes_dir: Path) -> None:
    created = create_episode(episodes_dir, "Test")
    assert resolve_episode_dir(episodes_dir, "001-test") == created.directory


@pytest.mark.parametrize("missing", ["2", "002", "001-nope", "banana"])
def test_resolve_missing_raises(episodes_dir: Path, missing: str) -> None:
    create_episode(episodes_dir, "Test")
    with pytest.raises(EpisodeNotFoundError):
        resolve_episode_dir(episodes_dir, missing)


# --- cycle time ---


def test_cycle_time_days_floor_of_elapsed() -> None:
    assert cycle_time_days(_full_episode()) == 4  # 4.5 days floors to 4


def test_cycle_time_none_when_unpublished() -> None:
    episode = _full_episode()
    episode.published_at = ""
    assert cycle_time_days(episode) is None


def test_cycle_time_none_when_unparseable() -> None:
    episode = _full_episode()
    episode.published_at = "sometime in july"
    assert cycle_time_days(episode) is None


# --- CLI handlers in-process (new/idea/list/show) ---


def test_cli_new_creates_and_reports(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["new", "Test"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "001-test" in out
    assert (repo / "episodes" / "001-test" / "meta.toml").is_file()


def test_cli_new_flags_land_in_meta(repo: Path) -> None:
    assert (
        main(
            [
                "new",
                "FizzBuzz, But It's a Dinosaur",
                "--ingredient",
                "neetcode",
                "--effort",
                "S",
                "--teaches",
                "modulo / FizzBuzz",
                "--tags",
                "algorithms, modulo",
            ]
        )
        == EXIT_OK
    )
    result = scan_episodes(repo / "episodes")
    (episode,) = result.episodes
    assert episode.ingredient == "neetcode"
    assert episode.teaches == "modulo / FizzBuzz"
    assert episode.tags == ["algorithms", "modulo"]


def test_cli_new_unslugable_title_is_user_error(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", "🦖🦖🦖"]) == EXIT_USER_ERROR
    assert "empty slug" in capsys.readouterr().err


def test_cli_new_duplicate_slug_warns_on_stderr(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", "Test"]) == EXIT_OK
    assert main(["new", "Test"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "002-test" in captured.out
    assert "warning" in captured.err and "001-test" in captured.err


def test_cli_idea_is_minimal_fast_capture(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    thought = "button that burps the alphabet"
    assert main(["idea", thought]) == EXIT_OK
    assert "001-button-that-burps-the-alphabet" in capsys.readouterr().out
    (episode,) = scan_episodes(repo / "episodes").episodes
    assert episode.title == thought  # title = the thought, verbatim
    assert episode.status == "idea"


def test_cli_list_empty_exits_zero(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list"]) == EXIT_OK
    assert "no episodes yet" in capsys.readouterr().out


def test_cli_list_table_columns_and_cycle(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["new", "First Toy"]) == EXIT_OK
    published = _full_episode()  # a published episode with a 4.5-day cycle
    published.id, published.seq = "002-pants-index", 2  # id/seq must match the folder
    write_meta((repo / "episodes") / "002-pants-index", published)
    capsys.readouterr()  # drop ``new`` output
    assert main(["list"]) == EXIT_OK
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    header = lines[0]
    for column in ("id", "status", "ingredient", "effort", "title", "cycle"):
        assert column in header
    body = "\n".join(lines[1:])
    assert "001-first-toy" in body
    assert "002-pants-index" in body
    assert "4d" in body  # idea→published cycle time
    assert "🦖" in body  # non-ASCII title renders
    assert captured.err == ""


def test_cli_list_survives_corrupt_meta(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["new", "Good"]) == EXIT_OK
    assert main(["new", "Bad"]) == EXIT_OK
    (repo / "episodes" / "002-bad" / "meta.toml").write_text("corrupt [", encoding="utf-8")
    capsys.readouterr()
    assert main(["list"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "001-good" in captured.out
    assert "warning" in captured.err


def test_cli_list_survives_invalid_utf8_meta(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", "Good"]) == EXIT_OK
    assert main(["new", "Bad"]) == EXIT_OK
    (repo / "episodes" / "002-bad" / "meta.toml").write_bytes(b'title = "bad \xff byte"\n')
    capsys.readouterr()
    assert main(["list"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "001-good" in captured.out
    assert "warning" in captured.err and "UTF-8" in captured.err


def test_cli_show_by_full_id_prints_detail(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["new", "Test"]) == EXIT_OK
    capsys.readouterr()
    assert main(["show", "001-test"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "001-test" in out and "status:" in out


@pytest.mark.parametrize("lookup", ["2", "002-ghost"])
def test_cli_show_missing_meta_is_clean_user_error(
    repo: Path, capsys: pytest.CaptureFixture[str], lookup: str
) -> None:
    """An id-shaped folder with no meta.toml (cwp new killed between mkdir and
    write_meta) must exit 1 with a clean message, not a FileNotFoundError traceback."""
    assert main(["new", "Test"]) == EXIT_OK
    (repo / "episodes" / "002-ghost").mkdir()
    capsys.readouterr()
    assert main(["show", lookup]) == EXIT_USER_ERROR
    err = capsys.readouterr().err
    assert "002-ghost" in err and "meta.toml" in err
    assert "Traceback" not in err


def test_cli_show_by_bare_seq_prints_detail(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["new", "Test", "--hook", "a hook…"]) == EXIT_OK
    capsys.readouterr()
    assert main(["show", "1"]) == EXIT_OK
    out = capsys.readouterr().out
    for fragment in ("001-test", "status:", "idea", "a hook…", "pantsless_test: 0/4", "history:"):
        assert fragment in out


def test_cli_show_missing_is_user_error(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["show", "42"]) == EXIT_USER_ERROR
    assert "No episode" in capsys.readouterr().err


def test_cli_no_repo_root_is_env_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    if any(
        (parent / "pyproject.toml").exists() or (parent / ".git").exists()
        for parent in (tmp_path, *tmp_path.parents)
    ):
        pytest.skip("a repo marker exists above tmp_path on this machine")
    monkeypatch.chdir(tmp_path)
    assert main(["list"]) == EXIT_ENV_ERROR
    assert "No repo root" in capsys.readouterr().err


# --- integration: new → list → show through the production CLI entry point ---


def _run_cwp(repo: Path, *argv: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "cwp", *argv],
        cwd=repo,
        capture_output=True,
        check=False,
    )


def test_integration_new_then_list_then_show_round_trip(repo: Path) -> None:
    """The Step 2 acceptance loop, driven end-to-end via ``python -m cwp``."""
    title = "The Sock… Machine 🦖"
    created = _run_cwp(repo, "new", title)
    assert created.returncode == 0, created.stderr.decode("utf-8", "replace")
    assert "001-the-sock-machine" in created.stdout.decode("utf-8")

    directory = repo / "episodes" / "001-the-sock-machine"
    for name in PER_EPISODE_FILES:
        assert (directory / name).is_file()
    assert (directory / "capture").is_dir()
    assert (directory / "project" / "index.html").is_file()

    listed = _run_cwp(repo, "list")
    assert listed.returncode == 0, listed.stderr.decode("utf-8", "replace")
    out = listed.stdout.decode("utf-8")
    assert "001-the-sock-machine" in out
    assert title in out  # non-ASCII survives the pipe (cli reconfigures UTF-8)

    shown = _run_cwp(repo, "show", "001")
    assert shown.returncode == 0, shown.stderr.decode("utf-8", "replace")
    assert title in shown.stdout.decode("utf-8")


def test_integration_show_missing_exits_1(repo: Path) -> None:
    result = _run_cwp(repo, "show", "007")
    assert result.returncode == 1
    assert "No episode" in result.stderr.decode("utf-8", "replace")
