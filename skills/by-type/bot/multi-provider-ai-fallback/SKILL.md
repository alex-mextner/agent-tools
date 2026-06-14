---
name: multi-provider-ai-fallback
description: Use when calling LLM providers from a bot or service and you want resilience to one provider being down, rate-limited, or degraded. Build an ordered fallback chain over an OpenAI-compatible client, and never splice a fallback in mid-stream.
---

# Multi-provider AI fallback chain

A single LLM provider is a single point of failure: it has outages, rate limits, and
regional quirks. An ordered fallback chain — try provider A, on failure try B, then C —
keeps the bot answering. Most providers expose an OpenAI-compatible API, so the chain
can be one client class parameterized by base URL and key.

## Pattern

```ts
// Each provider is the SAME client shape with a different baseURL + apiKey.
// Keep credentials in config, not literals (see backend/config-loadconfig).
const providers = [
  { name: "primary",   baseURL: cfg.primaryBaseUrl,   apiKey: cfg.primaryKey },
  { name: "secondary", baseURL: cfg.secondaryBaseUrl, apiKey: cfg.secondaryKey },
];

async function complete(messages) {
  let lastErr;
  for (const p of providers) {
    try {
      return await openaiCompatible(p).chat.completions.create({ messages, ... });
    } catch (err) {
      lastErr = err;
      // Provider-specific quirk handling goes here: a 429 → try next; a malformed
      // tool-call from one provider → retry on the next; a transient 5xx → next.
      continue;
    }
  }
  throw lastErr;   // all providers failed — surface it, don't pretend success
}
```

## Hard rules

- **Never splice a fallback in mid-stream.** If provider A fails *after* you've already
  streamed tokens to the user, you cannot transparently continue on B — the partial
  output and the new output won't be coherent. Fall back only *before* the first token,
  or restart the whole turn, never stitch.
- **Handle provider quirks per provider**, not globally — one returns 429 where another
  returns 503; one mangles tool-call JSON. The retry policy belongs next to the provider
  it's for.
- **Fail loudly when all providers fail.** Don't return an empty/fake success; surface a
  user-friendly error (see `bot-error-resilience`).

## Why

Providers go down independently, so a chain's availability is much higher than any one
member's. The OpenAI-compatible shape means the chain is cheap — same call, different
endpoint. The mid-stream rule is the one people learn the hard way: a fallback that
kicks in after streaming started produces garbled output that's worse than a clean error.

## Note

This skill is about *resilience structure*. The specific providers, endpoints, and keys
are yours to supply via config — don't hardcode any provider's internal URL or key into
shared code.
