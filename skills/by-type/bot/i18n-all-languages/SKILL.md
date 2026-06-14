---
name: i18n-all-languages
description: Use whenever you add or change a user-facing string in a localized bot or app. Route every user-facing string through the translation function, and update every language file together so no locale falls behind.
---

# Every user-facing string through i18n, all locales together

A hardcoded user-facing string is a string that exists in exactly one language and
can never be translated without hunting it down. In a localized app, *every* string the
user sees goes through the translation layer.

## Rules

- **No hardcoded user-facing text.** Route it through your `t(lang, key)` (or
  equivalent). The code references a key; the text lives in the locale files.

  ```ts
  // BAD
  await ctx.reply("Payment received");

  // GOOD
  await ctx.reply(t(lang, "payment.received"));
  ```

- **Add the key to ALL locale files in the same change.** When you add a key to one
  language, add it to every supported language at once. A key present in `en` but
  missing in the others falls back to the key name (or English) for those users — a
  silent half-translation that ships unnoticed because the developer only tested in one
  language.
- **Keep the keys in sync.** A lint/test that asserts every locale file has the same key
  set catches the "forgot to add it to `de`" mistake before it ships.

## Why

i18n is all-or-nothing per string: a string that skips the translation layer is
permanently monolingual, and a key added to only one locale silently degrades every
other locale. Doing both — always through `t()`, always all files together — is the only
way the translations stay complete. Pluralization is a related trap; see
`russian-pluralization` for languages with multiple plural forms.
