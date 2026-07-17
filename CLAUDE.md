# coding_without_pants — Project instructions

## Overview

Local, single-user content-production toolkit **and** episode workshop for the YouTube channel
*"Coding without Pants"* — calm, funny, informative videos where a Hawaii dad builds one small useful
thing per episode, usually simple enough his pantsless 4-year-old can use it (the "Pantsless Test").
A `cwp` CLI runs **two loops** over flat files: **The Channel** (`idea → published`, drafting copy in
the channel voice via the `claude` CLI) and **The Pantsless Build** (his son speaks → local Whisper
transcribes → AI distills a brief → one-shot `claude -p` generates a self-contained kid toy → a
verify+repair loop proves it works → he uses it on camera). Each episode's toy (`project/index.html`)
lives beside the tooling. **Primary goal:** fun with his son + not-terrible output; the job/portfolio
angle is a byproduct. Full spec: [plan.md](plan.md).

## Stack

| Layer | Tool |
|---|---|
| Language / runtime | Python 3.12+ |
| Package / env | uv |
| CLI | argparse (stdlib) |
| Metadata | TOML (`meta.toml`), `tomllib` read / `tomli-w` write |
| Content | Markdown (`script.md`, `publish.md`, `brief.md`) |
| Episode project | self-contained HTML (`project/index.html`) |
| AI drafting + one-shot build | `claude` CLI (`claude -p`; OAuth, no API key in repo) |
| Speech-to-text | faster-whisper (local; default `small`, `--model medium` escalation; privacy — voice stays on-device) |
| Toy verification | playwright (headless Chromium) — same engine as `/judge-ui` |
| Audio decode | PyAV (bundled with faster-whisper); system ffmpeg NOT required — verified in Step 6 |
| Test / lint / types | pytest, ruff, mypy |

No backend, no database, no server, no ports. No PyTorch (faster-whisper uses CTranslate2).
`playwright` is a **runtime** dep (`verify.py` imports it inside `cwp build`), not dev-only.

## Commands

```powershell
uv sync                                   # install (Python 3.12+, tomli-w, faster-whisper, playwright)
uv run playwright install chromium        # once, for the toy verifier
# system ffmpeg NOT required — faster-whisper decodes via bundled PyAV (verified)

# Channel Loop
uv run cwp idea "<thought>"               # fast idea capture
uv run cwp new "<title>"                  # create an episode folder (idea status)
uv run cwp list                           # derived table of episodes + status + cycle time
uv run cwp show <id>                      # detail for one episode
uv run cwp next                           # which episode to work on + next action
uv run cwp status <id> <status>           # advance/change lifecycle state
uv run cwp draft <id> <outline|script|title|description> [--dry-run]   # AI draft in-voice
uv run cwp publish <id> [--url <url>]     # paste-ready YouTube metadata / mark published

# The Pantsless Build
uv run cwp capture <id> --audio <path>    # faster-whisper → transcript (redact-names scan; --record is v3)
uv run cwp brief <id>                     # distill noisy transcript → brief.md (must_haves + kid_quote)
uv run cwp build <id> [--force]           # one-shot generate + verify + repair → project/index.html

uv run pytest                             # tests
uv run ruff check .                        # lint
uv run mypy src                            # types
```

## Directory layout

```
coding_without_pants/
├── plan.md / CLAUDE.md / README.md
├── pyproject.toml            # uv; entry point cwp = "cwp.cli:main"
├── voice.md                  # channel voice — SoT for drafts (frozen v1)
├── build-contract.md         # one-shot generation contract — SoT for builds
├── pantsless-test.md         # 4-point kid-usability checklist template
├── docs/                     # production-notes.md, pantsless-build-research.md
├── src/cwp/                  # cli, config, episodes, lifecycle, drafting, publishing, capture, brief, verify, build, templates
├── episodes/NNN-slug/        # meta.toml, script.md, publish.md, brief.md, capture/, project/index.html
├── tests/                    # per-module + golden/garbage verifier fixtures + e2e smoke
└── .claude/skills/           # /cwp-ideas, /pantsless (v1.x wrappers over cwp)
```

## Architecture

- **`cli.py`** — argparse dispatch; thin. Exit codes: 0 ok, 1 user error, 2 environment/quality-gate.
- **`config.py`** — locates repo root + `episodes/`/`voice.md`/`build-contract.md`; channel defaults.
- **`episodes.py`** — Episode model, id/slug gen, atomic `meta.toml` read/write, folder-scan-**derived** index (no separate index file → no drift).
- **`lifecycle.py`** — permissive state machine (`idea→…→published`, plus `on-hold`/`cut`), append-only history, `next` priority.
- **`drafting.py`** — prompt assembly from `voice.md`; owns the `claude -p` call seam (stdin prompt — never argv, neutral cwd, preflight auth check, per-caller timeout, partial-write idempotency) that brief.py/build.py import; `--dry-run`, literal `<!-- AI DRAFT -->` marker.
- **`publishing.py`** — Studio-ordered paste block, validation, records `youtube_url` + sets `published`.
- **`capture.py`** — faster-whisper wrapper (local, default `small`; `language="en"`/`vad_filter`/`beam_size=5`/toy-vocab `initial_prompt` pinned); redact-names scan before write; the transcription boundary is a seam so tests mock it (real-model test opt-in via `CWP_RUN_REAL_WHISPER=1`).
- **`brief.py`** — kid-gateway distill: noisy transcript → `brief.md` (fenced TOML frontmatter; `must_haves[]` in a closed predicate vocabulary + redacted verbatim `kid_quote`); owns the ONE brief parse/write pair build.py/verify.py import.
- **`verify.py`** — static (`FORBIDDEN_PATTERNS`, one source of truth) + Playwright headless verifier (`--autoplay-policy=user-gesture-required`, AudioContext shim, `data-action-count` increment, ~15-click mash); vocabulary `must_haves` → keypoint assertions (deterministic, no LLM); calibrated with golden + single-defect garbage fixtures.
- **`build.py`** — generate→extract→verify→repair(≤2)→commit via the drafting.py seam (own ~300s timeout; timeout ≠ repair attempt; near-identical-retry abort); `needs_human` + exit 2 on exhaustion; never clobbers an existing toy.

Key invariants: episode **id/folder immutable** (retitle changes `title` only); `meta.toml` written **atomically**; AI is an **assistant, never a hard dependency** (every `claude`/whisper path degrades); repo is **public-safe** (media + `episodes/*/capture/` git-ignored, redact-names guard on every kid-speech text artifact via git-ignored `private/redact-names.txt`, publish-moment checklist, local Whisper, no secrets); the toy verifier is **calibrated** (golden + single-defect garbage fixtures) before it gates builds; CLI output is **UTF-8-reconfigured** (Windows cp1252 landmine).

## Current state

Plan written (Fable pre-build review applied 2026-07-15 — see `docs/plan-review-fable.md`), no code
yet. Build via `/build-phase` (11 automated code steps + 3 manual steps M1/M1.5/M2 — M1.5 is the
solo live-toolchain dry-run before the kid session); default flags `--reviewers code --isolation
worktree`, except Steps 8–9 escalate to `--reviewers deep` (verifier = measurement instrument,
build engine = reliability core). v1 ships BOTH loops (Channel + the Pantsless Build). v2 (later,
`/plan-feature`) = a public playable-toy gallery + architecture writeup. Update this section at the
end of each phase via `/repo-update`.

## Environment requirements

- Windows 11 + PowerShell (workspace default). No ports, no Docker.
- Python 3.12+ and `uv` on PATH; `playwright install chromium` once. System `ffmpeg` NOT
  required — faster-whisper decodes via bundled PyAV (verified in Step 6).
- The `claude` CLI installed and authenticated (OAuth) — required for `cwp draft`/`brief`/`build`;
  the rest of the Channel Loop works without it. Those commands preflight auth and fail fast with
  fix-it text if unavailable.
- faster-whisper downloads its model once on first `cwp capture`, then runs fully offline.
- Fresh worktrees: run `uv sync` + `uv run playwright install chromium` before the first quality gate.
