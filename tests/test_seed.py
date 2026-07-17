"""Seed-bank tests (Step 10): the 12-episode idea bank + ``cwp seed``.

House style (mirrors test_episodes.py): no mocks — real tmp_path episode roots, real
TOML round trips through the production ``episodes.py`` scan/load, and subprocess
integration through the ``python -m cwp`` entry point. NEVER touches the repo's real
``episodes/`` dir. The seed hooks/teaches carry non-ASCII (``≤``, ``–``, ``→``), so the
render paths are exercised under captured output and a real piped stdout.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cwp import episodes, templates
from cwp.cli import EXIT_OK, EXIT_USER_ERROR, main
from cwp.episodes import scan_episodes, seed_episodes

# plan.md §5.5 seed table (the drift-catcher): seq -> (title, ingredient, effort, teaches).
# The bank in templates.py is asserted against THIS, and the seeded episodes against the
# bank — so a wrong ingredient/effort/teaches fails loud at exactly one of the two seams.
EXPECTED_TABLE: dict[int, tuple[str, str, str, str]] = {
    1: (
        "The Number-Guessing Machine (Binary Search, No Cheating)",
        "neetcode",
        "S",
        "binary search",
    ),
    2: (
        "The Precise Moment Pants Become Optional: A Live Hawaii Pants Index",
        "hak",
        "S",
        "formula/heat-index modeling",
    ),
    3: (
        "The Sock-Matching Machine (Two Sum, But Socks)",
        "neetcode",
        "S",
        "hash-map pairing (Two Sum)",
    ),
    4: (
        "The Unbeatable Cookie-Splitter",
        "hak",
        "S",
        '"I cut, you choose" fairness/game theory',
    ),
    5: (
        "I Let My 4-Year-Old Prompt Claude (No Notes)",
        "kid",
        "S",
        "prompting / AI as a filmed topic",
    ),
    6: ("FizzBuzz, But It's a Dinosaur", "neetcode", "S", "modulo / FizzBuzz"),
    7: (
        "Is the Dice Cheating? (My Daughter Runs the Audit)",
        "kid",
        "S",
        "uniformity / chi-square intuition",
    ),
    8: (
        "A Bedtime Story Picker That Never Repeats (Until It Has To)",
        "hak",
        "S",
        "Fisher–Yates shuffle",
    ),
    9: (
        "Are We There Yet? (An Honest Answer, Powered by Math)",
        "xkcd",
        "M",
        "haversine distance",
    ),
    10: ("Shortest Path to the Potty (An Emergency BFS)", "xkcd", "M", "BFS / shortest path"),
    11: (
        "Scream-to-Watts: Could Bath-Time Meltdowns Power the House?",
        "xkcd",
        "M",
        "decibel → energy physics",
    ),
    12: (
        "Lego Ouch Calories: Barefoot Steps Converted to Calories Burned",
        "kid",
        "S",
        "light arithmetic modeling",
    ),
}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repo root (pyproject marker) with cwd inside it — the CLI resolves
    ``episodes/`` from cwd, so tests never touch the real repo's episodes dir."""
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


# --- the bank itself matches the plan §5.5 table (data-integrity, no I/O) ---


def test_bank_has_exactly_twelve_contiguous_seqs() -> None:
    seqs = [seed.seq for seed in templates.SEED_EPISODES]
    assert seqs == list(range(1, 13))


def test_bank_matches_plan_table() -> None:
    by_seq = {seed.seq: seed for seed in templates.SEED_EPISODES}
    for seq, (title, ingredient, effort, teaches) in EXPECTED_TABLE.items():
        seed = by_seq[seq]
        assert seed.title == title, seq
        assert seed.ingredient == ingredient, seq
        assert seed.effort == effort, seq
        assert seed.teaches == teaches, seq


def test_bank_ingredients_and_efforts_are_valid_and_balanced() -> None:
    from collections import Counter

    counts = Counter(seed.ingredient for seed in templates.SEED_EPISODES)
    # §5.5: balanced 3/3/3/3 across the four ingredients.
    assert counts == {"neetcode": 3, "hak": 3, "kid": 3, "xkcd": 3}
    for seed in templates.SEED_EPISODES:
        assert seed.ingredient in episodes.INGREDIENTS
        assert seed.effort in episodes.EFFORTS
        assert seed.hook.strip(), f"empty hook for seq {seed.seq}"


def test_bank_kid_ingredient_episodes_are_kid_usable() -> None:
    for seed in templates.SEED_EPISODES:
        if seed.ingredient == "kid":
            assert seed.kid_usable is True, f"kid episode {seed.seq} must be kid_usable"


# --- seed_episodes: creates all 12, validated through the real scan/load path ---


def test_seed_creates_all_twelve_matching_the_bank(tmp_path: Path) -> None:
    episodes_dir = tmp_path / "episodes"
    result = seed_episodes(episodes_dir, now="2026-07-17T00:00:00Z")
    assert len(result.created) == 12
    assert result.skipped == ()

    scan = scan_episodes(episodes_dir)
    assert scan.warnings == (), scan.warnings  # every seeded meta.toml loads clean
    assert len(scan.episodes) == 12

    by_seq = {episode.seq: episode for episode in scan.episodes}
    bank = {seed.seq: seed for seed in templates.SEED_EPISODES}
    assert sorted(by_seq) == list(range(1, 13))
    for seq, episode in by_seq.items():
        seed = bank[seq]
        assert episode.id == f"{episodes.format_seq(seq)}-{episode.slug}"
        assert episode.title == seed.title
        assert episode.status == "idea"
        assert episode.ingredient == seed.ingredient
        assert episode.effort == seed.effort
        assert episode.teaches == seed.teaches
        assert episode.hook == seed.hook
        assert episode.kid_usable == seed.kid_usable
        assert list(episode.tags) == list(seed.tags)


def test_seed_is_idempotent(tmp_path: Path) -> None:
    episodes_dir = tmp_path / "episodes"
    first = seed_episodes(episodes_dir)
    assert len(first.created) == 12

    second = seed_episodes(episodes_dir)
    assert second.created == ()
    assert second.skipped == tuple(range(1, 13))

    # No duplication: still exactly 12 folders after a second run.
    assert len(scan_episodes(episodes_dir).episodes) == 12


def test_seed_resumes_after_partial_seed(tmp_path: Path) -> None:
    """An interrupted seed leaves a contiguous 1..k prefix; re-running fills the rest
    at the correct seqs and never duplicates the already-present ones."""
    episodes_dir = tmp_path / "episodes"
    # Simulate an interruption after the first 3: create only seqs 1..3 by hand-limiting.
    for seed in templates.SEED_EPISODES[:3]:
        episodes.create_episode(
            episodes_dir,
            seed.title,
            status="idea",
            ingredient=seed.ingredient,
            effort=seed.effort,
            kid_usable=seed.kid_usable,
            hook=seed.hook,
            teaches=seed.teaches,
        )
    result = seed_episodes(episodes_dir)
    assert result.skipped == (1, 2, 3)
    assert tuple(e.seq for e in result.created) == tuple(range(4, 13))
    assert sorted(e.seq for e in scan_episodes(episodes_dir).episodes) == list(range(1, 13))


def test_seed_fills_gaps_without_aborting_the_tail(tmp_path: Path) -> None:
    """Regression: decoys occupying seqs 001/002/004 must NOT abort the remaining rows.
    Seed creates the other 9 (003, 005-012) at their pinned seqs and skips 001/002/004
    — no crash, no duplicate, exit-0 semantics."""
    episodes_dir = tmp_path / "episodes"
    # Unrelated content occupying a mid-sequence set of seqs (leaves a gap at 003).
    for seq in (1, 2, 4):
        episodes.create_episode(episodes_dir, f"Decoy {seq}", seq=seq)
    result = seed_episodes(episodes_dir)
    assert sorted(result.skipped) == [1, 2, 4]
    assert tuple(e.seq for e in result.created) == (3, 5, 6, 7, 8, 9, 10, 11, 12)

    scanned = scan_episodes(episodes_dir)
    assert scanned.warnings == (), scanned.warnings
    # 3 decoys + 9 seeded = 12 folders, seqs 1..12, no duplicates.
    assert sorted(e.seq for e in scanned.episodes) == list(range(1, 13))
    bank = {seed.seq: seed for seed in templates.SEED_EPISODES}
    by_seq = {e.seq: e for e in scanned.episodes}
    for seq in (3, 5, 6, 7, 8, 9, 10, 11, 12):  # the seeded ones carry bank content
        assert by_seq[seq].title == bank[seq].title
        assert by_seq[seq].ingredient == bank[seq].ingredient
    for seq in (1, 2, 4):  # decoys left untouched
        assert by_seq[seq].title == f"Decoy {seq}"


def test_seed_ignores_unrelated_higher_seq(tmp_path: Path) -> None:
    """A pre-existing HIGHER-seq episode no longer blocks seeding — all 12 land at their
    pinned seqs and the unrelated high-seq episode is left in place."""
    episodes_dir = tmp_path / "episodes"
    episodes.create_episode(episodes_dir, "Operator's own idea", seq=50)
    result = seed_episodes(episodes_dir)
    assert len(result.created) == 12
    assert tuple(e.seq for e in result.created) == tuple(range(1, 13))
    seqs = sorted(e.seq for e in scan_episodes(episodes_dir).episodes)
    assert seqs == [*range(1, 13), 50]


# --- through the production CLI: seed → list / show, under captured output ---


def test_cli_seed_then_list_shows_all_twelve(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["seed"]) == EXIT_OK
    capsys.readouterr()  # drop seed output
    assert main(["list"]) == EXIT_OK
    lines = capsys.readouterr().out.splitlines()
    for seq, (title, ingredient, effort, _teaches) in EXPECTED_TABLE.items():
        seq_str = episodes.format_seq(seq)
        row = next((line for line in lines if line.startswith(seq_str)), None)
        assert row is not None, f"no list row for {seq_str}"
        assert ingredient in row, (seq_str, ingredient)
        assert effort in row, (seq_str, effort)
        assert title in row, seq_str


def test_cli_seed_is_idempotent_second_run_prints_nothing_to_do(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["seed"]) == EXIT_OK
    capsys.readouterr()
    assert main(["seed"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "already fully seeded" in captured.out
    assert "nothing to do" in captured.out
    assert "seeded 0" not in captured.out  # no created lines on a full re-seed
    assert "12 seq(s) already occupied" in captured.err


def test_cli_seed_reports_mixed_created_and_occupied(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A partial run (some seqs occupied by unrelated content) is reported as created +
    occupied, NOT misreported as 'already fully seeded'."""
    episodes_dir = repo / "episodes"
    episodes.create_episode(episodes_dir, "Operator idea one", seq=1)
    episodes.create_episode(episodes_dir, "Operator idea two", seq=2)
    capsys.readouterr()
    assert main(["seed"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "seeded 10 episode(s)" in out
    assert "already occupied" in out
    assert "already fully seeded" not in out


def test_cli_show_renders_non_ascii_hook_and_teaches_under_capture(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The cp1252 landmine under pytest capture: show a seed whose hook + teaches carry
    non-ASCII (008 = Fisher–Yates en-dash; 011 = decibel→energy arrow)."""
    assert main(["seed"]) == EXIT_OK
    capsys.readouterr()
    assert main(["show", "008"]) == EXIT_OK
    out_eight = capsys.readouterr().out
    assert "Fisher–Yates shuffle" in out_eight
    assert main(["show", "011"]) == EXIT_OK
    out_eleven = capsys.readouterr().out
    assert "decibel → energy physics" in out_eleven
    assert "decibel→energy physics off a live mic" in out_eleven


def test_cli_seed_missing_episode_id_argument_is_user_error(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """seed takes no positional args — an extra token is an argparse usage error (exit 1)."""
    with pytest.raises(SystemExit) as excinfo:
        main(["seed", "extra-arg"])
    assert excinfo.value.code == EXIT_USER_ERROR


# --- integration: real piped stdout (the cp1252 UnicodeEncodeError guard) ---


def _run_cwp(repo: Path, *argv: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "cwp", *argv], cwd=repo, capture_output=True, check=False
    )


def test_integration_seed_then_list_then_show_survives_utf8_pipe(repo: Path) -> None:
    """Non-ASCII hooks/teaches must survive a real piped stdout (cli reconfigures UTF-8)."""
    seeded = _run_cwp(repo, "seed")
    assert seeded.returncode == 0, seeded.stderr.decode("utf-8", "replace")
    assert seeded.stdout.decode("utf-8").count("seeded ") >= 12

    listed = _run_cwp(repo, "list")
    assert listed.returncode == 0, listed.stderr.decode("utf-8", "replace")
    list_out = listed.stdout.decode("utf-8")
    for seq, (title, _ingredient, _effort, _teaches) in EXPECTED_TABLE.items():
        assert episodes.format_seq(seq) in list_out
        assert title in list_out

    shown = _run_cwp(repo, "show", "011")
    assert shown.returncode == 0, shown.stderr.decode("utf-8", "replace")
    assert "decibel → energy physics" in shown.stdout.decode("utf-8")
