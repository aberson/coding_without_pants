# The Pantsless Build — research notes

> Distilled from a parallel research pass (2026-07-15) on one-shot AI game/app generation,
> kid-usable HTML toy generation, and child speech-to-text. This is the reference behind
> the `cwp capture / brief / build` design in [../plan.md](../plan.md). Full raw findings
> were captured from three Sonnet research arms.

## 1. One-shot generation landscape (2025–2026)

The whole recent wave is *"one prompt → one self-contained HTML file that runs."* That is
**exactly** the shape of a Coding-without-Pants episode toy, so we ride the grain instead of
fighting it.

| Tool / technique | Relevance to `cwp build` |
|---|---|
| **`claude -p` headless mode** | The literal generation primitive. `claude -p "<brief>"` completes one agentic turn and exits — scriptable from a Python subprocess, reliably emits a working single `index.html`. Already the plan's chosen engine. |
| **Claude Artifacts** | Same single-file convention; its "outline, then generate everything into one .html" guidance is the template for our distill-then-build split. |
| **yashdew3/AI-Game-Generator** (OSS) | Closest open reference: prompt in → dependency-free HTML game out. Worth reading its prompt + output-validation code. |
| **Rosebud AI / Websim / OpenGame** | Precedent for "a spoken idea becomes one page immediately," and for *chat-refine-after-generate* as the cheap retry path. Confirm a single page (not a project scaffold) is the right end-state for a kid toy. |
| **GameGen-Verifier** (arXiv 2026) | Decompose a spec into checkable "keypoints" and verify the generated artifact headlessly. **This is our automated QA gate** — the brief's must-haves become scripted assertions. |
| **MarioGPT / "Five-Dollar Model"** | Legitimizes emoji/CSS-shape sprites (visuals from text, skip the image pipeline) as an established one-shot pattern, not a shortcut. |
| **Bolt.new / Lovable / v0.dev** | The "don't reach for this" signpost — they produce multi-file, dependency-heavy projects, the opposite of a portable kid toy. |

**Core techniques we adopt:** single-file zero-dependency delivery · spec-first distillation ·
emoji/CSS sprites · chat-refine retry · keypoint verification · kid-safe constraints in the
prompt · local STT before any network call · headless `claude -p` as the generation primitive.

## 2. Kid-usable generation prompt contract (`build-contract.md`)

The generation prompt is a fixed **MUST/NEVER contract** (the reusable asset `build-contract.md`),
with the per-episode brief substituted in. Key rules:

- **Feed a structured brief as variables** — `title`, `one_sentence_goal`, `single_action` verb,
  `teaches`, `visual_motif`, optional `kid_nickname`. Never let the model improvise scope.
- **Hard technical contract, up front, as MUST/NEVER:** single self-contained `index.html`; zero
  external resources (no CDN, no `@import`, no `<link href="http…">`, no `<img src="http…">`); no
  network calls (`fetch`/`XHR`/`WebSocket` forbidden); must run from a `file://` URL, no build step.
- **Paste the Pantsless Test verbatim** as literal acceptance criteria (reuse the rubric, don't
  reword it).
- **Exactly ONE primary action element** carrying `data-testid="main-action"`, covering ≥25% of the
  viewport (the verifier gates at ≥20% — an intentional measurement-jitter buffer), centered,
  high-contrast, gently animated to draw a toddler's eye, whose handler **increments a
  `data-action-count` attribute on every activation** — the machine-checkable hook the verifier
  asserts (pixel-diff is ruled out: the mandated idle animation would defeat it). **No other
  interactive elements at all.**
- **No dead-ends, no blocking dialogs** — forbid `alert`/`confirm`/`prompt`; every state loops back
  to the same big action (mash-forever toy, never a game-over screen with a tiny restart link).
- **Audio only inside a user-gesture handler** (never at top-level/on load, or autoplay policy
  silently suspends it). Inline a tested try/catch Web Audio snippet so the model reuses it.
- **Visuals:** emoji at large font-size, CSS gradients/`@keyframes`, inline `<svg>`, or `<canvas>`
  only. No `<img>` unless `src` is a `data:` URI.
- **One short few-shot skeleton** (doctype + `<style>` + `<script>` with placeholder comments) —
  the single biggest lever for cutting variance across generations.
- **Output format:** a single ```` ```html ```` fenced block and nothing else (for clean
  programmatic extraction).
- **On repair retries, reuse the exact template verbatim**, appending only the failure evidence.

### Sound & assets (zero external files)
- Web Audio oscillator beep: try/catch `new (AudioContext||webkitAudioContext)()` + Oscillator +
  Gain with an exponential ramp-down, created **and** started inside the click handler.
- Success chime: 3–4 scheduled oscillator notes (a tiny arpeggio).
- Large emoji (8–15rem) as the primary sprite; themed per episode via the brief's `visual_motif`.
- CSS `@keyframes` (bounce/pulse/wiggle/confetti) for feedback; `transform: scale(0.95)` on
  `:active` for tactile squish. Multi-modal (color+motion+sound) so a silent laptop still signals.

### Pitfalls one-shot generation hits (all handled by the contract or verifier)
- Blocking native dialogs freeze the page on first interaction (and hang the headless check).
- AudioContext created at top-level → silently suspended, no console error.
- Model reaches for Google Fonts / FontAwesome / Tailwind CDN unless each is **forbidden by name**.
- Text-dependent affordances ("Click here to start!") assume reading a 4-year-old can't do.
- A literal `</script>` inside a JS string/comment prematurely closes the tag and breaks parsing.
- Dead-end "Game Over" screens violate "can't break it, keep mashing."
- Small/hover-only hit targets are unusable on touch / for toddler mouse control.
- Model wraps output in prose or nested code fences → breaks naive single-fence extraction.

## 3. The generate → verify → repair loop (the reliability core)

```
generate → extract → static-check → headless-check → repair (≤2x) → commit
```

1. **Generate** — `claude -p` with the contract + brief delivered via **stdin** (never argv —
   Windows' ~32K argv ceiling; the repair prompt carries full HTML) and cwd in a neutral temp dir
   (so repo CLAUDE.md context never leaks into the prompt), reusing `drafting.py`'s
   preflight/idempotency mechanism but its OWN longer timeout (~300s vs ~60s drafts; a timeout is
   retried once and does not consume a repair attempt); capture stdout to a temp file. **Never**
   write `project/index.html` directly.
2. **Extract** — regex-pull the single ```` ```html ```` fence. No fence match = a repair-triggering
   failure (do not treat raw stdout as HTML).
3. **Static check** (fast, no browser) — assert a file-size floor, `<!DOCTYPE html>` present, and
   the ONE `FORBIDDEN_PATTERNS` constant (owned by `verify.py`; the contract cites it, never
   restates it): URL patterns scoped to resource-loading contexts (`src=`/`href=`/`fetch(`/
   `XMLHttpRequest`/`import(`/`<link rel="stylesheet"`) with an allowlist for inline-SVG namespace
   attributes (`xmlns="http://www.w3.org/…"`); `alert(`/`confirm(`/`prompt(`; and a `</script>`
   COUNT rule (>1 = a string-embedded breaker — bare presence is just the toy's own closing tag).
   Any hit fails with the exact offending line as evidence.
4. **Headless check** (Playwright; Chromium launched with `--autoplay-policy=user-gesture-required`
   — Playwright's default relaxes autoplay, which would mask gesture-gating bugs) — register
   `console` + `pageerror` listeners **before** `page.goto("file://…")`; inject a
   `page.add_init_script` shim wrapping `AudioContext`/`webkitAudioContext` recording construction
   time + `.state`; assert zero console errors after a settle window; locate
   `[data-testid="main-action"]`, assert it exists and its bounding box covers ≥20% of the viewport;
   `click()` it and assert (a) no new console errors, (b) the contract-mandated `data-action-count`
   attribute incremented (no pixel-diff — the mandated idle animation defeats it), and (c) no
   AudioContext existed pre-click and any constructed one reaches `state === "running"` post-click;
   then a rapid ~15-click mash asserting zero new errors + a monotonically increasing count
   ("can't break it"). Save `project/.repair/attempt-N.png` every attempt. Additionally, compile
   the brief's vocabulary-form `must_haves` (plan Appendix C's closed predicate set) into scripted
   keypoint assertions (GameGen-Verifier pattern) so *"matches what he asked for"* is checked, not
   just *"doesn't crash."*
5. **Repair** — on any failure, a second `claude -p` (stdin again) with the original brief +
   original HTML + the **exact** failure evidence verbatim (grep hit / console stack / missing
   selector / failed-click description / the fence-failure template: "your previous response had 0
   or >1 ```html fences — return exactly one, no prose"). Never a paraphrased "fix it." Full
   corrected HTML again, fenced-only. If a retry returns near-identical HTML to the previous
   attempt, abort straight to `needs_human` — don't burn the last slot on a stuck model.
6. **Budget** — cap at 2 repair attempts (3 shots total). On final failure, do **not** touch an
   existing `project/index.html` (respect clobber-protection), mark the episode `needs_human` in
   `meta.toml` notes, print the last evidence + screenshot path, exit 2. Never silently ship a broken
   toy ("AI is an assistant, never a hard dependency").
7. **Commit** — only on a full pass: atomic `os.replace` onto `project/index.html` (matching
   `episodes.py`'s atomic-write convention); log pass + check timings to `project/.repair/log.jsonl`
   for later prompt tuning.

**Calibration (measurement-validity rule):** the verifier must be anchored with a **golden** fixture
(a hand-written good toy — including an inline `<svg xmlns=…>` to pin the allowlist → passes) and
**single-defect garbage fixtures**: `garbage_button.html` (tiny main action), `garbage_audio.html`
(top-level AudioContext ONLY — catchable solely via the AudioContext shim, since it emits no
console error), `garbage_dialog.html` (`alert(`). Each must fail FOR ITS OWN defect (asserted on
the structured evidence, not just pass/fail) in tests before the verifier gates any real
generation.

## 4. Child speech-to-text → `faster-whisper`, local

| Option | Kind | Setup on Windows+uv | Note |
|---|---|---|---|
| **faster-whisper** ✅ | local | **Low** — `uv add faster-whisper`, prebuilt CTranslate2 Windows wheels, no C++ toolchain, no PyTorch. Decodes via bundled PyAV — system `ffmpeg` likely NOT needed (verify in Step 6). Model auto-downloads once, then offline. | **Recommended.** Default `small`, `--model medium` escalation (not `tiny`/`base`) for child speech. |
| openai-whisper | local | Medium — pulls multi-GB PyTorch; slower CPU inference. | Same weights, no accuracy edge, heavier runtime. |
| whisper.cpp (pywhispercpp) | local | Medium-high — CPU wheels exist; GPU needs CMake + VS toolchain. Manual ggml model download. | Same weights. |
| Vosk | local | Low-med — simple install but `pyaudio` mic capture is fiddly on Windows. | Adult-corpus-trained; weaker on spontaneous child speech. |
| Cloud ASR (Azure/Google/Deepgram) | cloud | Low — SDK + API key. | **Ruled out.** A child's voice would leave the machine; consumer terms allow retention/logging. Only viable with an enterprise no-retention contract. |

**Recommendation:** `faster-whisper`, local, default `small` (`--model medium` escalation), with
`language="en"`, `vad_filter=True`, `beam_size=5`, and a toy-vocabulary `initial_prompt` — real WER
levers for child speech. Privacy is satisfied *structurally* —
the audio never leaves the machine; only the distilled text brief reaches `claude`.

**Critical design implication:** child speech runs **~25% word-error-rate** (vs ~3% adult; Kid-Whisper
study). So the raw transcript is **noisy by design** — the pipeline must treat it as noisy input for
the distill step (recover intent, keep the funny mishears as `kid_quote`), and `cwp capture` must
offer a quick re-record/confirm affordance rather than trusting first-pass transcription.
