# ~/.anon/catalogs/ — built-in lists, selectable ("anonimizza: …")

A catalog is **the same file format as `entities.txt`**, with directives. One parser, one mental
model, no new machinery.

## Format

```
@type   CITTÀ                          # type of the entries that follow -> [CITTÀ-1]
@match  case-sensitive                 # or `insensitive` (default)
@context (?:comune di|sede di)\s+      # only match right after this context
@context off                           # stop requiring one for the entries that follow
@stem   on                             # `Pincopallino` also matches `Pincopallino1`, `-DB01`
Nome Entità
ALTRO TIPO|Nome Entità|alias|alias
```

**Ordering rule:** every directive applies to the entries that FOLLOW it, and a later directive
replaces the earlier one — the file is read top to bottom. A `@context` therefore is not a
property of the file but of the block it opens, and `@context off` is how you close it:

```
@type CITTÀ
@match case-sensitive
@context (?:sede di)\s+
Brescia          # only after "sede di"
@context off
Prato            # bare: any `Prato` is a city name here
```

An unknown `@directive` is a **hard error**, never ignored: a typo in `@stem`/`@context` would
silently change what gets redacted, and silent under-redaction is a leak.

## Use

```bash
python3 ~/.anon/anon.py --list-catalogs                  # what is installed
python3 ~/.anon/anon.py file.txt --catalogs it-cities    # one catalog
python3 ~/.anon/anon.py file.txt --catalogs it-cities,free-mail-domains
python3 ~/.anon/anon.py file.txt --patterns legal        # restrict the pattern groups
```

Pattern groups (`--patterns`): `identity` (emails, URLs, secrets), `network` (IP, phone,
hostname), `legal` (codice fiscale, partita IVA, IBAN, plate, address).

## Why every catalog is OFF by default

The goal is a document that is **not attributable** but still **useful**. Redacting more is not
automatically better: a report where every city has become `[CITTÀ-1]` has lost its substance and
protects almost nothing extra. High-signal items (identifiers validated by checksum, the operator's
own dictionary) are on; low-signal word lists are ticked on demand.

## Not shipped, on purpose: a phone-prefix catalog

Redacting bare calling codes (`+39`, `+44`) has no real use case here:

- `+39 030 1234567` is already redacted **as a whole** by the phone rule — and a prefix catalog
  would compete with it, claiming `+39` first and leaving `030 1234567` visible: **worse than not
  having it**;
- a country code alone does not identify anybody.

If you still need it, create `catalogs/intl-prefixes.txt` with `@type PREFISSO` and one code per
line — and expect that interaction.

## Growing `it-cities.txt`

`it-cities.txt` ships a starter set. Append more names under the same header — a public list, one
name per line — and never generate names: a list that *looks* complete but is not would create a
false sense of coverage.

Growth has a measured cost, so measure before committing to a big list. The scan slows with the
dictionary (`scripts/bench-check.py`, 2 MB corpus): 200 entries 1.60 MB/s, 1 000 → 0.93, 4 000 →
0.37, 7 900 → 0.20. At 1 000 entries the Pi guard's 12 MB cap already needs ~13 s of its 20 s
budget, and at 4 000 it exceeds it — so a list that big would make the guard refuse every large
document. `docs/OPEN-ISSUES.md` #26 tracks the fix; until then, a starter set is not a compromise,
it is what the budget allows.
