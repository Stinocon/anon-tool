#!/usr/bin/env python3
"""Deterministic tests for the anon engine.

The gate for the whole system is here: anon -> deanon must be byte-for-byte lossless, a
second anonymization must be a no-op, and the heuristics must not redact filenames or
public placeholder values (`example.com`, RFC 5737 IPs).

  python3 ~/.anon/tests/test_anon.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
    ("SEDE", "Sede di Brescia"),
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
                "Brescia",
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
            "sede di Brescia e Brescia; sede di Roma Nord; Ferraris Gianni; "
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

    def test_roundtrip_is_lossless(self) -> None:
        original = (
            "Spett.le Acme Italia S.r.l. (rif. Acme),\n"
            "sede operativa: Sede di Brescia.\n"
            "Referente: Mario Rossi, mario.rossi@acme.it, tel. +39 030 1234567.\n"
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
        for phone in ("+1 415 555 0199", "+44 20 7946 0958", "+39 030 1234567", "333 1234567"):
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
        ):
            self.assertEqual(len(anon.detect(snippet, self.entities)), 1, f"missed secret in {snippet!r}")

    def test_placeholder_secrets_are_not_redacted(self) -> None:
        for snippet in (
            "API_KEY=your-api-key-here",
            "password: changeme",
            "password: placeholder-value",
            "token: xxxx",
            "token: your-token",
        ):
            self.assertEqual(anon.detect(snippet, self.entities), [], f"false positive on {snippet!r}")

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
        ("AZIENDA|Contoso", ["Contoso", "contoso", "Contoso", "Contoso S.r.l.", "Contoso srl",
                            "Contoso S.R.L.", "Contoso s.r.l"], ["Contosost", "la contosotta"]),
        ("AZIENDA|Acme Italia", ["Acme Italia", "acme-italia", "Acme.Italia srl"], ["AcmeItalia", "Acme"]),
        ("PERSONA|Nicolò Rossi", ["Nicolò Rossi"], ["Nicolo Rossi"]),
        ("AZIENDA|Contoso|Contoso", ["Contoso", "Contoso"], []),
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
        source = "Contoso S.r.l. e poi Contoso, con Contoso in maiuscolo.\n"
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
            "@type CITTÀ\n@match case-sensitive\n@context (?:sede di)\\s+\nBrescia\n", encoding="utf-8"
        )
        src = self.tmp / "doc.txt"
        src.write_text("sede di Brescia, il prato e' verde. CF RSSMRA80A01H501U\n", encoding="utf-8")

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
    )

    NEGATIVE = (
        "in via del tutto eccezionale, 3 volte l'anno",
        "percorrere la via libera 4 corsie",
        "il viale alberato 2 piani",
        "nessun indirizzo in questa riga",
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

    def test_case_sensitive_keeps_lowercase_words_intact(self) -> None:
        entities = self.entities("@type CITTÀ\n@match case-sensitive\nPrato\n")
        self.assertTrue(anon.detect("Prato", entities))
        self.assertFalse(anon.detect("il prato è verde", entities), "`prato` is not a city here")

    def test_context_gates_a_low_signal_entry(self) -> None:
        entities = self.entities("@type CITTÀ\n@match case-sensitive\n@context (?:comune di|sede di)\\s+\nBrescia\n")
        self.assertTrue(anon.detect("sede di Brescia", entities))
        self.assertFalse(anon.detect("Brescia", entities), "without the context marker it is not redacted")
        # only the city is redacted, the context marker stays readable
        found = anon.detect("comune di Brescia", entities)
        self.assertEqual(["comune di Brescia"[s:e] for s, e, _t in found], ["Brescia"])

    def test_unknown_directive_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            self.entities("@contxt x\nCITTÀ|Brescia\n")
        with self.assertRaises(ValueError):
            self.entities("@stem maybe\nX|Y\n")
        with self.assertRaises(ValueError):
            self.entities("@context (unbalanced\nX|Y\n")

    def test_type_directive_applies_to_following_entries(self) -> None:
        entities = self.entities("@type CLIENTE\nAcme\n@type PERSONA\n\nMario Rossi\n")
        types = {ptype for _s, _e, ptype in anon.detect("Acme e Mario Rossi", entities)}
        self.assertEqual(types, {"CLIENTE", "PERSONA"})


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
        res = self.audit("variant.txt", "Il cliente Contoso Srl ha tre stabilimenti.\n")
        self.assertEqual(res.returncode, 4, res.stdout + res.stderr)
        report = json.loads(res.stdout)
        self.assertEqual(report["verdict"], "suspicious")
        self.assertEqual(report["near_miss"][0]["kind"], "variant")
        self.assertNotIn("Contoso", res.stdout)
        self.assertNotIn("Contoso", res.stdout, "entity names are sensitive too")
        self.assertIn("\u2022", report["near_miss"][0]["token_masked"])

    def test_reveal_shows_the_candidates(self) -> None:
        res = self.audit("variant2.txt", "Il cliente Contoso Srl ha tre stabilimenti.\n", "--reveal")
        self.assertEqual(res.returncode, 4)
        report = json.loads(res.stdout)
        self.assertTrue(report["revealed"])
        self.assertEqual(report["near_miss"][0]["token"], "Contoso")
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
        docx = self._make_docx("split.docx", {
            "word/document.xml": '<?xml version="1.0"?><w:document xmlns:w="x"><w:body>'
            + self._para("Referente: [EMAIL-") + self._para("1] fine") + "</w:body></w:document>",
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
            self.assertEqual(body.count("<w:p>"), 2, "the paragraph structure must be untouched")

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
        for shape in ("[1, 2, 3]", '"solo una stringa"', '{"entries": 5}'):
            broken = self.tmp / f"broken-{len(shape)}.map.json"
            broken.write_text(shape, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(DEANON_PY), str(document), str(broken), "--json"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 2, f"{shape!r}: {result.stdout} {result.stderr}")
            self.assertNotIn("Traceback", result.stderr)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
