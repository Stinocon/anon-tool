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

## `vendors.txt` — the IT vendor list

`vendors.txt` is the one catalog meant for every infrastructure report: 164 entries (`@type
FORNITORE`), one per hardware and software **company** — `Fortinet` is in, `FortiGate`/`NSa`
are not, because product families are a larger and faster-drifting list and usually arrive as
hostnames, which the HOST rule already covers.

It is split into two blocks, and that split is the whole design:

- **block 1, `@match case-sensitive`** — the names whose lowercase is **also an ordinary word or
  unit** (e.g. `Dell`/`dell'aria`, `Canon`/`canon`, `Axis`/`axis`, `Acer` the maple genus,
  `Apple`/`apple`, `Juniper`, `Brother`, `Snowflake`, `Slack`, `Elastic`, `Confluence`, `Tenable`,
  `Xerox`, `Adobe` the building material, `Google`/`to google`, `Oracle`, `Intel`/"threat intel",
  `Cisco` and `Barracuda` the fish, `Siemens` the SI unit, `Okta` the cloud-cover unit, `Gigabyte`
  the unit, `HP` horsepower, `SAP` tree sap, `Arista` the Italian roast, `Moxa`, `Sage`, `Zebra`,
  `Ruckus`, `Red Hat`, `New Relic`, `Open Text`). Matching those case-insensitively would redact
  `dell'aria`, `un gigabyte di memoria` and a variable named `intel`;
- **block 2, `@match insensitive`** — the names whose lowercase is not a word, so a lowercase
  spelling in a filename, a hostname or a spreadsheet matches too (`aruba cloud`, `kingston RAM`,
  `un server ibm`). That is why the short all-caps brands are here (`IBM`, `AMD`, `HPE`, `APC`,
  `QNAP`, `SUSE`). The map keeps the exact spelling it found, so `SonicWall` and `sonicwall` in the
  same document are two placeholders for one company, and `deanon` restores both.

The line is **measurable, and enforced**: `grep -ix "<name>" /usr/share/dict/words` — a name that is
a word goes in block 1. The OS list alone is not sufficient (it misses `gigabyte`, `google`,
`veritas`, `siemens`, `okta`, `hp`, `intel`, `xerox`, `arista`), so `VendorsCatalogTest` keeps the
recorded word per block-1 entry and sweeps block 2 against the OS dictionary as well: a name added
to the wrong block fails the suite instead of waiting for a review to notice.

Three things it deliberately does **not** do, all stated in the file's own header: product/model
names, the lowercase spelling of a block-1 name, and the sentence-initial Italian elision
("Dell'azienda risulta…" **is** redacted — an apostrophe is not a word character and the format has
no per-entry exclusion). Vendors that are also ordinary words are absent on purpose: `LG`, `F5`,
`MSI`, `AVG`, `Sharp`, `Crucial`, `Canonical`, `Meta`, `Zoom`, and `Docker`/`Kubernetes`/`Ubuntu`
(the technology, not the firm).

## `products.txt` — the product and platform list

`products.txt` is the other half of the same job: 164 entries in `vendors.txt` (the companies) and
130 entries here (products, platforms and product lines). A name lives in ONE of the two (`Fortinet` is a
vendor, `FortiGate` is a product), and `ProductsCatalogTest` fails if a name appears in both,
because the same surface would otherwise get two types depending on which list was ticked first.

It covers the families — `FortiGate`, `PowerEdge`, `Catalyst`, `vSphere`, `Windows`, `Docker`,
`Kubernetes` — and deliberately NOT the model or release numbers (`R740`, `DL380`, `NSa 2700`,
`Windows 11`): those change every quarter and in a real document they arrive as hostnames, which
the HOST rule already redacts.

The block rule is the same one, enforced by the same gate, and here it has more work to do: a
product name is very often an ordinary English word, so block 1 is long (`Word`, `Excel`, `Access`,
`Teams`, `Windows`, `Exchange`, `Outlook`, `Catalyst`, `Nexus`, `Umbrella`, `Firepower`, `Firebox`,
`Falcon`, `Defender`, `Horizon`, `Android`, `Chrome`, `Safari`, `Thunderbird`, `Azure`, `Docker`,
`Helm`, `Rancher`, `Tomcat`, `Apache`, `Zoom`). The gate caught one of these during the writing:
`Thunderbird` had been left in block 2 and the dictionary sweep refused it — the bird is a word.

Ticking both lists together adds 294 entries; `scripts/bench-check.py --entities 294 --mb 5` is the
measurement that says whether that is affordable (it is: see the tool's own output, ~1.7 MB/s
against the guard's 12 MB / 20 s).

## Not shipped, on purpose: a phone-prefix catalog

Redacting bare calling codes (`+39`, `+44`) has no real use case here:

- `+39 030 1234567` is already redacted **as a whole** by the phone rule — and a prefix catalog
  would compete with it, claiming `+39` first and leaving `030 1234567` visible: **worse than not
  having it**;
- a country code alone does not identify anybody.

If you still need it, create `catalogs/intl-prefixes.txt` with `@type PREFISSO` and one code per
line — and expect that interaction.

## Growing a list

`it-cities.txt` (50 entries) and `vendors.txt` (164 entries) ship starter sets. Append names under
the same header, in the block that fits the surface — a public list, one name per line — and never generate
names: a list that *looks* complete but is not would create a false sense of coverage. The count
stated in `vendors.txt` and here is bound to the file by `scripts/check-doc-numbers.py`, so adding
an entry means updating the number in the same change.

Growth has a measured cost, so measure before committing to a big list. The scan slows with the
dictionary (`scripts/bench-check.py`, 2 MB corpus): 200 entries 1.60 MB/s, 1 000 → 0.93, 4 000 →
0.37, 7 900 → 0.20. At 1 000 entries the Pi guard's 12 MB cap already needs ~13 s of its 20 s
budget, and at 4 000 it exceeds it — so a list that big would make the guard refuse every large
document. `docs/OPEN-ISSUES.md` #26 tracks the fix; until then, a starter set is not a compromise,
it is what the budget allows.
