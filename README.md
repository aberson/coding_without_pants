# Coding without Pants

The operating system and workshop for the YouTube channel **"Coding without Pants"** — calm,
funny, informative videos where a Hawaii dad builds **one small useful thing** per episode,
usually simple enough his pantsless 4-year-old can use it (the Pantsless Test).

One tiny `cwp` CLI over flat files runs **two loops**:

1. **The Channel Loop** — move an episode through `idea → scripted → built → recorded → edited →
   published`, draft copy in the channel voice via the `claude` CLI, and assemble paste-ready
   YouTube metadata. It removes the specific frictions that kill hobby channels; it is not a CMS.
2. **The Pantsless Build** (the heart of the channel) — the kid says what he wants, local Whisper
   transcribes it, AI distills a build brief, one-shot `claude -p` generates a self-contained
   kid-usable `index.html` toy, and an automated verify+repair loop proves it works *before he
   sees it*. Then he uses it on camera. The code you build is the thing you publish.

```
CAPTURE            DISTILL              BUILD                       REVEAL
kid rattles   ──►  faster-whisper  ──►  claude -p one-shot     ──►  he uses it on
off his wish        + kid-gateway        + verify + repair loop      camera (Pantsless
(record clip)       → build brief         → verified index.html       Test)
```

## Stack

| Layer | Tool | Why |
|---|---|---|
| Language / runtime | Python 3.12+ | stdlib `tomllib`, `argparse` — keeps the tool tiny |
| Package / env | `uv` | fast, reproducible |
| Metadata | TOML (`meta.toml`) | human-editable, atomically written |
| Episode project | self-contained HTML | runs in any browser from `file://`; passes the Pantsless Test |
| AI drafting + build | `claude` CLI (`claude -p`) | the one-shot generation primitive (OAuth; no API key in repo) |
| Speech-to-text | `faster-whisper` (local, CTranslate2) | a child's voice never leaves the machine |
| Toy verification | Playwright (headless Chromium) | console + keypoint checks on generated toys |
| Quality | pytest, ruff, mypy | tests gate every build step |

No backend, no database, no server, no ports, no PyTorch.

## Prerequisites

- Windows 11 + PowerShell (developed there; nothing is Windows-specific by design)
- Python 3.12+ and `uv` on PATH
- The `claude` CLI installed and authenticated (needed for `cwp draft` / `brief` / `build`;
  everything else works without it)

## Setup

```powershell
uv sync                                  # venv + deps (tomli-w, faster-whisper, playwright)
uv run playwright install chromium       # once, for the toy verifier
uv run cwp --help
```

The first `cwp capture` downloads the Whisper model once (~460 MB), then runs fully offline.

## Usage

```powershell
# Channel Loop
uv run cwp new "The Sock-Matching Machine (Two Sum, But Socks)"
uv run cwp list                           # derived episode table + cycle time
uv run cwp draft 003 script               # AI draft in the channel voice
uv run cwp publish 003 --url https://youtu.be/XXXX

# The Pantsless Build
uv run cwp capture 005 --audio clips/wish.wav   # local Whisper → transcript
uv run cwp brief 005                            # noisy transcript → build brief
uv run cwp build 005                            # generate + verify + repair → project/index.html
```

## Key design decisions

- **One repo, two loops** — every episode's browser toy lives beside the tooling that made it.
- **Folder-as-id, derived index** — the episode list is scanned from `episodes/*/meta.toml`
  on every run; drift is structurally impossible.
- **One-shot generation made dependable** — a calibrated verifier (static checks + headless
  Playwright keypoints, golden + single-defect garbage fixtures) gates every generated toy;
  ≤2 evidence-fed repairs, atomic commit, `needs_human` on exhaustion. Never ship a broken toy.
- **AI is an assistant, never a dependency** — every `claude`/whisper path degrades gracefully.
- **Kid privacy by construction** — audio and transcripts never committed (whole `capture/` dir
  git-ignored), local speech-to-text only, a redact-names guard on every text artifact derived
  from the kid's speech, and a publish-moment checklist (including YouTube "Made for Kids").

## Project structure

```
coding_without_pants/
├── plan.md                   # the full project plan (source of truth)
├── voice.md                  # channel voice — SoT for drafts
├── build-contract.md         # one-shot generation contract — SoT for builds
├── pantsless-test.md         # the 4-point kid-usability checklist
├── src/cwp/                  # cli, config, episodes, lifecycle, drafting, publishing,
│                             #   capture, brief, verify, build, templates
├── episodes/NNN-slug/        # the workshop: meta.toml, script.md, brief.md, project/index.html
├── tests/                    # per-module + golden/garbage verifier fixtures + e2e smoke
└── docs/                     # research notes, production notes, plan review
```

## Status

Pre-build: the plan is written and reviewed ([docs/plan-review-fable.md](docs/plan-review-fable.md));
code lands via the build steps in [plan.md](plan.md) §14. v1 acceptance: one episode reaches a
paste-ready `publish.md`, and one real kid clip goes capture → brief → build → a verified,
kid-usable toy end-to-end.
