"""Episode model (plan.md §4.1): id/slug generation, atomic ``meta.toml`` I/O, derived index.

The folder name IS the episode id (``<seq>-<slug>``) and is IMMUTABLE after creation —
retitling changes ``title`` in ``meta.toml`` only. There is NO index file: the episode
list is *derived* by scanning ``episodes/*/meta.toml`` on every ``cwp list`` (§4), so
drift is impossible.

Hand-edit and CLI-mutate are BOTH allowed (single user, last-writer-wins, no locking);
the CLI always writes ``meta.toml`` atomically (same-dir temp file + ``os.replace``) so
a killed run never leaves a half-written file (§4.3). Reads are permissive — a
hand-edited file only fails on structural problems (missing core keys, wrong TOML
shapes), and ``scan_episodes`` degrades those to warnings instead of breaking ``list``.

Powers ``cwp new`` / ``cwp idea`` / ``cwp list`` / ``cwp show`` (§6).
"""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import tomli_w

from cwp import templates
from cwp.config import DEFAULT_EPISODE_STATUS

SCHEMA_VERSION = 1
META_FILENAME = "meta.toml"
MAX_SLUG_LENGTH = 40

STATUSES = ("idea", "scripted", "built", "recorded", "edited", "published", "on-hold", "cut")
INGREDIENTS = ("neetcode", "hak", "xkcd", "kid")
EFFORTS = ("S", "M", "L")

# Defaults for fields ``cwp idea`` / bare ``cwp new`` cannot know yet. "hak" is the
# generic little-project bucket; S-effort + kid-usable is the channel's default shape.
DEFAULT_INGREDIENT = "hak"
DEFAULT_EFFORT = "S"

# §4.1 pinned shapes. seq is 3-digit zero-padded and widens past 999 ([0-9]{3,}).
SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
ID_RE = re.compile(r"(?P<seq>[0-9]{3,})-(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)")


class EpisodeError(Exception):
    """Base for episode-domain errors (CLI maps these to exit 1, user error)."""


class SlugError(EpisodeError):
    """The title cannot produce a non-empty §4.1 slug."""


class EpisodeNotFoundError(EpisodeError):
    """No episode folder matches the given id or seq."""


class MetaFormatError(EpisodeError):
    """A ``meta.toml`` exists but is not valid TOML / not episode-shaped."""


@dataclass
class PantslessTest:
    """The §5.4 per-episode design gate (``[pantsless_test]`` table)."""

    can_start_unaided: bool = False
    understands_goal: bool = False
    cant_break_it: bool = False
    enjoys_it: bool = False
    notes: str = ""


@dataclass
class HistoryEntry:
    """One ``[[history]]`` row — the append-only status trail (§4.1)."""

    status: str
    at: str


@dataclass
class Episode:
    """One episode = one ``episodes/<id>/`` folder; this mirrors ``meta.toml`` (§4.1).

    Timestamps are UTC ISO 8601 strings (``2026-07-15T00:00:00Z``); empty string means
    unset (``published_at`` / ``youtube_url`` before publish).
    """

    id: str
    seq: int
    slug: str
    title: str
    status: str = DEFAULT_EPISODE_STATUS
    ingredient: str = DEFAULT_INGREDIENT
    kid_usable: bool = True
    effort: str = DEFAULT_EFFORT
    hook: str = ""
    teaches: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    published_at: str = ""
    youtube_url: str = ""
    needs_human: bool = False
    notes: str = ""
    schema_version: int = SCHEMA_VERSION
    pantsless_test: PantslessTest = field(default_factory=PantslessTest)
    history: list[HistoryEntry] = field(default_factory=list)


@dataclass(frozen=True)
class CreatedEpisode:
    """What ``create_episode`` returns: the model, its folder, and any warnings."""

    episode: Episode
    directory: Path
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ScanResult:
    """Derived index: episodes sorted by seq + non-fatal per-folder warnings."""

    episodes: tuple[Episode, ...]
    warnings: tuple[str, ...]


def utc_now_iso() -> str:
    """Current UTC time in the §4.1 pinned shape (``2026-07-15T00:00:00Z``)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(title: str) -> str:
    """Derive the §4.1 slug: ``[a-z0-9]+(-[a-z0-9]+)*``, ≤ 40 chars.

    Lowercase; whitespace/hyphens/underscores become single hyphens (word-joining
    hyphens survive: "Number-Guessing" → "number-guessing"); everything else is
    stripped; runs collapse; ends are trimmed; truncation never leaves a trailing
    hyphen. Raises :class:`SlugError` when nothing slug-safe remains (e.g. an
    all-emoji title).
    """
    hyphenated = re.sub(r"[\s_-]+", "-", title.lower())
    stripped = re.sub(r"[^a-z0-9-]", "", hyphenated)
    collapsed = re.sub(r"-{2,}", "-", stripped).strip("-")
    slug = collapsed[:MAX_SLUG_LENGTH].rstrip("-")
    if not slug:
        raise SlugError(
            f"Title {title!r} produces an empty slug — include at least one ASCII letter or digit"
        )
    return slug


def parse_tags(raw: str) -> list[str]:
    """Split a comma-separated tags string (``--tags "a, b"``) into clean tags.

    Whitespace is trimmed and empty items dropped — tag-format policy lives here,
    not in the CLI layer.
    """
    return [tag.strip() for tag in raw.split(",") if tag.strip()]


def format_seq(seq: int) -> str:
    """3-digit zero-padded seq (§4.1); widens naturally past 999 (1000 → "1000")."""
    if seq < 1:
        raise ValueError(f"Seq must be >= 1, got {seq}")
    return f"{seq:03d}"


def _episode_dir_matches(episodes_dir: Path) -> Iterator[tuple[Path, re.Match[str]]]:
    """Yield ``(dir, ID_RE match)`` for every id-shaped child folder, sorted by name."""
    if not episodes_dir.is_dir():
        return
    for child in sorted(episodes_dir.iterdir()):
        if not child.is_dir():
            continue
        match = ID_RE.fullmatch(child.name)
        if match is not None:
            yield child, match


def next_seq(episodes_dir: Path) -> int:
    """``max(existing seq) + 1`` (§4.1), from folder NAMES so even a folder with a
    corrupt/missing ``meta.toml`` still reserves its seq (no id collisions)."""
    highest = 0
    for _directory, match in _episode_dir_matches(episodes_dir):
        highest = max(highest, int(match["seq"]))
    return highest + 1


def episode_to_dict(episode: Episode) -> dict[str, Any]:
    """The §4.1 ``meta.toml`` document, scalars before tables (readable TOML)."""
    return {
        "schema_version": episode.schema_version,
        "id": episode.id,
        "seq": episode.seq,
        "slug": episode.slug,
        "title": episode.title,
        "status": episode.status,
        "ingredient": episode.ingredient,
        "kid_usable": episode.kid_usable,
        "effort": episode.effort,
        "hook": episode.hook,
        "teaches": episode.teaches,
        "tags": list(episode.tags),
        "created_at": episode.created_at,
        "published_at": episode.published_at,
        "youtube_url": episode.youtube_url,
        "needs_human": episode.needs_human,
        "notes": episode.notes,
        "pantsless_test": {
            "can_start_unaided": episode.pantsless_test.can_start_unaided,
            "understands_goal": episode.pantsless_test.understands_goal,
            "cant_break_it": episode.pantsless_test.cant_break_it,
            "enjoys_it": episode.pantsless_test.enjoys_it,
            "notes": episode.pantsless_test.notes,
        },
        "history": [{"status": entry.status, "at": entry.at} for entry in episode.history],
    }


def _coerce_stamp(value: Any) -> str:
    """Normalize a timestamp field to the pinned §4.1 UTC ISO 8601 string shape.

    Hand-edits are allowed (§4.1), and TOML has NATIVE datetime/date literals — a
    human typing ``created_at = 2026-07-15T00:00:00Z`` (unquoted, valid TOML) gets a
    ``datetime`` object from ``tomllib``, not a string. Bare ``str()`` would silently
    bake in Python's ``2026-07-15 00:00:00+00:00`` shape forever on the next write;
    instead, natives are normalized to the pinned ``2026-07-15T00:00:00Z`` form
    (aware values converted to UTC, naive values assumed UTC). Strings pass through.
    """
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%dT00:00:00Z")
    return str(value)


def _episode_from_dict(data: Mapping[str, Any], *, source: object) -> Episode:
    """Build an Episode from parsed TOML. Permissive: only id/seq/slug/title are
    required; everything else defaults (hand-edits warn, never block — §4.1)."""
    try:
        raw_tags = data.get("tags", [])
        if not isinstance(raw_tags, list):
            raise TypeError("tags must be an array")
        raw_gate = data.get("pantsless_test", {})
        if not isinstance(raw_gate, Mapping):
            raise TypeError("pantsless_test must be a table")
        raw_history = data.get("history", [])
        if not isinstance(raw_history, list):
            raise TypeError("history must be an array of tables")
        history: list[HistoryEntry] = []
        for entry in raw_history:
            if not isinstance(entry, Mapping):
                raise TypeError("history entries must be tables")
            history.append(
                HistoryEntry(
                    status=str(entry.get("status", "")), at=_coerce_stamp(entry.get("at", ""))
                )
            )
        return Episode(
            id=str(data["id"]),
            seq=int(data["seq"]),
            slug=str(data["slug"]),
            title=str(data["title"]),
            status=str(data.get("status", DEFAULT_EPISODE_STATUS)),
            ingredient=str(data.get("ingredient", DEFAULT_INGREDIENT)),
            kid_usable=bool(data.get("kid_usable", True)),
            effort=str(data.get("effort", DEFAULT_EFFORT)),
            hook=str(data.get("hook", "")),
            teaches=str(data.get("teaches", "")),
            tags=[str(tag) for tag in raw_tags],
            created_at=_coerce_stamp(data.get("created_at", "")),
            published_at=_coerce_stamp(data.get("published_at", "")),
            youtube_url=str(data.get("youtube_url", "")),
            needs_human=bool(data.get("needs_human", False)),
            notes=str(data.get("notes", "")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            pantsless_test=PantslessTest(
                can_start_unaided=bool(raw_gate.get("can_start_unaided", False)),
                understands_goal=bool(raw_gate.get("understands_goal", False)),
                cant_break_it=bool(raw_gate.get("cant_break_it", False)),
                enjoys_it=bool(raw_gate.get("enjoys_it", False)),
                notes=str(raw_gate.get("notes", "")),
            ),
            history=history,
        )
    except KeyError as exc:
        raise MetaFormatError(f"{source}: missing required key {exc.args[0]!r}") from exc
    except (TypeError, ValueError) as exc:
        raise MetaFormatError(f"{source}: {exc}") from exc


def read_meta(meta_path: Path) -> Episode:
    """Parse one ``meta.toml`` (stdlib ``tomllib``) into an :class:`Episode`.

    EVERY read failure surfaces as :class:`MetaFormatError` — invalid TOML syntax,
    invalid UTF-8 bytes (wrong-encoding hand-edit, truncated multibyte sequence —
    ``tomllib`` raises a plain ``UnicodeDecodeError`` for those, not
    ``TOMLDecodeError``), or an unreadable/missing file. That keeps the permissive-
    read contract uniform: the CLI maps it to a clean exit 1 and ``scan_episodes``
    degrades it to a warning, never a crash.
    """
    try:
        with meta_path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise MetaFormatError(f"{meta_path}: invalid TOML: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise MetaFormatError(f"{meta_path}: not valid UTF-8: {exc}") from exc
    except OSError as exc:
        raise MetaFormatError(f"{meta_path}: unreadable: {exc}") from exc
    return _episode_from_dict(data, source=meta_path)


def write_meta(episode_dir: Path, episode: Episode) -> Path:
    """Atomically write ``<episode_dir>/meta.toml`` (temp file + ``os.replace``).

    The temp file lives in the SAME directory so the replace is same-volume atomic —
    a killed run leaves either the old file or the new file, never a torn one (§4.3).
    """
    episode_dir.mkdir(parents=True, exist_ok=True)
    target = episode_dir / META_FILENAME
    payload = tomli_w.dumps(episode_to_dict(episode)).encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(dir=episode_dir, prefix=".meta-", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def _write_if_absent(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8", newline="\n")


def write_episode_files(directory: Path, episode: Episode) -> None:
    """Create the §4.2 per-episode layout around ``meta.toml`` (which write_meta owns).

    Clobber-safe + idempotent: every templated file is written ONLY if absent
    (§4.2 — ``project/index.html`` is never auto-clobbered; the rest follow the same
    rule so FUTURE callers — ``cwp build``/``cwp draft`` or a repair flow re-running
    layout creation against an existing folder — never destroy edits; ``cwp new``
    itself always starts from a fresh folder). The ``capture/`` dir is created
    EMPTY — the whole dir is git-ignored (§4.3).
    """
    directory.mkdir(parents=True, exist_ok=True)
    _write_if_absent(
        directory / "script.md",
        templates.render_script_md(title=episode.title, episode_id=episode.id),
    )
    _write_if_absent(
        directory / "publish.md",
        templates.render_publish_md(title=episode.title, episode_id=episode.id),
    )
    _write_if_absent(
        directory / "brief.md",
        templates.render_brief_md(title=episode.title, episode_id=episode.id),
    )
    (directory / "capture").mkdir(exist_ok=True)
    project_dir = directory / "project"
    project_dir.mkdir(exist_ok=True)
    _write_if_absent(
        project_dir / "index.html",
        templates.render_index_html_placeholder(title=episode.title, episode_id=episode.id),
    )


def create_episode(
    episodes_dir: Path,
    title: str,
    *,
    status: str = DEFAULT_EPISODE_STATUS,
    ingredient: str = DEFAULT_INGREDIENT,
    effort: str = DEFAULT_EFFORT,
    kid_usable: bool = True,
    hook: str = "",
    teaches: str = "",
    tags: Sequence[str] = (),
    now: str | None = None,
) -> CreatedEpisode:
    """Create ``episodes/<seq>-<slug>/`` with meta.toml + all §4.2 files.

    seq = ``max(existing) + 1``; the id/folder is immutable from here on. A duplicate
    slug under a different seq is ALLOWED and returns a warning (§4.1). *now*
    overrides ``created_at`` (tests); default is real UTC now.
    """
    for value, name, allowed in (
        (status, "status", STATUSES),
        (ingredient, "ingredient", INGREDIENTS),
        (effort, "effort", EFFORTS),
    ):
        if value not in allowed:
            raise EpisodeError(f"Invalid {name} {value!r} (expected one of: {', '.join(allowed)})")
    slug = slugify(title)
    seq = next_seq(episodes_dir)
    episode_id = f"{format_seq(seq)}-{slug}"
    warnings: list[str] = []
    for existing_dir, match in _episode_dir_matches(episodes_dir):
        if match["slug"] == slug:
            warnings.append(
                f"slug {slug!r} already used by {existing_dir.name} (ids stay unique via seq)"
            )
            break
    created_at = now if now is not None else utc_now_iso()
    episode = Episode(
        id=episode_id,
        seq=seq,
        slug=slug,
        title=title,
        status=status,
        ingredient=ingredient,
        kid_usable=kid_usable,
        effort=effort,
        hook=hook,
        teaches=teaches,
        tags=list(tags),
        created_at=created_at,
        history=[HistoryEntry(status=status, at=created_at)],
    )
    directory = episodes_dir / episode_id
    try:
        directory.mkdir(parents=True)
    except FileExistsError as exc:
        raise EpisodeError(f"Episode folder already exists: {directory}") from exc
    write_meta(directory, episode)
    write_episode_files(directory, episode)
    return CreatedEpisode(episode=episode, directory=directory, warnings=tuple(warnings))


def scan_episodes(episodes_dir: Path) -> ScanResult:
    """The derived index (§4): read every ``episodes/*/meta.toml``, sorted by seq.

    NO index file exists by design. Non-episode-shaped folders are ignored; an
    id-shaped folder with a missing/corrupt/mismatched ``meta.toml`` becomes a
    warning, never a crash — ``cwp list`` must survive hand-edits.
    """
    episodes: list[Episode] = []
    warnings: list[str] = []
    for directory, _match in _episode_dir_matches(episodes_dir):
        meta_path = directory / META_FILENAME
        if not meta_path.is_file():
            warnings.append(f"{directory.name}: no {META_FILENAME} — skipped")
            continue
        try:
            episode = read_meta(meta_path)
        except (MetaFormatError, OSError) as exc:
            warnings.append(f"{exc} — skipped")
            continue
        if episode.id != directory.name:
            warnings.append(
                f"{meta_path}: id {episode.id!r} != folder name {directory.name!r}"
                " (the folder name is the real id)"
            )
        episodes.append(episode)
    episodes.sort(key=lambda episode: episode.seq)
    return ScanResult(episodes=tuple(episodes), warnings=tuple(warnings))


def resolve_episode_dir(episodes_dir: Path, id_or_seq: str) -> Path:
    """Find an episode folder by full id ("001-test") or bare seq ("001" / "1")."""
    token = id_or_seq.strip()
    if token.isascii() and token.isdigit():
        wanted_seq = int(token)
        for directory, match in _episode_dir_matches(episodes_dir):
            if int(match["seq"]) == wanted_seq:
                return directory
    else:
        for directory, _match in _episode_dir_matches(episodes_dir):
            if directory.name == token:
                return directory
    raise EpisodeNotFoundError(f"No episode matching {id_or_seq!r} under {episodes_dir}")


def load_episode(episodes_dir: Path, id_or_seq: str) -> tuple[Path, Episode]:
    """Resolve + read one episode; returns ``(folder, episode)``.

    This is the single-episode primitive future commands (status/draft/publish/…)
    build on, so it mirrors ``scan_episodes``'s tolerance: an id-shaped folder with
    NO ``meta.toml`` (e.g. a ``cwp new`` killed between mkdir and write_meta) raises
    :class:`EpisodeNotFoundError` — a bare folder is not an episode — instead of
    leaking a raw ``FileNotFoundError`` traceback through the CLI.
    """
    directory = resolve_episode_dir(episodes_dir, id_or_seq)
    meta_path = directory / META_FILENAME
    if not meta_path.is_file():
        raise EpisodeNotFoundError(
            f"Episode folder {directory.name} has no {META_FILENAME}"
            " (interrupted cwp new? remove the folder or restore its meta.toml)"
        )
    return directory, read_meta(meta_path)


def cycle_time_days(episode: Episode) -> int | None:
    """Whole days idea→published; ``None`` unless both timestamps are set and parse."""
    if not episode.created_at or not episode.published_at:
        return None
    try:
        start = _parse_utc(episode.created_at)
        end = _parse_utc(episode.published_at)
    except ValueError:
        return None
    return (end - start).days


def _parse_utc(stamp: str) -> datetime:
    parsed = datetime.fromisoformat(stamp)
    if parsed.tzinfo is None:  # naive hand-edited stamps are taken as UTC
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def format_table(episodes: Sequence[Episode]) -> str:
    """The ``cwp list`` table: id, status, ingredient, effort, title + cycle time."""
    headers = ("id", "status", "ingredient", "effort", "title", "cycle")
    rows: list[tuple[str, ...]] = []
    for episode in sorted(episodes, key=lambda entry: entry.seq):
        days = cycle_time_days(episode)
        rows.append(
            (
                episode.id,
                episode.status,
                episode.ingredient,
                episode.effort,
                episode.title,
                f"{days}d" if days is not None else "-",
            )
        )
    widths = [len(header) for header in headers]
    for row in rows:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row, strict=True)]

    def render(cells: tuple[str, ...]) -> str:
        return "  ".join(
            cell.ljust(width) for cell, width in zip(cells, widths, strict=True)
        ).rstrip()

    return "\n".join([render(headers), *(render(row) for row in rows)])


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def format_detail(episode: Episode) -> str:
    """The ``cwp show`` detail block: every §4.1 field, human-readable."""
    days = cycle_time_days(episode)
    gate = episode.pantsless_test
    checks = (gate.can_start_unaided, gate.understands_goal, gate.cant_break_it, gate.enjoys_it)
    lines = [
        f"id:            {episode.id}",
        f"title:         {episode.title}",
        f"status:        {episode.status}",
        f"ingredient:    {episode.ingredient}",
        f"effort:        {episode.effort}",
        f"kid_usable:    {_yes_no(episode.kid_usable)}",
        f"hook:          {episode.hook or '-'}",
        f"teaches:       {episode.teaches or '-'}",
        f"tags:          {', '.join(episode.tags) or '-'}",
        f"created_at:    {episode.created_at or '-'}",
        f"published_at:  {episode.published_at or '-'}",
        f"youtube_url:   {episode.youtube_url or '-'}",
        f"cycle:         {f'{days}d' if days is not None else '-'}",
        f"needs_human:   {_yes_no(episode.needs_human)}",
        f"notes:         {episode.notes or '-'}",
        f"pantsless_test: {sum(checks)}/4",
        f"  can_start_unaided: {_yes_no(gate.can_start_unaided)}",
        f"  understands_goal:  {_yes_no(gate.understands_goal)}",
        f"  cant_break_it:     {_yes_no(gate.cant_break_it)}",
        f"  enjoys_it:         {_yes_no(gate.enjoys_it)}",
        f"  notes:             {gate.notes or '-'}",
        "history:",
    ]
    lines.extend(f"  {entry.status} at {entry.at}" for entry in episode.history)
    return "\n".join(lines)
