"""Per-episode file templates (plan.md §4.2) + the 12-episode seed-bank data.

``meta.toml`` is NOT a template — it is GENERATED from the ``Episode`` dataclass
(``episodes.py``, plan.md §4.1). This module owns the skeletons for the other
per-episode files: ``script.md``, the ``publish.md`` / ``brief.md`` placeholders,
and the ``project/index.html`` placeholder.

Render helpers take plain strings (never an ``Episode``) so this module never
imports ``episodes.py`` — ``episodes.py`` imports *us*, and a cycle would break
``cwp --help``'s fast import path.

The three root SoT docs (``voice.md``, ``build-contract.md``, ``pantsless-test.md``)
are NOT templates — they were authored once, directly at the repo root, in Step 1.
"""

from __future__ import annotations

import html
from dataclasses import dataclass


@dataclass(frozen=True)
class SeedEpisode:
    """One row of the plan.md §5.5 seed bank (hooks: Appendix D) — pure data.

    ``cwp seed`` feeds each row through :func:`cwp.episodes.create_episode`, so this
    module never imports ``episodes.py`` (it imports *us*, and a cycle would break the
    fast ``cwp --help`` import path). The ``hook``/``teaches`` strings carry non-ASCII
    (``≤``, ``–``, ``→``); the file is UTF-8 and these are ordinary ``str`` literals, so
    ``cwp list``'s UTF-8-reconfigured stdout (``cli._reconfigure_utf8``) renders them
    without a ``UnicodeEncodeError`` on a Windows cp1252 console or a captured pipe.
    """

    seq: int
    title: str
    ingredient: str  # one of episodes.INGREDIENTS
    effort: str  # one of episodes.EFFORTS
    teaches: str
    hook: str
    kid_usable: bool
    tags: tuple[str, ...] = ()


# The 12-episode idea bank (plan.md §5.5 table + Appendix D hooks): ranked easiest-first,
# balanced 3/3/3/3 across ingredients (neetcode / hak / kid / xkcd), seqs 001–012. Seeded
# as ``idea``-status episodes by ``cwp seed`` (idempotent — see episodes.seed_episodes).
# ``kid_usable`` follows the toy, not the ingredient: every toy here passes the Pantsless
# Test EXCEPT 002 (an adult heat-index gag readout a 4-year-old does not operate). The
# three ``kid``-ingredient episodes (005 / 007 / 012) are kid-operated by definition.
SEED_EPISODES: tuple[SeedEpisode, ...] = (
    SeedEpisode(
        seq=1,
        title="The Number-Guessing Machine (Binary Search, No Cheating)",
        ingredient="neetcode",
        effort="S",
        teaches="binary search",
        hook=(
            "20 Questions as an app that always wins in ≤7 guesses, "
            'then races Dad guessing "randomly."'
        ),
        kid_usable=True,
    ),
    SeedEpisode(
        seq=2,
        title="The Precise Moment Pants Become Optional: A Live Hawaii Pants Index",
        ingredient="hak",
        effort="S",
        teaches="formula/heat-index modeling",
        hook="A dead-serious heat-index formula for exactly when pants stop being load-bearing.",
        kid_usable=False,
    ),
    SeedEpisode(
        seq=3,
        title="The Sock-Matching Machine (Two Sum, But Socks)",
        ingredient="neetcode",
        effort="S",
        teaches="hash-map pairing (Two Sum)",
        hook=(
            "The infamous interview question, solved for pairing "
            "a preschooler's socks after laundry."
        ),
        kid_usable=True,
    ),
    SeedEpisode(
        seq=4,
        title="The Unbeatable Cookie-Splitter",
        ingredient="hak",
        effort="S",
        teaches='"I cut, you choose" fairness/game theory',
        hook='Sibling cookie warfare ended with the "I cut, you choose" theorem in one button.',
        kid_usable=True,
    ),
    SeedEpisode(
        seq=5,
        title="I Let My 4-Year-Old Prompt Claude (No Notes)",
        ingredient="kid",
        effort="S",
        teaches="prompting / AI as a filmed topic",
        hook=(
            "Hand the keyboard to a kid who can't spell; "
            "build exactly what he types, live, unedited."
        ),
        kid_usable=True,
    ),
    SeedEpisode(
        seq=6,
        title="FizzBuzz, But It's a Dinosaur",
        ingredient="neetcode",
        effort="S",
        teaches="modulo / FizzBuzz",
        hook=(
            "Five clean lines, then a big-button counting toy "
            'that roars instead of printing "Fizz."'
        ),
        kid_usable=True,
    ),
    SeedEpisode(
        seq=7,
        title="Is the Dice Cheating? (My Daughter Runs the Audit)",
        ingredient="kid",
        effort="S",
        teaches="uniformity / chi-square intuition",
        hook=(
            "100 rolls into a tally app running a real fairness test; "
            'she\'s cleared to yell "CHEATER."'
        ),
        kid_usable=True,
    ),
    SeedEpisode(
        seq=8,
        title="A Bedtime Story Picker That Never Repeats (Until It Has To)",
        ingredient="hak",
        effort="S",
        teaches="Fisher–Yates shuffle",
        hook="A calm Fisher–Yates walk landing on one mashable PICK MY STORY button.",
        kid_usable=True,
    ),
    SeedEpisode(
        seq=9,
        title="Are We There Yet? (An Honest Answer, Powered by Math)",
        ingredient="xkcd",
        effort="M",
        teaches="haversine distance",
        hook="Real haversine distance under one big honest button.",
        kid_usable=True,
    ),
    SeedEpisode(
        seq=10,
        title="Shortest Path to the Potty (An Emergency BFS)",
        ingredient="xkcd",
        effort="M",
        teaches="BFS / shortest path",
        hook=(
            "Kid crayons a house-maze; BFS finds the provably shortest route before it's too late."
        ),
        kid_usable=True,
    ),
    SeedEpisode(
        seq=11,
        title="Scream-to-Watts: Could Bath-Time Meltdowns Power the House?",
        ingredient="xkcd",
        effort="M",
        teaches="decibel → energy physics",
        hook=(
            "Real decibel→energy physics off a live mic; "
            "a dial answers whether one tantrum runs the fridge."
        ),
        kid_usable=True,
    ),
    SeedEpisode(
        seq=12,
        title="Lego Ouch Calories: Barefoot Steps Converted to Calories Burned",
        ingredient="kid",
        effort="S",
        teaches="light arithmetic modeling",
        hook=(
            "Every Lego brick underfoot = a calorie; the kid taps "
            "a live tally, we do the excruciating math."
        ),
        kid_usable=True,
    ),
)

_SCRIPT_MD = """\
# {title}

<!-- Read-aloud script + on-screen action notes (plan.md §4.2).
     Draft it in the channel voice: cwp draft {episode_id} script -->

## Hook

_(the first 15 seconds — why keep watching?)_

## Script

## On-screen actions

"""

# The sentinel drafting.py checks before appending title/description drafts into
# publish.md: present only while publish.md is still the placeholder below; `cwp publish`
# (Step 5) regenerates the file without it. A drift test asserts it stays in _PUBLISH_MD.
PUBLISH_PLACEHOLDER_SENTINEL = "<!-- PLACEHOLDER"

# The sentinel `cwp build` (Step 9) checks for clobber protection: present ONLY in the
# scaffold index.html below, so a placeholder is safe to overwrite while a real (or
# hand-edited) toy that dropped it is protected without `--force`. Mirrors the publish
# sentinel; a drift test asserts it stays in the rendered placeholder.
INDEX_HTML_PLACEHOLDER_SENTINEL = "<!-- cwp:placeholder-toy -->"

_PUBLISH_MD = """\
# {title} — publish metadata

<!-- PLACEHOLDER — `cwp publish {episode_id}` (Step 5) regenerates this file with
     paste-ready YouTube metadata from meta.toml + any drafted blocks appended below
     by `cwp draft`. Edit meta.toml (title/hook/tags), not this file. -->
"""

_BRIEF_MD = """\
# {title} — build brief

<!-- PLACEHOLDER — `cwp brief {episode_id}` (Step 7) distills the kid transcript
     (capture/transcript.txt) into this brief, which `cwp build` turns into the
     toy at project/index.html. -->
"""


def render_script_md(*, title: str, episode_id: str) -> str:
    """The ``script.md`` skeleton: read-aloud script + on-screen action notes."""
    return _SCRIPT_MD.format(title=title, episode_id=episode_id)


def render_publish_md(*, title: str, episode_id: str) -> str:
    """The ``publish.md`` placeholder — real content is generated by ``cwp publish``."""
    return _PUBLISH_MD.format(title=title, episode_id=episode_id)


def render_brief_md(*, title: str, episode_id: str) -> str:
    """The ``brief.md`` placeholder — real content is generated by ``cwp brief``."""
    return _BRIEF_MD.format(title=title, episode_id=episode_id)


def render_index_html_placeholder(*, title: str, episode_id: str) -> str:
    """A minimal valid ``project/index.html`` placeholder.

    The verified toy is generated by ``cwp build`` (Step 9), which per §4.2 clobber
    protection only overwrites this page on a verified pass (or with ``--force``).
    Titles may contain HTML-special and non-ASCII characters — escaped here.
    """
    safe_title = html.escape(title)
    safe_id = html.escape(episode_id)
    return (
        "<!doctype html>\n"
        f"{INDEX_HTML_PLACEHOLDER_SENTINEL}\n"
        '<html lang="en">\n'
        '<meta charset="utf-8">\n'
        f"<title>{safe_title}</title>\n"
        f"<h1>{safe_title}</h1>\n"
        f"<p>Placeholder for episode {safe_id} — the real toy is generated by\n"
        f"<code>cwp build</code> and replaces this page on a verified pass.</p>\n"
        "</html>\n"
    )
