"""Publish prep (plan.md §14 Step 5): the Studio-ordered paste block + publish recording.

Powers ``cwp publish <id> [--url <url>]`` (§6 ``publishing.py`` row). One run WHOLESALE-
REGENERATES ``publish.md`` from meta.toml plus any drafted content already in the file
(Step 4's ``cwp draft title/description`` appends marker-first blocks there) and prints
the paste block to stdout. The block is **Studio-ordered** (:data:`STUDIO_FIELDS` —
Title, Description, Tags, Thumbnail text: the top-to-bottom order of YouTube Studio's
upload form), one ``##`` heading per field, so the operator pastes field-by-field with
zero reformatting.

Folding rules:

- Drafted sections WIN over auto-derived content. The description draft is used verbatim.
  The title draft is a candidate LIST (5 lines), so its first non-empty line is the folded
  title — prune the preserved draft block down to your pick, or delete the block to fall
  back to meta.toml's ``title``. The newest block wins when the same kind was drafted
  repeatedly.
- Auto-derived fallbacks: description = ``hook`` + a ``What you'll learn:`` line from
  ``teaches``; thumbnail text = a short punchy line — the title's leading clause (else the
  hook's), whole words within :data:`THUMBNAIL_MAX_CHARS`.
- The winning drafted blocks are PRESERVED verbatim below the paste block, so re-running
  ``cwp publish`` folds the same content again instead of silently reverting to the
  fallbacks — regeneration is idempotent and never loses a drafted section. The block
  heading shape is imported from ``drafting.py`` (``publish_draft_heading``), so the
  producer and this parser cannot drift.

Validation is WARN-DON'T-BLOCK: a missing title / description / tags / thumbnail text
each produce a stderr warning (via the CLI) and the file is still written with whatever is
available. An ``<!-- AI DRAFT -->`` marker anywhere in the regenerated content (the
literal imported from ``drafting.py`` — one source of truth) warns "review AI-drafted
content before publishing"; the marker persists in the preserved block until the operator
deletes it, so the warning repeats until the draft is actually reviewed.

The unconditional "Before you publish" checklist (§4.3 kid-privacy contract: the YouTube
"Made for Kids" audience setting + the real-name scan) is printed on STDOUT after the
paste block — documented choice: stdout keeps block + checklist together under
redirection, and stderr stays reserved for warnings. The checklist is also embedded in
``publish.md`` so the file is self-contained at review time.

``--url <url>`` additionally records ``youtube_url`` in meta.toml (last write wins — a
wrong link may be re-recorded) and transitions status to ``published`` via
``lifecycle.apply_status``, which appends the ``[[history]]`` row and stamps
``published_at`` only if still empty (§5.3: a re-publish never destroys the original
cycle-time stamp). Without ``--url`` the status is NOT touched.

Exit codes (cli.py): missing/corrupt episode or an empty ``--url`` → 1 (user error);
no repo root → 2 (environment).
"""

from __future__ import annotations

from collections.abc import Container, Mapping
from dataclasses import dataclass
from pathlib import Path

from cwp import episodes, lifecycle
from cwp.drafting import AI_DRAFT_MARKER, STDOUT_KINDS, publish_draft_heading
from cwp.episodes import Episode

PUBLISH_FILENAME = "publish.md"

# The Step 5 ordering contract: YouTube Studio's upload form, top to bottom.
STUDIO_FIELDS: tuple[str, ...] = ("Title", "Description", "Tags", "Thumbnail text")

THUMBNAIL_MAX_CHARS = 32  # a thumbnail line carries ~3-6 words; longer stops being punchy

CHECKLIST_HEADING = "Before you publish"
# The §4.3 kid-privacy contract — printed unconditionally AND embedded in publish.md.
CHECKLIST_ITEMS: tuple[str, ...] = (
    'Set the YouTube "Made for Kids" audience setting for this video (child content — COPPA).',
    "Real-name scan: the kid's nickname only — no real name or identifying details in the "
    "title, description, tags, or thumbnail text.",
)

# A title's leading clause ends at the first of these (subtitle/aside punctuation).
_CLAUSE_BREAKS = (":", "(", "—", "–")


@dataclass(frozen=True)
class DraftedSection:
    """One draft block parsed back out of publish.md (Step 4's append shape)."""

    kind: str
    text: str
    marked: bool  # True while the block still opens with the AI-draft marker (unreviewed)


@dataclass(frozen=True)
class PublishFields:
    """The four resolved Studio fields, drafted-over-derived merge already applied."""

    title: str
    description: str
    tags: str  # comma-joined — the shape Studio's tag box accepts as one paste
    thumbnail_text: str


@dataclass(frozen=True)
class PublishResult:
    """What :func:`run_publish` did (the CLI renders this)."""

    episode: Episode
    directory: Path
    publish_path: Path
    block: str  # the Studio-ordered paste block (also embedded in publish.md)
    warnings: tuple[str, ...]
    transition: lifecycle.Transition | None  # None without --url: status untouched


def _is_block_terminator(line: str, known_headings: Container[str]) -> bool:
    """A drafted block ends ONLY at the next AI-draft marker line, the next KNOWN
    drafted-section heading (the exact ``publish_draft_heading`` strings), or EOF.

    Generic ``---`` rules and ``##`` sub-headings are plausible LLM-drafted CONTENT and
    must survive the fold — publish wholesale-rewrites the file, so a lossy parse here
    would silently destroy drafted text permanently (review iteration 2, Finding 1).
    """
    stripped = line.strip()
    return stripped == AI_DRAFT_MARKER or stripped in known_headings


def _preceded_by_marker(lines: list[str], heading_index: int) -> bool:
    """True when the nearest non-blank line above the heading is the AI-draft marker."""
    for line in reversed(lines[:heading_index]):
        stripped = line.strip()
        if not stripped:
            continue
        return stripped == AI_DRAFT_MARKER
    return False


def extract_drafted_sections(publish_text: str) -> dict[str, DraftedSection]:
    """Parse Step-4 draft blocks (and this module's preserved copies) out of publish.md.

    A block is ``publish_draft_heading(kind)`` — optionally preceded by the AI-draft
    marker — followed by content running to the next block boundary
    (:func:`_is_block_terminator`: marker line, known drafted-section heading, or EOF;
    NEVER generic ``---``/``##`` lines, which are content). The LAST block with non-empty
    content wins per kind (repeat drafts append; the newest is the one the operator saw
    last); empty blocks are ignored.
    """
    headings = {publish_draft_heading(kind): kind for kind in STDOUT_KINDS}
    lines = publish_text.splitlines()
    found: dict[str, DraftedSection] = {}
    for index, line in enumerate(lines):
        kind = headings.get(line.strip())
        if kind is None:
            continue
        body: list[str] = []
        for content_line in lines[index + 1 :]:
            if _is_block_terminator(content_line, headings):
                break
            body.append(content_line)
        text = "\n".join(body).strip()
        if text:
            found[kind] = DraftedSection(
                kind=kind, text=text, marked=_preceded_by_marker(lines, index)
            )
    return found


def derive_description(hook: str, teaches: str) -> str:
    """The auto-derived description: the hook paragraph + a ``What you'll learn:`` line."""
    parts = []
    if hook.strip():
        parts.append(hook.strip())
    if teaches.strip():
        parts.append(f"What you'll learn: {teaches.strip()}")
    return "\n\n".join(parts)


def _leading_clause(text: str) -> str:
    stripped = text.strip()
    cut = len(stripped)
    for breaker in _CLAUSE_BREAKS:
        position = stripped.find(breaker)
        if 0 < position < cut:
            cut = position
    return stripped[:cut].strip()


def _trim_to_words(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    kept = ""
    for word in text.split():
        candidate = f"{kept} {word}".strip()
        if len(candidate) > limit:
            break
        kept = candidate
    return kept or text[:limit].rstrip()


def derive_thumbnail_text(title: str, hook: str) -> str:
    """A short punchy thumbnail line: the title's leading clause (else the hook's),
    trimmed to whole words within :data:`THUMBNAIL_MAX_CHARS`. Empty when both are."""
    for source in (title, hook):
        clause = _leading_clause(source)
        if clause:
            return _trim_to_words(clause, THUMBNAIL_MAX_CHARS)
    return ""


def _first_content_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and stripped != AI_DRAFT_MARKER:
            return stripped
    return ""


def resolve_fields(
    episode: Episode, drafted: Mapping[str, DraftedSection]
) -> tuple[PublishFields, list[str]]:
    """Merge meta.toml + drafted sections into the four Studio fields + a warning list.

    Drafted content wins over auto-derived; every missing field is a WARNING (the Step 5
    warn-don't-block contract), never an error — publish writes what it can.
    """
    warnings: list[str] = []
    title = ""
    title_draft = drafted.get("title")
    if title_draft is not None:  # the draft is a candidate list: its first line is the pick
        title = _first_content_line(title_draft.text)
    if not title:
        title = episode.title.strip()
    if not title:
        warnings.append("missing title — set title in meta.toml")

    description_draft = drafted.get("description")
    if description_draft is not None:
        description = description_draft.text
    else:
        description = derive_description(episode.hook, episode.teaches)
    if not description:
        warnings.append(
            "missing description — set hook/teaches in meta.toml"
            f" or run: cwp draft {episode.id} description"
        )

    tags = ", ".join(tag for tag in (raw.strip() for raw in episode.tags) if tag)
    if not tags:
        warnings.append("missing tags — set tags in meta.toml")

    thumbnail_text = derive_thumbnail_text(title, episode.hook)
    if not thumbnail_text:
        warnings.append("missing thumbnail text — set title or hook in meta.toml")

    fields = PublishFields(
        title=title, description=description, tags=tags, thumbnail_text=thumbnail_text
    )
    return fields, warnings


def _ordered_values(fields: PublishFields) -> tuple[str, ...]:
    """Field values in :data:`STUDIO_FIELDS` order (zipped strict — drift fails loud)."""
    return (fields.title, fields.description, fields.tags, fields.thumbnail_text)


def render_paste_block(fields: PublishFields) -> str:
    """The Studio-ordered paste block: one delimited ``##`` section per upload-form field."""
    parts = []
    for heading, value in zip(STUDIO_FIELDS, _ordered_values(fields), strict=True):
        parts.append(f"## {heading}\n\n{value.strip()}".rstrip())
    return "\n\n".join(parts) + "\n"


def _checklist_lines() -> str:
    return "\n".join(f"- [ ] {item}" for item in CHECKLIST_ITEMS)


def render_checklist() -> str:
    """The unconditional "Before you publish" checklist (stdout, after the paste block)."""
    return f"{CHECKLIST_HEADING}:\n{_checklist_lines()}\n"


def _checklist_section() -> str:
    return f"## {CHECKLIST_HEADING}\n\n{_checklist_lines()}"


def _preserved_draft_blocks(drafted: Mapping[str, DraftedSection]) -> str:
    blocks = []
    for kind in STDOUT_KINDS:  # stable order, independent of parse order
        section = drafted.get(kind)
        if section is None:
            continue
        lines = ([AI_DRAFT_MARKER] if section.marked else []) + [
            publish_draft_heading(kind),
            "",
            section.text,
        ]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_publish_file(
    episode: Episode, *, block: str, drafted: Mapping[str, DraftedSection]
) -> str:
    """The regenerated publish.md: header + paste block + embedded checklist + preserved
    draft blocks. NO placeholder sentinel — from here on ``cwp draft`` is stdout-only."""
    header_title = episode.title.strip() or episode.id
    note = (
        f"<!-- Generated by `cwp publish {episode.id}` — regenerated wholesale on every run.\n"
        "     Edit meta.toml (title/hook/teaches/tags) or the draft blocks at the bottom,\n"
        "     not the sections here. Paste each section into YouTube Studio, top to bottom. -->"
    )
    parts = [
        f"# {header_title} — publish metadata",
        note,
        block.rstrip("\n"),
        "---",
        _checklist_section(),
    ]
    preserved = _preserved_draft_blocks(drafted)
    if preserved:
        parts.extend(["---", preserved])
    return "\n\n".join(parts) + "\n"


def _read_publish_text(publish_path: Path) -> str:
    """Current publish.md text; a missing file is fine (regenerate from meta alone)."""
    try:
        return publish_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except UnicodeDecodeError as exc:
        raise episodes.EpisodeError(
            f"{publish_path} is not valid UTF-8 ({exc}) — fix or delete it, then re-run"
        ) from exc
    except OSError as exc:
        raise episodes.EpisodeError(f"{publish_path} unreadable: {exc}") from exc


def run_publish(
    episodes_dir: Path, id_or_seq: str, *, url: str | None = None, now: str | None = None
) -> PublishResult:
    """The full ``cwp publish`` flow behind the CLI (module docstring for the contract).

    Always regenerates + atomically writes publish.md and returns the paste block +
    warnings. With *url* it then records ``youtube_url`` (written to meta BEFORE the
    transition so ``apply_status``'s reload sees it) and transitions to ``published``
    via ``lifecycle.apply_status`` — history row appended, ``published_at`` stamped only
    if still empty. *now* overrides the transition stamp (tests).
    """
    if url is not None and not url.strip():
        raise episodes.EpisodeError("--url must be a non-empty URL")
    directory, episode = episodes.load_episode(episodes_dir, id_or_seq)
    publish_path = directory / PUBLISH_FILENAME
    drafted = extract_drafted_sections(_read_publish_text(publish_path))
    fields, warnings = resolve_fields(episode, drafted)
    block = render_paste_block(fields)
    content = render_publish_file(episode, block=block, drafted=drafted)
    if AI_DRAFT_MARKER in content:
        warnings.append(
            f"review AI-drafted content before publishing — {AI_DRAFT_MARKER} marker(s)"
            f" still present in {PUBLISH_FILENAME}"
        )
    episodes.atomic_write_bytes(publish_path, content.encode("utf-8"))
    transition: lifecycle.Transition | None = None
    if url is not None:
        episode.youtube_url = url.strip()
        episodes.write_meta(directory, episode)
        transition = lifecycle.apply_status(
            episodes_dir, id_or_seq, lifecycle.PUBLISHED_STATUS, now=now
        )
        episode = transition.episode
    return PublishResult(
        episode=episode,
        directory=directory,
        publish_path=publish_path,
        block=block,
        warnings=tuple(warnings),
        transition=transition,
    )
