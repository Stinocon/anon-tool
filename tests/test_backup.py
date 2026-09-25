#!/usr/bin/env python3
"""Tests for `scripts/anon-home-backup.sh`: the encrypted copy of the private store.

The store holds the REAL values (`maps/`), so the properties under test are about the boundary, not
about convenience: the archive must not be readable without the passphrase, a restore must write
nothing until the payload has been verified, and a payload that does not match its own manifest —
tampered, truncated, or decrypted with the wrong passphrase — must be refused rather than extracted.

The negative cases build their archive directly with `tarfile`, because that is the only way to
produce precisely the inputs a hostile or damaged archive would have: a member that is not in the
manifest, a name that walks up out of the home, a symlink.

  python3 tests/test_backup.py
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

HERE = Path(__file__).resolve().parent
HOME = HERE.parent
SCRIPT = HOME / "scripts" / "anon-home-backup.sh"

PASSPHRASE = "correct horse battery staple"
MARKER = "ACME-Contoso-Srl"   # a value that must never appear in the archive in clear

TAR = shutil.which("tar")
OPENSSL = shutil.which("openssl")
SHA = shutil.which("sha256sum") or shutil.which("shasum")
READY = bool(TAR and OPENSSL and SHA)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def manifest_line(name: str, data: bytes) -> str:
    return f"{hashlib.sha256(data).hexdigest()}  {name}\n"


class BackupTest(unittest.TestCase):
    """One round trip, then one refusal per way an archive can lie about itself."""

    def setUp(self) -> None:
        if not READY:
            self.skipTest("tar, openssl and a sha256 tool are all needed")
        self.tmp = Path(tempfile.mkdtemp(prefix="anon-backup-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    # ---- helpers -------------------------------------------------------------------------------

    def run_script(self, *args: str, passphrase: str | None = PASSPHRASE, extra_env: dict | None = None):
        env = dict(os.environ)
        env.pop("ANON_BACKUP_PASSPHRASE", None)
        if passphrase is not None:
            env["ANON_BACKUP_PASSPHRASE"] = passphrase
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            cwd=str(HOME), env=env, capture_output=True, text=True, timeout=120,
        )

    def make_home(self, name: str = "home") -> Path:
        """A home with the shapes that matter: a map, the dictionary, and what must NOT be copied."""
        home = self.tmp / name
        (home / "maps").mkdir(parents=True)
        (home / "downloads").mkdir()
        (home / "models").mkdir()
        (home / "__pycache__").mkdir()

        (home / "maps" / "PLACEHOLDER_1.json").write_text(
            f'{{"placeholder": "PLACEHOLDER_1", "value": "{MARKER}"}}\n', encoding="utf-8"
        )
        (home / "entities.txt").write_text(f"{MARKER}|AZIENDA\n", encoding="utf-8")
        (home / "downloads" / "documento redatto.pdf").write_text("redacted output\n", encoding="utf-8")
        (home / "entities con spazio.txt").write_text("a name with spaces\n", encoding="utf-8")
        (home / "relazione città è così.txt").write_text("a name with accents\n", encoding="utf-8")

        os.chmod(home / "maps" / "PLACEHOLDER_1.json", 0o600)
        os.chmod(home / "entities.txt", 0o600)

        (home / "models" / "model.gguf").write_bytes(b"\0" * 300_000)   # 2 GB in real life
        (home / "__pycache__" / "anon.cpython-313.pyc").write_bytes(b"\0pyc")
        (home / ".DS_Store").write_bytes(b"\0finder")
        return home

    def make_archive(self, members: dict[str, bytes], manifest: str | None, dest: Path) -> None:
        """A tar built from exactly the given members (a manifest of None means 'no manifest')."""
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w:gz") as tar:
            if manifest is not None:
                blob = manifest.encode("utf-8")
                info = tarfile.TarInfo("MANIFEST.sha256")
                info.size = len(blob)
                info.mode = 0o600
                tar.addfile(info, io.BytesIO(blob))
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o600
                tar.addfile(info, io.BytesIO(data))
        self.encrypt(raw.getvalue(), dest)

    def make_symlink_archive(self, dest: Path) -> None:
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w:gz") as tar:
            info = tarfile.TarInfo("maps")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc"
            tar.addfile(info)
        self.encrypt(raw.getvalue(), dest)

    def make_archive_with_extras(self, listed: dict[str, bytes], extras: list, dest: Path) -> None:
        """An archive whose LISTED files match their manifest, plus members added on top.

        The extras are not in the manifest and are not regular files, so the manifest check and the
        file count both pass: only the member audit can refuse this archive. An archive that some
        other protection already rejects cannot show whether the audit works at all.
        """
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w:gz") as tar:
            blob = "".join(manifest_line(name, data) for name, data in listed.items()).encode("utf-8")
            info = tarfile.TarInfo("MANIFEST.sha256")
            info.size = len(blob)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(blob))
            for name, data in listed.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o600
                tar.addfile(info, io.BytesIO(data))
            for info, payload in extras:
                tar.addfile(info, io.BytesIO(payload) if payload is not None else None)
        self.encrypt(raw.getvalue(), dest)

    def encrypt(self, payload: bytes, dest: Path) -> None:
        # `-pass stdin` takes the passphrase from stdin, so the payload cannot also come from stdin:
        # `-in -` with `-pass stdin` silently encrypts the tar UNDER ITS OWN BYTES as passphrase.
        plain = self.tmp / f"payload-{dest.name}.tar.gz"
        plain.write_bytes(payload)
        encrypted = subprocess.run(
            [OPENSSL, "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "600000", "-salt",
             "-pass", "stdin", "-in", str(plain), "-out", str(dest)],
            input=PASSPHRASE.encode(), capture_output=True,
        )
        plain.unlink()
        self.assertEqual(encrypted.returncode, 0, encrypted.stderr.decode())

    def decrypt(self, archive: Path, passphrase: str = PASSPHRASE) -> bytes:
        done = subprocess.run(
            [OPENSSL, "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "600000", "-pass", "stdin",
             "-in", str(archive)],
            input=passphrase.encode(), capture_output=True,
        )
        self.assertEqual(done.returncode, 0, done.stderr.decode())
        return done.stdout

    def raw_members(self, archive: Path) -> list[str]:
        """The member names exactly as they were written (bsdtar keeps the `./` it was given)."""
        return tarfile.open(fileobj=io.BytesIO(self.decrypt(archive)), mode="r:gz").getnames()

    def members(self, archive: Path) -> list[str]:
        return [name[2:] if name.startswith("./") else name for name in self.raw_members(archive)]

    @staticmethod
    def member(name: str, data: bytes) -> tuple[tarfile.TarInfo, bytes]:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        info.mode = 0o600
        return (info, data)

    @staticmethod
    def directory(name: str, mode: int = 0o700) -> tuple[tarfile.TarInfo, None]:
        info = tarfile.TarInfo(name)
        info.type = tarfile.DIRTYPE
        info.mode = mode
        return (info, None)

    def shimmed(self, commands: list[str]) -> dict[str, str]:
        """A PATH whose `commands` record whether they inherited the passphrase, then run for real."""
        shim = self.tmp / "shim"
        shim.mkdir(exist_ok=True)
        self.seen = self.tmp / "child-env.txt"
        for name in commands:
            real = shutil.which(name)
            self.assertIsNotNone(real, f"{name} is not in PATH")
            script = shim / name
            script.write_text(
                "#!/bin/sh\n"
                f'if [ -n "${{ANON_BACKUP_PASSPHRASE:-}}" ]; then echo LEAKED:{name} >> {self.seen}; '
                f'else echo CLEAN:{name} >> {self.seen}; fi\n'
                f'exec {real} "$@"\n',
                encoding="utf-8",
            )
            os.chmod(script, 0o755)
        return {"PATH": f"{shim}:{os.environ['PATH']}"}

    # ---- the round trip ------------------------------------------------------------------------

    def test_a_backup_restores_byte_for_byte(self) -> None:
        home = self.make_home()
        archive = self.tmp / "store.enc"
        backup = self.run_script("backup", f"--home={home}", f"--out={archive}")
        self.assertEqual(backup.returncode, 0, backup.stderr)
        self.assertIn("VERIFIED", backup.stdout)

        target = self.tmp / "restored"
        done = self.run_script("restore", str(archive), str(target))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("RESTORED", done.stdout)

        for relative in ("maps/PLACEHOLDER_1.json", "entities.txt", "downloads/documento redatto.pdf",
                         "entities con spazio.txt", "relazione città è così.txt"):
            self.assertEqual(
                (target / relative).read_bytes(), (home / relative).read_bytes(), relative
            )
        # the modes travel with the bytes: the store is private on the other side too
        self.assertEqual(stat.S_IMODE(os.stat(target / "entities.txt").st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(target / "maps" / "PLACEHOLDER_1.json").st_mode), 0o600)

    def test_the_model_and_the_caches_are_not_archived(self) -> None:
        home = self.make_home()
        archive = self.tmp / "store.enc"
        self.assertEqual(self.run_script("backup", f"--home={home}", f"--out={archive}").returncode, 0)
        names = self.members(archive)
        self.assertIn("maps/PLACEHOLDER_1.json", names)
        self.assertIn("MANIFEST.sha256", names)
        for absent in ("models/model.gguf", "__pycache__/anon.cpython-313.pyc", ".DS_Store"):
            self.assertNotIn(absent, names)
        # the manifest is the archive's bookkeeping, not a file of the store
        target = self.tmp / "t"
        self.assertEqual(self.run_script("restore", str(archive), str(target)).returncode, 0)
        self.assertFalse((target / "MANIFEST.sha256").exists())

    def test_the_archive_holds_the_files_and_nothing_else(self) -> None:
        """Exactly one member per archived file, plus the manifest.

        bsdtar adds a `._name` member per file carrying the macOS `com.apple.*` xattrs. libarchive
        hides those when listing, so only this count sees them — and GNU tar on Linux extracts them
        as REAL files, which would restore junk and make the count check refuse a good archive. On
        Linux the assertion is trivially true, where such members are never created.
        """
        home = self.make_home()
        archive = self.tmp / "store.enc"
        self.assertEqual(self.run_script("backup", f"--home={home}", f"--out={archive}").returncode, 0)
        raw = self.raw_members(archive)
        self.assertFalse(
            [n for n in raw if "/._" in n or n.startswith("._")], f"macOS metadata members: {raw}"
        )
        archived = [
            p for p in home.rglob("*") if p.is_file()
            and "models" not in p.parts and "__pycache__" not in p.parts
            and not p.name.endswith(".pyc") and p.name != ".DS_Store"
        ]
        self.assertEqual(len(raw), len(archived) + 1, raw)   # +1 is the manifest

    def test_the_value_is_not_readable_in_the_archive(self) -> None:
        home = self.make_home()
        archive = self.tmp / "store.enc"
        self.assertEqual(self.run_script("backup", f"--home={home}", f"--out={archive}").returncode, 0)
        self.assertNotIn(MARKER.encode(), archive.read_bytes())

    def test_the_archive_is_private_and_leaves_no_plaintext_behind(self) -> None:
        home = self.make_home()
        out = self.tmp / "outdir"
        archive = out / "store.enc"
        self.assertEqual(self.run_script("backup", f"--home={home}", f"--out={archive}").returncode, 0)
        self.assertEqual(stat.S_IMODE(os.stat(archive).st_mode), 0o600)
        # the directory holds the archive and nothing else: a decrypted copy left beside it would be
        # the whole store in clear, which is what the old version of this script did
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["store.enc"])

    # ---- refusals ------------------------------------------------------------------------------

    def test_a_wrong_passphrase_is_refused_before_anything_is_written(self) -> None:
        home = self.make_home()
        archive = self.tmp / "store.enc"
        self.assertEqual(self.run_script("backup", f"--home={home}", f"--out={archive}").returncode, 0)
        target = self.tmp / "never"
        done = self.run_script("restore", str(archive), str(target), passphrase="not the passphrase")
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse(target.exists(), "a refused restore must not create the target")
        self.assertFalse(self.tmp.joinpath("never.pre-restore-0").exists())

    def test_a_payload_that_contradicts_its_manifest_is_refused(self) -> None:
        real = b"the real value\n"
        manifest = manifest_line("maps/a.json", real)
        archive = self.tmp / "tampered.enc"
        self.make_archive({"maps/a.json": b"something else\n"}, manifest, archive)
        target = self.tmp / "never"
        done = self.run_script("restore", str(archive), str(target))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("manifest", done.stderr)
        self.assertFalse(target.exists())

    def test_a_payload_with_a_file_the_manifest_does_not_list_is_refused(self) -> None:
        listed = b"listed\n"
        archive = self.tmp / "extra.enc"
        self.make_archive(
            {"maps/a.json": listed, "maps/smuggled.json": b"not in the manifest\n"},
            manifest_line("maps/a.json", listed),
            archive,
        )
        target = self.tmp / "never"
        done = self.run_script("restore", str(archive), str(target))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("manifest", done.stderr)
        self.assertFalse(target.exists())

    def test_a_payload_that_walks_out_of_the_home_is_refused(self) -> None:
        escape = tarfile.TarInfo("../evil.txt")
        escape.size = 8
        escape.mode = 0o600
        archive = self.tmp / "escape.enc"
        self.make_archive_with_extras({"maps/a.json": b"real\n"}, [(escape, b"escaped\n")], archive)
        target = self.tmp / "home" / "store"
        target.parent.mkdir(parents=True)
        done = self.run_script("restore", str(archive), str(target))
        self.assertNotEqual(done.returncode, 0)
        # The exit code alone does not identify the layer: tar refuses `..` by itself on both
        # platforms, so a refusal here would happen with or without the audit. Naming the audit's own
        # finding is what pins it — the point of the check is that the member list is inspected
        # BEFORE anything is handed to tar.
        self.assertIn("parent-relative", done.stderr)
        self.assertFalse(target.exists())
        self.assertFalse((self.tmp / "home" / "evil.txt").exists(), "a member escaped the target")
        self.assertFalse((self.tmp / "evil.txt").exists())

    def test_a_payload_with_an_absolute_name_is_refused(self) -> None:
        absolute = tarfile.TarInfo("/etc/anon-absolute")
        absolute.size = 6
        absolute.mode = 0o600
        archive = self.tmp / "absolute.enc"
        self.make_archive_with_extras({"maps/a.json": b"real\n"}, [(absolute, b"write\n")], archive)
        target = self.tmp / "never"
        done = self.run_script("restore", str(archive), str(target))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("parent-relative", done.stderr)
        self.assertFalse(target.exists())
        self.assertFalse(Path("/etc/anon-absolute").exists())

    def test_a_payload_with_a_symlink_member_is_refused(self) -> None:
        link = tarfile.TarInfo("maps/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive = self.tmp / "symlink.enc"
        self.make_archive_with_extras({"maps/a.json": b"real\n"}, [(link, None)], archive)
        target = self.tmp / "never"
        done = self.run_script("restore", str(archive), str(target))
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse(target.exists())

    def test_a_payload_without_a_manifest_is_refused(self) -> None:
        archive = self.tmp / "nomanifest.enc"
        self.make_archive({"maps/a.json": b"data\n"}, None, archive)
        target = self.tmp / "never"
        done = self.run_script("restore", str(archive), str(target))
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse(target.exists())

    def test_an_empty_passphrase_is_refused_and_writes_no_archive(self) -> None:
        home = self.make_home()
        archive = self.tmp / "store.enc"
        done = self.run_script("backup", f"--home={home}", f"--out={archive}", passphrase="",
                               extra_env={"ANON_BACKUP_PASSPHRASE": ""})
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse(archive.exists())
        self.assertFalse((self.tmp / "store.enc.partial").exists())

    def test_an_existing_archive_is_refused_without_force(self) -> None:
        home = self.make_home()
        archive = self.tmp / "store.enc"
        self.assertEqual(self.run_script("backup", f"--home={home}", f"--out={archive}").returncode, 0)
        before = archive.read_bytes()
        done = self.run_script("backup", f"--home={home}", f"--out={archive}")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(archive.read_bytes(), before)

    def test_a_non_empty_target_is_refused_and_force_moves_it_aside(self) -> None:
        home = self.make_home()
        archive = self.tmp / "store.enc"
        self.assertEqual(self.run_script("backup", f"--home={home}", f"--out={archive}").returncode, 0)

        target = self.tmp / "inplace"
        (target / "maps").mkdir(parents=True)
        (target / "maps" / "old.json").write_text("the state being replaced\n", encoding="utf-8")

        refused = self.run_script("restore", str(archive), str(target))
        self.assertNotEqual(refused.returncode, 0)
        self.assertTrue((target / "maps" / "old.json").exists(), "the refused restore must change nothing")

        forced = self.run_script("restore", str(archive), str(target), "--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        kept = [p for p in self.tmp.iterdir() if p.name.startswith("inplace.pre-restore-")]
        self.assertEqual(len(kept), 1, "the previous tree must be kept, not deleted")
        self.assertTrue((kept[0] / "maps" / "old.json").exists())
        self.assertEqual((target / "maps" / "PLACEHOLDER_1.json").read_bytes(),
                         (home / "maps" / "PLACEHOLDER_1.json").read_bytes())

    # ---- the store's own contents are inputs too --------------------------------------------------

    def test_a_file_name_that_looks_like_a_tar_option_is_archived(self) -> None:
        """A name starting with `-` at the ROOT of the store must not be read as a tar option.

        bsdtar does not accept `--` after the first operand, so without the `./` prefix such an
        operand is parsed as an option (`-C.redacted` became `-C .redacted`) and the backup fails:
        no archive can be made at all until that file is removed. Reachability through the product
        is nil by construction — a download lives at `downloads/<token>/<name>`, and the directory
        prefix is enough to make its name an ordinary operand — so this is defence in depth for a
        name an operator put at the top level by hand.
        """
        home = self.make_home()
        tricky = home / "-C.redacted.pdf"
        tricky.write_text("a name that starts with a dash\n", encoding="utf-8")
        archive = self.tmp / "store.enc"
        done = self.run_script("backup", f"--home={home}", f"--out={archive}")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("-C.redacted.pdf", self.members(archive))
        target = self.tmp / "restored"
        self.assertEqual(self.run_script("restore", str(archive), str(target)).returncode, 0)
        self.assertEqual((target / "-C.redacted.pdf").read_bytes(), tricky.read_bytes())

    def test_a_download_name_that_starts_with_a_dash_is_archived(self) -> None:
        home = self.make_home()
        tricky = home / "downloads" / "-C.redacted.pdf"
        tricky.write_text("a download keeps its upload name\n", encoding="utf-8")
        archive = self.tmp / "store.enc"
        done = self.run_script("backup", f"--home={home}", f"--out={archive}")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("downloads/-C.redacted.pdf", self.members(archive))
        target = self.tmp / "restored"
        self.assertEqual(self.run_script("restore", str(archive), str(target)).returncode, 0)
        self.assertEqual((target / "downloads" / "-C.redacted.pdf").read_bytes(), tricky.read_bytes())

    def test_a_file_name_with_a_newline_is_refused_by_name(self) -> None:
        home = self.make_home()
        (home / "downloads" / "due\nrighe.txt").write_text("x\n", encoding="utf-8")
        archive = self.tmp / "store.enc"
        done = self.run_script("backup", f"--home={home}", f"--out={archive}")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("newline", done.stderr)
        self.assertFalse(archive.exists())

    def test_the_modes_of_the_store_come_back_unchanged(self) -> None:
        home = self.make_home()
        readable = home / "downloads" / "pubblico.txt"
        readable.write_text("not a secret\n", encoding="utf-8")
        os.chmod(readable, 0o644)
        archive = self.tmp / "store.enc"
        self.assertEqual(self.run_script("backup", f"--home={home}", f"--out={archive}").returncode, 0)
        target = self.tmp / "restored"
        self.assertEqual(self.run_script("restore", str(archive), str(target)).returncode, 0)
        self.assertEqual(stat.S_IMODE(os.stat(target / "downloads" / "pubblico.txt").st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(target / "entities.txt").st_mode), 0o600)

    def test_a_planted_manifest_in_a_subdirectory_is_refused(self) -> None:
        """Excluding the manifest by NAME excludes a `<subdir>/MANIFEST.sha256` too.

        Such a file is in no manifest and invisible to `shasum -c`, and the count that exists to
        catch exactly that would skip it: the restore used to report "1 file(s)" and write two.
        """
        archive = self.tmp / "planted.enc"
        self.make_archive_with_extras(
            {"maps/a.json": b"real\n"},
            [self.member("sub/MANIFEST.sha256", b"ATTACKER-PLANTED\n")],
            archive,
        )
        target = self.tmp / "never"
        done = self.run_script("restore", str(archive), str(target))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("manifest lists", done.stderr)
        self.assertFalse(target.exists())

    def test_no_program_started_before_the_passphrase_is_read_inherits_it(self) -> None:
        """The env path is documented; it must not reach ANY program the script starts.

        Not only `openssl`: the default destination uses `date`, the destination directory uses
        `dirname` and `mkdir`, the scratch tree uses `mktemp`. Each of them must run with the
        variable already removed from the environment.
        """
        env = self.shimmed(["openssl", "dirname", "mkdir", "date", "mktemp"])
        home = self.make_home()
        archive = self.tmp / "store.enc"
        done = self.run_script("backup", f"--home={home}", f"--out={archive}", extra_env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        restored = self.run_script("restore", str(archive), str(self.tmp / "restored"), extra_env=env)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        transcript = self.seen.read_text(encoding="utf-8")
        self.assertIn("CLEAN", transcript)
        self.assertNotIn("LEAKED", transcript, transcript)

    def test_the_archive_carries_no_extended_attributes(self) -> None:
        """The snapshot is contents, paths and modes.

        bsdtar records the macOS `com.apple.*` xattrs twice over — as `._name` members (which GNU tar
        extracts as real files) and as PAX records — so the tar invocation passes `--no-xattrs`.
        On Linux there is nothing to strip and this passes trivially.
        """
        home = self.make_home()
        archive = self.tmp / "store.enc"
        self.assertEqual(self.run_script("backup", f"--home={home}", f"--out={archive}").returncode, 0)
        raw = self.raw_members(archive)
        self.assertFalse([n for n in raw if "/._" in n or n.startswith("._")], raw)
        for member in tarfile.open(fileobj=io.BytesIO(self.decrypt(archive)), mode="r:gz").getmembers():
            keys = [k for k in member.pax_headers if "xattr" in k.lower() or "acl" in k.lower()]
            self.assertFalse(keys, f"{member.name}: {keys}")

    def test_a_file_named_like_macos_metadata_is_refused_by_name(self) -> None:
        """A real `._something` cannot be archived by bsdtar, which reads that name as AppleDouble
        metadata for its sibling.

        Measured: with such a file in the store, the archive bsdtar writes is corrupt ("Truncated
        input file") and cannot be extracted — with `COPYFILE_DISABLE=1`, with `--no-xattrs`, with
        `--no-mac-metadata` and with every pair of them. So the name is refused here, with the file
        named and the remedy given, rather than producing a broken archive or dropping it silently.
        """
        home = self.make_home()
        real = home / "._entities.txt"
        real.write_text("a real file with that name, not macOS metadata\n", encoding="utf-8")
        archive = self.tmp / "store.enc"
        done = self.run_script("backup", f"--home={home}", f"--out={archive}")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("macOS metadata", done.stderr)
        self.assertIn("._entities.txt", done.stderr)
        self.assertFalse(archive.exists())

    def test_a_directory_member_is_refused(self) -> None:
        """Our archive holds files only, and a directory member is not harmless.

        A member named `.` carrying mode 0777 is applied by `-p` to the restore TARGET itself, so a
        crafted archive took the store from 0700 to 0777 — with a `sub/MANIFEST.sha256` directory
        written on the way (in no manifest, and `find -type f` never counted it).
        """
        archive = self.tmp / "dirs.enc"
        self.make_archive_with_extras(
            {"maps/a.json": b"real\n"},
            [self.directory(".", 0o777), self.directory("sub/MANIFEST.sha256")],
            archive,
        )
        target = self.tmp / "never"
        done = self.run_script("restore", str(archive), str(target))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("plain file", done.stderr)
        self.assertFalse(target.exists())

    def test_the_destination_directory_is_created_when_it_does_not_exist(self) -> None:
        home = self.make_home()
        archive = self.tmp / "deep" / "deeper" / "store.enc"
        done = self.run_script("backup", f"--home={home}", f"--out={archive}")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(archive.exists())

    def test_a_missing_home_is_refused(self) -> None:
        out = self.tmp / "x.enc"
        done = self.run_script("backup", f"--home={self.tmp / 'absent'}", f"--out={out}")
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse(out.exists())

    def test_an_empty_home_is_refused(self) -> None:
        home = self.tmp / "empty"
        home.mkdir()
        out = self.tmp / "x.enc"
        done = self.run_script("backup", f"--home={home}", f"--out={out}")
        self.assertNotEqual(done.returncode, 0)
        self.assertFalse(out.exists())

    def test_usage_and_an_unknown_command(self) -> None:
        self.assertIn("usage:", self.run_script("help").stdout)
        self.assertIn("usage:", self.run_script("--help").stdout)
        unknown = self.run_script("nonsense")
        self.assertNotEqual(unknown.returncode, 0)
        self.assertIn("usage:", unknown.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
