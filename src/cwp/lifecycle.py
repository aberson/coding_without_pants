"""Episode lifecycle (plan.md §5.3): the PERMISSIVE state machine + ``cwp next`` priority.

The happy path is ``idea -> scripted -> built -> recorded -> edited -> published``, but
transitions NEVER block — a 4-year-old co-star means reshoots, so an unusual jump gets a
stderr warning and still succeeds. "Unusual" is defined mechanically: anything that is not
(a) one step forward on the happy path, (b) ``edited -> recorded`` (a reshoot), (c) any
move to or from ``on-hold``, or (d) a move INTO ``cut``. ``cut`` is terminal: moves OUT of
it are always unusual (even ``cut -> on-hold`` — the terminal rule beats the hold rule) —
still allowed, still warned. ``cut`` is also hidden from the default ``cwp list``
(``--all`` shows it).

Every transition appends one ``[[history]]`` row with a §4.1 UTC ISO 8601 stamp —
append-only, prior entries are never rewritten. Reaching ``published`` also stamps
``published_at`` (only if empty, so a re-publish never destroys the original cycle-time
stamp; ``cwp publish --url`` in Step 5 behaves consistently).

Powers ``cwp status`` / ``cwp next`` (§6).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from cwp import episodes
from cwp.episodes import Episode, HistoryEntry

HAPPY_PATH = ("idea", "scripted", "built", "recorded", "edited", "published")
HOLD_STATUS = "on-hold"
CUT_STATUS = "cut"
PUBLISHED_STATUS = "published"

RESHOOT = ("edited", "recorded")  # the (b) jump: the take didn't survive the edit

# §5.3: cut episodes are hidden from the default ``cwp list`` (``--all`` shows them).
# on-hold stays visible — parked work must not vanish from the overview.
HIDDEN_FROM_DEFAULT_LIST = (CUT_STATUS,)

# In-flight = an episode ``cwp next`` may suggest: not done, not killed, not parked.
NOT_IN_FLIGHT = (PUBLISHED_STATUS, CUT_STATUS, HOLD_STATUS)

# One-line suggested next action per in-flight status ({id} is the episode id).
NEXT_ACTIONS = {
    "idea": "draft a script: cwp draft {id} script",
    "scripted": "build the toy: cwp build {id}",
    "built": "record it (camera time — see docs/production-notes.md)",
    "recorded": "edit it (cut the video, then: cwp status {id} edited)",
    "edited": "publish prep: cwp publish {id}",
}

_USUAL_MOVES = (
    "usual moves are one step forward on "
    f"{' -> '.join(HAPPY_PATH)}, a reshoot (edited -> recorded), "
    "any <-> on-hold, or -> cut"
)


class UnknownStatusError(episodes.EpisodeError):
    """The target status is not one of the §4.1 statuses (CLI maps to exit 1)."""


@dataclass(frozen=True)
class Transition:
    """What ``apply_status`` did: the persisted episode + how the jump classified."""

    episode: Episode
    directory: Path
    old_status: str
    new_status: str
    stamp: str
    unusual_reason: str | None  # None = a §5.3 usual move; else why it warranted a warning
    published_at_stamped: bool


@dataclass(frozen=True)
class NextSuggestion:
    """``cwp next``'s pick: the most-advanced in-flight episode + its one-line action."""

    episode: Episode
    action: str


def unusual_reason(current: str, target: str) -> str | None:
    """``None`` when the jump is a §5.3 usual move; else a short human-readable reason.

    Mechanical definition (module docstring): usual = (a) one step forward on the happy
    path, (b) the reshoot, (c) any <-> on-hold, (d) -> cut. The terminal rule is checked
    FIRST so ``cut -> on-hold`` / ``cut -> anything`` stays unusual. Unknown (hand-edited)
    current statuses fall through to unusual naturally — permissive reads make them real.
    """
    if current == CUT_STATUS and target != CUT_STATUS:
        return f"{CUT_STATUS} is terminal ({_USUAL_MOVES})"
    if target == CUT_STATUS:
        return None  # (d) anything can be cut (including cut -> cut)
    if HOLD_STATUS in (current, target):
        return None  # (c) any <-> on-hold
    if (current, target) == RESHOOT:
        return None  # (b) reshoots happen
    if (
        current in HAPPY_PATH
        and target in HAPPY_PATH
        and HAPPY_PATH.index(target) == HAPPY_PATH.index(current) + 1
    ):
        return None  # (a) one step forward
    return _USUAL_MOVES


def apply_status(
    episodes_dir: Path, id_or_seq: str, target: str, *, now: str | None = None
) -> Transition:
    """Set an episode's status, append the ``[[history]]`` row, persist atomically.

    Permissive: an unusual jump is REPORTED (``Transition.unusual_reason``), never
    blocked. Reaching ``published`` stamps ``published_at`` if (and only if) it is
    still empty. *now* overrides the stamp (tests); default is real UTC now. Raises
    :class:`UnknownStatusError` for a status outside §4.1 (exit 1 at the CLI) and the
    usual :class:`episodes.EpisodeError` family for a missing/corrupt episode.
    """
    if target not in episodes.STATUSES:
        raise UnknownStatusError(
            f"Unknown status {target!r} (expected one of: {', '.join(episodes.STATUSES)})"
        )
    directory, episode = episodes.load_episode(episodes_dir, id_or_seq)
    old_status = episode.status
    stamp = now if now is not None else episodes.utc_now_iso()
    reason = unusual_reason(old_status, target)
    episode.status = target
    episode.history.append(HistoryEntry(status=target, at=stamp))  # append-only (§4.1)
    published_at_stamped = False
    if target == PUBLISHED_STATUS and not episode.published_at:
        episode.published_at = stamp
        published_at_stamped = True
    episodes.write_meta(directory, episode)
    return Transition(
        episode=episode,
        directory=directory,
        old_status=old_status,
        new_status=target,
        stamp=stamp,
        unusual_reason=reason,
        published_at_stamped=published_at_stamped,
    )


def visible_in_default_list(episode: Episode) -> bool:
    """§5.3: ``cut`` episodes are hidden from the default ``cwp list`` (``--all`` shows)."""
    return episode.status not in HIDDEN_FROM_DEFAULT_LIST


def _advancement(status: str) -> int:
    """Happy-path index for the ``cwp next`` priority; unknown statuses rank below idea."""
    try:
        return HAPPY_PATH.index(status)
    except ValueError:
        return -1


def next_action(episode: Episode) -> str:
    """The one-line suggested action for an in-flight episode's current status."""
    template = NEXT_ACTIONS.get(episode.status)
    if template is None:  # hand-edited unknown status — permissive reads make it real
        return (
            f"fix its status first: cwp status {episode.id} <status> "
            f"(current {episode.status!r} is not a known status)"
        )
    return template.format(id=episode.id)


def pick_next(candidates: Sequence[Episode]) -> NextSuggestion | None:
    """The most-advanced in-flight episode (closest to published), lowest seq on ties.

    In-flight = not published / cut / on-hold (§5.3). ``None`` when nothing is in
    flight — the CLI turns that into a friendly message, exit 0.
    """
    in_flight = [episode for episode in candidates if episode.status not in NOT_IN_FLIGHT]
    if not in_flight:
        return None
    best = min(in_flight, key=lambda episode: (-_advancement(episode.status), episode.seq))
    return NextSuggestion(episode=best, action=next_action(best))
