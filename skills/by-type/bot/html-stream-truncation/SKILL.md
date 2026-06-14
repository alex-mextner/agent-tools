---
name: html-stream-truncation
description: Use when streaming or chunking HTML-formatted text to a Telegram chat (e.g. a token-by-token AI reply with bold/code/links). Truncate at safe boundaries and close any unclosed tags, or the API rejects the message for malformed HTML.
---

# Safe truncation of HTML-formatted streams

When you stream a formatted reply (an AI response with `<b>`, `<code>`, `<a>` tags)
and chunk it to stay under the 4096-char message limit, a naive cut can land in the
middle of a tag or between an opening and closing tag. Telegram parses the chunk as
HTML and rejects malformed markup — the message fails to send, often silently mid-stream.

## Rules

- **Chunk below the limit with headroom** — split at, say, 4000 chars, not exactly 4096,
  to leave room for closing tags you have to append.
- **Prefer a newline boundary.** When you have to cut, back up to the last newline so
  you don't split a word or, worse, a tag.
- **Close unclosed tags at the cut, reopen them in the next chunk.** Track the open-tag
  stack as you emit; at a chunk boundary, append the closing tags in reverse order, and
  prepend the same opening tags to the start of the next chunk.

```ts
// Sketch: maintain a stack of open tags; at a boundary, close then reopen.
const open = ["b", "code"];                      // currently open
const tail = open.slice().reverse()              // close in reverse
  .map((t) => `</${t}>`).join("");
const head = open.map((t) => `<${t}>`).join("");  // reopen on next chunk
sendChunk(buffer + tail);
buffer = head;
```

## Why

A streamed AI reply is the common case here: the model emits a partial `<code>` block,
your buffer crosses 4096, and a blind cut produces `…some <cod` — which Telegram rejects
as malformed HTML, dropping the rest of the stream. Closing and reopening the tag stack
at each boundary keeps every chunk individually valid, so the stream survives chunking.
Pairs with `telegram-api-limits` (the size limits that force the chunking).
