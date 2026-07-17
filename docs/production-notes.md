# Production notes — minimal gear, anti-burnout cadence

The Channel Loop's `record` and `edit` stages have no `cwp` command by design — they
are the human parts. This doc is the operator reference `cwp next` and the §3.1 Record
row point at ("record it — camera time — see docs/production-notes.md"). The whole point
is to keep the filming side as low-ceremony as the code side, so the channel survives past
episode three.

The governing rule is the channel's, from `voice.md`: **one small useful thing per
episode, calmly.** Production choices that fight that rule (more gear, more takes, more
polish) are the ones that kill hobby channels. Everything below optimizes for *finishing
episodes*, not for maximizing any single one.

## Minimal filming gear

You already own enough. Do not buy anything until a real bottleneck names the purchase.

- **Camera:** the phone you have. Shoot 1080p, not 4K — smaller files, faster edits, no
  visible quality loss at YouTube's bitrate for screen-and-desk content.
- **Audio:** the single highest-leverage upgrade, and the only one worth money. A cheap
  wired lav clipped to your shirt beats any camera's built-in mic. Viewers forgive rough
  video; they leave on rough audio. If you buy one thing, buy this.
- **Light:** a window during the day. Face the light, don't sit in front of it. One cheap
  clip-on LED covers night shoots. No softbox rig.
- **Mount:** any phone tripod or a stack of books. Frame it once, tape a floor mark, reuse
  the exact frame every episode — consistent framing reads as "produced" for free.
- **Screen capture:** for the toy and the code, capture the screen directly (OBS or the OS
  recorder) rather than pointing the camera at a monitor. The `project/index.html` runs
  from `file://` — record it full-screen in a browser.

That is the whole kit. Adding gear adds setup time, and setup time is what you skip on a
tired evening — which is how episodes stop shipping.

## The "one small thing" cadence

- **Film in one sitting, or don't film.** If an episode needs a multi-day shoot, it is too
  big — cut its scope until it fits one sitting. The seed bank (`cwp seed`, plan §5.5) is
  ranked easiest-first and effort-tagged `S`/`M` precisely so you can pick an `S` on a low
  night.
- **Talk to the code, don't script every word.** `cwp draft <id> script` gives you a
  voice-consistent skeleton; use it as beats to hit, not a teleprompter. Dry and unhurried
  (the `hak` register) survives light stumbles — leave them in.
- **The toy is the star.** The reliability core already proved the toy works before you
  film (`cwp build` won't commit a broken one). So filming is a reveal, not a debugging
  session on camera. If the toy misbehaves live, stop, re-run `cwp build`, and reshoot the
  reveal — never try to fix it on camera.
- **Batch the boring parts.** Record several intros/outros back to back while the framing
  and light are already set. Draft several scripts in one `cwp draft` session. Context-
  switching is the tax; pay it once.

## Kid clips are bonus cutaways, never ship-blocking

The Pantsless Build's Reveal ("he uses it on camera") is the heart of the channel — but a
4-year-old co-star is not a reliable one, and the tooling is built around that fact.

- **A kid clip is a short cutaway, not a dependency.** If he's into it, you get gold. If
  he's not, the episode still ships with you demoing the toy. Never hold an episode hostage
  to a toddler's mood.
- **Capture opportunistically, edit ruthlessly.** Record more than you need on a phone
  whenever he's willing; keep the 10 good seconds. `cwp capture` transcribes clips locally
  (his voice never leaves the machine), and `cwp brief` distills the wish — but the footage
  itself stays git-ignored and private.
- **Privacy is non-negotiable and mostly automatic.** Media and the whole `capture/` dir
  are git-ignored; the redact-names guard scrubs real names from every text artifact; use
  his nickname only. At upload, set YouTube's **"Made for Kids"** audience correctly — this
  is the operator reminder in `pantsless-test.md`, and it is the one checklist item you
  cannot automate away.

## The reshoot-friendly lifecycle

The lifecycle (`cwp status`) is permissive on purpose — a kid means reshoots, and the tool
never punishes you for one.

- `edited → recorded` is a **first-class move**, not an error. The take didn't survive the
  edit? Drop back and reshoot; you get a one-line note, no friction, and the history trail
  keeps both.
- **Park, don't force.** A stale idea or a shoot that isn't happening goes `on-hold` — it
  stays visible in `cwp list` so it isn't lost, and `cwp next` skips it so it isn't nagging
  you. Only `cut` what you're truly done with.
- **`cwp next` is your producer.** It points at the most-advanced in-flight episode and its
  single next action. Trust it instead of re-deciding what to work on every session — the
  decision fatigue is the burnout, not the work.

## Anti-burnout defaults

- **Ship small and often** beats ship-big-and-rarely. A published `S` episode is worth more
  than a perfect `M` still in your head.
- **Perfectionism is the enemy named in the plan** (§5.1: revisit the voice only after 3
  published episodes). Give the first few episodes permission to be rough. Consistency
  compounds; polish doesn't.
- **When tired, pick an `S`, film in one sitting, let the kid clip be optional.** That
  single sentence is the whole survival strategy for the channel.
