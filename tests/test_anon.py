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
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import mock
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

    def test_context_off_closes_the_block(self) -> None:
        entities = self.entities(
            "@type CITTÀ\n@match case-sensitive\n@context (?:sede di)\\s+\nBrescia\n@context off\nPrato\n"
        )
        # The gated entry keeps its context...
        self.assertFalse(anon.detect("Brescia", entities), "the gated entry keeps its context")
        self.assertTrue(anon.detect("sede di Brescia", entities))
        # ...while an entry declared after `@context off` matches bare.
        self.assertTrue(anon.detect("Prato", entities), "`@context off` must clear the context")

    def test_a_later_context_replaces_the_earlier_one(self) -> None:
        entities = self.entities(
            "@type CITTÀ\n@match case-sensitive\n@context (?:comune di)\\s+\nBrescia\n"
            "@context (?:sede di)\\s+\nPrato\n"
        )
        self.assertTrue(anon.detect("comune di Brescia", entities))
        self.assertFalse(anon.detect("comune di Prato", entities), "the first context no longer applies")
        self.assertTrue(anon.detect("sede di Prato", entities))

    def test_only_exactly_off_clears_the_context(self) -> None:
        # `no` and `0` are legitimate @context REGEXES: reading them as "off" (the shared _FALSE
        # list, used by @stem) would silently drop a gate the operator wrote.
        entities = self.entities("@type CITTÀ\n@match case-sensitive\n@context no\\s+\nBrescia\n")
        self.assertTrue(anon.detect("no Brescia", entities), "`no` is a regex, not an off switch")
        self.assertFalse(anon.detect("Brescia", entities), "the gate must still apply")

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
    """

    SCRIPTS = ("anon.py", "deanon.py", "convert.py")
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

    def test_no_engine_script_imports_a_network_stack(self) -> None:
        for path in self._engine_scripts():
            leaked = {
                name
                for name in self._imports(path)
                if any(name == banned or name.startswith(f"{banned}.") for banned in self.NETWORK_MODULES)
            }
            self.assertFalse(leaked, f"{path.name} imports a network stack: {sorted(leaked)}")

    def test_the_document_paths_do_not_spawn_a_process(self) -> None:
        for path in self._engine_scripts():
            if path.name == "convert.py":
                continue  # its job is to run the anydoc engine, and `--install` runs pip
            self.assertNotIn("subprocess", self._imports(path), f"{path.name} spawns a process")


if __name__ == "__main__":
    unittest.main(verbosity=2)
