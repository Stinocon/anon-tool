#!/usr/bin/env python3
"""Deterministic tests for the anon engine.

The gate for the whole system is here: anon -> deanon must be byte-for-byte lossless, a
second anonymization must be a no-op, and the heuristics must not redact filenames or
public placeholder values (`example.com`, RFC 5737 IPs).

  python3 ~/.anon/tests/test_anon.py
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import unittest
import zipfile
from collections import Counter
from pathlib import Path
from unittest import mock
import xml.etree.ElementTree as ElementTree

sys.dont_write_bytecode = True  # keep ~/.anon free of __pycache__ from the importlib loads below

HERE = Path(__file__).resolve().parent
HOME = HERE.parent
ANON_PY = HOME / "anon.py"
DEANON_PY = HOME / "deanon.py"

# Load the engine as a module (no package layout to import from).
_spec = importlib.util.spec_from_file_location("anon_engine", ANON_PY)
assert _spec and _spec.loader
anon = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = anon  # dataclasses need the module registered before exec
_spec.loader.exec_module(anon)

_espec = importlib.util.spec_from_file_location("deanon_engine", DEANON_PY)
assert _espec and _espec.loader
deanon = importlib.util.module_from_spec(_espec)
sys.modules[_espec.name] = deanon
_espec.loader.exec_module(deanon)


# The synthetic dictionary used by the round-trip tests. Loaded through the REAL parser so the
# tests exercise the shipped code path (directives, legal forms, normalization) rather than a
# parallel implementation that could drift.
ENTITIES = [
    ("CLIENTE", "Acme"),
    ("AZIENDA", "Acme Italia S.r.l."),
    ("SEDE", "Sede di Ancona"),
    ("PERSONA", "Mario Rossi"),
]


class RoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp(prefix="anon-entities-")
        self.tmp_entities = Path(self._tmpdir) / "entities.txt"
        self.tmp_entities.write_text(
            "".join(f"{ptype}|{value}\n" for ptype, value in ENTITIES), encoding="utf-8"
        )
        self.entities = anon.load_entities(self.tmp_entities)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_fast_entity_scan_reproduces_the_reference_scan(self) -> None:
        """`entity_hits` is an optimization, not a rule: it must yield what `Entity.spans` yields.

        The dictionary covers every axis the fast scan branches on: a stem, a case-sensitive
        entry, a `@context` entry, an accented name (NFC and NFD in the text), legal forms and
        multi-word names.
        """
        path = Path(self._tmpdir) / "rich.txt"
        path.write_text(
            "\n".join([
                "@type CLIENTE",
                "Ferraris Group",
                "@stem on",
                "@type SERVIZIO",
                "Ferretti",
                "@stem off",
                "@match case-sensitive",
                "@type CITTÀ",
                "Prato",
                "@match insensitive",
                "@type SEDE",
                r"@context (?:sede di)\s+",
                "Ancona",
                "Roma Nord",
                "Roma",
                "@type PERSONA",
                "Ferraris Gianni",
                "König Söhne",
                "@type SERVIZIO",
                "Ferrettini Group",
            ]) + "\n",
            encoding="utf-8",
        )
        entities = anon.load_entities(path)
        text = (
            "Ferraris Group e ferraris group; Ferretti-DB01 e Ferretti; Prato ma non prato; "
            "sede di Ancona e Ancona; sede di Roma Nord; Ferraris Gianni; "
            "König Söhne e Ko\u0308nig So\u0308hne; Ferrettini e Ferrettini Group.\n"
        )
        reference = sorted(
            (start, end, entity.type) for entity in entities for start, end, _ in entity.spans(text)
        )
        fast = sorted(
            (start, end, entity.type) for entity, start, end in anon.entity_hits(text, entities)
        )
        self.assertTrue(reference, "the fixture must actually match")
        self.assertEqual(fast, reference)

    def test_an_address_at_the_end_of_a_sentence_is_still_an_address(self) -> None:
        r"""A trailing period is sentence punctuation, not part of the token.

        The IPv4 pattern ended with `(?![\w.])`, so `10.20.30.40.` — an address followed by the full
        stop that closes the sentence, which is how it is written in every report — matched nothing
        and stayed in the document. The HOST rule next to it already carried the guard
        (`(?!\.\w)`, commented "allow a trailing sentence period"); the IP rule never got it. That
        guard, not a narrower `(?!\.\d)`, is what belongs here: the point is to reject a longer
        dotted run whose continuation is a LABEL, and `.beta`/`.rc1`/`.x86_64` are one as `.5` is —
        `(?!\.\d)` still redacted `10.20.30.40.beta`, and failed this test on exactly that case.
        """
        entities = self.entities
        for text in ("il gateway e' 10.20.30.40.", "vedi 192.168.1.1.", "IP: 10.20.30.40."):
            types = {ptype for _, _, ptype in anon.detect(text, entities)}
            self.assertIn("IP", types, f"the address at the end of {text!r} was not found")
            redacted, entries, _ = anon.anonymize(text, entities)
            self.assertNotIn("10.20.30.40", redacted)
            self.assertNotIn("192.168.1.1", redacted)
            self.assertTrue(entries)
        # a four-octet prefix of a longer dotted run is still NOT an address, whether the
        # continuation is numeric or alphabetic
        for text in ("versione 10.20.30.40.5", "host 1.2.3.4.5.6", "versione 10.20.30.40.beta",
                     "release 1.2.3.4.rc1", "build 10.0.0.1.x86_64"):
            types = {ptype for _, _, ptype in anon.detect(text, entities)}
            self.assertNotIn("IP", types, f"{text!r} is not an address")
            redacted, _, _ = anon.anonymize(text, entities)
            self.assertEqual(redacted, text, "a version-like run must not be half redacted")

    def test_the_fast_scan_survives_entries_sharing_a_first_token(self) -> None:
        """The sub-index must not narrow a bucket into a MISS.

        `entity_hits` resolves a position by looking at the SECOND token of the text, so a bucket
        whose members share one first name is the shape that breaks it if the narrowing is wrong —
        and a miss here is an un-redacted name in an output document, the worst defect this tool
        can have. The reference scan is the standard: identical, entry by entry.
        """
        path = Path(self._tmpdir) / "shared.txt"
        shared = [f"Mario {surname}" for surname in ("Rossi", "Rossini", "Ros", "Rosa", "Rossi S.p.A")]
        shared += [f"Rossi S.p.A {index}" for index in range(12)]
        path.write_text("\n".join(["@type PERSONA", *shared, "@type SERVIZIO", "Mario", "Rossi"]),
                        encoding="utf-8")
        entities = anon.load_entities(path)
        text = (
            "Mario Rossi, Mario Rossini, Mario Ros e Mario Rosa; Mario Rossi S.p.A e Mario Rossi "
            "Spa; Rossi S.p.A 3 e Rossi S.p.A 11; Mario da solo; mario rossi minuscolo; "
            "Mario Rossi\u0301 e Mario Rossi. Rossi, Rossi S.p.A e Rossi S.p.A 7.\n"
        )
        reference = sorted(
            (start, end, entity.type) for entity in entities for start, end, _ in entity.spans(text)
        )
        fast = sorted(
            (start, end, entity.type) for entity, start, end in anon.entity_hits(text, entities)
        )
        self.assertTrue(reference, "the fixture must actually match")
        self.assertEqual(fast, reference)

    def test_the_fast_scan_matches_the_reference_on_the_unicode_folds(self) -> None:
        """`casefold()` and `re.IGNORECASE` are NOT the same fold, and the sub-index once used the
        folded token as the PATTERN: a name with ß, a ligature or İ stopped matching, i.e. it was
        left un-redacted. The patterns come from the raw token and only the lookup is folded; this
        test fails if that distinction is lost again."""
        path = Path(self._tmpdir) / "folds.txt"
        path.write_text(
            "@type AZIENDA\nHans Straße\nAcme Oﬃce\nMario İpek\n"
            "@context via\\s+\nAZIENDA|Straße\n@context off\nStraße Söhne\n",
            encoding="utf-8",
        )
        entities = anon.load_entities(path)
        text = "Hans Straße, Acme Oﬃce, Mario İpek, via Straße, Straße Söhne e Straße.\n"
        reference = sorted(
            (start, end, entity.type) for entity in entities for start, end, _ in entity.spans(text)
        )
        fast = sorted(
            (start, end, entity.type) for entity, start, end in anon.entity_hits(text, entities)
        )
        self.assertTrue(reference, "the fixture must actually match")
        self.assertEqual(fast, reference)

    @staticmethod
    def _resolve(hits: list, size: int) -> list:
        """An oracle for `detect`'s overlap resolution: longest first at the same start, then claim.

        It mirrors the resolver in `detect` (same key, same greedy claim) so the test can compare the
        FINAL spans, which is what a document actually gets, not just the candidate lists. The
        placeholder pre-claim is deliberately omitted: the corpus holds no placeholder.
        """
        claimed = bytearray(size)
        resolved = []
        # The same key `detect` sorts by, priority included: for entity hits the priority is constant,
        # but omitting it would make this oracle disagree with `detect` the day two types collide.
        for start, end, ptype in sorted(hits, key=lambda hit: (hit[0], -(hit[1] - hit[0]), hit[2])):
            if any(claimed[start:end]):
                continue
            claimed[start:end] = b"\x01" * (end - start)
            resolved.append((start, end, ptype))
        return sorted(resolved)

    def test_the_word_run_locator_matches_the_reference_on_every_difficult_shape(self) -> None:
        """The fast scan must never MISS, and its extras must be dissolved by the resolution.

        The locator used to be ONE alternation over every first token of the dictionary; it is now a
        walk over the word runs of the text with dict probes, sound only because every literal entry
        carries a `(?<!\\w)` prefix — a match can only START where a word run starts. The reference
        is `Entity.spans`, one full-text pass per entry per Unicode form. It is not a strict equality:
        `entity_hits` may yield an entry TWICE for one span (`Ferretti-Ferrettini` with `@stem on`
        gives the stem at 39 and again at 48, because the earlier match swallowed the separator and
        `finditer` does not rescan inside its own match), and that was already true of the alternation
        — measured on the previous implementation, which missed one hit here and added the same two.
        So the contract is: every reference hit is present, no extra reaches the output, and the
        resolved spans are identical. A miss is a name left un-redacted; an extra only costs time.
        """
        path = Path(self._tmpdir) / "locator.txt"
        path.write_text(
            "@type AZIENDA\n"
            "Acme\n"
            "Roma Nord\n"
            "D-Link\n"
            "Link\n"
            "A-B-C\n"
            "B-C\n"
            "Acme O\ufb03ce\n"
            "Stra\u00dfe S\u00f6hne\n"
            "Mario \u0130pek\n"
            "K\u00f6nig S\u00f6hne\n"
            "@stem on\n"
            "Ferretti\n"
            "@stem off\n"
            "Ferrettini\n"
            "@match sensitive\n"
            "Prato\n"
            "@match insensitive\n"
            "@context via\\s+\n"
            "Roma\n"
            "@context off\n"
            "A\n"
            "@stem on\n"
            "B\n"
            "@stem off\n",
            encoding="utf-8",
        )
        entities = anon.load_entities(path)
        corpus = [
            "Acme, ACME, acme; antAcme e AcmeAnt.",
            "Roma Nord, Roma, Nord, Roma Nord Est, aRoma Nord.",
            "D-Link, d-link, D-LINK, XD-Link, D-LinkX.",
            "a-b-c, A-B-C, B-C, b-c; Link, link, D-Link e Link.",
            "Acme O\ufb03ce e Acme Office e Acme O\ufb03ceX.",
            "Stra\u00dfe S\u00f6hne, Stra\u00dfe, Strassen, Stra\u00dfe So\u0308hne.",
            # An NFD text (what macOS writes): the combining mark is not a word character, so the
            # NFD variant of a first token that carries one is a NON-word source and belongs to the
            # alternation. A word-run walk alone cannot see it, which is why both locators run.
            unicodedata.normalize("NFD", "K\u00f6nig S\u00f6hne und K\u00f6nig, K\u00f6nigX."),
            "Mario \u0130pek, \u0130pek, ipek, Mario ipek, \u0130pekX.",
            "Ferretti1, Ferretti-DB01, Ferretti_srv, Ferrettini, Ferrettini Group.",
            "Prato e prato e PRATO; PratoX.",
            "via Roma, Via  Roma, via Roma Nord, davanti a via  Roma.",
            "A e a; AAA; aA; B1, B-DB01, B_srv, Bb e b.",
            "Conte, c, \u0301Acme, Acme\u0301, (Acme), Dell'Acme.",
            "Acme\nRoma Nord\nD-Link\n",
            "",
            "acme roma nord d-link ferretti1",
            "11A)A-O\ufb03ceFerrettiNordRomapratoAc\u0130pek1\nFerretti-Ferrettini",
        ]
        # A deterministic fuzz pass over the fixture's own alphabet: it reaches the combinations a
        # hand-written corpus misses, and the seed makes a failure reproducible.
        random.seed(20260923)
        alphabet = [
            "Acme", "Roma", "Nord", "D-Link", "-", " ", ",", ".", "'", "\u0130pek", "Stra\u00dfe",
            "O\ufb03ce", "Ferretti", "Ferrettini", "Prato", "prato", "via", "A", "a", "1", "\u0301",
            "Rossi", "Ac", "\n", "(", ")", "B", "B1", "Link", "B-C", "a-b-c",
        ]
        corpus += ["".join(random.choice(alphabet) for _ in range(random.randint(1, 24)))
                   for _ in range(400)]

        # Two regimes, one corpus: below `SCAN_WORD_SOURCES_MIN` sources the locator is the
        # alternation (the small-dictionary path, which is what the Pi guard uses and what every
        # earlier version used), above it the walk over the word runs. The slow path is the one that
        # is easy to get wrong, so the fixture above is extended past the threshold with fillers
        # rather than duplicated.
        filler = "\n".join(f"AZIENDA|Qq{index:02d}" for index in range(anon.SCAN_WORD_SOURCES_MIN + 4))
        path.write_text(path.read_text(encoding="utf-8") + filler + "\n", encoding="utf-8")
        many = anon.load_entities(path)
        self.assertGreater(
            len({
                e.first_token
                for e in many
                if e.first_token and not e.context and re.fullmatch(r"\w+", e.first_token)
            }),
            anon.SCAN_WORD_SOURCES_MIN,
            "the second fixture must cross the threshold on WORD sources, or the word-run locator is "
            "never exercised",
        )

        matched = 0
        for text in corpus:
            for label, fixtures in (("small dictionary", entities), ("large dictionary", many)):
                reference = sorted(
                    (start, end, entity.type)
                    for entity in fixtures
                    for start, end, _ in entity.spans(text)
                )
                fast = sorted(
                    (start, end, entity.type)
                    for entity, start, end in anon.entity_hits(text, fixtures)
                )
                matched += len(reference)
                for hit, count in Counter(reference).items():
                    self.assertGreaterEqual(
                        Counter(fast).get(hit, 0), count,
                        f"the fast scan MISSED {hit} in {text!r} ({label}): a name left un-redacted",
                    )
                for hit in Counter(fast) - Counter(reference):
                    # An extra is allowed when it is the SAME span as a reference hit (the two
                    # locators can both reach one entry: a word source that is a prefix of a
                    # non-word one, `B` behind `B-C`, is probed by the word walk and by the
                    # alternation's prefix loop) or when a longer reference hit CONTAINS it, which
                    # the longest-first claim in `detect` resolves. Anything else would be a span
                    # the engine invented, and the resolved comparison below would catch it anyway.
                    self.assertTrue(
                        (hit[0], hit[1], hit[2]) in set(reference)
                        or any(
                            start <= hit[0] and hit[1] <= end
                            for start, end, _ in reference
                        ),
                        f"the fast scan invented {hit} in {text!r} ({label}), outside any "
                        "reference hit",
                    )
                self.assertEqual(
                    self._resolve(fast, len(text)), self._resolve(reference, len(text)),
                    f"the resolved spans differ on {text!r} ({label})",
                )
        # And the two regimes must agree with EACH OTHER on the final spans: the threshold is a cost
        # decision, so picking the other locator may not change what a document gets.
        for text in corpus:
            small = sorted(
                (start, end, entity.type)
                for entity, start, end in anon.entity_hits(text, entities)
            )
            large = sorted(
                (start, end, entity.type)
                for entity, start, end in anon.entity_hits(text, many)
            )
            self.assertEqual(
                self._resolve(small, len(text)), self._resolve(large, len(text)),
                f"the two locators resolve differently on {text!r}",
            )
        self.assertGreater(matched, 200, "the fixture must actually match")

    def test_the_locator_fold_covers_everything_ignorecase_matches(self) -> None:
        """The scan keys a candidate on a fold, the pattern matches it with `re.IGNORECASE`, and the
        two must not disagree.

        They did: `str.casefold()` separates {I, İ, ı} while `re.IGNORECASE` treats them as one, so a
        dictionary entry `İpek` was found in a document spelling it `İpek` but MISSED in one spelling
        it `ipek` — a name left un-redacted, and the equivalence test above caught it. The property
        is one-directional (a key COARSER than the pattern is safe: it probes more, never misses):
        everything `re.IGNORECASE` matches must share one key. Exhaustive over every character of
        Unicode that has a case or a fold, one pass each — `findall` on a blob of the whole universe
        enumerates the equivalent characters by the engine itself, so this does not need a double
        loop over pairs.
        """
        universe = [
            chr(cp)
            for cp in range(0x110000)
            if unicodedata.category(chr(cp)) in ("Lu", "Ll", "Lt")
            or chr(cp).casefold() != chr(cp)
            or chr(cp).lower() != chr(cp)
        ]
        self.assertGreater(len(universe), 3000, "the universe must be the real one")
        blob = "".join(universe)
        for char in universe:
            for equivalent in re.findall(re.escape(char), blob, re.IGNORECASE):
                self.assertEqual(
                    anon._locator_key(equivalent), anon._locator_key(char),
                    f"re.IGNORECASE matches {equivalent!r} to {char!r} but the key separates them",
                )

    def test_roundtrip_is_lossless(self) -> None:
        original = (
            "Spett.le Acme Italia S.r.l. (rif. Acme),\n"
            "sede operativa: Sede di Ancona.\n"
            "Referente: Mario Rossi, mario.rossi@acme.it, tel. +39 02 1234567.\n"
            "Server: 10.20.30.40, vpn gateway 2a01:4f8:1c17:1234::1, host srvcrm.acme.local.\n"
            "Portale: https://intranet.acme.it/login?token=abc123 (fino al 31/12).\n"
            "Chiave: sk-abcdefghijklmnopqrstuvwxyz012345, header Bearer "
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop\n"
            "Password: S3cretValue! (non condividerla)\n"
            "Numero cliente 3331234567.\n"
        )
        redacted, entries, counts = anon.anonymize(original, self.entities)
        self.assertNotEqual(redacted, original)
        self.assertNotIn("mario.rossi@acme.it", redacted)
        self.assertNotIn("10.20.30.40", redacted)
        self.assertNotIn("Mario Rossi", redacted)
        self.assertTrue(entries)
        self.assertGreater(counts.get("EMAIL", 0), 0)

        restored, count = deanon.deanonize(redacted, entries)
        self.assertEqual(restored, original)
        self.assertGreater(count, 0)

    def test_second_pass_is_a_noop(self) -> None:
        original = "Scrivi a info@cliente.it e vai su host.cliente.it (10.0.0.5).\n"
        redacted, entries, _ = anon.anonymize(original, self.entities)
        self.assertTrue(entries)
        redacted2, entries2, _ = anon.anonymize(redacted, self.entities)
        self.assertEqual(redacted2, redacted)
        self.assertEqual(entries2, {})

    def test_filenames_are_not_redacted(self) -> None:
        text = "Vedi anon.py, deanon.py, main.js, README.md, index.ts e package.json.\n"
        redacted, entries, _ = anon.anonymize(text, self.entities)
        self.assertEqual(entries, {})
        self.assertEqual(redacted, text)

    def test_public_placeholders_are_not_redacted(self) -> None:
        text = (
            "Contatta user@example.com o visita https://example.com/docs su 192.0.2.10 "
            "(127.0.0.1:8080, localhost).\n"
        )
        redacted, entries, _ = anon.anonymize(text, self.entities)
        self.assertEqual(entries, {})
        self.assertEqual(redacted, text)

    def test_dictionary_longest_match_wins(self) -> None:
        text = "Acme Italia S.r.l. ha rilevato Acme.\n"
        redacted, entries, counts = anon.anonymize(text, self.entities, tag="aaaa")
        self.assertIn("[AZIENDA-1-aaaa]", redacted)
        self.assertIn("[CLIENTE-1-aaaa]", redacted)
        self.assertEqual(counts, {"AZIENDA": 1, "CLIENTE": 1})

    # --- regressions for the adversarial review (2026-09-21) -------------------------
    def test_source_code_is_not_redacted(self) -> None:
        """D1: '3+ dotted labels = host' redacted ordinary code and made the guard unusable."""
        for snippet in (
            "os.environ.get('X')",
            "numpy.random.normal(0, 1)",
            "logger.info('done')",
            "Path.home() / '.anon'",
            "child.stdout.on('data', cb)",
            "arr.at(0)",
            "obj.id",
            "process.env.NODE_ENV",
            "fs.promises.readFile(p)",
        ):
            self.assertEqual(anon.detect(snippet, self.entities), [], f"false positive on {snippet!r}")

    def test_real_hosts_are_still_redacted(self) -> None:
        for snippet in ("srvcrm.acme.local", "svc-bak01.banca.local", "mail.google.com", "db01.azienda.it"):
            self.assertEqual(len(anon.detect(snippet, self.entities)), 1, f"missed host {snippet!r}")

    def test_email_regex_is_linear(self) -> None:
        """D2: an unbounded local part made the regex quadratic (400k chars = minutes)."""
        import time

        blob = "a" * 200_000 + "@" + "b" * 200_000
        started = time.monotonic()
        anon.detect(blob, self.entities)
        self.assertLess(time.monotonic() - started, 2.0, "EMAIL regex is super-linear")

    def test_literal_placeholder_roundtrip(self) -> None:
        """D3: a literal `[EMAIL-1]` in the source broke the lossless round-trip."""
        for source in ("[EMAIL-1] e info@acme.it", "gia [IP-1] e poi 10.0.0.5", "[CLIENTE-2] e 3331234567"):
            redacted, entries, _ = anon.anonymize(source, self.entities)
            restored, _ = deanon.deanonize(redacted, entries)
            self.assertEqual(restored, source)

    def test_international_phone(self) -> None:
        for phone in ("+1 415 555 0199", "+44 20 7946 0958", "+39 02 1234567", "333 1234567"):
            self.assertEqual(len(anon.detect(phone, self.entities)), 1, f"missed phone {phone!r}")

    def test_public_infrastructure_values_are_safe(self) -> None:
        for snippet in (
            "nameserver 8.8.8.8",
            "broadcast 255.255.255.255",
            "API_KEY=your-api-key-here",
            "password: changeme",
            "token: placeholder-value",
        ):
            self.assertEqual(anon.detect(snippet, self.entities), [], f"false positive on {snippet!r}")

    # --- regressions for the adversarial review, round 2 (2026-09-21) ----------------
    def test_real_secret_shapes_are_redacted(self) -> None:
        """A substring placeholder-hint test let `password: postgrespassword` through."""
        for snippet in (
            "password: postgrespassword",
            "password: mypassword123",
            "db_password: hunter-x-hunter",
            "db_secret: abc123xyz789",
            "api_key=myexamplekey1234",
            "password: correct-horse-battery-staple",
            "password = my_secret.phrase",
            "password = correct_horse.battery",
            "password = abcdefgh (production)",
        ):
            self.assertEqual(len(anon.detect(snippet, self.entities)), 1, f"missed secret in {snippet!r}")

    def test_a_secret_touching_a_parenthesis_is_the_declared_miss(self) -> None:
        # The call lookahead rejects `value(` with no space. A literal secret written that way is the
        # declared, accepted miss; the alternative (`value (annotation)`) is kept by the test above,
        # and `token = re.compile(...)` (the case that motivated the rule) is rejected by
        # test_code_calls_are_not_secrets.
        self.assertEqual(anon.detect("password = abcdefgh(production)", self.entities), [])

    def test_placeholder_secrets_are_not_redacted(self) -> None:
        for snippet in (
            "API_KEY=your-api-key-here",
            "password: changeme",
            "password: placeholder-value",
            "token: xxxx",
            "token: your-token",
        ):
            self.assertEqual(anon.detect(snippet, self.entities), [], f"false positive on {snippet!r}")

    def test_code_calls_are_not_secrets(self) -> None:
        # Item #20: the KEY rule used to redact ordinary code — a right-hand side that is a CALL is
        # a reference, not the literal secret.
        for snippet in (
            "first_token=unicodedata.normalize(form, tokens[0]),",
            "const secret = scanForSecrets(candidate);",
            "token = re.compile(r'x')",
        ):
            self.assertEqual(anon.detect(snippet, self.entities), [], f"false positive on {snippet!r}")

    def test_a_dotted_value_is_still_redacted(self) -> None:
        # The declared trade-off of item #20: a dotted value is NOT treated as a code reference
        # (`my_secret.phrase` is a plausible real password), so it stays redacted. A dotted member
        # in source (`window.ANON_TOKEN`) is an accepted false positive: a missed secret is worse.
        for snippet in ("password: admin.secret", "password = my_secret.phrase", "password = correct_horse.battery"):
            self.assertEqual(len(anon.detect(snippet, self.entities)), 1, f"missed secret in {snippet!r}")

    def test_host_followed_by_a_file_extension(self) -> None:
        """`db01.azienda.it.log`: the trailing labels must be dropped, not the whole match."""
        self.assertEqual(anon.detect("db01.azienda.it.log", self.entities), [(0, 15, "HOST")])
        # NB: not `*.acme.*` — the test dictionary contains "Acme", and a curated entity
        # correctly wins over the host heuristic.
        self.assertEqual(anon.detect("srv.contoso.local.log", self.entities), [(0, 17, "HOST")])

    def test_counts_match_entries(self) -> None:
        """Reserved placeholder numbers must not inflate the reported counts."""
        _redacted, entries, counts = anon.anonymize("[EMAIL-1] e info@acme.it e altro@acme.it", self.entities)
        self.assertEqual(len(entries), 2)
        self.assertEqual(counts, {"EMAIL": 2})

    # --- dictionary robustness (2026-09-21): spelling variants must not be missed ---------
    ENTITY_VARIANTS = [
        ("AZIENDA|Contoso", ["Contoso", "contoso", "CONTOSO", "Contoso S.r.l.", "contoso srl",
                            "CONTOSO S.R.L.", "Contoso s.r.l"], ["Contosost", "la contosotta"]),
        ("AZIENDA|Acme Italia", ["Acme Italia", "acme-italia", "Acme.Italia srl"], ["AcmeItalia", "Acme"]),
        ("PERSONA|Nicolò Rossi", ["Nicolò Rossi"], ["Nicolo Rossi"]),
        ("AZIENDA|Contoso|Contoso-Italia", ["Contoso", "Contoso-Italia"], []),
    ]

    def _entities_from(self, line: str):
        import re

        raw = self.tmp_entities
        raw.write_text(line + "\n", encoding="utf-8")
        return anon.load_entities(raw)

    def test_entity_variants_are_recognized(self) -> None:
        for line, positives, negatives in self.ENTITY_VARIANTS:
            entities = self._entities_from(line)
            for text in positives:
                self.assertTrue(anon.detect(text, entities), f"{line!r}: missed {text!r}")
            for text in negatives:
                self.assertFalse(anon.detect(text, entities), f"{line!r}: false positive on {text!r}")

    def test_longest_match_wins_over_a_dictionary_hit(self) -> None:
        """A curated name inside a hostname must not fragment the hostname.

        `srv-crm01.contoso.local` must be redacted as a whole: claiming only the company part would
        leave `srv-crm01.` and `.local` readable, i.e. a partially redacted host.
        """
        self.tmp_entities.write_text("AZIENDA|Contoso\n", encoding="utf-8")
        entities = anon.load_entities(self.tmp_entities)
        text = "server srv-crm01.contoso.local qui"
        found = anon.detect(text, entities)
        self.assertEqual([text[start:end] for start, end, _type in found], ["srv-crm01.contoso.local"])

    def test_dictionary_wins_when_the_span_is_identical(self) -> None:
        """Same span from the dictionary and a heuristic: the operator's intent wins (and it is typed)."""
        self.tmp_entities.write_text("AZIENDA|Acme.local\n", encoding="utf-8")
        entities = anon.load_entities(self.tmp_entities)
        text = "il server Acme.local risponde"
        found = anon.detect(text, entities)
        self.assertEqual([(text[start:end], ptype) for start, end, ptype in found], [("Acme.local", "AZIENDA")])

    def test_entity_that_is_only_a_legal_form_stays_declarable(self) -> None:
        """A firm literally named `SA`/`AG`/`AB` must not be stripped into nothing."""
        for line, text in (("AZIENDA|SA", "SA"), ("AZIENDA|AG", "AG"), ("AZIENDA|AB", "AB")):
            entities = self._entities_from(line)
            self.assertTrue(entities, f"{line!r} produced no rules")
            self.assertTrue(anon.detect(text, entities), f"{line!r} did not redact {text!r}")

    def test_entity_accents_match_across_unicode_forms(self) -> None:
        import unicodedata

        entities = self._entities_from("PERSONA|Nicolò Rossi")
        nfd = unicodedata.normalize("NFD", "Nicolò Rossi")
        self.assertNotEqual(nfd, "Nicolò Rossi")
        self.assertTrue(anon.detect(nfd, entities), "an NFD file must match an NFC dictionary")

    def test_entity_variants_stay_lossless(self) -> None:
        """Each surface form gets its own placeholder: deanon restores the exact spelling."""
        entities = self._entities_from("AZIENDA|Contoso|Contoso S.r.l.")
        source = "Contoso S.r.l. e poi Contoso, con CONTOSO in maiuscolo.\n"
        redacted, entries, _ = anon.anonymize(source, entities)
        self.assertNotIn("contoso", redacted.casefold(), "a variant survived")
        restored, _ = deanon.deanonize(redacted, entries)
        self.assertEqual(restored, source)

    def test_each_run_gets_its_own_tag(self) -> None:
        """Two runs over the same text must not produce interchangeable placeholders."""
        first, first_entries, _ = anon.anonymize("mail info@acme.it\n", self.entities)
        second, second_entries, _ = anon.anonymize("mail info@acme.it\n", self.entities)
        self.assertNotEqual(anon.tag_of(first_entries), anon.tag_of(second_entries))
        self.assertNotEqual(first, second)
        # a document from the first run cannot be restored with the second run's map
        restored, _count = deanon.deanonize(first, second_entries)
        self.assertEqual(restored, first, "a foreign map must leave the placeholders untouched")

    def test_same_value_reuses_one_placeholder(self) -> None:
        text = "info@cliente.it, poi di nuovo info@cliente.it.\n"
        redacted, entries, counts = anon.anonymize(text, self.entities, tag="aaaa")
        self.assertEqual(redacted.count("[EMAIL-1-aaaa]"), 2)
        self.assertEqual(counts, {"EMAIL": 1})


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="anon-test-"))
        (self.tmp / "entities.txt").write_text(
            "# test dictionary\nCLIENTE|Acme\nAZIENDA|Acme Italia S.r.l.\nPERSONA|Mario Rossi\n",
            encoding="utf-8",
        )
        self.env = {**os.environ, "ANON_HOME": str(self.tmp)}

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_anon(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ANON_PY), *args],
            capture_output=True, text=True, env=self.env, check=False,
        )

    def test_cli_roundtrip_and_check(self) -> None:
        src = self.tmp / "nota.txt"
        src.write_text("Cliente Acme, referente Mario Rossi <mario@acme.it>.\n", encoding="utf-8")

        check = self.run_anon(str(src), "--check", "--json")
        self.assertEqual(check.returncode, 1, check.stderr)
        self.assertIn('"sensitive": true', check.stdout)
        self.assertNotIn("mario@acme.it", check.stdout)  # never leak the value in the report

        run = self.run_anon(str(src), "--quiet", "--map", str(self.tmp / "m.map.json"))
        self.assertEqual(run.returncode, 0, run.stderr)
        redacted = (self.tmp / "nota.redacted.txt").read_text(encoding="utf-8")
        self.assertNotIn("mario@acme.it", redacted)
        self.assertNotIn("Mario Rossi", redacted)
        self.assertTrue((self.tmp / "m.map.json").is_file())

        clean = subprocess.run(
            [sys.executable, str(ANON_PY), str(self.tmp / "nota.redacted.txt"), "--check"],
            capture_output=True, text=True, env=self.env, check=False,
        )
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)

        out = self.tmp / "finale.txt"
        de = subprocess.run(
            [sys.executable, str(DEANON_PY), str(self.tmp / "nota.redacted.txt"),
             str(self.tmp / "m.map.json"), "--out", str(out), "--quiet"],
            capture_output=True, text=True, env=self.env, check=False,
        )
        self.assertEqual(de.returncode, 0, de.stderr)
        self.assertEqual(out.read_text(encoding="utf-8"), src.read_text(encoding="utf-8"))

    def test_binary_input_is_refused(self) -> None:
        """A .docx read as text would leave a corrupted file *named* redacted. Refuse instead."""
        binary = self.tmp / "finto.docx"
        binary.write_bytes(b"PK\x03\x04" + bytes(range(256)) * 10)
        res = self.run_anon(str(binary), "--quiet")
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("REFUSED", res.stderr)
        self.assertFalse((self.tmp / "finto.redacted.docx").exists(), "no output must be written")
        self.assertEqual(list(self.tmp.glob("*.map.json")), [])

    def test_nul_bytes_are_treated_as_binary(self) -> None:
        blob = self.tmp / "blob.dat"
        blob.write_bytes(b"header\x00text with info@acme.it inside\n")
        self.assertEqual(self.run_anon(str(blob), "--quiet").returncode, 2)

    def test_binary_documents_fail_closed_on_check(self) -> None:
        """Pi's read decodes non-image files as text, so un-scannable containers must block."""
        import zipfile

        docx = self.tmp / "vero.docx"
        with zipfile.ZipFile(docx, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr("word/document.xml", "<w:t>Mario Rossi mario.rossi@acme.it</w:t>")
        pdf = self.tmp / "preludio.pdf"
        pdf.write_bytes(b"% commento\n" * 20 + b"%PDF-1.7\n<</A (mario@acme.it)>>\n")
        spanned = self.tmp / "spanned.zip"
        spanned.write_bytes(b"PK\x07\x08" + b"\x01\x02" * 60)
        for path in (docx, pdf, spanned):
            res = self.run_anon(str(path), "--check", "--json")
            self.assertEqual(res.returncode, 1, f"{path.name}: {res.stdout}")
            self.assertIn('"unscannable": true', res.stdout)

    def test_text_mentioning_the_pdf_signature_is_still_scanned(self) -> None:
        """`%PDF-` is printable ASCII: a document that merely mentions it must not be refused."""
        txt = self.tmp / "riferimento.txt"
        txt.write_text("L'header del file è %PDF- e poi il resto.\nmail: info@acme.it\n", encoding="utf-8")
        res = self.run_anon(str(txt), "--check", "--json")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn('"EMAIL": 1', res.stdout)
        self.assertNotIn("unscannable", res.stdout)
        self.assertEqual(self.run_anon(str(txt), "--quiet").returncode, 0)

    def test_images_are_exempt_and_text_starting_with_bm_is_not(self) -> None:
        png = self.tmp / "foto.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + bytes(64))
        res = self.run_anon(str(png), "--check", "--json")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertIn('"binary": true', res.stdout)
        # "BM" is not enough to be a BMP: the text must still be scanned
        bmw = self.tmp / "bmw.txt"
        bmw.write_text("BMW Serie 3 — contatto info@acme.it\n", encoding="utf-8")
        bmw_res = self.run_anon(str(bmw), "--check", "--json")
        self.assertEqual(bmw_res.returncode, 1, bmw_res.stdout)
        self.assertIn('"EMAIL": 1', bmw_res.stdout)

    def test_plain_text_is_still_accepted(self) -> None:
        txt = self.tmp / "ok.txt"
        txt.write_text("mail: info@azienda.it\n", encoding="utf-8")
        res = self.run_anon(str(txt), "--quiet", "--tag", "aaaa")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("[EMAIL-1-aaaa]", (self.tmp / "ok.redacted.txt").read_text(encoding="utf-8"))

    def test_stdin_check_reports_the_real_line(self) -> None:
        res = subprocess.run(
            [sys.executable, str(ANON_PY), "-", "--check", "--json"],
            input="prima riga\nseconda riga\nmail: mario@acme.it\n",
            capture_output=True, text=True, env=self.env, check=False,
        )
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn('"line": 3', res.stdout)

    def test_prune_maps_lists_and_deletes_only_with_yes(self) -> None:
        """The maps hold the REAL values: a deletion must be previewable and explicit."""
        maps = self.tmp / "maps"
        maps.mkdir()
        old = maps / "20200101-000000-aaaaaa.map.json"
        fresh = maps / "20260101-000000-bbbbbb.map.json"
        for path in (old, fresh):
            path.write_text('{"entries": {}}', encoding="utf-8")
        aged = 1_600_000_000  # 2020-09-13: deterministic, and unambiguously older than 30 days
        os.utime(old, (aged, aged))

        listed = self.run_anon("--prune-maps", "30")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn(old.name, listed.stdout)
        self.assertIn("--yes", listed.stdout)
        self.assertTrue(old.exists(), "a dry run must not delete anything")

        report = json.loads(self.run_anon("--prune-maps", "30", "--json").stdout)
        self.assertEqual(report["schema"], anon.SCHEMA)
        self.assertEqual(report["candidates"], 1)
        self.assertFalse(report["applied"])

        refused = self.run_anon("--prune-maps", "0", "--yes")
        self.assertEqual(refused.returncode, 2, "0 days would mean 'delete everything'")
        self.assertTrue(old.exists() and fresh.exists(), "a refused run must not delete anything")

        applied = self.run_anon("--prune-maps", "30", "--yes")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertFalse(old.exists(), "the old map must be gone")
        self.assertTrue(fresh.exists(), "a recent map must survive")

    def test_a_capped_findings_list_says_so(self) -> None:
        """`findings` is capped for output size; a truncated list must not read as the whole list."""
        src = self.tmp / "molti.txt"
        # Real findings, not placeholders: the engine protects existing placeholders on purpose.
        src.write_text(
            "\n".join(f"riga {index}: utente{index}@cliente{index}.it" for index in range(1, 61)),
            encoding="utf-8",
        )
        check = json.loads(self.run_anon(str(src), "--check", "--json").stdout)
        self.assertEqual(check["total"], 60)
        self.assertLess(len(check["findings"]), 60)
        self.assertTrue(check["findings_truncated"], "the truncation must be declared")

        audit = json.loads(self.run_anon(str(src), "--audit", "--json").stdout)
        self.assertTrue(audit["findings_truncated"])

        small = self.tmp / "piccolo.txt"
        small.write_text("una sola utente9@cliente9.it\n", encoding="utf-8")
        self.assertNotIn("findings_truncated", json.loads(self.run_anon(str(small), "--check", "--json").stdout))

    def test_short_stem_warns_and_a_long_one_does_not(self) -> None:
        """A 3-character stem redacts unrelated words: say so, but never refuse (see the note)."""
        stem_dictionary = self.tmp / "stems.txt"
        stem_dictionary.write_text("@stem on\nSERVIZIO|Abb\nSERVIZIO|Ferretti\n", encoding="utf-8")
        src = self.tmp / "stems-doc.txt"
        src.write_text("il servizio Abb e Ferretti\n", encoding="utf-8")
        res = self.run_anon(str(src), "--entities", str(stem_dictionary), "--stdout")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("'Abb'", res.stderr)
        self.assertNotIn("'Ferretti'", res.stderr)
        self.assertIn("[SERVIZIO-1-", res.stdout)

    def test_catalogs_flag_and_pattern_groups(self) -> None:
        catalogs = self.tmp / "catalogs"
        catalogs.mkdir()
        (catalogs / "mycity.txt").write_text(
            "@type CITTÀ\n@match case-sensitive\n@context (?:sede di)\\s+\nAncona\n", encoding="utf-8"
        )
        src = self.tmp / "doc.txt"
        src.write_text("sede di Ancona, il prato e' verde. CF RSSMRA80A01H501U\n", encoding="utf-8")

        listing = self.run_anon("--list-catalogs")
        self.assertEqual(listing.returncode, 0)
        self.assertIn("mycity", listing.stdout)

        with_catalog = self.run_anon(str(src), "--catalogs", "mycity", "--out", str(self.tmp / "a.txt"),
                                     "--map", str(self.tmp / "a.json"), "--quiet", "--tag", "aaaa")
        self.assertEqual(with_catalog.returncode, 0, with_catalog.stderr)
        redacted = (self.tmp / "a.txt").read_text(encoding="utf-8")
        self.assertIn("[CITTÀ-1-aaaa]", redacted)
        self.assertIn("il prato", redacted, "case-sensitive: the meadow is not a city")
        self.assertIn("[CODICEFISCALE-1-aaaa]", redacted)

        identity_only = self.run_anon(str(src), "--patterns", "identity", "--out", str(self.tmp / "b.txt"),
                                      "--map", str(self.tmp / "b.json"), "--quiet", "--tag", "aaaa")
        self.assertEqual(identity_only.returncode, 0, identity_only.stderr)
        self.assertIn("RSSMRA80A01H501U", (self.tmp / "b.txt").read_text(encoding="utf-8"),
                      "--patterns identity must leave the legal group off")

    def test_unknown_catalog_or_group_is_an_error(self) -> None:
        src = self.tmp / "doc.txt"
        src.write_text("ciao\n", encoding="utf-8")
        self.assertEqual(self.run_anon(str(src), "--catalogs", "nope").returncode, 2)
        self.assertEqual(self.run_anon(str(src), "--patterns", "nope").returncode, 2)

    def test_allowlist_disables_check(self) -> None:
        (self.tmp / "allow.txt").write_text(f"{self.tmp}/*\n", encoding="utf-8")
        src = self.tmp / "nota.txt"
        src.write_text("email: mario@acme.it\n", encoding="utf-8")
        res = self.run_anon(str(src), "--check", "--json")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn('"allowed": true', res.stdout)

    def test_allow_glob_disables_check_for_one_run(self) -> None:
        # Same effect as allow.txt, but scoped to this invocation: this is what the Pi guard
        # passes for its session-only allowlist, without writing a temp file.
        src = self.tmp / "nota.txt"
        src.write_text("email: mario@acme.it\n", encoding="utf-8")
        res = self.run_anon(str(src), "--check", "--json", "--allow-glob", f"{self.tmp}/*")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn('"allowed": true', res.stdout)

    def test_allow_glob_is_additive_to_the_allow_file(self) -> None:
        (self.tmp / "allow.txt").write_text("/nowhere/*\n", encoding="utf-8")
        src = self.tmp / "nota.txt"
        src.write_text("email: mario@acme.it\n", encoding="utf-8")
        res = self.run_anon(str(src), "--check", "--json", "--allow-glob", f"{self.tmp}/*")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn('"allowed": true', res.stdout)

    def test_product_version_is_single_sourced(self) -> None:
        # deanon.py must not keep its own version string: it reports anon.VERSION, so this catches a
        # revert to a hardcoded value (it cannot catch a version that is wrong but equal in both).
        # The web UI reads anon.VERSION directly, so it is single-sourced by construction.
        import re as _re

        anon_res = self.run_anon("--version")
        deanon_res = subprocess.run(
            [sys.executable, str(DEANON_PY), "--version"], capture_output=True, text=True, check=False
        )
        anon_v = _re.search(r"\d+\.\d+\.\d+", anon_res.stdout)
        deanon_v = _re.search(r"\d+\.\d+\.\d+", deanon_res.stdout)
        self.assertIsNotNone(anon_v, anon_res.stdout)
        self.assertIsNotNone(deanon_v, deanon_res.stdout)
        self.assertEqual(anon_v.group(0), deanon_v.group(0))

    def test_default_dictionaries_are_merged(self) -> None:
        # entities.txt (generic) + people.txt + clients.txt are all read when --entities is absent.
        (self.tmp / "people.txt").write_text("@type PERSONA\nGiulia Bianchi\n", encoding="utf-8")
        (self.tmp / "clients.txt").write_text("@type AZIENDA\nContoso S.p.A.\n", encoding="utf-8")
        src = self.tmp / "nota.txt"
        src.write_text("Giulia Bianchi per Contoso S.p.A.\n", encoding="utf-8")
        res = self.run_anon(str(src), "--check", "--json")
        self.assertIn('"sensitive": true', res.stdout)
        self.assertIn("PERSONA", res.stdout)
        self.assertIn("AZIENDA", res.stdout)

    def test_entities_flag_is_repeatable(self) -> None:
        first = self.tmp / "a.txt"
        first.write_text("PERSONA|Alfa Persona\n", encoding="utf-8")
        second = self.tmp / "b.txt"
        second.write_text("AZIENDA|Beta Azienda\n", encoding="utf-8")
        src = self.tmp / "nota.txt"
        src.write_text("Alfa Persona e Beta Azienda\n", encoding="utf-8")
        res = self.run_anon(
            str(src), "--check", "--json", "--entities", str(first), "--entities", str(second)
        )
        self.assertIn('"sensitive": true', res.stdout)
        self.assertIn("PERSONA", res.stdout)
        self.assertIn("AZIENDA", res.stdout)

    def test_a_missing_explicit_dictionary_is_an_error(self) -> None:
        # A typo in --entities must fail loudly, not silently drop a dictionary.
        src = self.tmp / "nota.txt"
        src.write_text("x\n", encoding="utf-8")
        res = self.run_anon(str(src), "--check", "--json", "--entities", str(self.tmp / "nope.txt"))
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("not found", res.stderr)

    def test_no_dictionary_at_all_still_works(self) -> None:
        # A fresh install has no curated dictionary: the engine must keep working with the pattern
        # rules, and `--check --json` must still emit valid JSON — the Pi guard reads "no JSON" as a
        # broken engine and fails OPEN, which is a leak.
        for name in ("entities.txt", "people.txt", "clients.txt"):
            (self.tmp / name).unlink(missing_ok=True)
        src = self.tmp / "nota.txt"
        src.write_text("email: mario@acme.it\n", encoding="utf-8")
        res = self.run_anon(str(src), "--check", "--json")
        self.assertIn('"sensitive": true', res.stdout)
        self.assertIn("EMAIL", res.stdout)

    def test_an_empty_entities_flag_is_an_error(self) -> None:
        src = self.tmp / "nota.txt"
        src.write_text("x\n", encoding="utf-8")
        res = self.run_anon(str(src), "--check", "--json", "--entities", "")
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)


class CodeFingerprintTest(unittest.TestCase):
    """The build fingerprint is what makes a stale container detectable: deterministic, over the
    shipped files, and moving the moment one of them is edited."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="anon-fingerprint-"))
        (self.tmp / "web").mkdir()
        (self.tmp / "anon.py").write_text("x = 1\n", encoding="utf-8")
        (self.tmp / "deanon.py").write_text("y = 2\n", encoding="utf-8")
        (self.tmp / "suggest.py").write_text("z = 3\n", encoding="utf-8")
        (self.tmp / "web" / "app.js").write_text("// a\n", encoding="utf-8")
        (self.tmp / "web" / "index.html").write_text("<p></p>\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_same_tree_hashes_the_same(self) -> None:
        first = anon.code_fingerprint(self.tmp)
        self.assertEqual(first, anon.code_fingerprint(self.tmp), "the digest must be deterministic")
        self.assertEqual(len(first), 64)

    def test_an_edit_to_a_shipped_file_changes_the_digest_and_copy_artifacts_do_not(self) -> None:
        first = anon.code_fingerprint(self.tmp)
        (self.tmp / "web" / "app.js").write_text("// changed\n", encoding="utf-8")
        changed = anon.code_fingerprint(self.tmp)
        self.assertNotEqual(first, changed, "an edited shipped file must move the digest")
        # Copy artifacts a build never ships must not move it, or a macOS `.DS_Store` landing in
        # `web/` would report a container stale that no rebuild could fix.
        (self.tmp / "web" / "__pycache__").mkdir()
        (self.tmp / "web" / "__pycache__" / "app.pyc").write_bytes(b"\x00")
        (self.tmp / "web" / ".DS_Store").write_bytes(b"\x00")
        self.assertEqual(changed, anon.code_fingerprint(self.tmp))

    def test_convert_follows_the_variant_so_slim_and_full_do_not_read_as_stale(self) -> None:
        """`convert.py` is shipped by the full image only: it must move the digest when present, and
        be excludable so a slim build is compared against its own file set."""
        without = anon.code_fingerprint(self.tmp)  # the tree has no convert.py yet
        self.assertEqual(without, anon.code_fingerprint(self.tmp, with_convert=False))
        self.assertEqual(without, anon.code_fingerprint(self.tmp, with_convert=True))
        (self.tmp / "convert.py").write_text("print()\n", encoding="utf-8")
        automatic = anon.code_fingerprint(self.tmp)
        self.assertNotEqual(without, automatic, "a present convert.py must move the digest")
        self.assertEqual(automatic, anon.code_fingerprint(self.tmp, with_convert=True))
        self.assertEqual(without, anon.code_fingerprint(self.tmp, with_convert=False))

    def test_a_parent_directory_named_like_an_ignored_dir_does_not_blank_the_digest(self) -> None:
        """`path.parts` used to span the ABSOLUTE path: a checkout under a directory named `venv`
        had every file filtered out and the digest stopped moving, so a stale container read fresh.
        The filter must look only at the path relative to the root."""
        root = Path(tempfile.mkdtemp(prefix="anon-fingerprint-parent-"))
        repo = root / "venv" / "repo"
        (repo / "web").mkdir(parents=True)
        (repo / "anon.py").write_text("x = 1\n", encoding="utf-8")
        (repo / "web" / "app.js").write_text("// a\n", encoding="utf-8")
        try:
            before = anon.code_fingerprint(repo)
            (repo / "web" / "app.js").write_text("// b\n", encoding="utf-8")
            self.assertNotEqual(before, anon.code_fingerprint(repo))
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_the_container_fresh_gate_is_wired_and_the_script_exists(self) -> None:
        """A script nobody runs is a comment: the gate has to declare it."""
        declaration = HOME / ".pi" / "verify.json"
        self.assertTrue(declaration.is_file(), f"the gate declaration is missing: {declaration}")
        verify = json.loads(declaration.read_text(encoding="utf-8"))
        gates = {gate["name"]: gate["command"] for gate in verify["gates"]}
        self.assertIn("container-fresh", gates)
        self.assertIn("check-container-fresh.py", gates["container-fresh"])
        self.assertTrue((HOME / "scripts" / "check-container-fresh.py").is_file())


class AddressCorpusTest(unittest.TestCase):
    """The address rule, measured on a corpus instead of reasoned about.

    Before this pass the rule had false negatives on this list (`V.le`, `P.zza`, `G.`, `n. 3`,
    `3/A` were all missed, because the abbreviated markers were escaped twice inside the pattern)
    AND it redacted prose (`in via del tutto eccezionale, 3 volte`). Both directions are pinned.
    """

    POSITIVE = (
        "Sede in Via Roma 12 per la verifica",
        "Ufficio in V.le Europa 12, secondo piano",
        "Sede in P.zza Garibaldi 3",
        "Recapito: Piazza G. Verdi, 3",
        "Recapito: Via Roma, n. 3",
        "Recapito: Via G. Verdi 3/A",
        "Corso Buenos Aires, 12-bis",
        "Piazza del Campo 1, Siena",
        "Viale dei Mille 3",
        "via della Repubblica 7",
        "VIA ROMA 12",
        "Indirizzo di fatturazione: Via dei Mille, 21/A",
        "Recapito: Via d'Azeglio 1",
        "Sede legale: Via dell'Università 12",
        "Uffici in Corso d'Italia 5",
        "Via l'Aquila 4",
        # L'accento scritto come apostrofo (la forma piu' comune in un testo semplice): il
        # token finisce con l'apostrofo, e la parte da leggere e' quella PRIMA, non quella dopo.
        "Recapito: via della Liberta' 27",
        "Sede legale: Via dell'Universita' 2",
        "Doppio apostrofo: via della Liberta'' 27",
    )

    NEGATIVE = (
        "in via del tutto eccezionale, 3 volte l'anno",
        "percorrere la via libera 4 corsie",
        "sulla via dell'emergenza' 2 volte l'anno",  # apostrofo, ma tutto minuscolo
        # L'apostrofo MEDIANO elide un articolo, non tronca il nome: leggere anche la parte PRIMA
        # dell'apostrofo farebbe dell'ARTICOLO maiuscolo il segnale, e queste sono frasi comuni che
        # arrivano alla regola con la forma di un indirizzo.
        "via Un'ora di lavoro, 3",
        "si procede via L'anno scorso, 3",
        "via All'incirca, 3",
        "via Dell'aria, 3",
        # L'articolo separato dal nome (spazio o a-capo): il token finisce con l'apostrofo come una
        # troncatura, ma non e' l'ULTIMO del nome, ed e' l'ultimo token l'unico che una troncatura
        # puo' essere.
        "si procede via L' anno scorso, 3",
        "via Un' anno intero, 3",
        "il viale alberato 2 piani",
        "nessun indirizzo in questa riga",
        # Un articolo eliso rimasto senza nome: la forma ridotta e' `Un`, che e' un articolo, non un
        # nome troncato (prima era un residuo dichiarato; ora `ELIDED_ARTICLES` lo chiude).
        "via Un' 3",
        "via dell' 3",
        "via D' 3",
        "via roma 12 in minuscolo (trade-off dichiarato: non redatto)",
        "il corso d'acqua 2 metri",
    )

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="anon-address-"))
        self.entities = anon.load_entities(self.work / "empty.txt")

    def tearDown(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    def test_addresses_are_detected(self) -> None:
        for text in self.POSITIVE:
            self.assertTrue(anon.detect(text, self.entities), f"missed address: {text!r}")

    def test_prose_is_not_redacted(self) -> None:
        for text in self.NEGATIVE:
            self.assertEqual(anon.detect(text, self.entities), [], f"false positive: {text!r}")

    def test_the_civic_suffix_is_part_of_the_span(self) -> None:
        """A half-redacted `Via G. Verdi 3` plus a visible `/A` is worse than no redaction."""
        text = "Recapito: Via G. Verdi 3/A"
        found = anon.detect(text, self.entities)
        self.assertEqual(len(found), 1)
        start, end, ptype = found[0]
        self.assertEqual(ptype, "INDIRIZZO")
        self.assertEqual(text[start:end], "Via G. Verdi 3/A")


class RecallCorpusTest(unittest.TestCase):
    """The engine's RECALL, measured on the labelled corpus (`tests/corpus.py`).

    `fp-sweep.py` measures the other direction (a clean corpus redacted by mistake). This is the
    one that decides whether an anonymizer is safe: a value a document DECLARES sensitive with no
    covered occurrence is a LEAK, and the gate fails here. The corpus is synthetic and declares its
    own dictionary, so the number is the same on any machine and no private data is involved.
    """

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("anon_recall_sweep", HOME / "scripts" / "recall-sweep.py")
        assert spec and spec.loader
        cls.sweep = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.sweep
        spec.loader.exec_module(cls.sweep)
        cspec = importlib.util.spec_from_file_location("anon_corpus", HERE / "corpus.py")
        assert cspec and cspec.loader
        cls.corpus = importlib.util.module_from_spec(cspec)
        sys.modules[cspec.name] = cls.corpus
        cspec.loader.exec_module(cls.corpus)

    def test_no_declared_value_is_left_uncovered(self) -> None:
        report = self.sweep.evaluate(self.corpus.DOCUMENTS)
        self.assertEqual(report["leaks"], [],
                         f"the engine left these DECLARED values in clear: {report['leaks']}")

    def test_no_must_not_string_is_redacted(self) -> None:
        report = self.sweep.evaluate(self.corpus.DOCUMENTS)
        self.assertEqual(report["violations"], [],
                         f"false positives on the labelled corpus: {report['violations']}")

    def test_every_pattern_family_is_exercised(self) -> None:
        """A corpus that stopped covering a family would silently stop measuring it."""
        declared = {ptype for doc in self.corpus.DOCUMENTS for _v, ptype in doc.get("must_find", [])}
        for expected in ("EMAIL", "IP", "HOST", "INDIRIZZO", "IBAN", "PARTITAIVA",
                         "CODICEFISCALE", "TARGA", "TEL", "URL", "KEY"):
            self.assertIn(expected, declared, f"the corpus no longer declares any {expected}")


class ValidatorTest(unittest.TestCase):
    """Checksum-validated identifiers: high precision is the whole point of having them."""

    def test_codice_fiscale(self) -> None:
        self.assertTrue(anon._valid_codice_fiscale("RSSMRA80A01H501U"))          # known-good example
        self.assertTrue(anon._valid_codice_fiscale("rssmra80a01h501u"))          # case-insensitive
        self.assertTrue(anon._valid_codice_fiscale("RSSMRA8LA01H501U"))          # omocodia (0 -> L)
        self.assertFalse(anon._valid_codice_fiscale("RSSMRA80A01H501X"))         # wrong check char
        self.assertFalse(anon._valid_codice_fiscale("RSSMRA80A01H50"))

    def test_partita_iva(self) -> None:
        # Worked example from the published algorithm: even positions doubled, digit-sums added.
        self.assertTrue(anon._valid_partita_iva("02342520158"))
        self.assertFalse(anon._valid_partita_iva("02342520159"))
        self.assertFalse(anon._valid_partita_iva("0234252015"))

    def test_iban(self) -> None:
        self.assertTrue(anon._valid_iban("IT60X0542811101000000123456"))
        self.assertTrue(anon._valid_iban("IT60 X054 2811 1010 0000 0123 456"))  # spaces allowed
        self.assertFalse(anon._valid_iban("IT60X0542811101000000123457"))

    def test_an_iban_followed_by_a_word_is_still_redacted(self) -> None:
        """The recall corpus found this: the loose body matched `… 456 entro`, the checksum then
        rejected the over-long value, and the real IBAN was left in clear."""
        text = "Pagamento sul conto IT60 X054 2811 1010 0000 0123 456 entro trenta giorni."
        found = anon.detect(text, [])
        ibans = [text[s:e] for s, e, ptype in found if ptype == "IBAN"]
        self.assertEqual(ibans, ["IT60 X054 2811 1010 0000 0123 456"])
        self.assertFalse(any("entro" in text[s:e] for s, e, _ptype in found))

    def test_every_italian_iban_grouping_is_redacted(self) -> None:
        """The checksum decides where the value ends: a loose body matches every real grouping and
        `shrink_words` trims only the token that follows, so no grouping is lost (the first fix —
        groups of four — left the bank-statement form `IT60 X 05428 …` in clear)."""
        for text in (
            "conto IT60 X054 2811 1010 0000 0123 456 fine",
            "conto IT60X0542811101000000123456 fine",
            "conto IT60 X 05428 11101 000000123456 fine",
            "conto IT60 X05 428 111 010 000 001 234 56 fine",
            "conto it60 x054 2811 1010 0000 0123 456 fine",
        ):
            with self.subTest(text=text):
                found = [text[s:e] for s, e, ptype in anon.detect(text, []) if ptype == "IBAN"]
                self.assertEqual(len(found), 1, f"IBAN not redacted in {text!r}")
                self.assertNotIn("fine", found[0])

    def test_targa(self) -> None:
        self.assertTrue(anon._valid_targa("AB123CD"))
        self.assertFalse(anon._valid_targa("AB1234C"))

    def test_identifiers_are_detected_in_text(self) -> None:
        text = (
            "CF RSSMRA80A01H501U, P.IVA 02342520158, IBAN IT60X0542811101000000123456, "
            "sede in Via Roma 12, targa AB123CD."
        )
        types = {ptype for _s, _e, ptype in anon.detect(text, [])}
        self.assertEqual(types, {"CODICEFISCALE", "PARTITAIVA", "IBAN", "INDIRIZZO", "TARGA"})

    def test_pattern_families_can_be_selected(self) -> None:
        text = "CF RSSMRA80A01H501U su 10.0.0.1, mail a@b.it"
        legal = {ptype for _s, _e, ptype in anon.detect(text, [], families={"legal"})}
        non_legal = {ptype for _s, _e, ptype in anon.detect(text, [], families={"identity", "network"})}
        self.assertEqual(legal, {"CODICEFISCALE"})
        self.assertEqual(non_legal, {"IP", "EMAIL"})
        self.assertEqual(
            {ptype for _s, _e, ptype in anon.detect(text, [])},
            {"CODICEFISCALE", "IP", "EMAIL"},
            "no families means every family",
        )


class DirectivesTest(unittest.TestCase):
    """Catalog directives: @type, @stem, @match, @context."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="anon-directives-"))
        self.file = self.tmp / "cat.txt"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def entities(self, text: str):
        self.file.write_text(text, encoding="utf-8")
        return anon.load_entities(self.file)

    def test_stem_covers_numbered_and_suffixed_names(self) -> None:
        entities = self.entities("@type HOST\n@stem on\nPincopallino\n")
        for text in ("Pincopallino", "Pincopallino1", "Pincopallino-DB01", "PINCOPALLINO_srv"):
            self.assertTrue(anon.detect(text, entities), f"stem missed {text!r}")
        self.assertFalse(anon.detect("Pincopallin", entities), "a shorter word is not the stem")

    def test_stem_is_opt_in_per_entry(self) -> None:
        entities = self.entities("@type HOST\nPincopallino\n")
        self.assertTrue(anon.detect("Pincopallino", entities))
        self.assertFalse(anon.detect("Pincopallino1", entities), "without @stem the suffix is not matched")

    def test_the_alias_form_carries_both_surfaces(self) -> None:
        entities = self.entities("PERSONA|Mario Rossi|m.rossi@x.it\n")
        text = "Referente Mario Rossi (m.rossi@x.it)"
        found = [text[s:e] for s, e, _t in anon.detect(text, entities)]
        self.assertIn("Mario Rossi", found)
        self.assertIn("m.rossi@x.it", found)

    def test_a_type_with_a_space_is_rejected_loudly(self) -> None:
        """`Mario Rossi|m.rossi@x.it` under an `@type` reads as TYPE=`Mario Rossi`, value=email, and
        the NAME is then never redacted. The mistake must fail, not leak — in BOTH spellings, so the
        grammar is one rule everywhere."""
        for line in ("@type PERSONA\nMario Rossi|m.rossi@x.it\n", "@type RAGIONE SOCIALE\nContoso\n"):
            with self.subTest(line=line), self.assertRaises(ValueError) as ctx:
                self.entities(line)
            self.assertIn("contains a space", str(ctx.exception))

    def test_an_entity_followed_by_an_elided_article_is_not_matched(self) -> None:
        """The elision is a property of the MATCH, not of the vendor list: any entry behaves so."""
        entities = self.entities("AZIENDA|Dell\n")
        self.assertEqual(anon.detect("Dell'azienda cresce", entities), [])
        self.assertTrue(anon.detect("un server Dell", entities))
        # The English genitive is NOT an elision: `Dell's` must still redact Dell (a fix that traded
        # a false positive for a false NEGATIVE would be worse than the one it removed).
        self.assertTrue(anon.detect("Dell's CTO si e' dimesso", entities))
        self.assertTrue(anon.detect("Dell\u2019s sede chiude", entities))

    def test_case_sensitive_keeps_lowercase_words_intact(self) -> None:
        entities = self.entities("@type CITTÀ\n@match case-sensitive\nPrato\n")
        self.assertTrue(anon.detect("Prato", entities))
        self.assertFalse(anon.detect("il prato è verde", entities), "`prato` is not a city here")

    def test_context_gates_a_low_signal_entry(self) -> None:
        entities = self.entities("@type CITTÀ\n@match case-sensitive\n@context (?:comune di|sede di)\\s+\nAncona\n")
        self.assertTrue(anon.detect("sede di Ancona", entities))
        self.assertFalse(anon.detect("Ancona", entities), "without the context marker it is not redacted")
        # only the city is redacted, the context marker stays readable
        found = anon.detect("comune di Ancona", entities)
        self.assertEqual(["comune di Ancona"[s:e] for s, e, _t in found], ["Ancona"])

    def test_context_off_closes_the_block(self) -> None:
        entities = self.entities(
            "@type CITTÀ\n@match case-sensitive\n@context (?:sede di)\\s+\nAncona\n@context off\nPrato\n"
        )
        # The gated entry keeps its context...
        self.assertFalse(anon.detect("Ancona", entities), "the gated entry keeps its context")
        self.assertTrue(anon.detect("sede di Ancona", entities))
        # ...while an entry declared after `@context off` matches bare.
        self.assertTrue(anon.detect("Prato", entities), "`@context off` must clear the context")

    def test_a_later_context_replaces_the_earlier_one(self) -> None:
        entities = self.entities(
            "@type CITTÀ\n@match case-sensitive\n@context (?:comune di)\\s+\nAncona\n"
            "@context (?:sede di)\\s+\nPrato\n"
        )
        self.assertTrue(anon.detect("comune di Ancona", entities))
        self.assertFalse(anon.detect("comune di Prato", entities), "the first context no longer applies")
        self.assertTrue(anon.detect("sede di Prato", entities))

    def test_only_exactly_off_clears_the_context(self) -> None:
        # `no` and `0` are legitimate @context REGEXES: reading them as "off" (the shared _FALSE
        # list, used by @stem) would silently drop a gate the operator wrote.
        entities = self.entities("@type CITTÀ\n@match case-sensitive\n@context no\\s+\nAncona\n")
        self.assertTrue(anon.detect("no Ancona", entities), "`no` is a regex, not an off switch")
        self.assertFalse(anon.detect("Ancona", entities), "the gate must still apply")

    def test_unknown_directive_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            self.entities("@contxt x\nCITTÀ|Ancona\n")
        with self.assertRaises(ValueError):
            self.entities("@stem maybe\nX|Y\n")
        with self.assertRaises(ValueError):
            self.entities("@context (unbalanced\nX|Y\n")

    def test_type_directive_applies_to_following_entries(self) -> None:
        entities = self.entities("@type CLIENTE\nAcme\n@type PERSONA\n\nMario Rossi\n")
        types = {ptype for _s, _e, ptype in anon.detect("Acme e Mario Rossi", entities)}
        self.assertEqual(types, {"CLIENTE", "PERSONA"})


def catalog_blocks(path: Path) -> tuple[list[str], list[str]]:
    """The two blocks a catalog declares, read from the FILE (the input the gate reads)."""
    first: list[str] = []
    second: list[str] = []
    current: list[str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "@match case-sensitive":
            current = first
        elif stripped == "@match insensitive":
            current = second
        elif stripped and not stripped.startswith(("#", "@")) and current is not None:
            current.append(stripped)
    return first, second


class CatalogBlocksTest(unittest.TestCase):
    """Shared gate for a shipped TWO-BLOCK catalog (`vendors.txt`, `products.txt`).

    A data file has no other way to fail loudly, so the load-bearing properties are pinned here,
    once, instead of being copied into every catalog's test class:

      * both blocks are read from the FILE, and the block rule is enforced in both directions — a
        case-sensitive name must declare the ordinary word it collides with, and a block-2 name may
        not be one of those words nor a word of the OS dictionary (the sweep that catches a name
        nobody thought about, skipped VISIBLY when there is no dictionary to read);
      * a declared LINE must be a live entry (`load_entities` dedups, so asking it whether there are
        duplicates is asking it to grade itself);
      * the size stated to the user is bound to the file, here and in `--list-catalogs`.

    Subclasses set `CATALOG`, `TYPE`, `STNAME` and `WORD_COLLISION`, and add the behaviour tests
    that are specific to what they list.
    """

    CATALOG: Path
    TYPE: str
    STNAME: str
    WORD_COLLISION: dict[str, str]
    MIN_SIZE = 100
    SYSTEM_DICTIONARIES = ("/usr/share/dict/words", "/usr/share/dict/american-english")

    def setUp(self) -> None:
        if not getattr(self, "CATALOG", None):
            # The base class is the shared gate, not a catalog: unittest still collects it, so it
            # says so out loud instead of erroring on a missing file.
            self.skipTest("abstract: CatalogBlocksTest is inherited, never run")
        self.tmp = Path(tempfile.mkdtemp(prefix="anon-catalog-"))
        (self.tmp / "catalogs").mkdir()
        shutil.copyfile(self.CATALOG, self.tmp / "catalogs" / self.CATALOG.name)
        self.env = {**os.environ, "ANON_HOME": str(self.tmp)}
        self.entities = anon.load_entities(self.CATALOG)
        self.size = anon.entity_count(self.entities)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_anon(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ANON_PY), *args], capture_output=True, text=True, env=self.env, check=False
        )

    def matched(self, text: str) -> list[str]:
        """Only the vendor hits: the pattern rules are other people's business here."""
        return [text[s:e] for s, e, ptype in anon.detect(text, self.entities) if ptype == self.TYPE]

    # --- the shipped artifact ---

    def test_the_list_ships_typed_and_fully_declared(self) -> None:
        self.assertTrue(self.CATALOG.is_file(), f"the catalog ships at {self.CATALOG}")
        self.assertEqual({entity.type for entity in self.entities}, {self.TYPE})
        self.assertGreaterEqual(self.size, self.MIN_SIZE, f"{self.CATALOG.name}: fewer than {self.MIN_SIZE} rows")
        # Read the FILE, not the parsed entities: `load_entities` already dedups, so asking it
        # whether there are duplicates is asking it to grade itself. A line that is silently
        # dropped (or declared twice) is what this must catch.
        header = self.CATALOG.read_text(encoding="utf-8")
        declared = [
            line.strip().casefold()
            for line in header.splitlines()
            if line.strip() and not line.startswith(("#", "@"))
        ]
        self.assertEqual(len(declared), len(set(declared)), "the list declares the same name twice")
        self.assertEqual(len(declared), self.size, "a declared line is not a live entry")

    def test_the_count_stated_to_the_user_comes_from_the_file(self) -> None:
        header = self.CATALOG.read_text(encoding="utf-8")
        self.assertIn(f"adds {self.size} entries", header)
        listing = self.run_anon("--list-catalogs")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertIn(f"{self.STNAME}\t{self.size} entries", listing.stdout)

    # --- the gate on the block rule (docs/OPEN-ISSUES.md #37) ---

    # The ordinary word each case-sensitive entry collides with. This map IS the gate: it is what
    # turns "check whether the name is also a word" from a judgement into something that fails.
    # Two reviews of the first version moved FIFTEEN names because nothing was watching: six were
    # words sitting in block 2 (`gigabyte`, `red hat`, ...), and `ibm`/`amd`/`hpe`/`apc` were kept
    # case-sensitive with no word behind them at all. The map answers both directions, and the
    # system word list below sweeps for a name that was never noticed in the first place.
    # The eight that a word LIST would not have caught are why both are needed: the system list has
    # no `gigabyte`, `google`, `veritas`, `siemens`, `okta`, `hp`, `intel`, `xerox` either.
    WORD_COLLISION = {
        "Acer": "acer",
        "Adobe": "adobe",
        "Amazon": "amazon",
        "Apple": "apple",
        "Arista": "arista",
        "Avast": "avast",
        "Axis": "axis",
        "Barracuda": "barracuda",
        "Brother": "brother",
        "Canon": "canon",
        "Check Point": "check point",
        "Cisco": "cisco",
        "Confluence": "confluence",
        "Dell": "dell",
        "Elastic": "elastic",
        "Gigabyte": "gigabyte",
        "Google": "google",
        "HP": "hp",
        "Intel": "intel",
        "Juniper": "juniper",
        "Moxa": "moxa",
        "New Relic": "new relic",
        "Oki": "oki",
        "Okta": "okta",
        "Open Text": "open text",
        "Oracle": "oracle",
        "Red Hat": "red hat",
        "Ruckus": "ruckus",
        "SAP": "sap",
        "Sage": "sage",
        "Siemens": "siemens",
        "Slack": "slack",
        "Snowflake": "snowflake",
        "Tenable": "tenable",
        "Trend Micro": "trend micro",
        "Veritas": "veritas",
        "Western Digital": "western digital",
        "Xerox": "xerox",
        "Zebra": "zebra",
    }
    SYSTEM_DICTIONARIES = ("/usr/share/dict/words", "/usr/share/dict/american-english")

    def blocks(self) -> tuple[list[str], list[str]]:
        """(case-sensitive, insensitive) as declared in the FILE — the input the gate reads."""
        return catalog_blocks(self.CATALOG)

    def system_words(self) -> set[str]:
        """The lowercase words of the OS dictionary; empty when there is none (see the SKIP)."""
        words: set[str] = set()
        for path in self.SYSTEM_DICTIONARIES:
            candidate = Path(path)
            if candidate.is_file():
                # Only lowercase entries: a Capitalized proper noun in the dictionary is a NAME,
                # not the ordinary word this rule is about.
                words |= {
                    word.strip()
                    for word in candidate.read_text(errors="replace").splitlines()
                    if word.strip().islower() and "'" not in word
                }
        return words

    def test_every_case_sensitive_name_records_its_ordinary_word(self) -> None:
        case_sensitive, _ = self.blocks()
        self.assertEqual(
            [name for name in case_sensitive if name not in self.WORD_COLLISION],
            [],
            "case-sensitive name with no recorded ordinary word — move it to block 2 or record the word",
        )
        self.assertEqual(
            [name for name in self.WORD_COLLISION if name not in case_sensitive],
            [],
            "the word map names something that is not in block 1 any more",
        )

    def test_no_case_insensitive_name_is_an_ordinary_word(self) -> None:
        """The direction that shredded reports: a word in block 2 is redacted in lowercase."""
        _, insensitive = self.blocks()
        collisions = set(self.WORD_COLLISION.values())
        for name in insensitive:
            self.assertNotIn(name.casefold(), collisions, f"{name!r} is in block 2 and is an ordinary word")

    def test_the_system_word_list_finds_no_collision_either(self) -> None:
        """The sweep for the name nobody thought about — skipped VISIBLY when there is no list."""
        words = self.system_words()
        if not words:
            self.skipTest(f"no system dictionary at {', '.join(self.SYSTEM_DICTIONARIES)}")
        print(f"{self.STNAME} gate: sweeping block 2 against {len(words)} system words", file=sys.stderr)
        _, insensitive = self.blocks()
        self.assertEqual(
            [name for name in insensitive if name.casefold() in words],
            [],
            "block 2 holds a name that is an ordinary word in the system dictionary",
        )

    # --- why the first block is case-sensitive ---


class VendorsCatalogTest(CatalogBlocksTest):
    """The shipped `catalogs/vendors.txt` — what it MUST match, and what it must NOT.

    `@match case-sensitive` on the ambiguous names is what keeps `Dell` from eating `dell'aria` and
    `Intel` from eating a line of code; whole-word anchoring keeps `Dell` out of `DellOrto`; the
    container path must never rewrite an XML ATTRIBUTE, or `urn:schemas-microsoft-com:vml` would
    become a placeholder and the package would stop being valid. One entry is a DECLARED false
    positive and is pinned as such rather than silently blessed: see the file's own header.
    """

    # `HOME/catalogs/...`: the engine's own tree — the repository when run from the repository,
    # `~/.anon` when run from the live tree, and the same file in both.
    CATALOG = HOME / "catalogs" / "vendors.txt"
    TYPE = "FORNITORE"
    STNAME = "vendors"
    MIN_SIZE = 160

    def test_the_italian_elision_is_not_redacted(self) -> None:
        for text in (
            "la configurazione dell'aria compressa",
            "i problemi dell'ufficio",
            "il colore dell'università di Bologna",
        ):
            self.assertEqual(self.matched(text), [], f"{text!r} must stay intact")

    def test_an_ordinary_word_that_shares_a_vendor_name_is_not_redacted(self) -> None:
        for text in (
            "il canon 35 della fotocamera",
            "l'axis del grafico",
            "una foglia di acer campestre",
            "il ginepro (juniper)",
            "my brother in law",
            "a tenable position",
            "the oracle said",
            "the amazon river",
            "un indizio su intel raccolto",
            "lo slack del cingolo",
        ):
            self.assertEqual(self.matched(text), [], f"{text!r} must stay intact")

    def test_a_lowercase_spelling_of_a_first_block_name_is_not_redacted(self) -> None:
        self.assertEqual(self.matched("hp 123 e un cavo incrociato"), [], "`hp` is not `HP`")
        self.assertEqual(self.matched("una mela, non una apple"), [], "`apple` is a fruit here")
        self.assertTrue(self.matched("HP LaserJet"), "the proper spelling is redacted")

    def test_no_second_block_name_is_an_ordinary_word(self) -> None:
        """Block 2 is `@match insensitive`, so an ordinary word there is redacted in lowercase.

        Six of these were found by an adversarial review AFTER the list first shipped, and `cisco`
        (a fish — and, unlike the six, an enum-listed word) plus `okta` (a cloud-cover unit) by the
        review of the fix. This pins the shapes that were found; a name added LATER is still
        unguarded, which is `docs/OPEN-ISSUES.md` #37.
        """
        for text in (
            "un gigabyte di memoria",
            "she wore a red hat",
            "avast, ye landlubbers",
            "in vino veritas",
            "il trend micro del mercato",
            "la western digital del film",
            "the cisco is a freshwater whitefish",
            "due okta di copertura nuvolosa",
        ):
            self.assertEqual(self.matched(text), [], f"{text!r} must stay intact")
        for text in (
            "un modulo da 64 Gigabyte",
            "Red Hat Enterprise Linux",
            "Avast Free Antivirus",
            "Veritas Backup Exec",
            "Trend Micro Apex One",
            "Western Digital Blue",
            "switch Cisco Catalyst",
            "Okta Identity Cloud",
        ):
            self.assertTrue(self.matched(text), f"{text!r} must be redacted")

    def test_a_vendor_inside_a_longer_word_is_not_matched(self) -> None:
        for text in ("DellOrto e figli snc", "NetgearSwitch", "il connettore HPX"):
            self.assertEqual(self.matched(text), [], f"{text!r} is one word, not a vendor")

    def test_the_elided_article_is_not_a_vendor(self) -> None:
        """`Dell'azienda` is "of the company", not the vendor Dell: an entry followed by an
        apostrophe and a letter is an elided Italian article. This was a DECLARED false positive;
        it is closed at the entity regex, so it holds for EVERY dictionary entry, not just Dell."""
        self.assertEqual(self.matched("Dell'azienda risulta in regola"), [])
        # A real reference still matches: the lookahead only fires on apostrophe + letter.
        self.assertEqual(self.matched("l'ordine per Dell è partito"), ["Dell"])
        self.assertEqual(self.matched("server Dell in sede"), ["Dell"])

    # --- the second block, and what the map does with it ---

    def test_a_second_block_name_matches_however_it_is_capitalized(self) -> None:
        for text in ("sonicwall tz", "SonicWall NSa", "SONICWALL", "un NAS Synology", "un SanDisk"):
            self.assertTrue(self.matched(text), f"{text!r} must be redacted")
        # The names whose lowercase is not a word live with the insensitive block: a lowercase
        # spelling in a hostname, a filename or a spreadsheet must still match. `un server ibm` used
        # to be a MISS (the acronyms sat in the case-sensitive block for the wrong reason).
        for text in ("aruba cloud", "kingston RAM", "eaton UPS", "un server ibm", "un server amd",
                     "un ups apc", "un server hpe"):
            self.assertTrue(self.matched(text), f"{text!r} must be redacted")

    def test_the_map_keeps_the_exact_spelling_it_found(self) -> None:
        redacted, entries, counts = anon.anonymize(
            "SonicWall in sede e sonicwall in filiale", self.entities, include_heuristics=False, tag="aaaaaa"
        )
        # Declared consequence of `@match insensitive`: one company, two spellings, two placeholders.
        # What matters is that neither original is lost, so `deanon` restores both as written.
        self.assertEqual(counts, {"FORNITORE": 2})
        self.assertEqual({entry["original"] for entry in entries.values()}, {"SonicWall", "sonicwall"})
        self.assertNotIn("sonicwall", redacted.lower())

    # --- the whole point: it is opt-in, and it is safe on a container ---

    def test_the_list_is_inert_until_it_is_selected(self) -> None:
        src = self.tmp / "nota.txt"
        src.write_text("Server Dell con backup Veeam.\n", encoding="utf-8")
        off = self.run_anon(str(src), "--check")
        self.assertEqual(off.returncode, 0, "without --catalogs the names are not findings")
        on = self.run_anon(str(src), "--catalogs", "vendors", "--check")
        self.assertEqual(on.returncode, 1, "selected, they are")

    def test_xml_namespaces_survive_the_redaction(self) -> None:
        """`urn:schemas-microsoft-com:vml` is a vendor name in an ATTRIBUTE: it must not move.

        Redacting it would replace part of a namespace URI with a placeholder — the package stops
        being valid and the document is destroyed, which is worse than any under-redaction.
        """
        docx = self.tmp / "edge.docx"
        with zipfile.ZipFile(docx, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "[Content_Types].xml",
                '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
                'content-types"><Default Extension="xml" ContentType="application/xml"/></Types>',
            )
            archive.writestr(
                "word/document.xml",
                '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
                'wordprocessingml/2006/main" xmlns:v="urn:schemas-microsoft-com:vml"><w:body>'
                "<w:p><w:r><w:t>Server Dell con Microsoft Windows e backup Veeam.</w:t></w:r></w:p>"
                "</w:body></w:document>",
            )

        out = self.tmp / "edge.redacted.docx"
        res = self.run_anon(str(docx), "--catalogs", "vendors", "--out", str(out))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertTrue(zipfile.is_zipfile(out), "the output is still a container")

        body = zipfile.ZipFile(out).read("word/document.xml").decode("utf-8")
        self.assertIn('xmlns:v="urn:schemas-microsoft-com:vml"', body, "the attribute was rewritten")
        self.assertIn("schemas.openxmlformats.org", body)
        ElementTree.fromstring(body)  # the part is still well-formed XML

        text = anon.container_text(out)
        for value in ("Dell", "Microsoft", "Veeam"):
            self.assertNotIn(value, text, f"{value} survived in the visible text")
        self.assertIn("[FORNITORE-1-", text)




class ProductsCatalogTest(CatalogBlocksTest):
    """The shipped `catalogs/products.txt` — the same gate, on the list where it matters more.

    A product name is very often an ordinary English word (`Word`, `Excel`, `Access`, `Windows`,
    `Teams`, `Catalyst`, `Nexus`, `Umbrella`, `Tomcat`, `Apache`, `Docker`, `Zoom`), so block 1 is
    the majority of the work here and the gate is the same one. Two things are pinned in addition:
    the product name is not a vendor name (the two lists must not disagree), and a model number is
    NOT in the list by design (the family is; the number is a hostname).
    """

    CATALOG = HOME / "catalogs" / "products.txt"
    TYPE = "PRODOTTO"
    STNAME = "products"
    MIN_SIZE = 120
    WORD_COLLISION = {
        "Access": "access",
        "Android": "android",
        "Apache": "apache",
        "Azure": "azure",
        "Catalyst": "catalyst",
        "Chrome": "chrome",
        "Defender": "defender",
        "Docker": "docker",
        "Exchange": "exchange",
        "Excel": "excel",
        "Falcon": "falcon",
        "Fedora": "fedora",
        "Firebox": "firebox",
        "Firepower": "firepower",
        "Helm": "helm",
        "Horizon": "horizon",
        "Jabber": "jabber",
        "Nexus": "nexus",
        "Outlook": "outlook",
        "Thunderbird": "thunderbird",
        "Rancher": "rancher",
        "Safari": "safari",
        "Teams": "teams",
        "Tomcat": "tomcat",
        "Umbrella": "umbrella",
        "Windows": "windows",
        "Word": "word",
        "Zoom": "zoom",
    }

    def test_the_two_lists_do_not_disagree_about_a_name(self) -> None:
        """A company name lives in `vendors.txt`; a product name here. Never both.

        A name in both would give the SAME surface two types depending on which list was ticked
        first, and the map would say `FORNITORE` in one document and `PRODOTTO` in the next.
        """
        vendor_catalog = self.CATALOG.parent / "vendors.txt"
        if not vendor_catalog.is_file():
            self.skipTest("vendors.txt is not installed next to this catalog")
        vendors = {name.casefold() for name in sum(catalog_blocks(vendor_catalog), [])}
        first, second = self.blocks()
        overlap = sorted(name for name in first + second if name.casefold() in vendors)
        self.assertEqual(overlap, [], "a name is in both catalogs — one of them must drop it")

    def test_a_model_number_is_not_in_the_list(self) -> None:
        """The family is here, the SKU is not: it changes every quarter and arrives as a hostname."""
        _, insensitive = self.blocks()
        self.assertNotIn("R740", insensitive)
        self.assertNotIn("DL380", insensitive)
        # ...but the families that carry them are, so a report names them whatever the generation.
        for family in ("PowerEdge", "ProLiant"):
            self.assertIn(family, insensitive)

    def test_an_ordinary_word_is_not_eaten(self) -> None:
        for text in (
            "il catalizzatore (catalyst) della reazione",
            "l'umbrella dell'ombrello",
            "a word about words",
            "le finestre (windows) della casa",
            "una tazza (cup) sul tavolo",
        ):
            self.assertEqual(self.matched(text), [], f"{text!r} must stay intact")

    def test_the_product_itself_is_redacted(self) -> None:
        for text in ("un firewall FortiGate 60F", "un server PowerEdge", "VMware vSphere",
                     "Windows Server 2022", "un cluster Kubernetes", "Microsoft Office 365"):
            self.assertTrue(self.matched(text), f"{text!r} must be redacted")

    def test_the_list_is_inert_until_it_is_selected(self) -> None:
        src = self.tmp / "nota.txt"
        src.write_text("Un firewall FortiGate e vSphere.\n", encoding="utf-8")
        self.assertEqual(self.run_anon(str(src), "--check").returncode, 0,
                         "without --catalogs the product names are not findings")
        self.assertEqual(self.run_anon(str(src), "--catalogs", "products", "--check").returncode, 1)


class AuditTest(unittest.TestCase):
    """`--audit`: is this file REALLY anonymized? The report must not leak what it checks."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="anon-audit-"))
        self.entities = self.tmp / "entities.txt"
        self.entities.write_text("AZIENDA|Contoso\n", encoding="utf-8")
        self.env = {**os.environ, "ANON_HOME": str(self.tmp)}

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def audit(self, name: str, content: str, *extra: str) -> subprocess.CompletedProcess[str]:
        path = self.tmp / name
        path.write_text(content, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(ANON_PY), str(path), "--audit", "--json",
             "--entities", str(self.entities), *extra],
            capture_output=True, text=True, env=self.env, check=False,
        )

    def test_clean_file(self) -> None:
        res = self.audit("clean.txt", "Solo note tecniche sul firewall perimetrale.\n")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(json.loads(res.stdout)["verdict"], "clean")

    def test_sensitive_file_reports_without_echoing_values(self) -> None:
        res = self.audit("bad.txt", "Referente mario.rossi@contoso.it su 10.42.7.19\n")
        self.assertEqual(res.returncode, 1)
        report = json.loads(res.stdout)
        self.assertEqual(report["verdict"], "sensitive")
        self.assertEqual(report["types"], {"EMAIL": 1, "IP": 1})
        self.assertNotIn("contoso.it", res.stdout, "the audit must not leak what it detected")
        self.assertNotIn("10.42.7.19", res.stdout)

    def test_variant_candidate_is_flagged_and_masked(self) -> None:
        res = self.audit("variant.txt", "Il cliente Con Toso Srl ha tre stabilimenti.\n")
        self.assertEqual(res.returncode, 4, res.stdout + res.stderr)
        report = json.loads(res.stdout)
        self.assertEqual(report["verdict"], "suspicious")
        self.assertEqual(report["near_miss"][0]["kind"], "variant")
        self.assertNotIn("Con Toso", res.stdout)
        self.assertNotIn("Contoso", res.stdout, "entity names are sensitive too")
        self.assertIn("\u2022", report["near_miss"][0]["token_masked"])

    def test_reveal_shows_the_candidates(self) -> None:
        res = self.audit("variant2.txt", "Il cliente Con Toso Srl ha tre stabilimenti.\n", "--reveal")
        self.assertEqual(res.returncode, 4)
        report = json.loads(res.stdout)
        self.assertTrue(report["revealed"])
        self.assertEqual(report["near_miss"][0]["token"], "Con Toso")
        self.assertEqual(report["near_miss"][0]["entity"], "Contoso")

    def test_binary_document_is_refused(self) -> None:
        path = self.tmp / "doc.docx"
        path.write_bytes(b"PK\x03\x04" + bytes(64))
        res = subprocess.run(
            [sys.executable, str(ANON_PY), str(path), "--audit"],
            capture_output=True, text=True, env=self.env, check=False,
        )
        self.assertEqual(res.returncode, 2)
        self.assertIn("binary", res.stderr)


class StructuredFormatTest(unittest.TestCase):
    """JSON/YAML: keys as well as values, and the round-trip must keep the syntax intact."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="anon-structured-"))
        self.entities = self.tmp / "entities.txt"
        self.entities.write_text("AZIENDA|Contoso\nPERSONA|Mario Rossi\n", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, *args], capture_output=True, text=True, check=False)

    def test_json_keys_and_values_and_roundtrip(self) -> None:
        source = self.tmp / "data.json"
        original = (
            '{\n  "Contoso S.r.l.": {\n    "referente": "Mario Rossi",\n'
            '    "email": "mario.rossi@contoso.it",\n    "nota": "quote \\"interna\\" ok"\n  }\n}\n'
        )
        source.write_text(original, encoding="utf-8")
        redacted = self.tmp / "data.redacted.json"
        run = self._run(str(ANON_PY), str(source), "--entities", str(self.entities),
                        "--out", str(redacted), "--map", str(self.tmp / "m.json"), "--quiet", "--tag", "aaaa")
        self.assertEqual(run.returncode, 0, run.stderr)
        text = redacted.read_text(encoding="utf-8")
        json.loads(text)  # the syntax must survive redaction, keys included
        self.assertNotIn("Contoso", text)
        self.assertNotIn("Mario Rossi", text)
        self.assertNotIn("contoso.it", text)

        back = self.tmp / "data.back.json"
        de = self._run(str(DEANON_PY), str(redacted), str(self.tmp / "m.json"), "--out", str(back), "--quiet")
        self.assertEqual(de.returncode, 0, de.stderr)
        self.assertEqual(back.read_text(encoding="utf-8"), original)

    def test_yaml_values_and_keys(self) -> None:
        source = self.tmp / "data.yaml"
        source.write_text("Contoso S.r.l.:\n  referente: Mario Rossi\n  email: mario@contoso.it\n", encoding="utf-8")
        out = self.tmp / "data.redacted.yaml"
        run = self._run(str(ANON_PY), str(source), "--entities", str(self.entities),
                        "--out", str(out), "--map", str(self.tmp / "y.json"), "--quiet", "--tag", "aaaa")
        self.assertEqual(run.returncode, 0, run.stderr)
        text = out.read_text(encoding="utf-8")
        self.assertIn("[AZIENDA-1-aaaa]:", text)
        self.assertNotIn("Mario Rossi", text)

    def test_every_json_output_carries_the_schema(self) -> None:
        source = self.tmp / "x.txt"
        source.write_text("mail mario.rossi@contoso.it\n", encoding="utf-8")
        outputs = [
            self._run(str(ANON_PY), str(source), "--entities", str(self.entities),
                      "--out", str(self.tmp / "x.red.txt"), "--map", str(self.tmp / "x.json"), "--json"),
            self._run(str(ANON_PY), str(source), "--check", "--json", "--entities", str(self.entities)),
            self._run(str(ANON_PY), str(source), "--audit", "--json", "--entities", str(self.entities)),
        ]
        for result in outputs:
            payload = json.loads(result.stdout)
            self.assertEqual(payload["schema"], anon.SCHEMA)
            self.assertIn("tool", payload)
            self.assertIn("version", payload)


class DeanonContainerTest(unittest.TestCase):
    """Office containers (docx/odt) are ZIPs of XML parts: deanon must rewrite them in place."""

    CONTENT_TYPES = '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'
    ENTRIES = {
        "[EMAIL-1]": {"type": "EMAIL", "original": "mario.rossi@contoso.it"},
        "[AZIENDA-1]": {"type": "AZIENDA", "original": "Contoso S.r.l."},
        "[IP-1]": {"type": "IP", "original": "10.42.7.19"},
    }

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="anon-deanon-"))
        self.map = self.tmp / "m.map.json"
        self.map.write_text(json.dumps({"entries": self.ENTRIES}), encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _para(text: str) -> str:
        return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"

    def _make_docx(self, name: str, parts: dict[str, str]) -> Path:
        import zipfile

        path = self.tmp / name
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", self.CONTENT_TYPES, zipfile.ZIP_DEFLATED)
            for part, xml in parts.items():
                archive.writestr(part, xml, zipfile.ZIP_DEFLATED)
        return path

    def _deanon(self, path: Path, out_name: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(DEANON_PY), str(path), str(self.map),
             "--out", str(self.tmp / out_name), "--json"],
            capture_output=True, text=True, check=False,
        )

    @staticmethod
    def _visible(data: bytes) -> str:
        import re

        return re.sub(r"<[^>]*>", "", data.decode("utf-8", "replace"))

    def test_all_parts_are_rewritten(self) -> None:
        docx = self._make_docx("parts.docx", {
            "word/document.xml": f'<?xml version="1.0"?><w:document xmlns:w="x"><w:body>{self._para("Corpo: [EMAIL-1] su [IP-1]")}</w:body></w:document>',
            "word/header1.xml": f'<?xml version="1.0"?><w:hdr xmlns:w="x">{self._para("H: [AZIENDA-1]")}</w:hdr>',
            "word/footer1.xml": f'<?xml version="1.0"?><w:ftr xmlns:w="x">{self._para("F: [EMAIL-1]")}</w:ftr>',
            "docProps/core.xml": '<?xml version="1.0"?><cp:coreProperties xmlns:cp="y"><cp:lastModifiedBy>[AZIENDA-1]</cp:lastModifiedBy></cp:coreProperties>',
        })
        res = self._deanon(docx, "parts.deanon.docx")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        report = json.loads(res.stdout)
        self.assertTrue(report["complete"])
        self.assertEqual(report["replaced"], 5)
        for part in ("word/document.xml", "word/header1.xml", "word/footer1.xml", "docProps/core.xml"):
            self.assertIn(part, report["parts"])

        import zipfile

        with zipfile.ZipFile(self.tmp / "parts.deanon.docx") as archive:
            self.assertIsNone(archive.testzip(), "the rewritten archive must be readable")
            body = archive.read("word/document.xml").decode()
            self.assertIn("mario.rossi@contoso.it", body)
            self.assertNotIn("[EMAIL-1]", self._visible(archive.read("word/document.xml")))

    def test_split_placeholder_is_repaired_without_touching_the_markup(self) -> None:
        """Word splits a placeholder across runs; the value goes in the FIRST fragment.

        Merging the runs would move whatever formatting they carry, so the repair never touches
        markup: it writes the value where the first fragment was and empties the others. The
        paragraph structure must therefore come out identical, and the part well-formed.

        This supersedes `..._is_reported_not_silently_ignored`: that behavior (report and exit 3)
        was the honest workaround while a repair was risky, not the goal.
        """
        # The split is between two RUNS of one paragraph — which is what Word does to a token when
        # formatting changes. An earlier version of this fixture put the two halves in two
        # PARAGRAPHS and still expected a repair: that is a different case, and repairing it moved
        # the value into the first paragraph and emptied the second (see the test below).
        docx = self._make_docx("split.docx", {
            "word/document.xml": '<?xml version="1.0"?><w:document xmlns:w="x"><w:body>'
            + '<w:p><w:r><w:t>Referente: [EMAIL-</w:t></w:r><w:r><w:rPr><w:sz w:val="18"/>'
            + "</w:rPr><w:t>1] fine</w:t></w:r></w:p></w:body></w:document>",
        })
        res = self._deanon(docx, "split.deanon.docx")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        report = json.loads(res.stdout)
        self.assertTrue(report["complete"])
        self.assertEqual(report["repaired"], 1)
        self.assertEqual(report["remaining"], 0)

        import xml.etree.ElementTree as ET
        import zipfile

        with zipfile.ZipFile(self.tmp / "split.deanon.docx") as archive:
            self.assertIsNone(archive.testzip(), "the rewritten archive must be readable")
            raw = archive.read("word/document.xml")
            body = raw.decode()
            ET.fromstring(raw)  # a repair must never break well-formedness
            visible = self._visible(raw)
            self.assertNotIn("[EMAIL-", visible)
            self.assertIn("fine", visible)
            # The count is compared with the INPUT, not with a number that belonged to an older
            # fixture: "untouched" means unchanged, whatever the document had.
            with zipfile.ZipFile(docx) as original:
                before = original.read("word/document.xml").decode()
            self.assertEqual(body.count("<w:p>"), before.count("<w:p>"),
                             "the repair must not add or remove a paragraph")
            self.assertEqual(body.count("<w:r>"), before.count("<w:r>"),
                             "nor a run: the emptied fragment stays where it was")

    def test_a_placeholder_split_across_two_paragraphs_is_not_repaired(self) -> None:
        """The mirrored defect: emptying "the other fragments" across a CONTAINER boundary.

        The repair puts the value in the first fragment and empties the rest, which is right between
        two runs of one sentence and destructive between two paragraphs: the value would come back in
        the wrong paragraph and the second one would lose its text. The same boundary rule as the
        redaction decides it, from the same code — and leaving it unrepaired is loud, because the
        verdict is computed from the output.
        """
        docx = self._make_docx("due-paragrafi.docx", {
            "word/document.xml": '<?xml version="1.0"?><w:document xmlns:w="x"><w:body>'
            + self._para("Prima riga [EMAIL-") + self._para("1] e qui il testo che non e' del valore")
            + "</w:body></w:document>",
        })
        res = self._deanon(docx, "due-paragrafi.deanon.docx")
        self.assertEqual(res.returncode, 3, "a cross-container split must not be repaired silently")
        report = json.loads(res.stdout)
        self.assertFalse(report["complete"])
        self.assertEqual(report["repaired"], 0)
        self.assertGreaterEqual(report["remaining"], 1, "the placeholder is still there, and reported")
        import zipfile

        with zipfile.ZipFile(self.tmp / "due-paragrafi.deanon.docx") as archive:
            body = archive.read("word/document.xml").decode()
        self.assertIn("e qui il testo che non e' del valore", body,
                      "the second paragraph's text must not be deleted")

    def test_deanon_reports_a_text_part_it_could_not_read(self) -> None:
        """The verdict is computed from the OUTPUT: a part nobody could read is not "clean".

        Otherwise a document carrying a placeholder in a part we cannot decode would report
        `complete: true` — a claim about a file we did not fully look at.
        """
        docx = self._make_docx("illeggibile.docx", {
            "word/document.xml": '<?xml version="1.0"?><w:document xmlns:w="x"><w:body>'
            + self._para("[EMAIL-1] ecco") + "</w:body></w:document>",
        })
        import zipfile

        with zipfile.ZipFile(docx, "a", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/legacy.vml", bytes(range(256)) * 8)
        res = self._deanon(docx, "illeggibile.deanon.docx")
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        report = json.loads(res.stdout)
        self.assertFalse(report["complete"])
        self.assertIn("word/legacy.vml", report["unreadable_parts"])

    def test_fragment_across_two_parts_is_still_reported(self) -> None:
        """A placeholder split between two PARTS cannot be repaired, and must stay fail-closed."""
        docx = self._make_docx("crosspart.docx", {
            "word/document.xml": '<?xml version="1.0"?><w:document xmlns:w="x"><w:body>'
            + self._para("[EMAIL-") + "</w:body></w:document>",
            "word/header1.xml": '<?xml version="1.0"?><w:hdr xmlns:w="x">'
            + self._para("1] nell'header") + "</w:hdr>",
        })
        res = self._deanon(docx, "crosspart.deanon.docx")
        self.assertEqual(res.returncode, 3, "an unrepairable split must not report success")
        self.assertIn("NOTHING RESTORED", res.stderr)

    def test_odf_mimetype_stays_first_and_stored(self) -> None:
        import zipfile

        odt = self.tmp / "doc.odt"
        with zipfile.ZipFile(odt, "w") as archive:
            archive.writestr(zipfile.ZipInfo("mimetype"), "application/vnd.oasis.opendocument.text", zipfile.ZIP_STORED)
            archive.writestr("content.xml", "<office:document-content>[AZIENDA-1]</office:document-content>", zipfile.ZIP_DEFLATED)
        res = self._deanon(odt, "doc.deanon.odt")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        with zipfile.ZipFile(self.tmp / "doc.deanon.odt") as archive:
            info = archive.infolist()[0]
            self.assertEqual(info.filename, "mimetype", "ODF requires mimetype first")
            self.assertEqual(info.compress_type, zipfile.ZIP_STORED, "mimetype must stay uncompressed")
            self.assertIn("Contoso", archive.read("content.xml").decode())

    def test_unknown_placeholder_is_reported_and_fails_closed(self) -> None:
        """A token the map does not know is left as-is AND makes the run incomplete.

        It is not "data to restore", but it IS a placeholder still visible in the document: the
        likely cause is a map from a different run, and delivering that silently would mix one
        client's values into another's document.
        """
        docx = self._make_docx("unknown.docx", {
            "word/document.xml": '<?xml version="1.0"?><w:document xmlns:w="x"><w:body>'
            + self._para("[EMAIL-1] e [EMAIL-9]") + "</w:body></w:document>",
        })
        res = self._deanon(docx, "unknown.deanon.docx")
        self.assertEqual(res.returncode, 3, "an unresolvable placeholder must not report success")
        report = json.loads(res.stdout)
        self.assertEqual(report["replaced"], 1)
        self.assertEqual(report["unknown_placeholders"], 1)
        self.assertFalse(report["complete"])
        self.assertIn("not in this map", res.stderr)

    def test_document_without_any_placeholder_is_reported(self) -> None:
        """A silent no-op is a failure: the delivery would carry placeholders or miss values."""
        docx = self._make_docx("clean.docx", {
            "word/document.xml": '<?xml version="1.0"?><w:document xmlns:w="x"><w:body>'
            + self._para("nessun placeholder qui") + "</w:body></w:document>",
        })
        res = self._deanon(docx, "clean.deanon.docx")
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        self.assertEqual(json.loads(res.stdout)["complete"], False)
        self.assertIn("NOTHING RESTORED", res.stderr)

    def test_xml_metacharacters_in_a_value_do_not_corrupt_the_document(self) -> None:
        """`Acme & Soehne` must land as `Acme &amp; Soehne`, or Word refuses to open the file."""
        self.map.write_text(json.dumps({"entries": {
            "[AZIENDA-1]": {"type": "AZIENDA", "original": "Acme & Söhne GmbH"},
            "[PERSONA-1]": {"type": "PERSONA", "original": 'A <B> & "C"'},
        }}), encoding="utf-8")
        docx = self._make_docx("escape.docx", {
            # [PERSONA-1] sits in an ATTRIBUTE value: an unescaped `"` there is what breaks XML.
            "word/document.xml": '<?xml version="1.0"?><w:document xmlns:w="x">'
            + '<w:p w:rsidR="[PERSONA-1]">'
            + self._para("[AZIENDA-1] e [PERSONA-1]") + "</w:p></w:document>",
        })
        res = self._deanon(docx, "escape.deanon.docx")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        import xml.dom.minidom
        import zipfile

        with zipfile.ZipFile(self.tmp / "escape.deanon.docx") as archive:
            raw = archive.read("word/document.xml").decode()
            xml.dom.minidom.parseString(raw)  # raises if the part is malformed
            self.assertIn("Acme &amp; Söhne GmbH", raw)
            self.assertIn("&quot;C&quot;", raw, "quotes must be escaped too (attribute values)")
            self.assertNotIn("<B>", raw)

    def test_embedded_text_parts_are_rewritten_and_verified(self) -> None:
        """A placeholder in a non-.xml part (an embedded note) must not survive silently."""
        docx = self._make_docx("embedded.docx", {
            "word/document.xml": '<?xml version="1.0"?><w:document xmlns:w="x"><w:body>'
            + self._para("corpo ok") + "</w:body></w:document>",
            "word/embeddings/note.txt": "il nome reale e' [EMAIL-1] qui\n",
        })
        res = self._deanon(docx, "embedded.deanon.docx")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        import zipfile

        with zipfile.ZipFile(self.tmp / "embedded.deanon.docx") as archive:
            self.assertIn("mario.rossi@contoso.it", archive.read("word/embeddings/note.txt").decode())

    def test_utf16_part_is_handled(self) -> None:
        """OOXML allows UTF-16 parts; decoded as UTF-8 the placeholder is NUL-interleaved."""
        body = '<?xml version="1.0" encoding="UTF-16"?><w:document xmlns:w="x"><w:body>'
        body += self._para("Corpo: [EMAIL-1]") + "</w:body></w:document>"
        docx = self._make_docx("utf16.docx", {"word/document.xml-raw": body})
        # rewrite that part as real UTF-16LE+BOM bytes
        import zipfile

        with zipfile.ZipFile(docx) as archive:
            items = [(i, archive.read(i)) for i in archive.infolist()]
        with zipfile.ZipFile(docx, "w") as archive:
            for info, blob in items:
                if info.filename == "word/document.xml-raw":
                    archive.writestr("word/document.xml", body.encode("utf-16"))
                else:
                    archive.writestr(info, blob)
        res = self._deanon(docx, "utf16.deanon.docx")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        with zipfile.ZipFile(self.tmp / "utf16.deanon.docx") as archive:
            decoded = archive.read("word/document.xml").decode("utf-16")
            self.assertIn("mario.rossi@contoso.it", decoded)
            self.assertNotIn("[EMAIL-1]", decoded)

    def test_empty_document_is_an_error(self) -> None:
        empty = self.tmp / "empty.docx"
        empty.write_bytes(b"")
        res = self._deanon(empty, "empty.deanon.docx")
        self.assertEqual(res.returncode, 2)
        self.assertFalse((self.tmp / "empty.deanon.docx").exists())

    def test_a_wrong_shaped_map_is_a_clean_error(self) -> None:
        """A hand-edited map must reach `deanon: exit 2`, never an unhandled AttributeError."""
        document = self.tmp / "doc.txt"
        document.write_text("Referente: [EMAIL-1]\n", encoding="utf-8")
        for shape in ("[1, 2, 3]", '"solo una stringa"', '{"entries": 5}', '{"entries": {"[EMAIL-1]": null}}'):
            broken = self.tmp / f"broken-{len(shape)}.map.json"
            broken.write_text(shape, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(DEANON_PY), str(document), str(broken), "--json"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 2, f"{shape!r}: {result.stdout} {result.stderr}")
            self.assertNotIn("Traceback", result.stderr)

    def test_a_deeply_nested_map_is_a_clean_error(self) -> None:
        """`json.loads` raises RecursionError on a nested file: exit 2, never a traceback."""
        document = self.tmp / "doc.md"
        document.write_text("Referente: [EMAIL-1]\n", encoding="utf-8")
        nested = self.tmp / "nested.map.json"
        # ~200k deep: the depth at which `json.loads` raises RecursionError (measured), which is
        # NOT a ValueError and used to escape as an unhandled traceback.
        nested.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(DEANON_PY), str(document), str(nested), "--json"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 2, result.stderr[:300])
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("malformed JSON", result.stderr)

    def test_the_listing_counts_without_copying_the_entries(self) -> None:
        """The listing needs a count, not a validated copy of every entry (O(N) allocations)."""
        entries = {
            f"[EMAIL-{index}-aaaaaa]": {"type": "EMAIL", "original": f"utente{index}@cliente{index}.it"}
            for index in range(1, 5001)
        }
        path = self.tmp / "grande.map.json"
        path.write_text(json.dumps({"entries": entries}), encoding="utf-8")
        metadata = deanon.load_map_metadata(path)
        self.assertEqual(metadata["entries"], 5000)
        # The contract the listing depends on: a validated COUNT, with no copy of the entries.
        self.assertIsInstance(deanon._count_entries(path, json.loads(path.read_text())["entries"]), int)

    def test_a_wrong_typed_filter_field_is_a_clean_error(self) -> None:
        """`catalogs: 5` is a malformed request, not a TypeError out of an iteration."""
        with self.assertRaises(ValueError):
            anon._as_list(5)
        with self.assertRaises(ValueError):
            anon._as_list(True)
        self.assertEqual(anon._as_list(["a", " b "]), ["a", "b"])
        self.assertEqual(anon._as_list("a,b"), ["a", "b"])
        self.assertEqual(anon._as_list(None), [])

    def test_output_in_place_is_atomic_and_safe(self) -> None:
        docx = self._make_docx("inplace.docx", {
            "word/document.xml": '<?xml version="1.0"?><w:document xmlns:w="x"><w:body>'
            + self._para("[EMAIL-1]") + "</w:body></w:document>",
        })
        res = subprocess.run(
            [sys.executable, str(DEANON_PY), str(docx), str(self.map), "--out", str(docx), "--quiet"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        import zipfile

        with zipfile.ZipFile(docx) as archive:
            self.assertIn("mario.rossi@contoso.it", archive.read("word/document.xml").decode())
            self.assertEqual(sorted(n for n in archive.namelist() if "tmp-" in n), [])

    def test_entry_order_and_compression_are_preserved(self) -> None:
        import zipfile

        path = self.tmp / "order.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", self.CONTENT_TYPES, zipfile.ZIP_STORED)
            archive.writestr("word/document.xml", f'<?xml version="1.0"?><w:document xmlns:w="x"><w:body>{self._para("[EMAIL-1]")}</w:body></w:document>', zipfile.ZIP_DEFLATED)
            archive.writestr("zzz/last.txt", "coda\n", zipfile.ZIP_STORED)
        before = [(i.filename, i.compress_type) for i in zipfile.ZipFile(path).infolist()]
        res = self._deanon(path, "order.deanon.docx")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        after = [(i.filename, i.compress_type) for i in zipfile.ZipFile(self.tmp / "order.deanon.docx").infolist()]
        self.assertEqual(before, after)

    def test_a_non_zip_with_zip_magic_is_an_error(self) -> None:
        broken = self.tmp / "broken.docx"
        broken.write_bytes(b"PK\x03\x04" + b"not really a zip" * 10)
        res = self._deanon(broken, "broken.deanon.docx")
        self.assertEqual(res.returncode, 2, "must fail loudly, not write a corrupt file")
        self.assertFalse((self.tmp / "broken.deanon.docx").exists())

    @unittest.skipUnless(shutil.which("pandoc"), "pandoc not installed")
    def test_end_to_end_pipeline_is_lossless(self) -> None:
        """The real flow: source -> anon -> (work) -> pandoc docx -> deanon == source text."""
        source = self.tmp / "source.md"
        source.write_text(
            "# Report\n\nCliente Contoso S.r.l., referente mario.rossi@contoso.it, server 10.42.7.19.\n",
            encoding="utf-8",
        )
        run = subprocess.run(
            [sys.executable, str(ANON_PY), str(source), "--out", str(self.tmp / "redacted.md"),
             "--map", str(self.tmp / "flow.map.json"), "--quiet"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        redacted = (self.tmp / "redacted.md").read_text(encoding="utf-8")
        self.assertNotIn("contoso.it", redacted)

        docx = self.tmp / "final.docx"
        subprocess.run(["pandoc", str(self.tmp / "redacted.md"), "-o", str(docx)], check=True)
        de = subprocess.run(
            [sys.executable, str(DEANON_PY), str(docx), str(self.tmp / "flow.map.json"),
             "--out", str(self.tmp / "final.deanon.docx"), "--quiet"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(de.returncode, 0, de.stderr)
        back = subprocess.run(
            ["pandoc", str(self.tmp / "final.deanon.docx"), "-t", "plain"],
            capture_output=True, text=True, check=True,
        ).stdout
        for value in ("Contoso S.r.l.", "mario.rossi@contoso.it", "10.42.7.19"):
            self.assertIn(value, back, "the real value must be back in the delivered document")


class TagAllocatorTest(unittest.TestCase):
    """The per-map tag must be unique among the maps that EXIST, not merely "probably unique".

    A collision makes a wrong map resolve the placeholders silently — the exact failure the tag
    was introduced to prevent (DEC-0012 §4).
    """

    def test_the_allocator_retries_a_taken_tag_and_stays_inside_the_placeholder_syntax(self) -> None:
        # Deterministic: with urandom pinned to zeros the only candidate it can produce is "000000".
        with mock.patch.object(anon.os, "urandom", lambda n: b"\x00" * n):
            self.assertEqual(anon.new_tag(), "000000")
            widened = anon.new_tag({"000000"})
        self.assertNotEqual(widened, "000000", "a taken tag must not be reused")
        self.assertLessEqual(len(widened), 8, "the widening must stay inside the placeholder syntax")

    def test_existing_tags_reads_the_directory_and_skips_unreadable_maps(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="anon-tags-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "a.map.json").write_text('{"tag": "aaaaaa"}\n', encoding="utf-8")
        (tmp / "b.map.json").write_text('{"tag": "bbbbbb"}\n', encoding="utf-8")
        (tmp / "no-tag.map.json").write_text('{"entries": {}}\n', encoding="utf-8")
        (tmp / "broken.map.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(anon.existing_tags(tmp), {"aaaaaa", "bbbbbb"})
        self.assertEqual(anon.existing_tags(tmp / "missing"), set(), "a missing dir is not an error")

    def test_a_generated_tag_is_never_one_of_the_existing_ones(self) -> None:
        taken = {anon.new_tag() for _ in range(200)}
        self.assertNotIn(anon.new_tag(taken), taken)

    def test_allocate_tag_scans_every_directory_it_is_given(self) -> None:
        # Deterministic: with urandom pinned, the only 6-hex candidate is "abc123", so a map that
        # already holds it forces the allocator to widen — and it only sees the maps in the dirs it
        # was given, which is exactly why main() passes both the default and the destination.
        tmp = Path(tempfile.mkdtemp(prefix="anon-alloc-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        default, elsewhere = tmp / "default", tmp / "elsewhere"
        for directory in (default, elsewhere):
            directory.mkdir()
        (elsewhere / "m.map.json").write_text('{"tag": "abc123"}\n', encoding="utf-8")
        pinned_urandom = lambda n: (b"\xab\xc1\x23" + b"\x00" * n)[:n]  # noqa: E731
        with mock.patch.object(anon.os, "urandom", pinned_urandom):
            self.assertEqual(anon.allocate_tag([default]), "abc123", "invisible in the other dir")
            widened = anon.allocate_tag([default, elsewhere])
        self.assertNotEqual(widened, "abc123", "a tag used in EITHER directory must be avoided")
        self.assertEqual(len(widened), 8, "the fallback widens within the placeholder syntax")

    def test_a_pinned_tag_is_used_verbatim(self) -> None:
        self.assertEqual(anon.allocate_tag([Path("/nonexistent")], "pinned"), "pinned")


class BatchTest(unittest.TestCase):
    """`--dry-run` and `--batch`: the folder workflow, without a source ever being touched."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="anon-batch-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        (self.home / "entities.txt").write_text("AZIENDA|Acme\nPERSONA|Mario Rossi\n", encoding="utf-8")
        self.folder = self.tmp / "cliente"
        (self.folder / "sub").mkdir(parents=True)
        self.env = {**os.environ, "ANON_HOME": str(self.home)}
        (self.folder / "verbale.txt").write_text(
            "Cliente Acme, referente Mario Rossi <mario.rossi@acme.it>.\n", encoding="utf-8"
        )
        (self.folder / "sub" / "nota.md").write_text("nessun dato qui\n", encoding="utf-8")
        (self.folder / "allegato.docx").write_bytes(b"PK\x03\x04" + b"\x07" * 64)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_anon(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ANON_PY), *args],
            capture_output=True, text=True, env=self.env, check=False,
        )

    def snapshot(self) -> set[str]:
        return {str(p.relative_to(self.tmp)) for p in self.tmp.rglob("*") if p.is_file()}

    def test_dry_run_reports_the_plan_and_writes_nothing(self) -> None:
        src = self.folder / "verbale.txt"
        before = self.snapshot()
        res = self.run_anon(str(src), "--dry-run", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.snapshot(), before, "a dry run must not create a single file")
        report = json.loads(res.stdout)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["entries"], 3)  # AZIENDA, PERSONA, EMAIL
        self.assertTrue(report["would_write"].endswith("verbale.redacted.txt"))
        self.assertFalse((src.parent / "verbale.redacted.txt").exists())

    def test_batch_redacts_text_and_skips_binaries_and_its_own_outputs(self) -> None:
        first = self.run_anon(str(self.folder), "--batch")
        self.assertEqual(first.returncode, 0, first.stderr)
        redacted = (self.folder / "verbale.txt").parent / "verbale.redacted.txt"
        self.assertTrue(redacted.is_file())
        self.assertNotIn("mario.rossi@acme.it", redacted.read_text(encoding="utf-8"))
        # the source is untouched, byte for byte
        self.assertIn("mario.rossi@acme.it", (self.folder / "verbale.txt").read_text(encoding="utf-8"))
        self.assertFalse((self.folder / "allegato.redacted.docx").exists(), "a binary is never copied")
        self.assertIn("skipped", first.stdout)

        second = self.run_anon(str(self.folder), "--batch")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("own output", second.stdout, "re-running must not re-anonymize the outputs")

    def test_batch_dry_run_writes_nothing_at_all(self) -> None:
        before = self.snapshot()
        res = self.run_anon(str(self.folder), "--batch", "--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("would write", res.stdout)

    def test_batch_check_is_a_folder_gate(self) -> None:
        res = self.run_anon(str(self.folder), "--batch", "--check")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("SENSITIVE", res.stdout)
        self.assertNotIn("mario.rossi@acme.it", res.stdout, "the verdict never echoes the value")
        self.assertFalse((self.folder / "verbale.redacted.txt").exists())

    def test_batch_writes_into_a_separate_output_directory(self) -> None:
        out = self.tmp / "out"
        res = self.run_anon(str(self.folder), "--batch", "--out", str(out))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((out / "verbale.redacted.txt").is_file())
        self.assertFalse((self.folder / "verbale.redacted.txt").exists(), "the source folder stays clean")

    def test_batch_refuses_what_it_cannot_do_safely(self) -> None:
        inside = self.run_anon(str(self.home), "--batch")
        self.assertEqual(inside.returncode, 2)
        self.assertIn("private store", inside.stderr)
        self.assertEqual(self.run_anon(str(self.folder), "--batch", "--stdout").returncode, 2)
        self.assertEqual(self.run_anon(str(self.folder), "--batch", "--map", "/tmp/x.json").returncode, 2)
        self.assertEqual(self.run_anon(str(self.folder / "verbale.txt"), "--batch").returncode, 2)


    def test_batch_check_fails_closed_on_an_unscannable_file(self) -> None:
        """A folder whose only content is a .docx is NOT clean: the redaction never saw it."""
        only_binary = self.tmp / "solo_binari"
        only_binary.mkdir()
        (only_binary / "verbale.docx").write_bytes(b"PK\x03\x04" + b"\x07" * 64)
        res = self.run_anon(str(only_binary), "--batch", "--check")
        self.assertEqual(res.returncode, 1, "an unscannable folder must not exit 0")
        self.assertIn("non scansionabile", res.stdout)

    def test_stdout_still_writes_the_map(self) -> None:
        """`--stdout` redirects the text; it must not opt out of the map, or the pipeline cannot
        be reversed."""
        src = self.folder / "verbale.txt"
        res = self.run_anon(str(src), "--stdout")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("[AZIENDA-1-", res.stdout)
        maps = list((self.home / "maps").glob("*.map.json"))
        self.assertEqual(len(maps), 1, "the map must exist even when the text went to stdout")

    def test_is_own_output_is_anchored_and_case_insensitive(self) -> None:
        self.assertTrue(anon._is_own_output("verbale.redacted.md"))
        self.assertTrue(anon._is_own_output("VERBALE.REDACTED.MD"))
        self.assertTrue(anon._is_own_output("x.map.json"))
        # a real source that merely mentions the word must still be processed
        self.assertFalse(anon._is_own_output("note.redacted.draft.txt"))
        self.assertFalse(anon._is_own_output("x.deanon-notes.md"))

    def test_batch_refuses_an_out_directory_that_is_the_scanned_folder(self) -> None:
        res = self.run_anon(str(self.folder), "--batch", "--out", str(self.folder))
        self.assertEqual(res.returncode, 2)
        self.assertIn("--out", res.stderr)

    def test_batch_out_keeps_subdirectories_apart(self) -> None:
        twin = self.folder / "sub" / "verbale.txt"
        twin.write_text("Cliente Acme.\n", encoding="utf-8")
        out = self.tmp / "out2"
        res = self.run_anon(str(self.folder), "--batch", "--out", str(out))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((out / "verbale.redacted.txt").is_file())
        self.assertTrue((out / "sub" / "verbale.redacted.txt").is_file(), "same name, different folder")


    def test_batch_check_also_skips_a_symlink_out_of_the_tree(self) -> None:
        """The gate mode must skip it too: `--check` on a folder that silently follows a link would
        report on files the operator never named."""
        outside = self.tmp / "fuori2" / "segreto.txt"
        outside.parent.mkdir()
        outside.write_text("Cliente Acme\n", encoding="utf-8")
        (self.folder / "scorciatoia2.txt").symlink_to(outside)
        res = self.run_anon(str(self.folder), "--batch", "--check", "--json")
        report = json.loads(res.stdout)
        scanned = {Path(row["file"]).name for row in report["files"]}
        self.assertNotIn("scorciatoia2.txt", scanned)
        self.assertIn("scorciatoia2.txt",
                      {Path(row["file"]).name for row in report["skipped"]})

    def test_a_symlink_out_of_the_tree_is_skipped_not_followed(self) -> None:
        """A folder walk scans the FOLDER. `is_file()` follows symlinks, so a link inside the tree
        used to pull in a file the operator never put in scope — including a map from the private
        store, where the real values live. Skipping is the only answer that keeps the scope honest.
        """
        outside = self.tmp / "fuori" / "segreto.txt"
        outside.parent.mkdir()
        outside.write_text("Cliente Acme, referente Mario Rossi\n", encoding="utf-8")
        escape = self.folder / "scorciatoia.txt"
        escape.symlink_to(outside)
        inside_store = self.home / "maps" / "20200101-000000-aaaaaa.map.json"
        inside_store.parent.mkdir(exist_ok=True)
        inside_store.write_text('{"entries": {}}', encoding="utf-8")
        (self.folder / "mappa.txt").symlink_to(inside_store)

        res = self.run_anon(str(self.folder), "--batch", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        report = json.loads(res.stdout)
        reasons = {Path(row["file"]).name: row["reason"] for row in report["skipped"]}
        self.assertEqual(reasons.get("scorciatoia.txt"), "symlink outside the scan root")
        self.assertEqual(reasons.get("mappa.txt"), "symlink into the private store")
        # Nothing was written next to a symlink that points elsewhere.
        self.assertFalse((self.folder / "scorciatoia.redacted.txt").exists())
        self.assertFalse((self.folder / "mappa.redacted.txt").exists())
        # And the real file outside the tree is untouched: the walk did not reach it.
        self.assertIn("Mario Rossi", outside.read_text(encoding="utf-8"))


class ContainerRedactionTest(unittest.TestCase):
    """`anon.py verbale.docx` -> `verbale.redacted.docx`: the document comes back, not Markdown.

    The reason this matters: the Markdown path LOSES headers, footers, comments and the document
    properties (measured, `scripts/convert-fidelity.py`), so a client name in a letterhead was
    neither redacted nor delivered. Rewriting the parts covers them where they live.
    """

    PART_TYPES = (
        '[Content_Types].xml',
        '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/></Types>',
    )
    RELS = (
        '_rels/.rels',
        '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
        'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
        '2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>',
    )

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="anon-container-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        (self.home / "entities.txt").write_text("AZIENDA|Contoso\nPERSONA|Mario Rossi\n", encoding="utf-8")
        self.env = {**os.environ, "ANON_HOME": str(self.home)}

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, name: str, body: str, header: str = "", author: str = "") -> Path:
        """A minimal but valid package: body, optional header, optional properties."""
        docx = self.tmp / name
        with zipfile.ZipFile(docx, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(*self.PART_TYPES)
            archive.writestr(*self.RELS)
            archive.writestr(
                "word/document.xml",
                '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
                f'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{body}</w:t></w:r></w:p>'
                "</w:body></w:document>",
            )
            if header:
                archive.writestr(
                    "word/header1.xml",
                    '<?xml version="1.0"?><w:hdr xmlns:w="http://schemas.openxmlformats.org/'
                    f'wordprocessingml/2006/main"><w:p><w:r><w:t>{header}</w:t></w:r></w:p></w:hdr>',
                )
            if author:
                archive.writestr(
                    "docProps/core.xml",
                    '<?xml version="1.0"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/'
                    'package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
                    f"<dc:creator>{author}</dc:creator></cp:coreProperties>",
                )
        return docx

    def run_anon(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ANON_PY), *args], capture_output=True, text=True, env=self.env, check=False
        )

    def all_text(self, path: Path) -> str:
        # The container's own view: the same one the redaction and its verification use.
        return anon.container_text(path)

    def test_a_docx_is_redacted_in_place_and_comes_back_whole(self) -> None:
        src = self.build(
            "verbale.docx",
            "Referente: Mario Rossi, cliente Contoso.",
            header="Spett.le Contoso, uso interno",
            author="Mario Rossi",
        )
        res = self.run_anon(str(src), "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        redacted = self.tmp / "verbale.redacted.docx"
        self.assertTrue(redacted.is_file())
        self.assertTrue(zipfile.is_zipfile(redacted), "the output is still a real container")

        text = self.all_text(redacted)
        for value in ("Mario Rossi", "Contoso"):
            self.assertNotIn(value, text, f"{value!r} survived the redaction")
        self.assertIn("PERSONA-1-", text)
        self.assertIn("AZIENDA-1-", text)

        # the reverse direction, on the SAME file, must restore every part exactly
        report = json.loads(res.stdout)
        restored = self.tmp / "verbale.deanon.docx"
        back = subprocess.run(
            [sys.executable, str(DEANON_PY), str(redacted), report["map"], "--out", str(restored), "--json"],
            capture_output=True, text=True, env=self.env, check=False,
        )
        self.assertEqual(back.returncode, 0, back.stderr)
        self.assertTrue(json.loads(back.stdout)["complete"])
        self.assertEqual(self.all_text(restored), self.all_text(src), "part by part, the text is back")

    def test_a_value_split_across_two_runs_is_still_redacted(self) -> None:
        src = self.build("spezzato.docx", "<w:t>Mario </w:t></w:r><w:r><w:t>Rossi</w:t></w:r>")
        res = self.run_anon(str(src), "--quiet")
        self.assertEqual(res.returncode, 0, res.stderr)
        text = self.all_text(self.tmp / "spezzato.redacted.docx")
        self.assertNotIn("Mario", text)
        self.assertNotIn("Rossi", text)
        # And the map must hold the REAL text, not the detection view's separator spaces: a
        # mangled value would be written straight back into the document on restore.
        res = self.run_anon(str(src), "--json")
        back = self.tmp / "spezzato.deanon.docx"
        restored = subprocess.run(
            [sys.executable, str(DEANON_PY), str(self.tmp / "spezzato.redacted.docx"),
             json.loads(res.stdout)["map"], "--out", str(back), "--json"],
            capture_output=True, text=True, env=self.env, check=False,
        )
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertIn("Mario Rossi", self.all_text(back), "the restored text is mangled by the view")

    def test_dry_run_on_a_container_writes_nothing(self) -> None:
        src = self.build("nota.docx", "Cliente Contoso.")
        before = {p.name for p in self.tmp.iterdir()}
        res = self.run_anon(str(src), "--dry-run", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual({p.name for p in self.tmp.iterdir()}, before, "a dry run writes nothing")
        self.assertTrue(json.loads(res.stdout)["dry_run"])
        self.assertFalse((self.tmp / "nota.redacted.docx").exists())

    def pack(self, name: str, parts: dict[str, str]) -> Path:
        """A minimal package: the two structural parts every Office/ODF ZIP carries, plus yours."""
        path = self.tmp / name
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(*self.PART_TYPES)
            archive.writestr(*self.RELS)
            for part, xml in parts.items():
                archive.writestr(part, xml)
        return path

    def assert_round_trip(self, name: str, parts: dict[str, str]) -> None:
        """Redact, then look INSIDE the output package, then restore and compare part by part.

        Reading the output's parts is the assertion that matters: a test that only checked a
        converted Markdown would pass even if the container itself were handed back untouched.
        """
        src = self.pack(name, parts)
        res = self.run_anon(str(src), "--json")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        out = self.tmp / f"{Path(name).stem}.redacted{Path(name).suffix}"
        self.assertTrue(out.is_file(), f"{name}: no redacted output")
        self.assertTrue(zipfile.is_zipfile(out), f"{name}: the output is no longer a container")

        text = self.all_text(out)
        self.assertNotIn("Contoso", text, f"{name}: the value survived")
        self.assertNotIn("Mario Rossi", text, f"{name}: the value survived")
        self.assertRegex(text, r"\[(AZIENDA|PERSONA)-1-", f"{name}: no placeholder written")

        report = json.loads(res.stdout)
        self.assertTrue(report.get("map"), f"{name}: no map was written")
        back = self.tmp / f"{Path(name).stem}.deanon{Path(name).suffix}"
        restored = subprocess.run(
            [sys.executable, str(DEANON_PY), str(out), report["map"], "--out", str(back), "--json"],
            capture_output=True, text=True, env=self.env, check=False,
        )
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertTrue(json.loads(restored.stdout)["complete"], f"{name}: restore incomplete")
        self.assertEqual(self.all_text(back), self.all_text(src), f"{name}: text not restored")
        # And the value is back WHERE THE FORMAT KEEPS ITS TEXT, not merely somewhere in the file.
        with zipfile.ZipFile(back) as archive:
            for part in parts:
                self.assertIn("Contoso", archive.read(part).decode("utf-8"), f"{name}: {part}")

    def test_an_xlsx_is_redacted_where_spreadsheets_keep_strings(self) -> None:
        self.assert_round_trip("foglio.xlsx", {"xl/sharedStrings.xml":
            '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            "<si><t>Cliente Contoso</t></si><si><t>Referente Mario Rossi</t></si></sst>"})

    def test_an_odt_is_redacted_where_open_documents_keep_text(self) -> None:
        self.assert_round_trip("verbale.odt", {"content.xml":
            '<?xml version="1.0"?><office:document-content xmlns:office="urn:oasis:names:tc:opendocument:'
            'xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body>'
            "<office:text><text:p>Cliente Contoso</text:p><text:p>Referente Mario Rossi</text:p>"
            "</office:text></office:body></office:document-content>"})

    def test_a_pptx_is_redacted_where_slides_keep_text(self) -> None:
        self.assert_round_trip("slide.pptx", {"ppt/slides/slide1.xml":
            '<?xml version="1.0"?><p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
            ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp>'
            "<p:txBody><a:p><a:r><a:t>Cliente Contoso</a:t></a:r></a:p><a:p><a:r><a:t>Referente Mario Rossi"
            "</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"})

    def test_audit_verifies_the_placeholders_inside_the_parts(self) -> None:
        """`--audit` on a container: is this file really redacted, and is it really the same map?

        Without this, the only answer for a document was "convert it to Markdown first" — which is
        not an answer about the file the operator is holding.
        """
        src = self.build("controllo.docx", "Referente Mario Rossi, cliente Contoso.",
                         header="Spett.le Contoso", author="Mario Rossi")
        before = json.loads(self.run_anon("--audit", str(src), "--json").stdout)
        self.assertEqual(before["verdict"], "sensitive")
        self.assertTrue(before["container"])
        self.assertEqual(before["placeholders_present"], 0)

        res = self.run_anon(str(src), "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        after = json.loads(self.run_anon("--audit", str(self.tmp / "controllo.redacted.docx"), "--json").stdout)
        self.assertEqual(after["verdict"], "clean", after)
        self.assertEqual(after["total"], 0, "a real value survived the redaction")
        self.assertEqual(after["placeholders_present"], before["total"],
                         "the redacted document must carry one placeholder per value it replaced")

    def test_a_utf16_part_without_a_bom_is_still_redacted(self) -> None:
        """Text without a BOM is text. Judging "binary" from a NUL byte skips the part in BOTH the
        rewrite and its verification, so the value survives and the check reports zero leftovers:
        one mistake, two views, and the report agrees with itself."""
        src = self.tmp / "wide.docx"
        props = ('<?xml version="1.0"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/'
                 'package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/">'
                 "<dc:creator>Mario Rossi</dc:creator></cp:coreProperties>")
        with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(*self.PART_TYPES)
            archive.writestr(*self.RELS)
            archive.writestr("docProps/core.xml", props.encode("utf-16-le"))  # no BOM on purpose
        res = self.run_anon(str(src), "--quiet")
        self.assertEqual(res.returncode, 0, res.stderr)
        with zipfile.ZipFile(self.tmp / "wide.redacted.docx") as archive:
            part = archive.read("docProps/core.xml").decode("utf-16-le")
        self.assertNotIn("Mario Rossi", part, "a BOM-less UTF-16 part was passed through untouched")
        self.assertIn("PERSONA-1-", part)

    def test_a_part_that_expands_like_a_bomb_is_refused(self) -> None:
        """A million bytes that compress to a few hundred: refused BEFORE it is inflated.

        `zipfile` inflates a part into memory before anyone can look at it, and the per-character
        index costs tens of bytes per character, so the cap has to be applied to the declared size.
        """
        src = self.pack("bomba.docx", {"word/document.xml": "<w:t>" + "A" * 1_000_000 + "</w:t>"})
        res = self.run_anon(str(src), "--quiet")
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("REFUSED", res.stderr)
        self.assertFalse((self.tmp / "bomba.redacted.docx").exists(), "nothing is written")

    def test_a_name_split_by_a_tab_or_a_break_in_a_footer_is_still_redacted(self) -> None:
        """The realistic split: not every boundary between two fragments of a name is structural.

        A letterhead writes `Contoso<w:tab/>S.r.l.`, a two-line address breaks with `<w:br/>`, and a
        bookmark or a content control can sit anywhere — all of them are inside ONE text container,
        so the value must be redacted. The first version of this rule listed only the run-level tags
        and refused ordinary documents, which is the other way to get it wrong.
        """
        cases = {
            "tab": "<w:t>Contoso</w:t><w:tab/><w:t>S.r.l.</w:t>",
            "break": "<w:t>Contoso</w:t><w:br/><w:t>S.r.l.</w:t>",
            "bookmark": ("<w:t>Contoso</w:t><w:bookmarkStart w:id=\"1\" w:name=\"x\"/>"
                         "<w:bookmarkEnd w:id=\"1\"/><w:t>S.r.l.</w:t>"),
            "content control": ("<w:t>Contoso</w:t></w:r><w:sdt><w:sdtContent>"
                                "<w:r><w:t>S.r.l.</w:t></w:r></w:sdtContent></w:sdt>"),
        }
        for label, body in cases.items():
            with self.subTest(label):
                src = self.pack(f"footer-{label.replace(' ', '-')}.docx",
                                {"word/footer1.xml": f"<w:p><w:r>{body}</w:r></w:p>"})
                res = self.run_anon(str(src), "--quiet")
                self.assertEqual(res.returncode, 0, (label, res.stderr))
                out = self.tmp / f"footer-{label.replace(' ', '-')}.redacted.docx"
                self.assertTrue(zipfile.is_zipfile(out), label)
                text = self.all_text(out)
                self.assertNotIn("Contoso", text, label)
                self.assertIn("AZIENDA-1-", text, label)

    def test_a_part_named_as_text_that_cannot_be_read_is_refused(self) -> None:
        """A part whose NAME promises text and whose bytes cannot be read is refused, not skipped.

        Skipping it is the quiet version of the same mistake the whole container pass exists to
        avoid: the document would be delivered with a value still inside, and the verification would
        not see it either (same view, same blind spot). Legacy VML is the realistic case — it is XML
        by definition, so a `.vml` part that is not decodable is corrupt, not borderline.
        """
        src = self.pack("vml.docx", {"word/document.xml": "<w:t>Cliente Contoso</w:t>"})
        with zipfile.ZipFile(src, "a", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/drawings/legacy.vml", bytes(range(256)) * 8)
        res = self.run_anon(str(src), "--quiet")
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("REFUSED", res.stderr)
        self.assertIn("legacy.vml", res.stderr, "the message must say which part")
        self.assertFalse((self.tmp / "vml.redacted.docx").exists(), "nothing is written")

    def test_the_containers_word_really_uses_all_redact(self) -> None:
        """A corpus of the places a client name actually lives in, and of how Word splits it there.

        Each of these is a boundary between two fragments of ONE value that Word produces routinely:
        a table cell, a footnote, a comment, a text box, a tracked insertion, a tracked deletion, a
        field in the middle, a math run, a hyperlink, a content control wrapped around the paragraph.
        A rule that refuses any of them refuses ordinary documents — which is exactly what the two
        corrections before this test were about.
        """
        two_runs = "<w:r><w:t>Contoso</w:t></w:r><w:r><w:t>S.r.l.</w:t></w:r>"
        constructs = {
            "table cell": f"<w:tbl><w:tr><w:tc><w:p>{two_runs}</w:p></w:tc></w:tr></w:tbl>",
            "text box": ("<w:r><w:pict><v:shape xmlns:v=\"urn:schemas-microsoft-com:vml\">"
                         "<v:textbox><w:txbxContent><w:p>" + two_runs +
                         "</w:p></w:txbxContent></v:textbox></v:shape></w:pict></w:r>"),
            "tracked insertion": f'<w:ins w:id="1" w:author="a">{two_runs}</w:ins>',
            "tracked deletion": ("<w:del w:id=\"2\" w:author=\"a\"><w:r><w:delText>Contoso</w:delText></w:r>"
                                 "<w:r><w:delText>S.r.l.</w:delText></w:r></w:del>"),
            "field in the middle": ("<w:r><w:t>Contoso</w:t></w:r><w:r>"
                                    '<w:fldChar w:fldCharType="begin"/><w:instrText>PAGE</w:instrText>'
                                    '<w:fldChar w:fldCharType="end"/></w:r>'
                                    "<w:r><w:t>S.r.l.</w:t></w:r>"),
            "math run": ('<m:oMath xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math">'
                         "<m:r><m:t>Contoso</m:t></m:r><m:r><m:t>S.r.l.</m:t></m:r></m:oMath>"),
            "hyperlink": ('<w:hyperlink r:id="rId9" xmlns:r="http://schemas.openxmlformats.org/'
                          'officeDocument/2006/relationships"><w:r><w:t>Contoso</w:t></w:r></w:hyperlink>'
                          "<w:r><w:t>S.r.l.</w:t></w:r>"),
            "block content control": ("<w:sdt><w:sdtContent><w:p>" + two_runs + "</w:p></w:sdtContent></w:sdt>"),
        }
        for label, body in constructs.items():
            with self.subTest(label):
                name = f"contesto-{label.replace(' ', '-')}.docx"
                src = self.pack(name, {"word/document.xml": f"<w:body>{body}</w:body>"})
                res = self.run_anon(str(src), "--quiet")
                self.assertEqual(res.returncode, 0, (label, res.stderr))
                out = self.tmp / f"{name[:-5]}.redacted.docx"
                self.assertTrue(zipfile.is_zipfile(out), label)
                text = self.all_text(out)
                self.assertNotIn("Contoso", text, label)
                self.assertIn("AZIENDA-1-", text, label)

    def test_run_properties_between_two_fragments_are_not_a_boundary(self) -> None:
        """The shape Word actually writes in a footer: every run carries its own formatting block.

        A per-tag classification called `<w:rFonts/>`, `<w:sz/>` and `<w:spacing/>` boundaries, so an
        ordinary document was refused and the UI fell back to Markdown alone. They are formatting:
        they never open a text container, and the two fragments are in the same paragraph.
        """
        props = ('<w:rPr><w:rFonts w:cstheme="minorHAnsi"/><w:spacing w:val="40"/>'
                 '<w:sz w:val="18"/><w:szCs w:val="22"/></w:rPr>')
        src = self.pack("proprieta.docx", {"word/footer1.xml":
            f"<w:p><w:r>{props}<w:t>Spett.le Contoso</w:t></w:r>"
            f"<w:r>{props}<w:t>S.r.l.</w:t></w:r></w:p>"})
        res = self.run_anon(str(src), "--quiet")
        self.assertEqual(res.returncode, 0, res.stderr)
        out = self.tmp / "proprieta.redacted.docx"
        self.assertTrue(zipfile.is_zipfile(out))
        text = self.all_text(out)
        self.assertNotIn("Contoso", text)
        self.assertIn("AZIENDA-1-", text)

    def test_a_refusal_names_the_tag_that_blocked_it(self) -> None:
        """A REFUSED must be actionable: the operator has to know WHAT to look at."""
        src = self.pack("bloccato.docx", {"word/footer1.xml":
            "<w:p><w:r><w:t>Contoso</w:t></w:r></w:p><w:p><w:r><w:t>S.r.l.</w:t></w:r></w:p>"})
        res = self.run_anon(str(src), "--quiet")
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("REFUSED", res.stderr)
        self.assertIn("w:p", res.stderr, "the message must name the boundary that caused the refusal")

    def test_a_match_reaching_across_a_paragraph_is_refused_not_rewritten(self) -> None:
        """A value that only matches by joining TWO elements must be refused.

        Rewriting it puts the placeholder in the first fragment and EMPTIES the others, so the
        second paragraph's text would be destroyed — and the map would hold the separator spaces as
        part of the value. Refusing is the fail-closed answer; silently losing a paragraph is not.
        """
        src = self.pack("due.docx", {"word/document.xml":
            '<w:p><w:r><w:t>Mario</w:t></w:r></w:p><w:p><w:r><w:t>Rossi</w:t></w:r></w:p>'})
        res = self.run_anon(str(src), "--quiet")
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("REFUSED", res.stderr)
        self.assertFalse((self.tmp / "due.redacted.docx").exists(), "nothing is written")

    def test_check_names_the_findings_and_stays_unscannable(self) -> None:
        """`--check` on a document: the records AND the refusal, together.

        `unscannable` is not a formality to drop now that the parts can be scanned: the `read` tool
        decodes a non-image file as text (which redacts nothing), and the guard's auto-remediation
        keys on that field. Reporting the findings is an addition, never a substitute.
        """
        src = self.build("esame.docx", "Referente Mario Rossi, cliente Contoso.", header="Spett.le Contoso")
        res = self.run_anon("--check", str(src), "--json")
        self.assertEqual(res.returncode, 1, "a document with values inside is not 'clean'")
        report = json.loads(res.stdout)
        self.assertTrue(report["unscannable"] and report["binary"] and report["container"])
        self.assertEqual(report["types"], {"AZIENDA": 2, "PERSONA": 1}, "both parts are named")
        self.assertTrue(report["findings"])
        # The findings name TYPES and positions, never the values themselves.
        self.assertNotIn("Contoso", res.stdout)
        self.assertNotIn("Mario", res.stdout)

    def test_a_fake_container_is_refused_with_nothing_written(self) -> None:
        fake = self.tmp / "finto.docx"
        fake.write_bytes(b"PK\x03\x04" + bytes(range(256)) * 10)
        res = self.run_anon(str(fake), "--quiet")
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("REFUSED", res.stderr)
        self.assertFalse((self.tmp / "finto.redacted.docx").exists())
        self.assertEqual(list(self.home.glob("maps/*.map.json")), [])


class OfflineContractTest(unittest.TestCase):
    """No engine script may gain a network import by accident (DEC-0012 §2).

    This is a CANARY, not a proof. It checks the AST for a STATIC import of a network stack — the
    one-line regression, which is the shape that actually happens — not the behavioural claim "the
    engine makes no network call". A dynamic import (`__import__("socket")`), a transitive one (a
    helper that imports socket) or an exec'd string would slip past, and `convert.py --install`
    legitimately reaches the network through pip: an import denylist cannot prove a behaviour.

    `web/server.py` is out of scope by design — it IS an HTTP server (DEC-0012 §1/§3). The document
    paths (`anon.py`, `deanon.py`) are also checked for `subprocess`: today neither spawns
    anything, and relaxing that should be a deliberate act rather than a drive-by edit.

    ONE exception is declared, and it is tested rather than trusted: `suggest.py` is the local-model
    seam, it is a CLIENT of the engine (it imports `anon`, never the reverse), and it is the only
    file in the project allowed to reach the network. The tests below assert all three parts, so a
    second module cannot quietly acquire the capability and the exception cannot spread.
    """

    SCRIPTS = ("anon.py", "deanon.py", "convert.py")
    # The declared exception, named so it stays one: see the class docstring.
    NETWORK_CLIENT = "suggest.py"
    # A network STACK, not a network-shaped name: `urllib.parse` and `http.cookies` are parsers and
    # constants, and must stay importable by the engine. A submodule is matched on its full dotted
    # name, so `urllib.request` fails where `urllib.parse` passes.
    NETWORK_MODULES = {
        "socket",
        "socketserver",
        "ssl",
        "ftplib",
        "smtplib",
        "poplib",
        "imaplib",
        "telnetlib",
        "urllib.request",
        "http.client",
        "http.server",
        "xmlrpc.client",
        "xmlrpc.server",
        "requests",
        "httpx",
        "aiohttp",
        "urllib3",
        "pycurl",
        "websocket",
    }

    def _imports(self, path: Path) -> set[str]:
        """Every dotted module name the file imports statically."""
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # convert.py is never imported by the suite: fail cleanly here
            self.fail(f"{path.name} does not parse: {exc}")
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        return names

    def _engine_scripts(self) -> list[Path]:
        found = [HOME / name for name in self.SCRIPTS if (HOME / name).is_file()]
        # The slim install (`make up-slim`) ships no convert.py on purpose: anon.py and deanon.py
        # are the engine and must be there for the check to mean anything.
        self.assertTrue({"anon.py", "deanon.py"} <= {p.name for p in found}, "engine scripts not found")
        return found

    def _network_imports(self, path: Path) -> set[str]:
        """The network modules among the file's static imports."""
        return {
            name
            for name in self._imports(path)
            if any(name == banned or name.startswith(f"{banned}.") for banned in self.NETWORK_MODULES)
        }

    def test_no_engine_script_imports_a_network_stack(self) -> None:
        for path in self._engine_scripts():
            leaked = self._network_imports(path)
            self.assertFalse(leaked, f"{path.name} imports a network stack: {sorted(leaked)}")

    def test_the_network_capability_lives_in_exactly_one_named_module(self) -> None:
        """The seam is the exception, and an exception that is not checked becomes the rule."""
        client = HOME / self.NETWORK_CLIENT
        self.assertTrue(client.is_file(), f"{self.NETWORK_CLIENT} is the declared network client")
        self.assertTrue(
            self._network_imports(client),
            f"{self.NETWORK_CLIENT} must import the transport it is the declared client of",
        )
        for path in self._engine_scripts():
            self.assertFalse(
                self._network_imports(path), f"{path.name} must stay network-free"
            )

    def test_the_engine_never_imports_the_seam(self) -> None:
        """The arrow points one way: the seam uses the engine, the engine does not know the seam.

        If `anon.py` imported `suggest.py` — even lazily, even inside `--suggest` — the engine would
        gain a network capability by the back door, and the claim in DEC-0012 §2 would be false
        while every other test stayed green.
        """
        for path in self._engine_scripts():
            self.assertNotIn("suggest", self._imports(path), f"{path.name} must not import the seam")
        self.assertIn("anon", self._imports(HOME / self.NETWORK_CLIENT),
                      f"{self.NETWORK_CLIENT} is a client of the engine")

    def test_the_document_paths_do_not_spawn_a_process(self) -> None:
        for path in self._engine_scripts():
            if path.name == "convert.py":
                continue  # its job is to run the anydoc engine, and `--install` runs pip
            self.assertNotIn("subprocess", self._imports(path), f"{path.name} spawns a process")


if __name__ == "__main__":
    unittest.main(verbosity=2)
