---
name: telegram-api-limits
description: Use when sending Telegram messages, captions, callback buttons, or formatted text, or when designing rate-limited send loops. The Bot API has hard size and rate limits that silently truncate or reject; design around them.
---

# Telegram Bot API limits

The Telegram Bot API enforces hard limits. Exceed them and the API truncates, rejects,
or rate-limits you — sometimes silently. Design for them from the start instead of
discovering them as production incidents.

## The limits that bite

- **Message text: 4096 characters.** Longer text is rejected. Split long output into
  multiple messages (see HTML-stream-truncation for splitting *formatted* text safely).
- **Caption: 1024 characters.** A photo/document caption is far shorter than a message —
  long captions are truncated. Put the detail in a follow-up message, not the caption.
- **`callback_data`: 64 bytes.** Inline-button payloads are tiny. **Never** stuff state
  into `callback_data`. Store the state server-side keyed by a short id, and put only
  that short key in the button.
- **Entities: ~100 per message.** A message with hundreds of formatting entities
  (links, bold spans) is rejected. Keep formatting bounded.
- **Rate: ~30 messages/second globally, ~1 message/second per chat** (and bursts to a
  group are limited harder). Exceeding it returns `429` with a `retry_after`; respect
  it with backoff, don't hammer.

## Patterns

```ts
// callback_data: store state, send a key.
const token = await store.put({ action: "confirm", orderId });   // short id
button({ text: "Confirm", callback_data: token });               // fits in 64 bytes
// on callback: const { action, orderId } = await store.get(token);
```

For send loops, queue outbound messages and pace them under the per-chat and global
rates; on `429`, sleep for `retry_after` and retry.

## Why

These limits are not advisory — the API enforces them. The `callback_data` one in
particular is a classic trap: it works in testing with short ids and explodes when a
real payload (a UUID, a serialized object) overflows 64 bytes. Designing for
server-side state and paced sends from day one avoids a whole category of
"works-on-my-machine" failures.
