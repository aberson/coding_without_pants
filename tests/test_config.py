"""config.py repo-root discovery + canonical-path tests (Step 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cwp.config import Paths, RepoRootNotFoundError, find_repo_root, get_paths


def _has_marker_above(path: Path) -> bool:
    return any(
        (parent / "pyproject.toml").exists() or (parent / ".git").exists()
        for parent in (path, *path.parents)
    )


def test_finds_root_by_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    deep = tmp_path / "episodes" / "001-test" / "project"
    deep.mkdir(parents=True)
    assert find_repo_root(deep) == tmp_path


def test_finds_root_by_git_dir(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "src" / "cwp"
    nested.mkdir(parents=True)
    assert find_repo_root(nested) == tmp_path


def test_finds_root_by_git_file_worktree(tmp_path: Path) -> None:
    """In a git worktree ``.git`` is a FILE, not a directory — it must still count."""
    (tmp_path / ".git").write_text("gitdir: /somewhere/else\n", encoding="utf-8")
    nested = tmp_path / "docs"
    nested.mkdir()
    assert find_repo_root(nested) == tmp_path


def test_nearest_marker_wins(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    inner = tmp_path / "vendored"
    inner.mkdir()
    (inner / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert find_repo_root(inner) == inner


def test_start_may_be_a_file(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    marker_file = tmp_path / "plan.md"
    marker_file.write_text("# plan\n", encoding="utf-8")
    assert find_repo_root(marker_file) == tmp_path


def test_default_start_is_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert find_repo_root() == tmp_path.resolve()


def test_raises_when_no_marker(tmp_path: Path) -> None:
    if _has_marker_above(tmp_path):
        pytest.skip("a repo marker exists above tmp_path on this machine")
    with pytest.raises(RepoRootNotFoundError):
        find_repo_root(tmp_path)


def test_paths_properties(tmp_path: Path) -> None:
    paths = Paths(root=tmp_path)
    assert paths.episodes_dir == tmp_path / "episodes"
    assert paths.voice_md == tmp_path / "voice.md"
    assert paths.build_contract_md == tmp_path / "build-contract.md"
    assert paths.pantsless_test_md == tmp_path / "pantsless-test.md"


def test_get_paths_resolves_this_repo() -> None:
    paths = get_paths(Path(__file__))
    assert (paths.root / "pyproject.toml").is_file()
    assert paths.voice_md.is_file()
    assert (paths.root / "plan.md").is_file()
