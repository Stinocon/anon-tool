# ~/.anon/catalogs/ — built-in lists, selectable ("anonimizza: …")

A catalog is **the same file format as `entities.txt`**, with directives. One parser, one mental
model, no new machinery.

## Format

```
@type   CITTÀ                          # type of the entries that follow -> [CITTÀ-1]
@match  case-sensitive                 # or `insensitive` (default)
@context (?:comune di|sede di)\s+      # only match right after this context
@stem   on                             # `Pincopallino` also matches `Pincopallino1`, `-DB01`
Nome Entità
ALTRO TIPO|Nome Entità|alias|alias
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

## Adding the complete ISTAT city list

`it-cities.txt` ships a starter set. Append the full ISTAT dataset (public, one name per line)
under the same header. Never generate names: a list that *looks* complete but is not would create
a false sense of coverage.
