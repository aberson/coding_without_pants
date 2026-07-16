# Build Contract — the one-shot toy generation prompt

This document IS the generation prompt (single source of truth for builds; sibling to
`voice.md`, which governs drafts). `cwp build` substitutes the episode brief's fields into the
placeholders below and delivers this contract to `claude -p` via stdin, one shot. The verifier
then holds the output to every MUST/NEVER rule here.

**Brief fields substituted per build:** `{one_sentence_goal}`, `{single_action}`,
`{visual_motif}`, `{must_haves}`, `{kid_quote}`, `{kid_nickname}`.

---

## Your job

Build ONE self-contained browser toy for a 4-year-old who cannot read.

- **Goal:** {one_sentence_goal}
- **The one action:** {single_action}
- **Visual motif:** {visual_motif}
- **Must-haves:** {must_haves}
- **In his words:** "{kid_quote}" — {kid_nickname} asked for this. Honor the spirit of the wish.

## MUST

1. Produce a **single, self-contained `index.html`** — all markup, CSS, and JS inline in one
   file. It must run correctly when opened from a **`file://` URL**: no build step, no server.
2. Include **exactly ONE** primary action element carrying `data-testid="main-action"`. It must:
   - cover **at least 25% of the viewport** (the verifier gates at ≥20% — an intentional
     measurement-jitter buffer; aim for 25%+), centered, high-contrast;
   - be **gently animated at idle** (a slow CSS pulse/bounce) to draw a toddler's eye;
   - carry a `data-action-count` attribute initialized to `"0"` in the markup, and its handler
     must **increment `data-action-count` on every activation** — this is the machine-checkable
     hook the verifier asserts on every click;
   - give big feedback on every press: color + motion (and optionally sound — recipe below).
3. **No reading required.** The goal must be obvious from looks alone: shape, size, color,
   motion, and the emoji sprite tell the whole story.
4. **No dead-ends.** Every state loops back to the same big action. The toy must survive rapid
   repeated mashing (the verifier mashes ~15 clicks) with zero errors, a monotonically
   increasing count, and no degraded or stuck state. A mash-forever toy — never a game-over
   screen with a tiny restart link.
5. **Visuals from code only:** large emoji (8–15rem font-size) as the primary sprite, CSS
   gradients / `@keyframes`, inline `<svg>`, or `<canvas>`. Multi-modal feedback (color +
   motion + sound) so a silent laptop still signals.
6. **Web Audio ONLY inside a user-gesture handler** — construct AND start it inside the click
   handler using the recipe below. Never at top level or on load (autoplay policy silently
   suspends it, with no console error to catch you).
7. Start from the skeleton below and keep its shape.

## NEVER

- **No external resources:** no CDN scripts, no Google Fonts, no FontAwesome, no Tailwind
  (each forbidden by name), no `@import`, no `<link href="http…">`, no `<img src="http…">`.
  No `<img>` at all unless its `src` is a `data:` URI.
- **No network calls:** `fetch(`, `XMLHttpRequest`, `WebSocket`, and dynamic `import()` are
  forbidden.
- **No blocking dialogs:** `alert(`, `confirm(`, `prompt(` — they freeze the page on first
  interaction (and hang the headless verifier).
- **No other interactive elements** besides the one main action: no menus, settings, restart
  buttons, difficulty pickers, links, or inputs.
- **No text-dependent affordances** ("Click here to start!") — the kid can't read them.
- Never emit a literal `</script>` inside a JS string or comment — it prematurely closes the
  script tag and breaks parsing (the verifier counts `</script>` occurrences).

## Sound recipe (the only approved audio pattern)

Created **and** started inside the main-action handler, wrapped in try/catch — sound is a
bonus, never a way to break the toy:

```js
// inside the main-action click handler ONLY — never at top level:
try {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.connect(gain);
  gain.connect(ctx.destination);
  osc.frequency.value = 440; // pitch the theme per toy
  gain.gain.setValueAtTime(0.3, ctx.currentTime);
  gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.3);
  osc.start();
  osc.stop(ctx.currentTime + 0.3);
} catch (e) {
  /* no sound is fine; a broken toy is not */
}
```

For a success chime, schedule 3–4 oscillator notes (a tiny arpeggio) the same way.

## Skeleton (start from this)

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title><!-- toy title --></title>
<style>
  /* full-viewport layout; center the ONE big action */
  /* gentle idle @keyframes (pulse/bounce) on the main action */
  /* big feedback @keyframes for each press; transform: scale(0.95) on :active */
</style>
</head>
<body>
  <button data-testid="main-action" data-action-count="0" aria-label="<!-- the one action -->">
    <!-- large emoji sprite, 8–15rem -->
  </button>
  <script>
    // state + the ONE handler:
    // 1. increment data-action-count EVERY activation
    // 2. update the visuals (color + motion)
    // 3. optional gesture-gated Web Audio (see the sound recipe)
  </script>
</body>
</html>
```

## Acceptance — the Pantsless Test (verbatim)

Can a 4-year-old…
- [ ] **Start it unaided?**  - [ ] **Understand the goal?** (no reading)
- [ ] **Not break it?**      - [ ] **Enjoy it?**

## Output format

Return **a single ```` ```html ```` fenced code block and nothing else** — no prose before or
after, no nested fences. The block contains the complete `index.html`. (Anything else breaks
programmatic extraction and triggers a repair retry.)

## Repair retries

On a repair retry, this contract is reused **verbatim**, with the failure evidence appended at
the end (the exact forbidden-pattern grep hit / console error / missing selector / failed-click
description / fence-count complaint). Address the evidence; return the full corrected HTML,
again as a single ```` ```html ```` fenced block and nothing else.

## Enforcement note (maintainers)

The machine-enforced forbidden-pattern list is **owned by `verify.py`'s `FORBIDDEN_PATTERNS`
constant** — the one source of truth (plan.md §3.2). This contract cites it and never restates
it; when the constant changes, this document does not drift.
