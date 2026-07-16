"""Root repo artifacts (Step 1): .gitignore privacy globs + the three SoT docs."""

from __future__ import annotations

from pathlib import Path

import pytest

from cwp.config import get_paths

REPO_ROOT = get_paths(Path(__file__)).root

# plan.md §4.3 privacy globs + environment/session artifacts — each must be a literal
# line in .gitignore (kid privacy is enforced structurally, not by convention).
REQUIRED_IGNORE_PATTERNS = [
    "media/",
    "private/",
    "clips/",
    "*.mp4",
    "*.mov",
    "*.wav",
    "*.m4a",
    "*.mp3",
    "*.ogg",
    "*.m4v",
    "*.3gp",
    "episodes/*/capture/",
    "episodes/*/project/.repair/",
    ".venv",
    "__pycache__/",
    ".plan-expedite-state*",
    ".ui-review-evidence/",
    ".build-step/",
]


@pytest.mark.parametrize("pattern", REQUIRED_IGNORE_PATTERNS)
def test_gitignore_contains_pattern(pattern: str) -> None:
    gitignore = REPO_ROOT / ".gitignore"
    assert gitignore.is_file()
    lines = [line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()]
    assert pattern in lines, f"required .gitignore pattern missing: {pattern}"


def test_three_root_sot_docs_exist() -> None:
    paths = get_paths(Path(__file__))
    for doc in (paths.voice_md, paths.build_contract_md, paths.pantsless_test_md):
        assert doc.is_file(), f"missing root SoT doc: {doc}"


def _plan_appendix_markdown_block(appendix_letter: str) -> str:
    """Extract the fenced ```markdown block of a plan.md §15 appendix (the SoT seed).

    Locates the ``### <letter>.`` appendix heading, then the next
    ```` ```markdown ```` fence, and returns the fence body. This keeps the plan
    appendix the ONE source of truth — the tests carry no third copy of the prose.
    """
    plan_text = (REPO_ROOT / "plan.md").read_text(encoding="utf-8")
    heading_prefix = f"### {appendix_letter}."
    heading_idx = plan_text.find(heading_prefix)
    assert heading_idx != -1, f"plan.md appendix heading not found: {heading_prefix!r}"
    fence_open = plan_text.find("```markdown\n", heading_idx)
    assert fence_open != -1, f"no ```markdown fence after plan.md appendix {appendix_letter}"
    body_start = fence_open + len("```markdown\n")
    fence_close = plan_text.find("\n```", body_start)
    assert fence_close != -1, f"unclosed ```markdown fence in plan.md appendix {appendix_letter}"
    return plan_text[body_start:fence_close]


def test_voice_md_equals_plan_appendix_a_verbatim() -> None:
    expected = _plan_appendix_markdown_block("A")
    actual = (REPO_ROOT / "voice.md").read_text(encoding="utf-8")
    assert actual.rstrip("\n") == expected.rstrip("\n")


def test_pantsless_test_md_equals_plan_appendix_e_verbatim() -> None:
    expected = _plan_appendix_markdown_block("E")
    actual = (REPO_ROOT / "pantsless-test.md").read_text(encoding="utf-8")
    assert actual.rstrip("\n") == expected.rstrip("\n")


def test_build_contract_covers_the_machine_hooks() -> None:
    text = (REPO_ROOT / "build-contract.md").read_text(encoding="utf-8")
    for anchor in (
        'data-testid="main-action"',
        "data-action-count",
        "file://",
        "FORBIDDEN_PATTERNS",
        "{one_sentence_goal}",
        "{single_action}",
        "{visual_motif}",
        "{must_haves}",
        "{kid_quote}",
        "{kid_nickname}",
        "```html",
        "alert",
        "fetch(",
        "AudioContext",
        "Pantsless Test",
        "verbatim",
    ):
        assert anchor in text, f"build-contract.md missing anchor: {anchor!r}"
