"""Brief (plan.md §14 Step 7): the kid-gateway distill — noisy transcript → ``brief.md``.

Powers ``cwp brief <id> [--dry-run]``. Reads ``episodes/<id>/capture/transcript.txt``
(Step 6's output), assembles a distill prompt that treats the transcript as ~25%-WER
noisy child speech (recover INTENT; keep the funniest verbatim mishearing as
``kid_quote``), calls the drafting.py claude seam (preflight first, draft-class ~60s
timeout), validates the reply against the Appendix C schema, redact-scans it, and
atomically writes ``brief.md``.

**Serialization (Appendix C, pinned).** ``brief.md`` OPENS with a fenced TOML
frontmatter block (```` ```toml … ``` ````) holding the structured fields, followed by
an optional human-readable prose body. This module owns the ONE parse/write pair —
:func:`write_brief` / :func:`load_brief` — that ``verify.py`` (Step 8) and ``build.py``
(Step 9) import; nothing else may parse brief.md. :func:`write_brief` validates before
writing, so the one writer can never produce a file its own loader rejects.

**The closed ``must_haves`` predicate vocabulary** lives here as the structured
constant :data:`MUST_HAVE_VOCABULARY` (prefix + payload rule + verifier meaning per
entry — Step 8 imports it to compile keypoint assertions; the distill prompt renders
its spec from the same constant, so prompt and validator cannot drift).
:func:`validate_must_have` / :func:`must_have_problem` enforce the rules:
a known prefix, split at the FIRST colon (:func:`split_must_have` — CSS payloads may
contain colons), a non-empty unpadded payload where one is required, and no payload at
all for ``sound_on_action``.

**``pantsless`` representation (the Appendix C "4 criteria" DECIDE, recorded here):**
a ``[pantsless]`` TOML table of exactly four booleans whose keys are DERIVED from
``episodes.PantslessTest``'s field names (:data:`PANTSLESS_CRITERIA` — one source of
truth with ``meta.toml``'s ``[pantsless_test]`` table, so the two can never drift).
Validation is shape-only (all four keys present, each a bool); the distill prompt asks
for all four ``= true`` (they are the criteria the build MUST satisfy), but a
hand-edited ``false`` still loads — the brief records commitments, not test results.

**Redaction (§4.3).** Superset of the letter of the spec: EVERY free-text field the
model produced (``one_sentence_goal``, ``single_action``, ``visual_motif``,
``kid_quote``, the ``must_haves`` payloads, and the prose body) is scanned via
capture.py's redact-names scanner before the write — a misheard real name can land in
any of them. ``kid_nickname`` is excluded: it IS the replacement value, resolved from
the redact file's FIRST nickname (else :data:`DEFAULT_KID_NICKNAME`) and FORCE-SET
after parsing — the nickname is operator configuration, never a model choice (a reply
that omits or mangles it therefore never burns the re-ask). Redaction preserves
vocabulary validity: nicknames are non-empty by ``load_redact_names``'s contract, so a
redacted payload stays non-empty.

**Reply handling.** Extraction from the model reply is LENIENT (the fence may sit
after preamble text; a fence-less reply that parses whole as TOML is accepted; text
after the closing fence becomes the prose body) — but :func:`load_brief` is STRICT
(the fence must open the file), because this module is brief.md's only writer. On an
invalid reply (no parseable TOML, schema violation, out-of-vocabulary must_have):
exactly ONE re-ask with the exact validation errors appended, then
:class:`BriefDistillError` (CLI exit 2) with the rejected reply flushed to
``brief.partial.txt`` for inspection.

Exit-code mapping (cli.py): :class:`BriefError` (missing episode/transcript, bad
redact file, unreadable brief.md — an :class:`cwp.episodes.EpisodeError`) → 1;
:class:`cwp.drafting.DraftEnvError` (claude missing / unauthed / timed out) and
:class:`BriefDistillError` (unparseable after the re-ask) → 2.
"""

from __future__ import annotations

import dataclasses
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import tomli_w

from cwp import capture, drafting, episodes
from cwp.capture import ScanState
from cwp.episodes import Episode

BRIEF_FILENAME = "brief.md"
DEFAULT_KID_NICKNAME = "the kid"  # used when private/redact-names.txt is absent/empty
MUST_HAVES_MIN = 3
MUST_HAVES_MAX = 5

# The four pantsless criteria, DERIVED from meta.toml's [pantsless_test] model (module
# docstring: one source of truth — episodes.PantslessTest owns the key names).
PANTSLESS_CRITERIA: tuple[str, ...] = tuple(
    field.name for field in dataclasses.fields(episodes.PantslessTest) if field.name != "notes"
)

_REQUIRED_STRING_FIELDS = (
    "one_sentence_goal",
    "single_action",
    "visual_motif",
    "kid_quote",
    "kid_nickname",
)


class BriefError(episodes.EpisodeError):
    """Base for brief-domain failures (CLI maps to exit 1, user error)."""


class BriefNotFoundError(BriefError):
    """``brief.md`` does not exist in the episode folder."""


class BriefFormatError(BriefError):
    """``brief.md`` (or a model reply) exists but violates the Appendix C contract:
    missing/misplaced TOML fence, invalid TOML, or schema/vocabulary violations."""


class TranscriptMissingError(BriefError):
    """``capture/transcript.txt`` is missing or empty — run ``cwp capture`` first."""


class BriefDistillError(drafting.DraftEnvError):
    """The model could not produce a valid brief after the one re-ask (CLI exit 2)."""


@dataclass(frozen=True)
class MustHavePredicate:
    """One entry of the closed vocabulary: prefix + payload rule + verifier meaning.

    ``verify.py`` (Step 8) compiles each prefix deterministically into a keypoint
    assertion; ``description`` doubles as the distill prompt's spec line.
    """

    prefix: str
    takes_payload: bool
    payload_hint: str  # e.g. "<css-selector>"; "" when takes_payload is False
    description: str

    @property
    def form(self) -> str:
        """The canonical written form, e.g. ``visible:<emoji-or-word>``."""
        return f"{self.prefix}:{self.payload_hint}" if self.takes_payload else self.prefix


# THE closed vocabulary (Appendix C) — brief.py owns this constant; verify.py imports it.
MUST_HAVE_VOCABULARY: tuple[MustHavePredicate, ...] = (
    MustHavePredicate(
        prefix="visible",
        takes_payload=True,
        payload_hint="<emoji-or-word>",
        description="the text/emoji is visible in the DOM after load",
    ),
    MustHavePredicate(
        prefix="element",
        takes_payload=True,
        payload_hint="<css-selector>",
        description="the CSS selector matches at least one element",
    ),
    MustHavePredicate(
        prefix="sound_on_action",
        takes_payload=False,
        payload_hint="",
        description=(
            "no AudioContext exists before the first main-action click; one is created"
            " and running after it (takes NO payload)"
        ),
    ),
    MustHavePredicate(
        prefix="state_change",
        takes_payload=True,
        payload_hint="<data-attr>",
        description="clicking the main action changes the named data attribute",
    ),
)

_PREDICATES_BY_PREFIX: dict[str, MustHavePredicate] = {
    predicate.prefix: predicate for predicate in MUST_HAVE_VOCABULARY
}
_ALLOWED_FORMS = ", ".join(predicate.form for predicate in MUST_HAVE_VOCABULARY)


def split_must_have(entry: str) -> tuple[str, str]:
    """Split a must_have entry into ``(prefix, payload)`` at the FIRST colon.

    The ONE splitting rule (verify.py's compiler reuses it): payloads may themselves
    contain colons (``element:a:hover``), so only the first colon separates. A
    colon-less entry yields ``(entry, "")``.
    """
    prefix, _sep, payload = entry.partition(":")
    return prefix, payload


def must_have_problem(entry: str) -> str | None:
    """The exact validation error for *entry*, or ``None`` when it is vocabulary-valid.

    These strings feed the re-ask prompt verbatim, so they say precisely what to fix.
    """
    prefix, payload = split_must_have(entry)
    predicate = _PREDICATES_BY_PREFIX.get(prefix)
    if predicate is None:
        return f"unknown predicate {prefix!r} (the only allowed forms are: {_ALLOWED_FORMS})"
    if not predicate.takes_payload:
        if ":" in entry:
            return f"{predicate.prefix} takes no payload — write exactly {predicate.prefix!r}"
        return None
    if ":" not in entry:
        return f"{predicate.prefix} needs a payload — write it as {predicate.form!r}"
    if not payload or payload != payload.strip():
        return f"{predicate.prefix} payload must be non-empty with no leading/trailing whitespace"
    return None


def validate_must_have(entry: str) -> bool:
    """True iff *entry* is in the closed Appendix C vocabulary (prefix + payload rules)."""
    return must_have_problem(entry) is None


@dataclass(frozen=True)
class Brief:
    """The Appendix C brief: the fenced-TOML fields + the optional prose body."""

    one_sentence_goal: str
    single_action: str
    visual_motif: str
    must_haves: tuple[str, ...]  # 3-5 entries, each vocabulary-valid
    kid_quote: str  # verbatim misheard words, redact-names-scanned
    kid_nickname: str  # the redaction replacement value (never a real name)
    pantsless: dict[str, bool]  # keys = PANTSLESS_CRITERIA (module docstring)
    prose: str = ""  # optional human-readable body after the closing fence


@dataclass(frozen=True)
class BriefResult:
    """What :func:`run_brief` did (the CLI renders this).

    ``brief is None`` means ``--dry-run`` (print ``prompt``). ``reasked`` marks that
    the first reply failed validation and the one re-ask succeeded.
    """

    prompt: str
    brief: Brief | None
    path: Path | None
    scan_state: ScanState
    redacted_count: int
    reasked: bool


def _to_document(brief: Brief) -> dict[str, Any]:
    """The TOML document for the frontmatter fence — scalars first, the table LAST
    (a scalar after a TOML table would land inside the table on re-parse)."""
    return {
        "one_sentence_goal": brief.one_sentence_goal,
        "single_action": brief.single_action,
        "visual_motif": brief.visual_motif,
        "must_haves": list(brief.must_haves),
        "kid_quote": brief.kid_quote,
        "kid_nickname": brief.kid_nickname,
        "pantsless": dict(brief.pantsless),
    }


def _schema_problems(data: object) -> list[str]:
    """EVERY Appendix C violation in *data*, as exact re-ask-ready strings.

    Unknown TOP-LEVEL keys are tolerated (forward compatibility, mirroring
    episodes.py's permissive reads); unknown keys INSIDE ``[pantsless]`` are not —
    that table is pinned to exactly the four criteria.
    """
    if not isinstance(data, Mapping):
        return ["the TOML top level must be a table holding the brief fields"]
    problems: list[str] = []
    for name in _REQUIRED_STRING_FIELDS:
        value = data.get(name)
        if value is None:
            problems.append(f"missing required field {name!r}")
        elif not isinstance(value, str):
            problems.append(f"{name} must be a string, got {type(value).__name__}")
        elif not value.strip():
            problems.append(f"{name} must not be empty")
    raw_must_haves = data.get("must_haves")
    if raw_must_haves is None:
        problems.append("missing required field 'must_haves'")
    elif not isinstance(raw_must_haves, list) or not all(
        isinstance(entry, str) for entry in raw_must_haves
    ):
        problems.append("must_haves must be an array of strings")
    else:
        if not MUST_HAVES_MIN <= len(raw_must_haves) <= MUST_HAVES_MAX:
            problems.append(
                f"must_haves needs {MUST_HAVES_MIN}-{MUST_HAVES_MAX} entries,"
                f" got {len(raw_must_haves)}"
            )
        for entry in raw_must_haves:
            problem = must_have_problem(entry)
            if problem is not None:
                problems.append(f"must_haves entry {entry!r}: {problem}")
    raw_pantsless = data.get("pantsless")
    if raw_pantsless is None:
        problems.append("missing required table 'pantsless'")
    elif not isinstance(raw_pantsless, Mapping):
        problems.append("pantsless must be a table of four booleans")
    else:
        for key in PANTSLESS_CRITERIA:
            if key not in raw_pantsless:
                problems.append(f"pantsless is missing criterion {key!r}")
            elif not isinstance(raw_pantsless[key], bool):
                problems.append(f"pantsless.{key} must be a boolean (true/false)")
        for key in raw_pantsless:
            if key not in PANTSLESS_CRITERIA:
                problems.append(
                    f"pantsless has unknown key {key!r}"
                    f" (exactly these are allowed: {', '.join(PANTSLESS_CRITERIA)})"
                )
    return problems


def _brief_from_mapping(data: Mapping[str, Any], *, prose: str) -> Brief:
    """Build a :class:`Brief` from an already-validated (zero-problems) mapping."""
    raw_pantsless = data["pantsless"]
    return Brief(
        one_sentence_goal=str(data["one_sentence_goal"]),
        single_action=str(data["single_action"]),
        visual_motif=str(data["visual_motif"]),
        must_haves=tuple(str(entry) for entry in data["must_haves"]),
        kid_quote=str(data["kid_quote"]),
        kid_nickname=str(data["kid_nickname"]),
        pantsless={key: bool(raw_pantsless[key]) for key in PANTSLESS_CRITERIA},
        prose=prose,
    )


# The frontmatter fence: ```toml opening a line, everything up to the FIRST line that
# is a bare ``` (universal newlines everywhere — read_text and subprocess text mode).
_TOML_FENCE_RE = re.compile(r"^```toml[ \t]*\n(?P<toml>.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)


def _find_fence(text: str) -> tuple[str, int, int] | None:
    """First ```toml fence in *text* → ``(inner_toml, start, end)``; ``None`` if absent."""
    match = _TOML_FENCE_RE.search(text)
    if match is None:
        return None
    return match.group("toml"), match.start(), match.end()


def write_brief(episode_dir: Path, brief: Brief) -> None:
    """Atomically write ``<episode_dir>/brief.md`` (Appendix C shape) — THE one writer.

    Validates the full schema first and raises :class:`BriefFormatError` on any
    violation, so this writer can never produce a brief.md that :func:`load_brief`
    rejects (round-trip by construction).
    """
    document = _to_document(brief)
    problems = _schema_problems(document)
    if problems:
        raise BriefFormatError(f"refusing to write an invalid brief: {'; '.join(problems)}")
    content = f"```toml\n{tomli_w.dumps(document)}```\n"
    prose = brief.prose.strip()
    if prose:
        content += f"\n{prose}\n"
    episodes.atomic_write_bytes(episode_dir / BRIEF_FILENAME, content.encode("utf-8"))


def load_brief(episode_dir: Path) -> Brief:
    """Parse ``<episode_dir>/brief.md`` — THE one parser (Steps 8/9 import this).

    STRICT (module docstring): the ```toml fence must open the file (Appendix C —
    "brief.md opens with a fenced TOML frontmatter block"); everything after the
    closing fence is the prose body. Raises :class:`BriefNotFoundError` on a missing
    file and :class:`BriefFormatError` on a missing fence, invalid TOML, or any
    schema/vocabulary violation. The Step-2 placeholder brief.md has no fence, so it
    fails here with the regenerate hint — a placeholder is not a brief.
    """
    path = episode_dir / BRIEF_FILENAME
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise BriefNotFoundError(f"{path} not found — distill one: cwp brief <id>") from exc
    except UnicodeDecodeError as exc:
        raise BriefFormatError(f"{path}: not valid UTF-8: {exc}") from exc
    except OSError as exc:
        raise BriefError(f"{path}: unreadable: {exc}") from exc
    body = content.lstrip("\ufeff \t\r\n")
    found = _find_fence(body)
    if found is None or found[1] != 0:
        raise BriefFormatError(
            f"{path}: does not open with a ```toml frontmatter fence — not a distilled"
            " brief (regenerate it: cwp brief <id>)"
        )
    toml_text, _start, end = found
    try:
        data = tomllib.loads(toml_text)
    except tomllib.TOMLDecodeError as exc:
        raise BriefFormatError(f"{path}: invalid TOML in the frontmatter fence: {exc}") from exc
    problems = _schema_problems(data)
    if problems:
        raise BriefFormatError(f"{path}: {'; '.join(problems)}")
    return _brief_from_mapping(data, prose=body[end:].strip())


def _vocabulary_spec() -> str:
    """The vocabulary section of the distill prompt, rendered FROM the constant."""
    return "\n".join(
        f"- `{predicate.form}` — {predicate.description}" for predicate in MUST_HAVE_VOCABULARY
    )


# Worked examples for the distill prompt (Step 7 spec: include 2-3). The element
# example uses a TOML literal string so the model needn't escape inner quotes.
_WORKED_EXAMPLES = """\
- "visible:🦖" — a dinosaur emoji must be visible once the page loads
- 'element:[data-testid="main-action"]' — the one big main-action element must exist
- "state_change:data-action-count" — pressing the main action must change that attribute"""


def build_distill_prompt(transcript: str, episode: Episode, kid_nickname: str) -> str:
    """Assemble the distill prompt: noisy-transcript guidance + the strict TOML contract.

    The vocabulary spec is rendered from :data:`MUST_HAVE_VOCABULARY` and the pantsless
    keys from :data:`PANTSLESS_CRITERIA`, so the prompt can never drift from the
    validator that judges the reply.
    """
    criteria_lines = "\n".join(f"{name} = true" for name in PANTSLESS_CRITERIA)
    return (
        "You distill what a 4-year-old asked for into a structured BUILD BRIEF for a tiny\n"
        'self-contained web toy, for the YouTube channel "Coding without Pants".\n'
        "\n"
        "## The transcript (local speech-to-text of the kid, verbatim)\n"
        "\n"
        f"{transcript}\n"
        "\n"
        "## How to read the transcript\n"
        "\n"
        "- It is NOISY child speech transcribed at roughly 25% word error rate: words are\n"
        "  misheard, mangled, or split. Recover the INTENT — what toy does the kid actually\n"
        "  want? Never take a garbled word literally.\n"
        "- EXCEPTION: kid_quote keeps his words VERBATIM. Pick the funniest mishearing and\n"
        "  copy it exactly as transcribed — do not clean it up.\n"
        f'- Refer to the child only as "{kid_nickname}" — never use a real name.\n'
        "\n"
        "## Episode context (meta.toml)\n"
        "\n"
        f"- id: {episode.id}\n"
        f"- title: {episode.title}\n"
        f"- hook: {episode.hook or '(none yet)'}\n"
        "\n"
        "## Output format (strict)\n"
        "\n"
        "Reply with ONE fenced TOML block — open with ```toml, close with ``` — holding\n"
        "exactly these fields:\n"
        "\n"
        "- one_sentence_goal (string): what the toy is, in one plain sentence\n"
        "- single_action (string): the ONE action the kid performs (one verb)\n"
        "- visual_motif (string): the emoji/theme he asked for (dinosaur, sock, cookie, ...)\n"
        f"- must_haves (array of {MUST_HAVES_MIN}-{MUST_HAVES_MAX} strings): entries ONLY\n"
        "  from the closed vocabulary below — no free-form text\n"
        "- kid_quote (string): his funniest verbatim misheard words, exactly as transcribed\n"
        f'- kid_nickname (string): exactly "{kid_nickname}"\n'
        "- [pantsless] table: the four criteria the build must satisfy — emit all four as\n"
        "  true (they are non-negotiable for a 4-year-old's toy):\n"
        "\n"
        "```\n"
        "[pantsless]\n"
        f"{criteria_lines}\n"
        "```\n"
        "\n"
        "You may add ONE short plain-prose paragraph AFTER the closing fence (optional).\n"
        "\n"
        "## must_haves vocabulary (CLOSED — use exactly these forms, nothing else)\n"
        "\n"
        f"{_vocabulary_spec()}\n"
        "\n"
        "Worked examples:\n"
        "\n"
        f"{_WORKED_EXAMPLES}\n"
        "\n"
        "Return ONLY the fenced TOML block (plus the optional prose paragraph) — no other\n"
        "commentary.\n"
    )


def _reask_prompt(prompt: str, errors: str) -> str:
    """The ONE re-ask: the original prompt + the exact validation errors appended."""
    return (
        f"{prompt}\n"
        "## Correction required\n"
        "\n"
        "Your previous reply failed validation:\n"
        "\n"
        f"{errors}\n"
        "\n"
        "Return ONLY the corrected fenced TOML block — fix exactly these problems and\n"
        "change nothing else.\n"
    )


def _parse_distill_response(text: str, kid_nickname: str) -> Brief:
    """Parse + validate a model reply (LENIENT extraction — module docstring).

    ``kid_nickname`` is force-set before validation: it is operator configuration, so
    a reply that omits or mangles it never burns the re-ask. Raises
    :class:`BriefFormatError` whose message is the exact re-ask-ready error list.
    """
    body = text.strip()
    found = _find_fence(body)
    if found is not None:
        toml_text, _start, end = found
        prose = body[end:].strip()
    else:  # lenient fallback: maybe the model replied with bare, fence-less TOML
        toml_text, prose = body, ""
    try:
        data = tomllib.loads(toml_text)
    except tomllib.TOMLDecodeError as exc:
        raise BriefFormatError(
            "- the reply did not contain a parseable ```toml block"
            f" (TOML error: {exc}) — return exactly one fenced TOML block"
        ) from exc
    data["kid_nickname"] = kid_nickname
    problems = _schema_problems(data)
    if problems:
        raise BriefFormatError("\n".join(f"- {problem}" for problem in problems))
    return _brief_from_mapping(data, prose=prose)


def _redact_brief(brief: Brief, pairs: tuple[tuple[str, str], ...]) -> tuple[Brief, int]:
    """Redact-scan every free-text field (module docstring scope) via capture's scanner."""
    total = 0

    def scan(value: str) -> str:
        nonlocal total
        redacted, count = capture.redact_text(value, pairs)
        total += count
        return redacted

    redacted_brief = replace(
        brief,
        one_sentence_goal=scan(brief.one_sentence_goal),
        single_action=scan(brief.single_action),
        visual_motif=scan(brief.visual_motif),
        must_haves=tuple(scan(entry) for entry in brief.must_haves),
        kid_quote=scan(brief.kid_quote),
        prose=scan(brief.prose),
    )
    return redacted_brief, total


def run_brief(
    episodes_dir: Path,
    redact_path: Path,
    id_or_seq: str,
    *,
    dry_run: bool = False,
    timeout: float | None = None,
) -> BriefResult:
    """The full ``cwp brief`` flow behind the CLI: read → distill → validate → scan → write.

    ``--dry-run`` returns after prompt assembly (no preflight, no claude call).
    Otherwise: preflight once per process, ONE claude call (draft-class timeout,
    ``brief.partial.txt`` as the idempotency flush), at most ONE re-ask with the exact
    validation errors, then the redact scan and the atomic write. Raises
    :class:`TranscriptMissingError` (exit 1) before any claude call when the
    transcript is missing or empty, and :class:`BriefDistillError` (exit 2) when the
    re-ask also fails validation.
    """
    directory, episode = episodes.load_episode(episodes_dir, id_or_seq)
    transcript_path = directory / "capture" / capture.TRANSCRIPT_FILENAME
    try:
        transcript = transcript_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise TranscriptMissingError(
            f"{transcript_path} not found — capture the kid clip first:"
            f" cwp capture {episode.id} --audio <clip>"
        ) from exc
    except UnicodeDecodeError as exc:
        raise BriefError(f"{transcript_path}: not valid UTF-8: {exc}") from exc
    except OSError as exc:
        raise BriefError(f"{transcript_path}: unreadable: {exc}") from exc
    if not transcript:
        raise TranscriptMissingError(
            f"{transcript_path} is empty — re-capture: cwp capture {episode.id} --audio <clip>"
        )
    pairs = capture.load_redact_names(redact_path)
    scan_state: ScanState = "unscanned" if pairs is None else "scanned"
    kid_nickname = pairs[0][1] if pairs else DEFAULT_KID_NICKNAME
    prompt = build_distill_prompt(transcript, episode, kid_nickname)
    if dry_run:
        return BriefResult(
            prompt=prompt,
            brief=None,
            path=None,
            scan_state=scan_state,
            redacted_count=0,
            reasked=False,
        )
    drafting.ensure_claude_ready()
    effective_timeout = drafting.DRAFT_TIMEOUT if timeout is None else timeout
    partial_path = directory / "brief.partial.txt"
    text = drafting.call_claude(prompt, timeout=effective_timeout, partial_path=partial_path)
    reasked = False
    try:
        parsed = _parse_distill_response(text, kid_nickname)
    except BriefFormatError as first_error:
        reasked = True
        retry_prompt = _reask_prompt(prompt, str(first_error))
        text = drafting.call_claude(
            retry_prompt, timeout=effective_timeout, partial_path=partial_path
        )
        try:
            parsed = _parse_distill_response(text, kid_nickname)
        except BriefFormatError as second_error:
            episodes.atomic_write_bytes(partial_path, text.encode("utf-8"))
            raise BriefDistillError(
                "the reply still failed brief validation after one re-ask:\n"
                f"{second_error}\n(the rejected reply was saved to {partial_path})"
            ) from second_error
    redacted_count = 0
    if pairs:
        parsed, redacted_count = _redact_brief(parsed, pairs)
    write_brief(directory, parsed)
    return BriefResult(
        prompt=prompt,
        brief=parsed,
        path=directory / BRIEF_FILENAME,
        scan_state=scan_state,
        redacted_count=redacted_count,
        reasked=reasked,
    )
