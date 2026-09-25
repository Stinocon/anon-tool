"""corpus.py — a labelled, synthetic corpus for measuring the engine's RECALL.

`scripts/fp-sweep.py` measures the other direction (false positives over a corpus known to be
clean). This file is the oracle for the direction that matters more for an anonymizer: of the
values a document DECLARES sensitive, how many does `anon.detect()` actually cover?

Every document here is synthetic — no real name, client or address. Each one declares:

  * `entities`   the dictionary text (TIPO|valore lines) the document assumes, exactly as
                 `anon.load_entities` reads it. Recall is meaningless without it: the engine can
                 only find what the operator declared.
  * `catalogs`   catalog names loaded from `catalogs/` (opt-in, off by default).
  * `patterns`   pattern families (`identity`/`network`/`legal`), or None for all.
  * `must_find`  (value, type) pairs that MUST be covered. A value with no covered occurrence is a
                 LEAK, and the gate fails.
  * `must_not`   strings that must NEVER be covered. A covered one is a false positive, and the
                 gate fails.
  * `declared_fp` (value, type, why) accepted false positives, reported and not failed.
  * `known_miss`  (value, why) values the engine is known not to find — the hole is stated, not
                 hidden.

The corpus is deliberately adversarial: the `must_not` entries are ordinary prose that RESEMBLES
a pattern (`in via del tutto eccezionale, 3 volte`), and the `known_miss` entries are the declared
limits (a contextual reference, a name absent from the dictionary).
"""

from __future__ import annotations

# A value the engine must cover. Tuples keep the file readable and diffable.
Document = dict

DOCUMENTS: list[Document] = [
    {
        "name": "verbale-sopralluogo",
        "entities": (
            "@type AZIENDA\n"
            "Contoso S.r.l.\n"
            "@type PERSONA\n"
            "PERSONA|Mario Rossi|m.rossi@contoso.it\n"
            "Giulia Bianchi\n"
        ),
        "catalogs": ["it-cities", "vendors", "products"],
        "patterns": None,
        "text": (
            "Oggetto: verbale di sopralluogo presso la sede di Ancona.\n"
            "\n"
            "Cliente Contoso S.r.l., referente Mario Rossi (m.rossi@contoso.it, +39 335 1234567).\n"
            "Sede in Via Roma 12, 60100 Ancona (AN).\n"
            "Firewall FortiGate, hostname fw-ancona.contoso.local, indirizzo 10.42.7.19.\n"
            "Server Dell PowerEdge R740 su 10.42.7.20, seconda NIC su 2001:db8::42.\n"
            "Portale https://vpn.contoso.it accessibile dal browser.\n"
            "Switch Cisco Catalyst 2960, seriale non rilevante.\n"
            "Verbale redatto da Giulia Bianchi.\n"
        ),
        "must_find": [
            ("Contoso S.r.l.", "AZIENDA"),
            ("Mario Rossi", "PERSONA"),
            ("m.rossi@contoso.it", "EMAIL"),  # the EMAIL pattern outranks the dictionary on this span
            ("+39 335 1234567", "TEL"),
            ("Via Roma 12", "INDIRIZZO"),
            ("Ancona", "CITTÀ"),
            ("FortiGate", "PRODOTTO"),
            ("fw-ancona.contoso.local", "HOST"),
            ("10.42.7.19", "IP"),
            ("10.42.7.20", "IP"),
            ("2001:db8::42", "IP"),
            ("https://vpn.contoso.it", "URL"),
            ("Dell", "FORNITORE"),
            ("PowerEdge", "PRODOTTO"),
            ("Cisco", "FORNITORE"),
            ("Catalyst", "PRODOTTO"),
            ("Giulia Bianchi", "PERSONA"),
        ],
        # A city in an address, WITHOUT the catalog's marker (`sede di`), is not redacted by
        # design: `it-cities` is context-gated so a report is not shredded. Declared, not hidden.
        "must_not": ["60100", "R740", "2960"],
        "known_miss": [
            ("Ancona (AN)", "a city in an address without the `sede di` marker: the catalog is context-gated"),
        ],
    },
    {
        "name": "contratto-fornitura",
        "entities": (
            "@type AZIENDA\n"
            "Northwind S.p.A.\n"
        ),
        "catalogs": [],
        "patterns": None,
        "text": (
            "Contratto di fornitura tra Northwind S.p.A. e il committente sotto indicato.\n"
            "Partita IVA 12345678903, codice fiscale del referente RSSMRA85T10A562S.\n"
            "Pagamento sul conto IT60 X054 2811 1010 0000 0123 456 entro trenta giorni.\n"
            "Sede operativa: Corso Buenos Aires 12.\n"
            "Automezzo aziendale targato AB123CD.\n"
        ),
        "must_find": [
            ("Northwind S.p.A.", "AZIENDA"),
            ("12345678903", "PARTITAIVA"),
            ("RSSMRA85T10A562S", "CODICEFISCALE"),
            ("IT60 X054 2811 1010 0000 0123 456", "IBAN"),
            ("Corso Buenos Aires 12", "INDIRIZZO"),
            ("AB123CD", "TARGA"),
        ],
        "must_not": ["committente", "fornitura"],
    },
    {
        "name": "log-config",
        "entities": "",
        "catalogs": [],
        "patterns": None,
        "text": (
            "2026-01-01 10:00:00 ERROR auth failed for admin@northwind.it\n"
            "token=Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.abc123def456ghi789\n"
            'api_key = "AKIAIOSFODNN7EXAMPLE"\n'
            "gateway 10.0.0.1 unreachable, dns gw.northwind.it\n"
        ),
        "must_find": [
            ("admin@northwind.it", "EMAIL"),
            ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.abc123def456ghi789", "KEY"),
            ("AKIAIOSFODNN7EXAMPLE", "KEY"),
            ("10.0.0.1", "IP"),
            ("gw.northwind.it", "HOST"),
        ],
        "must_not": ["2026-01-01", "10:00:00"],
    },
    {
        "name": "prose-traps",
        "entities": "",
        # Only it-cities: with `products` on, Windows/Excel would be redacted — and that is a real
        # choice, not a defect, so the trap is tested with the catalog that is on.
        "catalogs": ["it-cities"],
        "patterns": None,
        "text": (
            "in via del tutto eccezionale, 3 volte l'anno\n"
            "il prato è verde, il corso d'acqua è largo 2 metri\n"
            "Windows Server 2019 aggiornato con Excel\n"
            "l'articolo costa 10 euro, il modello è fuori produzione\n"
        ),
        "must_find": [],
        "must_not": [
            "in via del tutto eccezionale, 3",
            "il prato è verde",
            "corso d'acqua",
            "Windows",
            "Excel",
        ],
        "declared_fp": [],
    },
    {
        "name": "declared-holes",
        "entities": "@type PERSONA\nMario Rossi\n",
        "catalogs": [],
        "patterns": None,
        "text": (
            "Il cliente di Brescia ha chiesto un preventivo.\n"
            "Il nuovo responsabile, Giovanni Neri, ha firmato il verbale.\n"
        ),
        "must_find": [],
        "must_not": [],
        "known_miss": [
            ("il cliente di Brescia", "contextual reference: no catalog, no marker"),
            ("Giovanni Neri", "proper name absent from the dictionary"),
        ],
    },
]
