---
name: telegram-tone-of-voice
description: Use when writing user-facing copy for a Telegram (or chat) bot — replies, prompts, notifications, push previews. Frame messages as a benefit to the user, front-load the meaning into the first words, cut filler, and don't nag.
---

# Bot tone of voice

A chat bot's copy is its entire personality and most of its UX. A few consistent rules
make it feel helpful rather than robotic or creepy.

## Principles

- **Frame as a benefit, not surveillance.** "Here's your spending summary" reads as a
  service; "I tracked everything you spent" reads as monitoring. Same data, opposite
  feeling. Describe what the user *gets*, not what the bot *watched*.
- **Front-load the first two words.** Push notifications and chat lists show only the
  start of a message. Put the actual information first — "Payment received: $40" — not
  "Hi! Just letting you know that your payment of…". The user decides whether to open
  it from the preview.
- **Cut filler.** "Just", "I wanted to let you know", "please note that", "as you may
  know" — delete them. Chat copy should be tight; every extra word dilutes the signal.
- **Don't nag about automatic actions.** If the bot does something on a schedule, it
  doesn't need to announce every run, apologize, or ask permission each time. Repeated
  "I'm about to…" / "I just did…" messages train users to mute the bot.
- **Match the relationship register consistently.** Pick a form of address and keep it
  uniform across every string — switching between formal and casual mid-flow feels
  broken.

## Why

Tone is the difference between a bot users keep and one they mute. Benefit-framing
avoids the "this thing is spying on me" reaction; front-loading respects that most
messages are read as a one-line preview; cutting filler and nagging keeps the bot from
becoming noise. These are copy decisions, so they live in your i18n strings (see
`i18n-all-languages`) — write them once, well.

## Note

The *specifics* of register and personality are product/brand decisions for your bot.
This skill is the portable shape — benefit-framing, front-loading, no filler, no
nagging — not a particular voice.
