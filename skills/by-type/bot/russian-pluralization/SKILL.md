---
name: russian-pluralization
description: Use when displaying a count next to a noun in a language with multiple plural forms (Russian, Polish, Czech, and other Slavic languages). Pick the grammatical form from the number with a plural helper; never hardcode one form next to a variable count.
---

# Plural forms for Slavic languages

English has two number forms (1 item / 2 items). Russian and other Slavic languages
have **three**, selected by the number's last digits: one form for 1/21/31…, another
for 2-4/22-24…, a third for 0/5-20/25-30… Hardcoding a single noun form next to a
variable count is grammatically wrong for most values.

## Rule

Select the form from the number with a plural helper, and feed it the three forms:

```ts
// Returns the correct one of (one, few, many) for n.
function ruPlural(n: number, one: string, few: string, many: string): string {
  const mod10 = n % 10;
  const mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

// Usage — never hardcode one form next to a count.
`${n} ${ruPlural(n, "файл", "файла", "файлов")}`;   // 1 файл, 2 файла, 5 файлов
```

For broader locale coverage, the same idea is built into ICU MessageFormat / `Intl`
plural rules — use those if your i18n stack already provides them. The helper above is
the minimal standalone version for when it doesn't.

## Why

`"5 файл"` or `"2 файлов"` reads as broken to a native speaker — it's the localization
equivalent of "5 item". The selection is purely a function of the number, so it belongs
in a reusable helper called at every count-plus-noun site, not copy-pasted with a guess.
Pairs with `i18n-all-languages`: the three forms live in your locale files, the helper
picks among them.
