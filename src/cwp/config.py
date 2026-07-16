"""Locate the repo root and the canonical file paths; channel defaults.

The repo root is found by walking up from a start path (default: cwd) to the first
directory containing ``pyproject.toml`` or ``.git``. ``.git`` may be a FILE, not a
directory, inside a git worktree — plain ``exists()`` covers both.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

CHANNEL_NAME = "Coding without Pants"
DEFAULT_WHISPER_MODEL = "small"  # --model medium to escalate (plan.md §2)
DEFAULT_EPISODE_STATUS = "idea"

_ROOT_MARKERS = ("pyproject.toml", ".git")


class RepoRootNotFoundError(RuntimeError):
    """No ``pyproject.toml`` or ``.git`` found walking up from the start path."""


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up from *start* (default: cwd) to the first dir containing a root marker."""
    current = (start if start is not None else Path.cwd()).resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        for marker in _ROOT_MARKERS:
            if (candidate / marker).exists():
                return candidate
    raise RepoRootNotFoundError(
        f"No repo root found walking up from {current} (looked for pyproject.toml or .git)"
    )


@dataclass(frozen=True)
class Paths:
    """Canonical repo-relative paths (plan.md §8)."""

    root: Path

    @property
    def episodes_dir(self) -> Path:
        return self.root / "episodes"

    @property
    def voice_md(self) -> Path:
        return self.root / "voice.md"

    @property
    def build_contract_md(self) -> Path:
        return self.root / "build-contract.md"

    @property
    def pantsless_test_md(self) -> Path:
        return self.root / "pantsless-test.md"


def get_paths(start: Path | None = None) -> Paths:
    """Resolve the canonical :class:`Paths` for the repo containing *start*."""
    return Paths(root=find_repo_root(start))
