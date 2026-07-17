# coding_without_pants — Project Plan

> The channel's operating system and its workshop, in one repo. A tiny `cwp` CLI runs two
> loops: **The Channel** (idea → published) and **The Pantsless Build** (your 4-year-old
> speaks → AI builds → he uses it on camera). Every episode's browser toy lives right beside
> the tooling that made it.

**Primary goal (in priority order):** (1) a fun, repeatable way to play with your son + AI;
(2) output that is *not* terrible — genuinely usable toys and watchable videos; (3) refine your
skills and have a public place to share them. The job/portfolio angle is a deliberate
*byproduct*, never a driver (see §13).

---

## 1. What This Is

**`coding_without_pants`** is a single-user, local content-production toolkit **and** episode
workshop for the YouTube channel *"Coding without Pants"* — calm, funny, informative videos where
a Hawaii dad builds **one small useful thing** per episode, usually simple enough his pantsless
4-year-old can use it.

The repo is deliberately two things at once, driven by one `cwp` CLI over flat files:

1. **A content pipeline** (The Channel Loop) — move an episode through
   `idea → scripted → built → recorded → edited → published`, draft copy in the channel voice via
   the `claude` CLI, and assemble paste-ready YouTube metadata. Its job is to **remove the specific
   frictions that kill hobby channels**, not to be a CMS.
2. **The Pantsless Build** (the heart of the channel) — your son says what he wants, local Whisper
   transcribes it, an AI distills it into a build brief, a one-shot generation produces a
   self-contained kid-usable `index.html`, and an automated verify+repair loop proves it works
   *before he sees it*. Then he uses it on camera — the Pantsless Test. **The code you build is the
   thing you publish.**

**The channel voice** synthesizes four influences:

| Influence | Contributes | The channel's… |
|---|---|---|
| **NeetCode** | one concrete problem, clean solution, zero fluff | **structure** |
| **Hak (mostlyhak)** | calm, unhurried, follow-along | **tone** |
| **xkcd what-if** | absurd premise, real rigor | **hook** |
| **The 4-year-old** | must be usable by someone who won't wear pants | **the differentiator (Pantsless Test)** |

**Who uses it:** just the operator (single user, local). No hosting, no multi-user, no login.

**Out of scope for v1:**
- Auto-upload to YouTube (no Google OAuth/Data API — operator pastes metadata into Studio).
- Any web UI/dashboard for the tool (the *episodes* are web; the tool is a CLI).
- Thumbnail *image* generation (tool generates thumbnail *text*; image made by hand).
- Cloud speech-to-text (privacy — a child's voice stays on-device; see §11).
- The public playable-toy gallery + architecture writeup (that's v2 — see §13 Roadmap).
- Analytics, scheduling, or any always-on/autonomous behavior.

**v1 acceptance bar (falsifiable, and it proves *both* loops):**
1. Episode 001 goes `idea → paste-ready publish.md` with every cwp-owned stage
   (idea/script/publish-prep) done only via `cwp` (Channel Loop). Record/Edit are manual by
   design (§3.1).
2. One real kid clip goes `capture → brief → build → a verified, kid-usable toy` end-to-end
   (Pantsless Build), with the toy passing the automated verifier and the Pantsless Test.

Not "cwp is feature-complete." (See Steps M1 + M2.)

---

## 2. Stack

| Layer | Tool | Why |
|---|---|---|
| Language / runtime | Python 3.12+ | Workspace standard; stdlib `tomllib` for reading TOML |
| Package / env | `uv` | Workspace standard |
| CLI framework | `argparse` (stdlib) | Zero-dep subcommand dispatch; keeps the tool tiny |
| Metadata | TOML (`meta.toml`), `tomllib` read / `tomli-w` write | Human-editable + atomic-writable |
| Content | Markdown (`script.md`, `publish.md`, `brief.md`) | Plain, diffable, editor-agnostic |
| Episode project | Self-contained HTML (`project/index.html`) | Runs in any browser; passes the Pantsless Test; shares as a link |
| AI drafting + build | `claude` CLI (subprocess, `claude -p`) | Subscription OAuth already on the machine; the one-shot generation primitive |
| Speech-to-text | `faster-whisper` (local; default `small`, `--model medium` to escalate) | On-device → a child's voice never leaves the machine; low Windows setup |
| Toy verification | `playwright` (headless Chromium) | Console-error + keypoint checks on generated toys; same engine as `/judge-ui` |
| Audio decode | PyAV (bundled with `faster-whisper`) | Decodes non-WAV input; system `ffmpeg` NOT required — faster-whisper decodes via bundled PyAV (verified in Step 6) |
| Test / lint / types | `pytest`, `ruff`, `mypy` | Workspace convention |

No backend, no database, no server, no ports. Runtime deps beyond stdlib: `tomli-w`,
`faster-whisper`, `playwright` (**runtime, not dev** — `verify.py` imports it inside `cwp build`);
dev group: `pytest`, `ruff`, `mypy`. System: `playwright install chromium` once. System `ffmpeg`
is NOT required — faster-whisper decodes via bundled PyAV (verified in Step 6: an MP3 decoded
through `faster_whisper.audio.decode_audio` with ffmpeg stripped from PATH). Notably **no
PyTorch** (faster-whisper uses CTranslate2).

---

## 3. How It All Works — The Two Loops

This is the explicit pipeline. Everything the tool does is one of two loops.

### 3.1 The Channel Loop (every episode)

```
idea ──► script ──► build ──► record ──► edit ──► publish
```

| Stage | `cwp` command | Skills that help | Output |
|---|---|---|---|
| Idea | `cwp idea "<thought>"` / `cwp new "<title>"` | `/cwp-ideas` (new), `/user-brainstorm`, `/deep-research` | an `idea` episode |
| Script | `cwp draft <id> script` | (reads `voice.md`) | `script.md` |
| Build | `cwp build <id>` (Pantsless Build) or by hand | `/judge-ui` (verify) | `project/index.html` |
| Record | — (operator) | `docs/production-notes.md` | raw footage (git-ignored) |
| Edit | — (operator) | — | cut video |
| Publish | `cwp publish <id> [--url]` | — | `publish.md` + upload |

### 3.2 The Pantsless Build (kid → AI → toy; nested inside "build")

Your 3D-printer intuition, made real: **Capture + Distill = setup**, **Build = let it print**,
**Reveal = watch him use it.**

```
CAPTURE            DISTILL              BUILD                       REVEAL
kid rattles   ──►  faster-whisper  ──►  claude -p one-shot     ──►  he uses it on
off his wish        + kid-gateway        + verify + repair loop      camera (Pantsless
(record clip)       → build brief         → verified index.html       Test)
```

| Stage | `cwp` command | What it does | Tech |
|---|---|---|---|
| **Capture** | `cwp capture <id> --audio <path>` | Transcribe your son's clip (recorded on a phone) to text; offer a re-record hint if garbled. Live mic capture (`--record`) is v3 — it needs an audio-capture dep. | `faster-whisper`, local |
| **Distill** | `cwp brief <id>` | Turn the noisy transcript into a tight build brief (the ONE action, visual motif, `must_haves[]`, the verbatim funny `kid_quote`) | `claude -p` (kid-gateway prompt) |
| **Build** | `cwp build <id> [--force]` | One-shot-generate the toy, then verify+repair until it works or `needs_human` | `claude -p` + `playwright` |
| **Reveal** | — (operator) + Pantsless-Test checklist | He uses the verified toy on camera — the payoff shot | camera |

**The generate → verify → repair loop** (the reliability core; full detail in
[docs/pantsless-build-research.md](docs/pantsless-build-research.md) §3):

```
generate → extract → static-check → headless-check → repair (≤2x) → commit
```

1. **Generate** `claude -p` (build-contract + brief delivered via **stdin**, never argv — Windows'
   ~32K argv ceiling; cwd set to a neutral temp dir so repo CLAUDE.md context never leaks into the
   prompt) → temp file (never write `project/` directly). Own timeout constant (~300s; drafts use
   ~60s) — a timeout is retried once and does NOT consume a repair attempt.
2. **Extract** the single ```` ```html ```` fence (no fence = a repair-triggering failure with a
   fence-specific evidence template: "0 or >1 fences — return exactly one").
3. **Static check** — ONE `FORBIDDEN_PATTERNS` constant in `verify.py` (single source of truth,
   cited by `build-contract.md`, never restated): URL patterns scoped to resource-loading contexts
   (`src=`, `href=`, `fetch(`, `XMLHttpRequest`, `import(`, `<link rel="stylesheet"`) with an
   `xmlns="http://www.w3.org/…"` allowlist for inline SVG; `alert(`/`confirm(`/`prompt(`; and a
   `</script>` COUNT rule (>1 = a string-embedded breaker — bare presence is just the toy's own
   closing tag). Each hit fails with the exact line.
4. **Headless check** (Playwright, Chromium launched with `--autoplay-policy=user-gesture-required`)
   — zero console errors; the one `[data-testid="main-action"]` exists and covers ≥20% of the
   viewport (intentional buffer under the contract's ≥25% target); clicking it increments the
   contract-mandated `data-action-count` attribute (no pixel-diff — the mandated idle animation
   defeats it); an injected `AudioContext` shim asserts no context exists before the first click
   and any constructed one reaches `state === "running"` after; a rapid ~15-click mash asserts zero
   new errors + a monotonic count ("can't break it"); plus the brief's `must_haves` — drawn from
   the closed, machine-checkable predicate vocabulary (Appendix C) — compiled into keypoint
   assertions.
5. **Repair** — feed the **exact** failure evidence back for a second shot (≤2 retries). A retry
   returning near-identical HTML aborts straight to `needs_human` (no budget burn on a stuck model).
6. **Commit** — atomic `os.replace` only on a full pass; otherwise mark `needs_human`, save the
   failure screenshot, exit 2. **Never ship a broken toy; never clobber an existing one.**

This reuses three things the project already has: `drafting.py`'s claude-call seam
(preflight/stdin/idempotency — each caller pins its own timeout), `episodes.py`'s atomic write, and
the `/judge-ui` headless-verify pattern.

---

## 4. Data Store

Everything is flat files under the repo. **No separate index file** — the episode list is *derived*
by scanning `episodes/*/meta.toml` on every `cwp list`, so drift is impossible (one source of truth).

### 4.1 Entity: Episode (one entity; the folder name is the id)

**ID format (pinned):**
- `id` = `<seq>-<slug>`, e.g. `001-the-number-guessing-machine`.
- `seq` = 3-digit zero-padded, `[0-9]{3}` (`001`–`999`; widen to 4 digits past 999).
- `slug` = `[a-z0-9]+(-[a-z0-9]+)*`, derived from the title (lowercase, non-alphanumeric stripped,
  spaces → single hyphens, collapsed, trimmed, ≤ 40 chars).
- `seq` assigned at creation as `max(existing seq) + 1`. **id/folder is immutable**; retitling
  changes `title` in `meta.toml` only. The `seq` prefix guarantees uniqueness (duplicate slug across
  different `seq`s is allowed, with a warning).

**`meta.toml`** (read via `tomllib`, written via `tomli-w` with an atomic temp-file + `os.replace`):

```toml
schema_version = 1
id       = "001-the-number-guessing-machine"
seq      = 1
slug     = "the-number-guessing-machine"
title    = "The Number-Guessing Machine (Binary Search, No Cheating)"   # mutable
status   = "idea"                  # idea|scripted|built|recorded|edited|published|on-hold|cut
ingredient = "neetcode"            # neetcode | hak | xkcd | kid
kid_usable = true
effort   = "S"                     # S | M | L
hook     = "20 Questions turned into an app that always wins in 7 guesses…"
teaches  = "binary search"
tags     = ["algorithms", "binary-search"]
created_at   = "2026-07-15T00:00:00Z"   # UTC ISO 8601
published_at = ""
youtube_url  = ""
needs_human  = false               # set true when the build loop exhausts its repair budget
notes = ""

[pantsless_test]                   # the real per-episode design gate
can_start_unaided = false
understands_goal  = false
cant_break_it     = false
enjoys_it         = false
notes = ""

[[history]]                        # append-only status trail
status = "idea"
at = "2026-07-15T00:00:00Z"
```

**Hand-edit vs CLI-mutate (resolved):** both allowed. Single user, no daemon → last-writer-wins, no
locking. CLI always writes atomically so a crash never corrupts the file.

### 4.2 Per-episode folder layout

```
episodes/001-the-number-guessing-machine/
├── meta.toml          # status, lifecycle, Pantsless Test
├── script.md          # read-aloud script + on-screen action notes
├── publish.md         # generated paste-ready YouTube metadata (cwp publish)
├── brief.md           # the build brief distilled from the kid transcript (cwp brief)
├── capture/           # ENTIRE dir git-ignored — verbatim child speech never reaches the public repo
│   └── transcript.txt # faster-whisper output, redact-names-scanned on write (§4.3)
└── project/
    ├── index.html     # the verified buildable toy (cwp build; never auto-clobbered)
    └── .repair/       # attempt-N.png screenshots + log.jsonl (build-loop artifacts, git-ignored)
```

**Clobber protection:** `cwp new` creates `project/index.html` only if absent; `cwp draft` never
touches `project/`; `cwp build` writes only on a verified pass (else `needs_human`, no write).
Regenerating over an existing toy requires `--force`.

### 4.3 Dedup, corruption, public/private

- **Dedup / idempotency:** re-`cwp new` yields a new `seq`; `cwp draft`/`cwp build` flush to temp and
  commit atomically, so a killed run is re-runnable, never half-written.
- **Corruption:** atomic writes; `schema_version` for migrations; `[[history]]` append-only.
- **Public repo + kid privacy (resolved):** the repo is intended to be **public**. Therefore:
  - Raw audio/video/media git-ignored (`*.mp4`, `*.mov`, `*.wav`, `*.m4a`, `*.mp3`, `*.ogg`,
    `*.m4v`, `*.3gp` — phone formats included — `media/`, `private/`, `clips/`,
    `episodes/*/capture/` — the WHOLE capture dir, transcript included —
    `episodes/*/project/.repair/`). A unit test asserts these patterns exist in `.gitignore`
    (Step 1).
  - The child's real name is **never committed** — enforced structurally, not by convention: a
    git-ignored `private/redact-names.txt` (one `real-name = nickname` pair per line) is consulted
    by `capture.py` and `brief.py` before any write; matches are redacted to the nickname by
    default (`--allow-names` to override). Absent file → no-op plus a one-time "text artifacts are
    unscanned" notice. `cwp publish` prints a "Before you publish" checklist (real-name scan +
    Made-for-Kids audience setting) beside the paste block.
  - Whisper runs **locally**, so his voice never leaves the machine.
  - No secrets in-tree (the `claude` CLI holds its own OAuth token).

---

## 5. Domain model — Voice, Lifecycle, and the Pantsless Test

### 5.1 The channel voice (`voice.md`)

Repo-root `voice.md` is the **single source of truth** for tone, read on every draft. Seeded
**verbatim** from the operator's description and **frozen for v1** (a comedic voice guide is a
perfectionism sink; revisit only after 3 published episodes). Full seed in the Appendix.

### 5.2 The build contract (`build-contract.md`)

Repo-root `build-contract.md` is the single source of truth for the **one-shot generation prompt** —
the MUST/NEVER kid-safe rules (one obvious action carrying machine-checkable hooks —
`data-testid="main-action"` plus a `data-action-count` increment per activation — Web Audio only in
a gesture handler, emoji sprites, no reading required, no dead-ends, no other interactive elements,
single fenced HTML block). Sibling to `voice.md`.
Seed derived from [docs/pantsless-build-research.md](docs/pantsless-build-research.md) §2.

### 5.3 The episode lifecycle (permissive state machine)

```
idea → scripted → built → recorded → edited → published
                    │         ▲
                    │         └── reshoots allowed (edited → recorded)
                    ▼
              on-hold  ←──────→  (any state)         cut  (terminal; hidden from default list)
```

Forward is the happy path, but transitions are **permissive** (a 4-year-old co-star means reshoots),
recorded in `[[history]]`, warn-but-never-block on unusual jumps. `cwp next` = the most-advanced
in-flight episode (closest to `published`), tie-broken by lowest `seq`, plus its next action.

### 5.4 The Pantsless Test

The differentiator, encoded as a real gate (`[pantsless_test]` + `pantsless-test.md`) **and** as
scripted verifier keypoints:

1. **Can start unaided** · 2. **Understands the goal** (no reading) · 3. **Can't break it** ·
4. **Enjoys it.** Doubles as the recurring on-camera bit.

### 5.5 Seed episode bank (first 12, ranked easiest-first; balanced 3/3/3/3)

Seeded as `idea`-status episodes at build time. Full hooks in the Appendix.

| seq | Title | Ingredient | Effort | Teaches |
|---|---|---|---|---|
| 001 | The Number-Guessing Machine (Binary Search, No Cheating) | neetcode | S | binary search |
| 002 | The Precise Moment Pants Become Optional: A Live Hawaii Pants Index | hak | S | formula/heat-index modeling |
| 003 | The Sock-Matching Machine (Two Sum, But Socks) | neetcode | S | hash-map pairing (Two Sum) |
| 004 | The Unbeatable Cookie-Splitter | hak | S | "I cut, you choose" fairness/game theory |
| 005 | I Let My 4-Year-Old Prompt Claude (No Notes) | kid | S | prompting / AI as a filmed topic |
| 006 | FizzBuzz, But It's a Dinosaur | neetcode | S | modulo / FizzBuzz |
| 007 | Is the Dice Cheating? (My Daughter Runs the Audit) | kid | S | uniformity / chi-square intuition |
| 008 | A Bedtime Story Picker That Never Repeats (Until It Has To) | hak | S | Fisher–Yates shuffle |
| 009 | Are We There Yet? (An Honest Answer, Powered by Math) | xkcd | M | haversine distance |
| 010 | Shortest Path to the Potty (An Emergency BFS) | xkcd | M | BFS / shortest path |
| 011 | Scream-to-Watts: Could Bath-Time Meltdowns Power the House? | xkcd | M | decibel → energy physics |
| 012 | Lego Ouch Calories: Barefoot Steps Converted to Calories Burned | kid | S | light arithmetic modeling |

---

## 6. Modules

All source under `src/cwp/`.

| Module | Responsibility |
|---|---|
| `cli.py` | `argparse` dispatch for all sub-commands; thin. `main()` reconfigures stdout/stderr to UTF-8 (`errors="replace"`) — Windows cp1252 consoles + captured output; heavy deps (`faster_whisper`, `playwright`) imported lazily inside their handlers so `cwp list`/`--help` stay fast. Exit codes: 0 ok, 1 user error, 2 environment/quality-gate failure. |
| `config.py` | Locate repo root + `episodes/`, `voice.md`, `build-contract.md`, `pantsless-test.md`; channel defaults. |
| `episodes.py` | `Episode` model, §4.1 id/slug gen, atomic `meta.toml` read/write, folder-scan-**derived** index. Powers `new`, `idea`, `list`, `show`. |
| `lifecycle.py` | §5.3 permissive state machine, append-only history, `next` priority. Powers `status`, `next`. |
| `drafting.py` | Prompt assembly from `voice.md` + context. Owns the exported `claude -p` call seam (prompt via **stdin**, neutral cwd, preflight auth check, per-caller timeout, partial-write idempotency: on timeout/exception the captured stdout flushes atomically to the temp file, never the target) that `brief.py`/`build.py` import. `--dry-run`; drafts open with the literal `<!-- AI DRAFT -->` marker. Powers `draft`. |
| `publishing.py` | Studio-ordered paste block, field validation, `AI DRAFT`-marker warning, record `youtube_url` + set `published`. Powers `publish`. |
| `capture.py` | `faster-whisper` wrapper (local, default `small`; `language="en"`, `vad_filter=True`, `beam_size=5`, toy-vocabulary `initial_prompt`), audio import (`--audio`), redact-names scan before write, re-record hint when confidence is low (mean segment `avg_logprob` < −1.0 or ≤2 words; tune after M2). Powers `capture`. |
| `brief.py` | The kid-gateway distill — assemble a prompt from the noisy transcript, recover intent, emit `brief.md` (TOML frontmatter per Appendix C; `must_haves[]` in the closed predicate vocabulary + verbatim, redact-names-scanned `kid_quote`) via the drafting.py claude seam. Owns the ONE brief parse/write pair (and the predicate-vocabulary constant) that `build.py`/`verify.py` import. Powers `brief`. |
| `verify.py` | The static + Playwright headless verifier. Owns `FORBIDDEN_PATTERNS` and `MAIN_ACTION_MIN_VIEWPORT_PCT = 20` (single sources of truth); Chromium launched with `--autoplay-policy=user-gesture-required`; AudioContext shim + `data-action-count` + ~15-click mash; compiles vocabulary-form `must_haves` into keypoint assertions (deterministic mapper — no LLM in the verifier). Calibrated with golden + single-defect garbage fixtures. Reused by `build.py`; mirrors `/judge-ui`. |
| `build.py` | The §3.2 generate→extract→verify→repair→commit orchestration via the drafting.py seam (own ~300s timeout; timeout ≠ repair attempt; near-identical-retry abort). Powers `build`. |
| `templates.py` | Template strings for the per-episode files (`meta.toml`, `script.md`, `publish.md`, `brief.md`, `project/index.html`) and the 12-episode bank data. The three root SoT docs (`voice.md`, `build-contract.md`, `pantsless-test.md`) are NOT templates — they are authored once, directly at repo root, in Step 1. |
| `__main__.py` | `python -m cwp` → `cli.main()`. |

---

## 7. Skills map — how this uses your claude-skills toolkit

The project is designed to lean on the existing toolkit; three small new project skills wrap the
`cwp` engine for conversational use.

| Pipeline stage | Existing skills | New project skills |
|---|---|---|
| Plan / expand | `/plan-init` ✓, `/plan-feature`, `/plan-review`, `/plan-wrap` | — |
| Publish / track | `/repo-init`, `/repo-sync`, `/repo-update` | — |
| Build the tool | `/plan-expedite`, `/build-phase`, `/build-step`, `/review-gauntlet` | — |
| Find episode ideas | `/user-brainstorm`, `/goblin-suggest`, `/deep-research` | **`/cwp-ideas`** (runs the idea-lens→curate workflow, writes new `idea` episodes) |
| The Pantsless Build | `/user-gateway` (pattern for the kid-gateway), `/judge-ui` (verify the toy) | **`/pantsless`** (live capture→brief→build with your son), thin wrappers over `cwp capture/brief/build` |
| Accept the toy/episode | `/user-walkthrough`, `/user-uat`, `/user-shakedown` | — |
| Skill up (feeds the channel) | `/user-learn` — learn an episode's topic hands-on, then teach it | — |
| Keep skills sharp | `/skill-eval-setup`, `/skill-iterate`, `/skill-evolve` | (applied to the new `/cwp-*` skills) |
| Session hygiene | `/session-wrap`, `/user-wrap`, `/user-afterparty` | — |

The new `/cwp-*` skills are **v1.x niceties** — the `cwp` CLI commands are the tested engine; the
skills are thin conversational front doors (especially `/pantsless` for the live-with-kid moment).

---

## 8. Project Structure

```
coding_without_pants/
├── plan.md · CLAUDE.md · README.md
├── pyproject.toml            # uv; entry point cwp = "cwp.cli:main"
├── .gitignore                # media/, private/, clips/, *.mp4/*.mov/*.wav/*.m4a/*.mp3/*.ogg, episodes/*/capture/, project/.repair/, .venv, __pycache__
├── voice.md                  # channel voice — SoT for drafts (frozen v1)
├── build-contract.md         # one-shot generation contract — SoT for builds
├── pantsless-test.md         # the 4-point kid-usability checklist template
├── docs/
│   ├── production-notes.md   # minimal-gear + anti-burnout workflow tips
│   └── pantsless-build-research.md   # one-shot gen + whisper reference
├── src/cwp/                  # cli, config, episodes, lifecycle, drafting, publishing, capture, brief, verify, build, templates
├── episodes/NNN-slug/        # the workshop (seeded with 12 ideas)
├── tests/                    # per-module + golden/garbage verifier fixtures + e2e smoke
└── .claude/skills/           # /cwp-ideas, /pantsless (v1.x)
```

---

## 9. Key Design Decisions

1. **One repo, two loops.** The thing you publish and the thing you build are the same artifact.
2. **The Pantsless Build is v1, not a later phase** — it's the heart of the channel; a working
   kid→AI→toy loop is a v1 acceptance criterion.
3. **Folder-as-id, derived index — no separate index file.** Drift is structurally impossible.
4. **id immutable; title mutable.** Comedic titles evolve; folder links/history must not break.
5. **Permissive, non-linear lifecycle.** Reshoots are guaranteed with a 4-year-old co-star.
6. **Local speech-to-text (`faster-whisper`).** Privacy by construction — a child's voice never
   leaves the machine. The ~25% child-speech WER means the transcript is *noisy by design*; the
   distill step recovers intent and keeps the funny mishears as `kid_quote`.
7. **One-shot generation via `claude -p`, made reliable by a verify+repair loop.** We ride the
   one-shot-HTML grain; the loop (static + headless keypoint checks, exact-evidence repair, atomic
   commit, `needs_human` on exhaustion) is what turns "one-shot" into "dependable." This is also the
   *not-terrible-material* guarantee.
8. **AI is an assistant, never a dependency.** Every `claude`-shelling path degrades gracefully
   (preflight, timeout, idempotent partial-write, `--dry-run`, review marker, `needs_human`).
9. **Metadata-prep, not auto-upload, for v1.** Skips Google OAuth/quota; `cwp publish` emits a
   Studio-ordered block so the last-mile paste is trivial.
10. **Calibrated verifier.** The toy verifier is anchored with a golden (passes) + garbage (fails)
    fixture before it gates real builds (measurement-validity rule).
11. **Small deps, no over-build.** No config system, no plugins; the acceptance bar is "ship one
    episode + one kid-built toy," not "cwp is feature-complete."
12. **Public-repo-safe by construction.** Media AND the whole `episodes/*/capture/` dir
    git-ignored, a redact-names guard on every text artifact derived from the kid's speech
    (structural, not convention — §4.3), a publish-moment checklist, no secrets, local Whisper.

---

## 10. API Route Contract

**N/A** — local CLI, no backend API, no HTTP surface, no ports.

---

## 11. Open Questions / Risks

| Item | Risk | Mitigation |
|---|---|---|
| Over-building the tool | Tool becomes the hobby; no video ships | Hard v1 cap (no config/plugins); acceptance = ship episode 001 + one kid-built toy (M1/M2); time-box |
| `claude` hang | A draft/build hangs and eats a nap window | Subprocess timeout + immediate partial-write (idempotent) |
| `claude` auth expired | Opaque mid-session failure | Preflight `claude -p ok`; fail fast with fix-it text |
| Off-voice AI content live | AI copy auto-flows into publish | `AI DRAFT` marker; `cwp publish` warns while markers remain |
| Noisy child transcript | ~25% WER garbles the wish | Distill recovers intent + keeps `kid_quote`; `cwp capture` offers re-record; transcript treated as noisy input, not ground truth |
| Broken one-shot toy shipped | A crashing/unusable toy reaches the kid/camera | Verify+repair loop; `needs_human` + exit 2 on exhaustion; never clobber an existing toy |
| Last-mile paste friction | Metadata needs reformatting → stalls | `cwp publish` emits ONE Studio-ordered block |
| No success metric | Can't tell if friction dropped | `created_at`/`published_at` → `cwp list` shows idea→published cycle time |
| **YouTube "Made for Kids" flag** (operator) | Child content → COPPA audience setting | Operator sets it per video; flagged in `pantsless-test.md` |
| **Filming a 4-year-old publicly** (operator) | Privacy/comfort of a child on a public channel | Operator's call; plan bakes privacy-by-default (redact-names guard, media + `capture/` git-ignored, local Whisper, publish checklist) |
| Kid as production dependency | Bad mood → failed take | Lifecycle allows `edited → recorded`; treat kid clips as short bonus cutaways, never ship-blocking |
| `voice.md` perfectionism | Endless tinkering | Seed verbatim, freeze v1, revisit after 3 published |

---

## 12. How to Run

```powershell
# From the repo root (c:\Users\abero\dev\coding_without_pants)
uv sync                                  # venv + install (Python 3.12+, tomli-w, faster-whisper, playwright)
uv run playwright install chromium       # once, for the toy verifier
# system ffmpeg NOT required — faster-whisper decodes via bundled PyAV (verified)
```

Channel Loop:

```powershell
uv run cwp idea "button that burps the alphabet"      # fast idea capture
uv run cwp new "The Sock-Matching Machine (Two Sum, But Socks)"
uv run cwp list                                       # derived table + cycle time
uv run cwp next                                        # what to work on + next action
uv run cwp draft 003 script                            # draft in-voice via claude
uv run cwp status 003 built
uv run cwp publish 003 --url https://youtu.be/XXXX     # paste-ready metadata + mark published
```

The Pantsless Build:

```powershell
uv run cwp capture 005 --audio clips/wish.wav          # faster-whisper → transcript (offers re-record)
uv run cwp brief 005                                   # distill the noisy transcript → brief.md
uv run cwp build 005                                   # one-shot generate + verify + repair → project/index.html
# open episodes/005-.../project/index.html and hand the laptop to your son (Reveal)
```

Quality gates:

```powershell
uv run pytest
uv run ruff check .
uv run mypy src
```

---

## 13. Roadmap

**v1 — the two working loops (this plan).** Channel Loop + the full Pantsless Build (capture →
brief → build → verify/repair). Acceptance: Steps M1 + M2.

**v2 — portfolio byproducts (light; a `/plan-feature` phase later).** A **playable-toy gallery on
GitHub Pages** (one index page linking every episode's `index.html` — instantly demoable) + a
one-page architecture writeup (the two loops). Cheap because the toys are already self-contained
HTML. *Job-signal note:* this is the deliberately-minimal portfolio surface — see the mapping below.
It is **not** a driver; it exists so the project doesn't disqualify you and quietly supports the
lanes you care about.

**v3 — optional polish.** `cwp draft --description/--title` batch, auto-chapters from the script,
richer `cwp next` planning, live mic capture (`cwp capture --record` — needs an audio-capture dep,
e.g. sounddevice). Only if the loops earn it.

**Portfolio → career-ops lane mapping** (from `career-ops/data/application-strategy.md`, kept light):

| Your target lane | What this project shows |
|---|---|
| Applied AI / Agentic Systems Engineer | The Pantsless Build is a real agentic pipeline: whisper → distill → one-shot generate → headless verify → repair loop |
| GenAI Evaluation, Reliability & Governance | The verify+repair loop *is* an eval/reliability story — assert the artifact matches its spec before shipping (the "not-terrible-material" guarantee) |
| AI Developer Tools / DevRel / Field Engineering | The channel itself: teaching, calm clear communication, a public playable gallery — the standout DevRel fit |

The elegant alignment: **optimizing for "fun with my son + not-embarrassing output" produces the
job signal for free.** No separate portfolio grind.

---

## 14. Development Process

Built with `/build-phase` walking the Automated Steps below. Default flags:
`--reviewers code --isolation worktree` (the tool is a CLI; the toy HTML is *content*, not the tool's
UI, and Playwright is a test tool — so `code` reviewers + calibration tests are the gate, no runtime
reviewers). **Exception:** Steps 8 and 9 escalate to `--reviewers deep` — the verifier is the
measurement instrument and the build engine is the reliability core; a silent miss there ships a
broken toy to the kid/camera (consider `/review-deep --model-override bugs=fable` per the workspace
model doctrine). Each step is one vertical slice with an integration test through the production
`cwp` entry point. No autonomous/background behavior (the build loop's retries are foreground and
bounded), so no soak phase — but a real producer→consumer + external-subprocess pipeline exists
(audio → whisper → brief → claude → html → playwright), so **Step 11 is a dedicated end-to-end smoke
gate**, **Step 8 calibrates the verifier with golden + single-defect garbage anchors**, and
**Step M1.5 is the live-substrate shakeout** (the first REAL claude+whisper run, before the kid
session).

> Worktree note: fresh worktrees don't inherit `.venv` — run `uv sync` + `uv run playwright install
> chromium` before the first quality gate on Steps 6–11.

### Automated Steps
(These run unattended via `/build-phase`.)

### Step 1: Scaffold + CLI skeleton + config + root SoT seeds
- **Problem:** Stand up the uv project, the `cwp` argparse entry (`--help`, `version`; `main()`
  reconfigures stdout/stderr to UTF-8 with `errors="replace"`; heavy deps imported lazily inside
  subcommand handlers), `config.py` (repo-root/`episodes`/`voice.md`/`build-contract.md` discovery),
  a `templates.py` stub, `pyproject.toml` with the `cwp` entry point (`playwright` in
  `[project.dependencies]` — verify.py imports it at runtime; pytest/ruff/mypy in the dev group),
  `.gitignore` (the §4.3 privacy globs), ruff+mypy config (`[[tool.mypy.overrides]]` with
  `ignore_missing_imports` for `faster_whisper.*`/`ctranslate2.*`), AND author the three root SoT
  docs verbatim from this plan's appendices + research §2: `voice.md` (Appendix A),
  `build-contract.md` (Appendix B), `pantsless-test.md` (Appendix E) — Steps 4 and 9 consume them.
  (Confirmed: `claude -p` DOES auto-discover cwd CLAUDE.md by default — claude v2.1.170 `--help`
  documents `--bare` as the mode that skips "CLAUDE.md auto-discovery", i.e. discovery is on unless
  suppressed; `--bare` also restricts auth to `ANTHROPIC_API_KEY` (OAuth never read), so it is
  unusable with this project's subscription auth, and `--safe-mode` disables ALL customizations
  (CLAUDE.md included) with OAuth intact but is a troubleshooting mode — neutral-cwd (temp dir)
  stands as the chosen mechanism per §3.2, with `--safe-mode` as the verified fallback flag.)
- **Type:** code
- **Issue:** #2
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `pyproject.toml`, `src/cwp/{__init__,__main__,cli,config,templates}.py`, `.gitignore`, ruff/mypy config, `voice.md`, `build-contract.md`, `pantsless-test.md`
- **Done when:** `uv run cwp --help` lists all sub-commands fast (no heavy imports at module top); `uv run cwp version` prints a version; the three root SoT files exist with the appendix content; a test asserts `.gitignore` contains the §4.3 privacy patterns; a test prints a non-ASCII title under captured output without raising; `ruff check .` and `mypy src` clean.
- **Depends on:** none
- **Status:** DONE (2026-07-16)

### Step 2: Episode model + new/idea/list/show
- **Problem:** `episodes.py` (Episode dataclass, §4.1 id/slug gen, atomic `meta.toml` read/write,
  folder-scan-derived index) + the per-episode file templates, wired to `cwp new`, `cwp idea`
  (fast capture), `cwp list`, `cwp show`.
- **Type:** code
- **Issue:** #3
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `src/cwp/episodes.py`, template content, `tests/test_episodes.py`
- **Done when:** `cwp new "Test"` creates `001-test/` with all files + valid `meta.toml`; `cwp idea "x"` adds a minimal idea episode; `cwp list`/`cwp show 001` work; unit tests cover id/slug/collision/scan; an integration test drives `new → list` through the CLI.
- **Depends on:** 1
- **Status:** DONE (2026-07-16)

### Step 3: Lifecycle (status + next)
- **Problem:** `lifecycle.py` (§5.3 permissive state machine, append-only history + UTC timestamps, `on-hold`/`cut`, warn-on-unusual-jump, `next` priority), wired to `cwp status`/`cwp next`.
- **Type:** code
- **Issue:** #4
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `src/cwp/lifecycle.py`, `tests/test_lifecycle.py`
- **Done when:** `cwp status 001 built` records the transition; a backward transition warns but succeeds; `cwp status 001 cut` hides it from default `cwp list`; `cwp next` returns the right episode + action; tests cover forward/backward/terminal + `next` tie-breaking.
- **Depends on:** 2
- **Status:** DONE (2026-07-16)

### Step 4: AI drafting (Channel Loop)
- **Problem:** `drafting.py` — assemble a prompt from `voice.md` (seeded in Step 1) + episode
  context, shell to `claude -p` (prompt via **stdin**, never argv — Windows ~32K argv ceiling;
  neutral cwd per §3.2; preflight auth check; ~60s draft timeout; partial-write idempotency: on
  timeout/exception flush captured stdout atomically to the temp file, never the target),
  `--dry-run`, and the literal `<!-- AI DRAFT -->` marker as the first line of drafted content —
  wired to `cwp draft <id> <outline|script|title|description>`. Exports the claude-call seam that
  `brief.py`/`build.py` import (one subprocess wrapper, three callers).
- **Type:** code
- **Issue:** #5
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `src/cwp/drafting.py`, `tests/test_drafting.py`
- **Done when:** `cwp draft 001 title --dry-run` prints the assembled prompt without calling `claude`; with a fake `claude` shim on PATH, `cwp draft 001 script` writes marked content; all four variants (`outline|script|title|description`) run through the one shared code path with a test each; a missing/unauthed `claude` prints fix-it text and exits 2; tests **mock the `claude` boundary** (no real API in CI) and cover the auth-fail path.
- **Depends on:** 1, 2
- **Status:** DONE (2026-07-16)

### Step 5: Publish-prep (Channel Loop)
- **Problem:** `publishing.py` — assemble `publish.md` into one Studio-ordered paste block, validate required fields, warn on remaining `<!-- AI DRAFT -->` markers, print the unconditional "Before you publish" checklist (real-name scan + Made-for-Kids audience setting) beside the paste block, record `youtube_url` + set `published` via `--url` — wired to `cwp publish`.
- **Type:** code
- **Issue:** #6
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `src/cwp/publishing.py`, `tests/test_publishing.py`
- **Done when:** `cwp publish 001` writes an ordered Title/Description/Tags/Thumbnail-text block + warns on missing fields/markers + prints the "Before you publish" checklist; `cwp publish 001 --url <u>` records the URL, sets `published_at`, transitions to `published`; tests cover ordering/validation/record/checklist.
- **Depends on:** 3, 4
- **Status:** DONE (2026-07-16)

### Step 6: Capture (faster-whisper)
- **Problem:** `capture.py` — a `faster-whisper` wrapper (local, default `small`, `--model medium`
  escalation, offline after first download; `language="en"`, `vad_filter=True`, `beam_size=5`, and
  a toy-vocabulary `initial_prompt` — real WER levers for child speech), audio import (`--audio`
  only; live mic `--record` is v3), redact-names scan via `private/redact-names.txt`
  (redact-by-default, `--allow-names` override, absent file → no-op + one-time notice) before
  writing `capture/transcript.txt`, with a re-record hint when confidence is low (mean segment
  `avg_logprob` < −1.0 OR transcript ≤ 2 words; tune after M2) — wired to `cwp capture`. Verify
  whether system `ffmpeg` is actually needed for non-WAV decode (faster-whisper bundles PyAV) and
  update plan.md/CLAUDE.md accordingly; if it IS needed, preflight `shutil.which("ffmpeg")` with
  fix-it text.
- **Type:** code
- **Issue:** #7
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `src/cwp/capture.py`, `tests/test_capture.py`
- **Done when:** `cwp capture 005 --audio tests/fixtures/hello.wav` writes a redaction-scanned transcript; the whisper call is behind a seam so tests **mock the transcription boundary** (a canned transcript); one real-model test is opt-in via `CWP_RUN_REAL_WHISPER=1` (deselected by default — faster-whisper is a required dep, so `importorskip` would never skip); the low-confidence heuristic prints the re-record hint in a test; a name in the redact list never appears in the written transcript.
- **Depends on:** 2
- **Status:** DONE (2026-07-16)

### Step 7: Brief (kid-gateway distill)
- **Problem:** `brief.py` — assemble a distill prompt from the noisy transcript (treat it as
  ~25%-WER noisy data: recover intent, keep the verbatim funny `kid_quote`), call the drafting.py
  claude seam, and write `brief.md` as a **fenced TOML frontmatter block + prose body** (Appendix C:
  `one_sentence_goal`, `single_action`, `visual_motif`, `must_haves[]` — each entry in the closed
  predicate vocabulary — `kid_quote`, `kid_nickname`, Pantsless criteria). The distill prompt
  instructs the model to emit `must_haves` ONLY in vocabulary form. Owns the single brief
  parse/write pair (and the vocabulary constant) that `build.py`/`verify.py` import. `kid_quote`
  passes the redact-names scan before write — wired to `cwp brief`.
- **Type:** code
- **Issue:** #8
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `src/cwp/brief.py`, `tests/test_brief.py`
- **Done when:** with a fake `claude` shim, `cwp brief 005` reads the transcript and writes a `brief.md` whose frontmatter parses round-trip via brief.py's own loader, with non-empty vocabulary-form `must_haves[]` and `kid_quote`; a must_have outside the vocabulary triggers one re-ask, then exit 2; missing transcript → user error (exit 1); tests mock the `claude` boundary and assert the schema + redaction.
- **Depends on:** 4, 6
- **Status:** DONE (2026-07-16)

### Step 8: Toy verifier (calibrated)
- **Problem:** `verify.py` — the static checks (the ONE `FORBIDDEN_PATTERNS` constant per §3.2:
  resource-context-scoped URL patterns with the SVG-xmlns allowlist, `alert(`/`confirm(`/`prompt(`,
  the `</script>`-count rule; doctype; size floor) + the Playwright headless check (Chromium
  launched with `--autoplay-policy=user-gesture-required`; console/pageerror listeners registered
  before goto; `[data-testid="main-action"]` presence + ≥20% viewport via
  `MAIN_ACTION_MIN_VIEWPORT_PCT` — the intentional buffer under the contract's 25% target; click
  increments `data-action-count`; AudioContext init-script shim — none constructed pre-click,
  `state === "running"` post-click; ~15-click rapid mash with zero new errors + monotonic count; no
  interactive elements besides main-action) + compiling a brief's vocabulary-form `must_haves`
  (imported from brief.py) into keypoint assertions — a deterministic mapper, no LLM in the
  verifier. Calibrate with a **golden** fixture (passes — includes an inline `<svg xmlns=…>` to pin
  the allowlist) and **single-defect garbage fixtures**: `garbage_button.html` (tiny main action),
  `garbage_audio.html` (top-level AudioContext ONLY — proves the shim catches what console errors
  cannot), `garbage_dialog.html` (an `alert(` call).
- **Type:** code
- **Issue:** #9
- **Flags:** --reviewers deep --isolation worktree
- **Produces:** `src/cwp/verify.py`, `tests/fixtures/{golden,garbage_button,garbage_audio,garbage_dialog}.html`, `tests/test_verify.py`
- **Done when:** `verify(golden.html)` passes; each single-defect garbage fixture fails FOR ITS OWN defect (asserted on the structured evidence, not just pass/fail); the must_haves compiler correctly maps ≥3 vocabulary predicates **not present in any fixture** to assertions; the verifier returns structured evidence (which check failed, with the offending line/selector/console error); requires `playwright install chromium`.
- **Depends on:** 1, 7
- **Status:** DONE (2026-07-16)

### Step 9: Build engine (generate → verify → repair → commit)
- **Problem:** `build.py` — the §3.2 loop via the drafting.py claude seam (prompt =
  `build-contract.md` from Step 1 + the brief, via **stdin**, neutral cwd; own ~300s timeout — a
  timeout is retried once and does NOT consume a repair attempt) → extract the single HTML fence
  (extraction failure gets the fence-specific evidence template) → `verify.py` → on failure repair
  with exact evidence (≤2 retries; a near-identical retry aborts straight to `needs_human`) → on
  pass atomic-commit to `project/index.html` (never clobber; `--force` to overwrite) → on
  exhaustion set `needs_human` + exit 2. Missing `brief.md` → user error (exit 1). Save
  `.repair/attempt-N.png` + `log.jsonl` (minimal schema: attempt, timestamp, check, pass,
  duration_ms, evidence) — wired to `cwp build`.
- **Type:** code
- **Issue:** #10
- **Flags:** --reviewers deep --isolation worktree
- **Produces:** `src/cwp/build.py`, `tests/test_build.py`
- **Done when:** with a fake `claude` shim returning (a) a golden toy → `cwp build 005` commits `project/index.html` and logs a pass; (b) a broken toy twice then a good one → repair succeeds on the evidence; (c) broken every time → `needs_human=true`, exit 2, existing toy untouched; (d) the same broken toy twice in a row → near-identical abort before the last slot is spent; (e) a timeout → one same-slot retry then a timeout-specific `needs_human` message; missing brief exits 1; tests mock the `claude` boundary and exercise all paths.
- **Depends on:** 1, 4, 7, 8
- **Status:** DONE (2026-07-17)

### Step 10: Seed 12 ideas + docs
- **Problem:** Seed the 12-episode idea bank as `idea` episodes; write `docs/production-notes.md`;
  finalize `README.md` and `CLAUDE.md`. (The three root SoT docs — `voice.md`,
  `build-contract.md`, `pantsless-test.md` — were authored in Step 1; this step only verifies they
  still match the appendices.)
- **Type:** code
- **Issue:** #11
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `docs/production-notes.md`, 12 seeded `episodes/*/`, `README.md`, updated `CLAUDE.md`
- **Done when:** `cwp list` shows all 12 seeds with correct ingredient/effort, under captured UTF-8 output (the seed hooks contain non-ASCII); `README.md` quickstart matches the actual CLI; a test asserts the 12 seeds load + validate.
- **Depends on:** 2

### Step 11: End-to-end smoke gate (both loops)
- **Problem:** One integration test through the production `cwp` entry point on a temp episode:
  Channel Loop (`new → draft(fake claude) → status → publish`) **and** Pantsless Build
  (`capture(canned transcript) → brief(fake claude) → build(fake claude → golden toy) → verify`),
  asserting the derived index, the ordered publish block, and a committed verified toy — no
  exceptions, no folder/index drift.
- **Type:** code
- **Issue:** #12
- **Flags:** --reviewers code --isolation worktree
- **Produces:** `tests/test_smoke_e2e.py`
- **Done when:** the round-trip is green for both loops with mocked external boundaries.
- **Depends on:** 5, 9, 10

### Manual Steps
(These run after `/build-phase` completes. Operator drives.)

### Step M1: Dogfood the Channel Loop
- **Source step:** v1 acceptance bar (§1.1)
- **Issue:** #13
- **Commands:**
  ```powershell
  cd c:\Users\abero\dev\coding_without_pants
  uv run cwp next
  uv run cwp draft 001 outline
  # …edit script.md, build the toy (by hand or via the Pantsless Build)…
  uv run cwp status 001 built
  uv run cwp publish 001
  ```
- **What to look for:**
  | Check | Expected outcome |
  |---|---|
  | Friction | idea → publish-ready felt low-effort |
  | Draft quality | sounds like the channel voice; light edits only |
  | Paste block | `publish.md` pasted into Studio with no reformatting |
  | Acceptance | every cwp-owned stage of episode 001 (idea/script/publish-prep) went through `cwp` to a paste-ready `publish.md` |

### Step M1.5: Solo Pantsless dry-run (real toolchain — no kid yet)
- **Source step:** de-risk gate before M2 — the first LIVE claude + whisper + Playwright interaction (every automated step mocks the claude boundary)
- **Issue:** #14
- **Commands:**
  ```powershell
  cd c:\Users\abero\dev\coding_without_pants
  # any short self-recorded clip works — YOU describing a toy is fine
  uv run cwp capture 006 --audio clips\dryrun.wav
  uv run cwp brief 006
  uv run cwp build 006
  # open episodes/006-.../project/index.html yourself
  ```
- **What to look for:**
  | Check | Expected outcome |
  |---|---|
  | First-run plumbing | whisper model download completes; chromium present; `claude` auth preflight passes (system ffmpeg confirmed NOT needed in Step 6 — no check required) |
  | Generate → verify | real `claude -p` output survives extraction + static + headless checks (or repair recovers on the evidence) |
  | Repair loop | on any failure, the evidence-fed retry visibly addresses the evidence |
  | Timing | capture + brief + build fits a nap window — note the wall-clock |
  | Verdict | a verified toy exists WITHOUT the kid having been needed for any of it |

### Step M2: Dogfood the Pantsless Build — with your son (the heart of the channel)
- **Source step:** v1 acceptance bar (§1.2)
- **Issue:** #15
- **Commands:**
  ```powershell
  cd c:\Users\abero\dev\coding_without_pants
  # record your son describing what he wants (phone/mic), save as clips/wish.wav
  uv run cwp capture 005 --audio clips/wish.wav
  uv run cwp brief 005
  uv run cwp build 005
  # open episodes/005-.../project/index.html and hand him the laptop
  ```
- **What to look for:**
  | Check | Expected outcome |
  |---|---|
  | Capture | the transcript is close enough (or the re-record hint fired sensibly) |
  | Distill | `brief.md` captured his intent + preserved the funny `kid_quote` |
  | Build | the verify+repair loop produced a toy that opens with no console errors |
  | Pantsless Test | your son can start it unaided, gets it without reading, can't break it, enjoys it |
  | Fun | *the moment felt like play, not a chore* — the real success metric |
  | Acceptance | one kid clip went capture → brief → verified toy end-to-end via `cwp` |

**Please run M1, then M1.5, then M2** once the automated steps complete — M1.5 spends the
first-live-run risk on disposable input instead of the kid's patience.

---

## 15. Appendix

### A. `voice.md` seed (frozen v1)

```markdown
# Coding without Pants — Voice

We build ONE small useful thing per video. Calmly. A little absurdly. Simple enough
my pantsless 4-year-old could use it.

## The recipe
- **NeetCode**: one concrete, named problem. Clean solution. Zero fluff. Show the whole thing.
- **Hak (mostlyhak)**: calm and unhurried. "Let's just figure this out together." Never a hype voice.
- **xkcd what-if**: start from a ridiculous premise, then answer it with REAL rigor. The comedy is in
  taking the silly question seriously.
- **The Pantsless Test**: the thing we build has to be usable by a 4-year-old. Big buttons. Obvious
  goal. Can't-break-it. Actually fun.

## Do
- Explain like the viewer is smart but new. One idea at a time.
- Keep the joke dry. Let the absurd premise do the work.
- End with the kid trying it (the Pantsless Test on camera).

## Don't
- No hype, no "SMASH that like button" energy.
- No jargon without a plain-language version first.
- Never ship something the kid can't operate.

## The name
Surf shorts (Hawaii) + a 4-year-old who refuses pants + tools simple enough he could use them.
"Coding without pants" = low-ceremony, playful, genuinely usable.
```

### B. `build-contract.md` seed (the one-shot generation contract)

The MUST/NEVER kid-safe rules, derived verbatim from
[docs/pantsless-build-research.md](docs/pantsless-build-research.md) §2: single self-contained
`index.html`, zero external resources (no CDN/fonts/`<img http>`), no network calls, one
`data-testid="main-action"` covering ≥25% of the viewport (the verifier gates at ≥20% — an
intentional measurement-jitter buffer) whose handler **increments a `data-action-count` attribute
on every activation** (the machine-checkable hook the verifier asserts), NO interactive elements
besides the main action, no reading required, Web Audio only inside a user-gesture handler,
emoji/CSS/`<svg>`/`<canvas>` visuals only (inline-SVG xmlns URLs are allowlisted by the verifier),
no `alert`/`confirm`/`prompt`, no dead-ends (must survive rapid repeated mashing), a short few-shot
skeleton, and a single ```` ```html ```` fenced output block. The forbidden-pattern list is owned
by `verify.py`'s `FORBIDDEN_PATTERNS` constant (one source of truth — this contract cites it, never
restates it). The brief's fields (`one_sentence_goal`, `single_action`, `visual_motif`,
`must_haves`, `kid_quote`, `kid_nickname`) are substituted per build.

### C. `brief.md` schema (distilled by `cwp brief`)

**Serialization (pinned):** `brief.md` opens with a fenced TOML frontmatter block (```` ```toml ````
… ```` ``` ````) holding the structured fields below, followed by an optional human-readable prose
body. `brief.py` owns the ONE parse/write pair; `build.py` and `verify.py` import it — never
re-parse.

| Field | Meaning |
|---|---|
| `one_sentence_goal` | what the toy is, in one plain sentence |
| `single_action` | the ONE verb/action the kid performs |
| `visual_motif` | the emoji/theme he asked for (dinosaur, sock, cookie…) |
| `must_haves[]` | 3–5 entries in the **closed predicate vocabulary** below → the verifier's keypoints |
| `kid_quote` | his verbatim (mis-heard) words, redact-names-scanned — the comedic gold |
| `kid_nickname` | the nickname substituted for any real name (also the redaction replacement value) |
| `pantsless` | the 4 criteria the build must satisfy |

**`must_haves` predicate vocabulary (closed — the distill prompt may ONLY emit these; the
vocabulary constant lives in `brief.py`, and `verify.py` compiles each deterministically):**

| Predicate | Verifier assertion |
|---|---|
| `visible:<emoji-or-word>` | the text/emoji is visible in the DOM after load |
| `element:<css-selector>` | the selector exists |
| `sound_on_action` | AudioContext shim: no context pre-click; one created + `running` after the main-action click |
| `state_change:<data-attr>` | clicking the main action changes the named data-attribute |

### D. Full seed episode bank (hooks) — 12 episodes

1. **The Number-Guessing Machine (Binary Search, No Cheating)** — *neetcode, S.* 20 Questions as an
   app that always wins in ≤7 guesses, then races Dad guessing "randomly."
2. **The Precise Moment Pants Become Optional: A Live Hawaii Pants Index** — *hak, S.* A dead-serious
   heat-index formula for exactly when pants stop being load-bearing.
3. **The Sock-Matching Machine (Two Sum, But Socks)** — *neetcode, S.* The infamous interview
   question, solved for pairing a preschooler's socks after laundry.
4. **The Unbeatable Cookie-Splitter** — *hak, S.* Sibling cookie warfare ended with the "I cut, you
   choose" theorem in one button.
5. **I Let My 4-Year-Old Prompt Claude (No Notes)** — *kid, S.* Hand the keyboard to a kid who can't
   spell; build exactly what he types, live, unedited.
6. **FizzBuzz, But It's a Dinosaur** — *neetcode, S.* Five clean lines, then a big-button counting toy
   that roars instead of printing "Fizz."
7. **Is the Dice Cheating? (My Daughter Runs the Audit)** — *kid, S.* 100 rolls into a tally app
   running a real fairness test; she's cleared to yell "CHEATER."
8. **A Bedtime Story Picker That Never Repeats (Until It Has To)** — *hak, S.* A calm Fisher–Yates
   walk landing on one mashable PICK MY STORY button.
9. **Are We There Yet? (An Honest Answer, Powered by Math)** — *xkcd, M.* Real haversine distance under
   one big honest button.
10. **Shortest Path to the Potty (An Emergency BFS)** — *xkcd, M.* Kid crayons a house-maze; BFS finds
    the provably shortest route before it's too late.
11. **Scream-to-Watts: Could Bath-Time Meltdowns Power the House?** — *xkcd, M.* Real decibel→energy
    physics off a live mic; a dial answers whether one tantrum runs the fridge.
12. **Lego Ouch Calories: Barefoot Steps Converted to Calories Burned** — *kid, S.* Every Lego brick
    underfoot = a calorie; the kid taps a live tally, we do the excruciating math.

### E. `pantsless-test.md` template

```markdown
# Pantsless Test — Episode <id>

Can a 4-year-old…
- [ ] **Start it unaided?**  - [ ] **Understand the goal?** (no reading)
- [ ] **Not break it?**      - [ ] **Enjoy it?**

Notes:

---
Operator reminders (not tool-enforced):
- Set YouTube "Made for Kids" audience appropriately for this video.
- Use the kid's nickname only; keep real name / identifying details out of the repo.
```

### F. Reference

- One-shot generation recipe, repair loop, kid speech-to-text comparison:
  [docs/pantsless-build-research.md](docs/pantsless-build-research.md).
- Minimal-gear + anti-burnout production tips: `docs/production-notes.md` (written in Step 10).
