"""lifecycle.py tests (Step 3): the §5.3 permissive state machine, history, status/next CLI.

House style: no mocks — real tmp_path episode roots, real TOML round trips, and a
subprocess integration test through the production ``python -m cwp`` entry point.
NEVER touches the repo's real ``episodes/`` dir.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cwp.cli import EXIT_OK, EXIT_USER_ERROR, main
from cwp.episodes import (
    Episode,
    EpisodeNotFoundError,
    create_episode,
    read_meta,
)
from cwp.lifecycle import (
    HAPPY_PATH,
    NEXT_ACTIONS,
    UnknownStatusError,
    apply_status,
    next_action,
    pick_next,
    unusual_reason,
    visible_in_default_list,
)


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


def _episode(seq: int, status: str) -> Episode:
    return Episode(id=f"{seq:03d}-e{seq}", seq=seq, slug=f"e{seq}", title=f"E{seq}", status=status)


# --- unusual_reason: the mechanical §5.3 classification ---


@pytest.mark.parametrize(
    ("current", "target"),
    [
        *zip(HAPPY_PATH, HAPPY_PATH[1:], strict=False),  # (a) every one-step-forward move
        ("edited", "recorded"),  # (b) the reshoot
        ("built", "on-hold"),  # (c) any -> on-hold
        ("on-hold", "recorded"),  # (c) on-hold -> any
        ("on-hold", "on-hold"),
        ("idea", "cut"),  # (d) -> cut from anywhere
        ("published", "cut"),
        ("cut", "cut"),  # (d) covers re-cut too
    ],
)
def test_usual_transitions_have_no_reason(current: str, target: str) -> None:
    assert unusual_reason(current, target) is None


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("built", "scripted"),  # backward
        ("idea", "built"),  # skip forward
        ("idea", "published"),  # giant leap
        ("recorded", "edited-typo-not-a-status"),  # unknown target (pre-validation path)
        ("idea", "idea"),  # same-status is not a forward step
        ("published", "edited"),  # backing out of published
        ("cut", "idea"),  # terminal: OUT of cut always warns
        ("cut", "on-hold"),  # terminal rule beats the on-hold rule
        ("cut", "published"),
        ("some-hand-edit", "built"),  # unknown current status falls through to unusual
    ],
)
def test_unusual_transitions_return_a_reason(current: str, target: str) -> None:
    assert unusual_reason(current, target) is not None


def test_out_of_cut_reason_names_the_terminal_rule() -> None:
    reason = unusual_reason("cut", "idea")
    assert reason is not None and "terminal" in reason


# --- apply_status: persist + history append + published_at stamping ---


def test_apply_status_forward_persists_and_appends_history(episodes_dir: Path) -> None:
    create_episode(episodes_dir, "Test", now="2026-07-15T00:00:00Z")
    transition = apply_status(episodes_dir, "001", "scripted", now="2026-07-16T01:02:03Z")
    assert transition.old_status == "idea"
    assert transition.new_status == "scripted"
    assert transition.unusual_reason is None

    persisted = read_meta(episodes_dir / "001-test" / "meta.toml")
    assert persisted.status == "scripted"
    assert [(entry.status, entry.at) for entry in persisted.history] == [
        ("idea", "2026-07-15T00:00:00Z"),
        ("scripted", "2026-07-16T01:02:03Z"),
    ]


def test_apply_status_history_is_append_only_across_many_jumps(episodes_dir: Path) -> None:
    """Prior [[history]] rows are NEVER rewritten — each transition adds exactly one row."""
    create_episode(episodes_dir, "Test", now="2026-07-15T00:00:00Z")
    stamps = [f"2026-07-16T00:00:0{i}Z" for i in range(4)]
    for stamp, status in zip(stamps, ("scripted", "built", "scripted", "cut"), strict=True):
        before = read_meta(episodes_dir / "001-test" / "meta.toml").history
        apply_status(episodes_dir, "001", status, now=stamp)
        after = read_meta(episodes_dir / "001-test" / "meta.toml").history
        assert after[: len(before)] == before  # the old rows survive byte-for-byte
        assert len(after) == len(before) + 1
        assert (after[-1].status, after[-1].at) == (status, stamp)


def test_apply_status_stamp_is_pinned_utc_shape(episodes_dir: Path) -> None:
    create_episode(episodes_dir, "Test")
    transition = apply_status(episodes_dir, "001", "scripted")  # real utc_now_iso()
    assert transition.stamp.endswith("Z")
    assert "T" in transition.stamp


def test_apply_status_backward_warns_but_succeeds(episodes_dir: Path) -> None:
    create_episode(episodes_dir, "Test")
    apply_status(episodes_dir, "001", "built")
    transition = apply_status(episodes_dir, "001", "scripted")
    assert transition.unusual_reason is not None
    assert read_meta(episodes_dir / "001-test" / "meta.toml").status == "scripted"


def test_apply_status_published_stamps_published_at(episodes_dir: Path) -> None:
    create_episode(episodes_dir, "Test")
    transition = apply_status(episodes_dir, "001", "published", now="2026-07-20T12:00:00Z")
    assert transition.published_at_stamped is True
    assert read_meta(episodes_dir / "001-test" / "meta.toml").published_at == (
        "2026-07-20T12:00:00Z"
    )


def test_apply_status_republish_keeps_original_published_at(episodes_dir: Path) -> None:
    """A re-publish appends history but never destroys the first cycle-time stamp."""
    create_episode(episodes_dir, "Test")
    apply_status(episodes_dir, "001", "published", now="2026-07-20T12:00:00Z")
    apply_status(episodes_dir, "001", "edited", now="2026-07-21T00:00:00Z")
    second = apply_status(episodes_dir, "001", "published", now="2026-07-22T12:00:00Z")
    assert second.published_at_stamped is False
    assert read_meta(episodes_dir / "001-test" / "meta.toml").published_at == (
        "2026-07-20T12:00:00Z"
    )


def test_apply_status_non_published_never_touches_published_at(episodes_dir: Path) -> None:
    create_episode(episodes_dir, "Test")
    apply_status(episodes_dir, "001", "published", now="2026-07-20T12:00:00Z")
    apply_status(episodes_dir, "001", "recorded", now="2026-07-21T00:00:00Z")
    assert read_meta(episodes_dir / "001-test" / "meta.toml").published_at == (
        "2026-07-20T12:00:00Z"
    )


def test_apply_status_unknown_status_raises_before_any_write(episodes_dir: Path) -> None:
    create_episode(episodes_dir, "Test", now="2026-07-15T00:00:00Z")
    with pytest.raises(UnknownStatusError, match="shipped"):
        apply_status(episodes_dir, "001", "shipped")
    persisted = read_meta(episodes_dir / "001-test" / "meta.toml")
    assert persisted.status == "idea"
    assert len(persisted.history) == 1


def test_apply_status_missing_episode_raises(episodes_dir: Path) -> None:
    with pytest.raises(EpisodeNotFoundError):
        apply_status(episodes_dir, "042", "built")


# --- default-list visibility (§5.3: cut hidden, on-hold visible) ---


@pytest.mark.parametrize(
    ("status", "visible"),
    [("idea", True), ("published", True), ("on-hold", True), ("cut", False)],
)
def test_visible_in_default_list(status: str, visible: bool) -> None:
    assert visible_in_default_list(_episode(1, status)) is visible


# --- pick_next: most-advanced in-flight, lowest-seq tie-break ---


def test_pick_next_most_advanced_wins() -> None:
    picked = pick_next([_episode(1, "idea"), _episode(2, "edited"), _episode(3, "built")])
    assert picked is not None
    assert picked.episode.seq == 2
    assert picked.action == "publish prep: cwp publish 002-e2"


def test_pick_next_tie_breaks_by_lowest_seq() -> None:
    picked = pick_next([_episode(3, "built"), _episode(1, "built"), _episode(2, "built")])
    assert picked is not None
    assert picked.episode.seq == 1


def test_pick_next_excludes_published_cut_and_on_hold() -> None:
    picked = pick_next(
        [
            _episode(1, "published"),
            _episode(2, "cut"),
            _episode(3, "on-hold"),
            _episode(4, "idea"),
        ]
    )
    assert picked is not None
    assert picked.episode.seq == 4


@pytest.mark.parametrize("status", ["published", "cut", "on-hold"])
def test_pick_next_none_when_nothing_in_flight(status: str) -> None:
    assert pick_next([_episode(1, status)]) is None
    assert pick_next([]) is None


@pytest.mark.parametrize("status", sorted(NEXT_ACTIONS))
def test_next_action_per_status_mentions_the_episode_or_step(status: str) -> None:
    """Every in-flight status has a concrete one-liner; cwp-runnable ones carry the id."""
    action = next_action(_episode(7, status))
    assert action  # non-empty for every in-flight status
    if "cwp" in action:
        assert "007-e7" in action


def test_next_action_covers_every_in_flight_status() -> None:
    """NEXT_ACTIONS and the in-flight statuses must not drift apart."""
    in_flight = [status for status in HAPPY_PATH if status != "published"]
    assert sorted(NEXT_ACTIONS) == sorted(in_flight)


def test_pick_next_unknown_status_counts_as_in_flight_least_advanced() -> None:
    """Permissive reads make hand-edited statuses real: not published/cut/on-hold ->
    in-flight, ranked below idea, with a fix-it action instead of a crash."""
    weird = _episode(1, "someday")
    picked = pick_next([weird, _episode(2, "idea")])
    assert picked is not None
    assert picked.episode.seq == 2  # idea outranks the unknown status
    alone = pick_next([weird])
    assert alone is not None
    assert "cwp status 001-e1" in alone.action


# --- CLI in-process: cwp status / cwp next / cwp list --all ---


def test_cli_status_forward_records_transition(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", "Test"]) == EXIT_OK
    capsys.readouterr()
    assert main(["status", "001", "scripted"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "001-test: idea -> scripted" in captured.out
    assert captured.err == ""  # a usual move: no warning


def test_cli_status_jump_warns_on_stderr_but_succeeds(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", "Test"]) == EXIT_OK
    capsys.readouterr()
    assert main(["status", "001", "built"]) == EXIT_OK  # idea -> built skips scripted
    captured = capsys.readouterr()
    assert "001-test: idea -> built" in captured.out
    assert "warning" in captured.err and "unusual" in captured.err
    persisted = read_meta(repo / "episodes" / "001-test" / "meta.toml")
    assert persisted.status == "built"


def test_cli_status_backward_warns_but_succeeds(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", "Test"]) == EXIT_OK
    assert main(["status", "001", "scripted"]) == EXIT_OK
    assert main(["status", "001", "built"]) == EXIT_OK
    capsys.readouterr()
    assert main(["status", "001", "scripted"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "built -> scripted" in captured.out
    assert "unusual" in captured.err


def test_cli_status_published_prints_published_at(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", "Test"]) == EXIT_OK
    capsys.readouterr()
    assert main(["status", "001", "published"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "published_at:" in out
    assert read_meta(repo / "episodes" / "001-test" / "meta.toml").published_at.endswith("Z")


def test_cli_status_unknown_status_is_user_error(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", "Test"]) == EXIT_OK
    capsys.readouterr()
    assert main(["status", "001", "shipped"]) == EXIT_USER_ERROR
    assert "Unknown status" in capsys.readouterr().err


def test_cli_status_missing_episode_is_user_error(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["status", "042", "built"]) == EXIT_USER_ERROR
    assert "No episode" in capsys.readouterr().err


def test_cli_status_out_of_cut_warns_terminal(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", "Test"]) == EXIT_OK
    assert main(["status", "001", "cut"]) == EXIT_OK
    capsys.readouterr()
    assert main(["status", "001", "idea"]) == EXIT_OK  # permissive: un-cut is allowed
    captured = capsys.readouterr()
    assert "terminal" in captured.err
    assert "cut -> idea" in captured.out


def test_cli_cut_hides_from_default_list_and_all_shows_it(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", "Keep Me"]) == EXIT_OK
    assert main(["new", "Cut Me"]) == EXIT_OK
    assert main(["status", "002", "cut"]) == EXIT_OK
    capsys.readouterr()

    assert main(["list"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "001-keep-me" in captured.out
    assert "002-cut-me" not in captured.out
    assert "1 cut hidden" in captured.err

    assert main(["list", "--all"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "001-keep-me" in captured.out and "002-cut-me" in captured.out
    assert captured.err == ""


def test_cli_on_hold_stays_in_default_list(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """§5.3 hides only cut — parked (on-hold) work must not vanish from the overview."""
    assert main(["new", "Parked"]) == EXIT_OK
    assert main(["status", "001", "on-hold"]) == EXIT_OK
    capsys.readouterr()
    assert main(["list"]) == EXIT_OK
    assert "001-parked" in capsys.readouterr().out


def test_cli_list_all_episodes_cut_is_friendly_exit_zero(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", "Only"]) == EXIT_OK
    assert main(["status", "001", "cut"]) == EXIT_OK
    capsys.readouterr()
    assert main(["list"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "1 cut hidden" in captured.out
    assert "--all" in captured.out


def test_cli_next_picks_most_advanced_with_action(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", "Fresh Idea"]) == EXIT_OK
    assert main(["new", "Almost Done"]) == EXIT_OK
    assert main(["status", "002", "edited"]) == EXIT_OK
    capsys.readouterr()
    assert main(["next"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "002-almost-done" in out
    assert "[edited]" in out
    assert "next: publish prep: cwp publish 002-almost-done" in out


def test_cli_next_tie_breaks_by_lowest_seq(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["new", "First"]) == EXIT_OK
    assert main(["new", "Second"]) == EXIT_OK
    capsys.readouterr()
    assert main(["next"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "001-first" in out and "002-second" not in out
    assert "next: draft a script: cwp draft 001-first script" in out


def test_cli_next_skips_on_hold_and_published(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", "Held"]) == EXIT_OK
    assert main(["new", "Done"]) == EXIT_OK
    assert main(["new", "Live"]) == EXIT_OK
    assert main(["status", "001", "edited"]) == EXIT_OK
    assert main(["status", "001", "on-hold"]) == EXIT_OK  # most advanced, but parked
    assert main(["status", "002", "published"]) == EXIT_OK
    capsys.readouterr()
    assert main(["next"]) == EXIT_OK
    assert "003-live" in capsys.readouterr().out


def test_cli_next_empty_pipeline_is_friendly_exit_zero(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["next"]) == EXIT_OK
    assert "nothing in flight" in capsys.readouterr().out


def test_cli_next_all_parked_is_friendly_exit_zero(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["new", "Held"]) == EXIT_OK
    assert main(["status", "001", "on-hold"]) == EXIT_OK
    capsys.readouterr()
    assert main(["next"]) == EXIT_OK
    assert "nothing in flight" in capsys.readouterr().out


# --- integration: the Step 3 acceptance loop through the production CLI entry point ---


def _run_cwp(repo: Path, *argv: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "cwp", *argv],
        cwd=repo,
        capture_output=True,
        check=False,
    )


def test_integration_status_next_and_cut_round_trip(repo: Path) -> None:
    """new x2 -> forward status -> backward warn -> next -> cut -> list hides it."""
    assert _run_cwp(repo, "new", "The Toy").returncode == 0
    assert _run_cwp(repo, "new", "The Other Toy").returncode == 0

    forward = _run_cwp(repo, "status", "001", "scripted")
    assert forward.returncode == 0, forward.stderr.decode("utf-8", "replace")
    assert "idea -> scripted" in forward.stdout.decode("utf-8")
    assert forward.stderr == b""  # usual move: silent

    backward = _run_cwp(repo, "status", "001", "idea")
    assert backward.returncode == 0  # warns but NEVER blocks
    assert "unusual" in backward.stderr.decode("utf-8", "replace")

    # 001 is back at idea; advance 002 so next must pick it (most advanced).
    assert _run_cwp(repo, "status", "002", "built").returncode == 0
    next_result = _run_cwp(repo, "next")
    assert next_result.returncode == 0
    next_out = next_result.stdout.decode("utf-8")
    assert "002-the-other-toy" in next_out and "next:" in next_out

    # History is the full append-only trail of every transition above.
    persisted = read_meta(repo / "episodes" / "001-the-toy" / "meta.toml")
    assert [entry.status for entry in persisted.history] == ["idea", "scripted", "idea"]

    cut = _run_cwp(repo, "status", "002", "cut")
    assert cut.returncode == 0
    listed = _run_cwp(repo, "list")
    assert listed.returncode == 0
    listed_out = listed.stdout.decode("utf-8")
    assert "001-the-toy" in listed_out and "002-the-other-toy" not in listed_out
    listed_all = _run_cwp(repo, "list", "--all")
    assert "002-the-other-toy" in listed_all.stdout.decode("utf-8")


def test_integration_unknown_status_exits_1(repo: Path) -> None:
    assert _run_cwp(repo, "new", "The Toy").returncode == 0
    result = _run_cwp(repo, "status", "001", "shipped")
    assert result.returncode == 1
    assert "Unknown status" in result.stderr.decode("utf-8", "replace")
