#!/usr/bin/env python3
"""anon.py — deterministic, local anonymizer for text documents.

The anonymizer CANNOT be an LLM: to anonymize, a model would first have to receive the
data. So detection is 100% deterministic (regex + a curated dictionary) and 100% local
(Python stdlib only, no network, no telemetry).

  input file  ->  <name>.redacted.<ext>  (+ a reversible map in ~/.anon/maps/)

Every sensitive span is replaced by a typed placeholder (`[EMAIL-1]`, `[CLIENTE-2]`, ...)
and recorded in a JSON map. `deanon.py` re-applies the real values to the finished
document, so "client document -> work in Pi -> final report" loses nothing.

Modes
  anon.py FILE [--out PATH] [--map PATH] [--entities PATH ...] [--stdout] [--quiet]
  anon.py FILE --check [--json]      # no output written; report only (used by anon-guard)
  anon.py --prune-maps DAYS [--yes]  # list (or delete) the maps older than DAYS

Exit codes
  0  ok (or --check: nothing sensitive found, or --audit: clean)
  1  --check: sensitive content found
  2  error (bad arguments, unreadable file, unparseable entities)
  4  --audit: nothing directly sensitive, but near-miss candidates (not clean, not proof)

Design notes
  * Idempotent: already-present placeholders are protected, so anonymizing twice is a no-op
    and never corrupts the document.
  * Lossless: the map stores the exact matched substring, so anon -> deanon reproduces the
    original byte for byte.
  * The map (which holds the REAL values) is written only under ~/.anon/maps/ (mode 0600),
    never next to the document, never inside a working directory.
"""

from __future__ import annotations

import argparse
import array
import fnmatch
import ipaddress
import json
import os
import re
import sys
import time
import zipfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape as _sax_escape
from typing import Callable, Iterable

VERSION = "1.7.0"
SCHEMA = "anon/1"  # stable machine contract for every --json output of the suite

ANON_HOME = Path(os.environ.get("ANON_HOME") or (Path.home() / ".anon"))
DEFAULT_ENTITIES = ANON_HOME / "entities.txt"
# The dictionary is split by kind so each file stays small and precise. All three are optional, and
# each may open with `@type X` and then list bare values. `entities.txt` stays the generic one.
DEFAULT_PEOPLE = ANON_HOME / "people.txt"  # `@type PERSONA`
DEFAULT_CLIENTS = ANON_HOME / "clients.txt"  # `@type AZIENDA` — companies and their addresses
DEFAULT_DICTIONARIES = (DEFAULT_ENTITIES, DEFAULT_PEOPLE, DEFAULT_CLIENTS)
# Named access for the front-ends (the web UI's Dizionario tab, the query parameter `file`).
DICTIONARIES = {"entities": DEFAULT_ENTITIES, "people": DEFAULT_PEOPLE, "clients": DEFAULT_CLIENTS}
DEFAULT_MAPS = ANON_HOME / "maps"
DEFAULT_ALLOW = ANON_HOME / "allow.txt"
CATALOGS_DIR = ANON_HOME / "catalogs"

# `[EMAIL-1]` is the bare form; `[EMAIL-1-a3f9]` carries a per-map tag. The tag is what makes a
# WRONG MAP detectable: placeholders are numbered per document, so without it a document from run
# A silently accepts run B's map whenever the numbers happen to line up. Absent tag = a map made
# before this existed (still supported).
PLACEHOLDER_RE = re.compile(r"\[[A-Z][A-Z0-9_]*-\d+(?:-[0-9a-f]{4,8})?\]")

# Values that are structurally sensitive-looking but are deliberately public: RFC 2606
# reserved domains, RFC 5737 documentation networks, loopback/wildcard. Redacting them
# would only hurt readability (and turn every `example.com` in a test into noise).
SAFE_EXACT = {
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "::1",
    "example.com",
    "example.org",
    "example.net",
    "user@example.com",
    "test@example.com",
    "noreply@example.com",
    "john.doe@example.com",
}
SAFE_DOMAIN_SUFFIXES = (".example", ".test", ".invalid", ".localhost")
SAFE_NETS: tuple[ipaddress._BaseNetwork, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("0.0.0.0/32"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("::1/128"),
    # Public resolvers / netmask: appear in every network config, never sensitive.
    ipaddress.ip_network("8.8.8.8/32"),
    ipaddress.ip_network("8.8.4.4/32"),
    ipaddress.ip_network("1.1.1.1/32"),
    ipaddress.ip_network("1.0.0.1/32"),
    ipaddress.ip_network("9.9.9.9/32"),
    ipaddress.ip_network("255.255.255.255/32"),
)

# File extensions that look like a two-label "FQDN" but are filenames. Without this,
# `anon.py` (TLD .py == Paraguay) or `main.js` would be redacted and reads blocked.
FILE_EXT = {
    "py", "pyc", "pyi", "ts", "tsx", "js", "jsx", "mjs", "cjs", "json", "json5", "md",
    "markdown", "txt", "text", "rst", "yaml", "yml", "toml", "ini", "cfg", "conf", "env",
    "log", "csv", "tsv", "xml", "html", "htm", "css", "scss", "less", "sh", "bash", "zsh",
    "fish", "ps1", "bat", "cmd", "c", "h", "cc", "cpp", "hpp", "cs", "java", "kt", "go",
    "rs", "rb", "php", "pl", "lua", "swift", "scala", "sql", "db", "sqlite", "lock", "map",
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "ico", "pdf", "doc", "docx", "xls",
    "xlsx", "ppt", "pptx", "odt", "ods", "odp", "rtf", "epub", "zip", "tar", "gz", "tgz",
    "bz2", "xz", "7z", "rar", "dmg", "exe", "msi", "deb", "rpm", "bin", "iso", "img",
    "mp3", "mp4", "mov", "avi", "mkv", "wav", "flac", "ttf", "otf", "woff", "woff2",
    "plist", "service", "patch", "diff", "bak", "tmp", "old", "orig", "example", "am",
}

# A dotted token is only a hostname when its LAST label looks like a real TLD. The earlier
# rule ("3+ labels = host") redacted ordinary code — `os.environ.get`, `numpy.random.normal`,
# `child.stdout.on` — which made the default-on guard unusable. Word-like ccTLDs that collide
# with English/method names (`in`, `id`, `is`, `as`, `at`, `by`, `be`, `do`, `to`, `me`, `no`,
# `so`) and generic gTLD words (`info`, `name`, `site`, `app`, `dev`, `cloud`, `home`, ...) are
# deliberately absent: `logger.info`, `obj.id`, `arr.at` must not be treated as hosts.
ORG_TLDS = {
    "com", "org", "net", "edu", "gov", "mil", "int", "eu",
    "it", "ch", "fr", "de", "es", "pt", "nl", "uk", "ie", "dk", "se", "fi", "pl", "cz", "sk",
    "hu", "ro", "gr", "hr", "si", "rs", "bg", "lt", "lv", "ee", "mt", "cy", "lu", "us", "ca",
    "mx", "br", "ar", "cl", "au", "nz", "jp", "cn", "kr", "sg", "za", "tr", "ru", "ua", "il",
    "ae", "sa", "eg", "al", "ba", "mk", "ge", "am", "az", "kz", "md",
    "local", "lan", "internal", "intranet", "corp", "azienda",
}


def _is_safe(value: str) -> bool:
    """True for known-public values that must NOT be redacted."""
    v = value.strip().lower()
    if not v:
        return True
    if v in SAFE_EXACT:
        return True
    if any(v.endswith(suffix) for suffix in SAFE_DOMAIN_SUFFIXES):
        return True
    if "://" in v or v.startswith("www."):
        host = re.sub(r"^[a-z][a-z0-9+.\-]*://", "", v).split("/")[0]
        host = host.rsplit("@", 1)[-1].split(":")[0]
        return _is_safe(host[4:] if host.startswith("www.") else host)
    if "@" in v:
        return _is_safe(v.rsplit("@", 1)[1])
    try:
        addr = ipaddress.ip_address(v)
    except ValueError:
        return False
    return any(addr in net for net in SAFE_NETS)


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _valid_host(value: str) -> bool:
    labels = value.split(".")
    if len(labels) < 2:
        return False
    tld = labels[-1].lower()
    return tld in ORG_TLDS and tld not in FILE_EXT


def _valid_phone(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    return 9 <= len(digits) <= 15


# Values that are obviously written-in-place, not secrets: redacting them would block reading
# every README and example config. The match is on the WHOLE value — a substring test made
# `password: postgrespassword` and `password: mypassword123` slip through, which is worse than
# a false positive (an unredacted real secret).
_PLACEHOLDER_VALUES = frozenset(
    {
        "changeme", "change-me", "change_me", "changed", "placeholder", "redacted", "dummy",
        "example", "sample", "fake", "none", "null", "nil", "todo", "secret", "password",
        "your", "your-password", "your-secret", "your-token", "your-key", "your-api-key",
        "your-api-key-here", "your-key-here", "your-token-here", "your-secret-here",
        "see-above", "see-below", "see-above-", "n-a", "na", "unknown", "not-set", "notset",
        "xxx", "xxxx", "xxxxx", "xxxxx xxxxx", "test", "testing", "local", "dev",
    }
)


# Anchored (not substring) prefix test: `placeholder-value` and `your-token` are placeholders,
# while `postgrespassword`, `mypassword123` and `correct-horse-battery-staple` are real secrets.
_PLACEHOLDER_PREFIX = re.compile(
    r"^(?:your|placeholder|example|sample|dummy|fake|changeme|change|secret|password|token|"
    r"key|xxx|todo|none|null|not|see)(?:[-_.]|$)"
)


# --- Italian/IBAN identifiers: FORMAT + CHECKSUM. A validated identifier has almost no false
# positives, which is the opposite of a word list — this is where a privacy tool gets its
# precision. Algorithms: DPR 633/1972 (partita IVA), the standard CF check-character tables,
# ISO 13616 mod-97 (IBAN).
_OMOCodia = str.maketrans({"L": "0", "M": "1", "N": "2", "P": "3", "Q": "4",
                          "R": "5", "S": "6", "T": "7", "U": "8", "V": "9"})
_CF_ODD = {
    "0": 1, "1": 0, "2": 5, "3": 7, "4": 9, "5": 13, "6": 15, "7": 17, "8": 19, "9": 21,
    "A": 1, "B": 0, "C": 5, "D": 7, "E": 9, "F": 13, "G": 15, "H": 17, "I": 19, "J": 21,
    "K": 2, "L": 4, "M": 18, "N": 20, "O": 11, "P": 3, "Q": 6, "R": 8, "S": 12, "T": 14,
    "U": 16, "V": 10, "W": 22, "X": 25, "Y": 24, "Z": 23,
}
# Even positions: a digit is its own value, a letter is its 0-based alphabet index (A=0..Z=25).
_CF_EVEN = {
    **{str(digit): digit for digit in range(10)},
    **{char: index for index, char in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")},
}
_CF_FORMAT = re.compile(
    r"[A-Z]{6}[0-9LMNPQRSTUV]{2}[ABCDEHLMPRST][0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{3}[A-Z]"
)


def _valid_codice_fiscale(value: str) -> bool:
    """16 chars whose 16th is the check character computed from the first 15."""
    candidate = value.strip().upper()
    if not _CF_FORMAT.fullmatch(candidate):
        return False
    # Omocodia: a numeric field may be written with letters (0->L, 1->M, ...). Decode first.
    body = candidate[:15]
    digits = [body[6], body[7], body[9], body[10], body[12], body[13], body[14]]
    decoded = "".join(digits).translate(_OMOCodia)
    if not decoded.isdigit():
        return False
    body = body[:6] + decoded[0:2] + body[8] + decoded[2:4] + body[11] + decoded[4:7]
    total = 0
    for index, char in enumerate(body, start=1):
        total += _CF_ODD[char] if index % 2 else _CF_EVEN[char]
    return "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[total % 26] == candidate[15]


def _valid_partita_iva(value: str) -> bool:
    """11 digits; the 11th is (10 - total mod 10) mod 10, doubling the EVEN positions."""
    candidate = re.sub(r"\D", "", value)
    if len(candidate) != 11:
        return False
    total = 0
    for index, char in enumerate(candidate[:10], start=1):
        digit = int(char)
        if index % 2 == 0:
            doubled = digit * 2
            total += doubled // 10 + doubled % 10
        else:
            total += digit
    return (10 - total % 10) % 10 == int(candidate[10])


_IBAN_FORMAT = re.compile(r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}")


def _valid_iban(value: str) -> bool:
    """ISO 13616: move the first 4 chars to the end, letters -> 10..35, mod 97 must be 1."""
    candidate = re.sub(r"[\s\-]", "", value).upper()
    if not _IBAN_FORMAT.fullmatch(candidate):
        return False
    rearranged = candidate[4:] + candidate[:4]
    numeric = "".join(str(int(char, 36)) for char in rearranged)
    return int(numeric) % 97 == 1


def _valid_targa(value: str) -> bool:
    """Current Italian plate format (AA123BB). No checksum exists, so the format is the rule."""
    return bool(re.fullmatch(r"[A-Z]{2}\d{3}[A-Z]{2}", value.strip().upper()))


def _valid_secret(value: str) -> bool:
    """False for values that are placeholders (`your-api-key-here`, `changeme`)."""
    lowered = value.casefold().strip()
    if lowered in _PLACEHOLDER_VALUES:
        return False
    if _PLACEHOLDER_PREFIX.match(lowered):
        return False
    if lowered.startswith(("<", "${", "{{", "%(", "[", "@")):
        return False  # templated / variable reference, not a literal secret
    if set(lowered) <= set("*._- "):
        return False  # `****`, `....`, `----`
    return True


@dataclass(frozen=True)
class Rule:
    type: str
    regex: re.Pattern[str]
    capture: int = 0
    validator: Callable[[str], bool] | None = None
    trim_trailing: str = ""
    # Pattern family, so a caller can enable/disable a whole class (`identity`, `network`,
    # `legal`). Ticked on demand in the UI, `--patterns` on the command line.
    family: str = "identity"
    # When the validator rejects a match, retry with trailing labels removed. A hostname
    # followed by a file extension (`db01.azienda.it.log`) must still yield the host.
    shrink_labels: bool = False


# Ordered by confidence: a URL/email/JWT/key span is claimed before any heuristic can
# carve a substring out of it. Dictionary entities (curated, real) come next; IP/phone/host
# heuristics last.


# Street words that introduce an address. The abbreviations (`v.le`, `p.zza`) are how Italian
# documents write them, and matching them is what keeps `V.le Europa, 12` one span instead of a
# half-redacted "V.le" plus a visible name.
ADDRESS_MARKERS = (
    r"(?:via|viale|v\.?le|piazza|piazzale|p\.?zza|p\.?za|corso|strada|vicolo|largo|lungomare|"
    r"traversa|borgo|stradone|salita)"
)
ADDRESS_SRC = (
    r"(?<![\w])" + ADDRESS_MARKERS + r"\s+"
    # Street name: 1..5 tokens, with dots and hyphens allowed inside (`G.`, `Mazzini-Rossi`).
    r"(?P<strada>[^\W\d_][\w'\u2019.\-]*(?:\s+[\w'\u2019.\-]+){0,4}?)"
    r"[,\s]+(?:n\.?\s*)?"
    # Civic number, required, with the Italian suffix (`3/A`, `12-bis`).
    r"(?P<civico>\d{1,4})(?:\s*[/\-]\s*[A-Za-z0-9]{1,3})?(?![\w])"
)
ADDRESS_RE = re.compile(ADDRESS_SRC, re.IGNORECASE)


def _address_street(value: str) -> str | None:
    """The street-name part of a matched address, with its ORIGINAL capitalization.

    `re.IGNORECASE` decides whether a string matches; the captured group still carries the
    characters as written, which is exactly what `_valid_address` has to read.
    """
    shape = ADDRESS_RE.fullmatch(value.strip())
    return shape.group("strada") if shape else None


def _valid_address(value: str) -> bool:
    """A marker word plus a civic number is not an address on its own.

    `in via del tutto eccezionale, 3 volte` has the shape and none of the content, and it was the
    false positive that made this rule untrustworthy. Requiring one capitalized name token drops
    that whole class while keeping every form in the tests' address corpus (`Via G. Verdi 3/A`,
    `Piazza G. Verdi, 3`, `Via Roma, n. 3`, `V.le Europa 12`, `VIA ROMA 12`).

    Declared trade-off: an all-lowercase address (`via roma 12`) is NOT redacted - precision over
    recall, the same choice the hostname heuristic makes, and the reason the guard stays usable.
    """
    street = _address_street(value)
    if street is None:
        return False
    # `d'Azeglio`, `dell'Università`, `l'Aquila`: the elided article is lowercase and the name is
    # not, so the capitalization test looks at the part AFTER the apostrophe.
    for token in street.split():
        head = token.rsplit("'", 1)[-1].rsplit("\u2019", 1)[-1]
        if head[:1].isupper():
            return True
    return False


RULES: tuple[Rule, ...] = (
    Rule(
        "URL",
        re.compile(r"(?:(?:https?|ftp|sftp)://|www\.)[^\s<>\"'()\[\]{}]+", re.IGNORECASE),
        trim_trailing=".,;:!?)]}\u00bb\"'",
    ),
    # Local part and labels are BOUNDED (RFC 5321/1035): an unbounded `+` here made the
    # regex quadratic on a long run of word chars with a single '@' (minified JS, base64
    # blobs): 8k chars took 1.7s, 400k would have taken minutes — and the guard's timeout
    # turns that into a silent fail-open. Bounded quantifiers keep it linear.
    Rule(
        "EMAIL",
        re.compile(
            r"[A-Za-z0-9._%+\-]{1,64}@"
            r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
            r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?){1,10}"
        ),
    ),
    Rule("KEY", re.compile(r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\b")),
    Rule("KEY", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    Rule("KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    Rule("KEY", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
    Rule("KEY", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    Rule("KEY", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    Rule(
        "KEY",
        re.compile(r"(?i:bearer)\s+([A-Za-z0-9._\-]{20,})", re.IGNORECASE),
        capture=1,
        validator=_valid_secret,
    ),
    Rule(
        "KEY",
        re.compile(
            # An optional `prefix_` is allowed so `db_password:` / `db_secret:` match; the
            # lookbehind (instead of \b) is what lets an underscore-prefixed name through.
            r"(?i:(?<![A-Za-z0-9])(?:[A-Za-z0-9]+[_-])?(?:api[_-]?key|apikey|secret|"
            r"client[_-]?secret|password|passwd|pwd|token|access[_-]?token|refresh[_-]?token)"
            r"\b\s*[:=]\s*[\"']?)"
            # Reject a right-hand side that is a CALL (`token = re.compile(...)`): the `(` comes
            # immediately after the value, which a literal secret does not do. The first lookahead
            # keeps the token whole, so backtracking cannot truncate it to make the call check pass.
            # Deliberately NOT rejected: a dotted value (`variant.first_token`, `admin.secret`) — it
            # can be a real password, and a missed secret is worse than a false positive (item #20).
            r"([A-Za-z0-9._\-+/=]{8,})(?![A-Za-z0-9._\-+/=])(?!\()"
        ),
        capture=1,
        validator=_valid_secret,
    ),
    # --- legal/administrative identifiers (validated, so precision is high) ----------------
    Rule(
        "CODICEFISCALE",
        re.compile(r"(?<![\w])[A-Z]{6}[0-9LMNPQRSTUV]{2}[ABCDEHLMPRST][0-9LMNPQRSTUV]{2}[A-Z][0-9LMNPQRSTUV]{3}[A-Z](?![\w])", re.IGNORECASE),
        validator=_valid_codice_fiscale,
        family="legal",
    ),
    Rule(
        "PARTITAIVA",
        # `(?:IT)?` — writing `IT?` would mean "an I followed by an optional T".
        re.compile(r"(?<![\d.])(?:IT)?\s?(\d{11})(?!\d)", re.IGNORECASE),
        capture=1,
        validator=_valid_partita_iva,
        family="legal",
    ),
    Rule(
        "IBAN",
        re.compile(r"(?<![\w])IT\s?\d{2}\s?[A-Z]\s?(?:[A-Z0-9]\s?){10,30}(?![\w])", re.IGNORECASE),
        validator=_valid_iban,
        family="legal",
    ),
    Rule(
        "TARGA",
        re.compile(r"(?<![\w])[A-Z]{2}\s?\d{3}\s?[A-Z]{2}(?![\w])"),
        validator=_valid_targa,
        family="legal",
    ),
    Rule(
        # An address identifies a person as surely as a name does. The marker word is required,
        # and at least one name token plus a civic number: `via Roma 12`, `Piazza G. Verdi, 3`.
        "INDIRIZZO",
        ADDRESS_RE,
        validator=_valid_address,
        family="legal",
    ),
)

HEURISTIC_RULES: tuple[Rule, ...] = (
    Rule("IP", re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"), validator=_valid_ip, family="network"),
    Rule(
        "IP",
        re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])"),
        validator=_valid_ip,
        family="network",
    ),
    Rule(
        "TEL",
        re.compile(
            # +CC international (covers +39 Italy too), 0039 and bare Italian mobiles
            r"(?<![\w.])(?:\+\d{1,3}[\s.\-/]?\d{2,4}[\s.\-/]?\d{3,4}[\s.\-/]?\d{2,4}"
            r"|0039[\s.\-/]?\d{5,12}"
            r"|3\d{2}[\s.\-/]?\d{3,4}[\s.\-/]?\d{3})(?![\w])"
        ),
        validator=_valid_phone,
        family="network",
    ),
    Rule(
        "HOST",
        re.compile(
            r"(?<![\w.\-])(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+"
            r"[A-Za-z]{2,63}(?![\w\-])(?!\.\w)"  # allow a trailing sentence period
        ),
        validator=_valid_host,
        shrink_labels=True,
        family="network",
    ),
)

PATTERN_FAMILIES = ("identity", "network", "legal")

# Tie-break priorities, used only when two candidates cover the SAME span: the strong patterns
# win, then the curated dictionary (operator intent), then the heuristics.
ENTITY_PRIORITY = 500
HEURISTIC_PRIORITY = 1000


# Legal forms are stripped from the dictionary value and tolerated as an optional suffix, so a
# single entry matches the whole family: `Contoso` covers `Contoso S.r.l.`, `contoso srl`, `CONTOSO`.
# Whole-token match, bounded quantifiers only (no nested `*` on `\s`: no ReDoS surface).
LEGAL_FORM_SRC = (
    r"(?:s\.?\s?r\.?\s?l\.?\s?s?"  # srl / s.r.l. / srls
    r"|s\.?\s?p\.?\s?a\.?"  # spa
    r"|s\.?\s?n\.?\s?c\.?"  # snc
    r"|s\.?\s?a\.?\s?s\.?"  # sas
    r"|s\.?\s?a\.?"  # sa
    r"|s\.?\s?s\.?"  # ss
    r"|ltd|llc|inc|gmbh|ag|bv|nv|sarl|sàrl|sl|oy|ab|aps|pte|pty|corp|kg|ug|sro|doo|ooo|plc|llp"
    r"|ltda|eirl|slu|sagl|sr[l]?"
    r"|&\s?c\.?|e\s?c\.)"
)
LEGAL_FORM_RE = re.compile(LEGAL_FORM_SRC, re.IGNORECASE)
LEGAL_SUFFIX_RE = r"(?:\s+" + LEGAL_FORM_SRC + r"(?![a-z0-9]))?"
# Separators allowed between the words of a name: `Acme-Italia` = `Acme.Italia` = `Acme Italia`.
NAME_SEPARATOR_RE = r"[\s.\-_'’,]+"


ENTITY_GROUP = "entity"


@dataclass(frozen=True)
class Entity:
    """A dictionary entry: what to look for, what it is, and how it matches."""

    type: str
    surface: str
    regex: re.Pattern[str]
    # Fast-scan metadata filled by `_entity_regexes` (see `entity_hits`). An entry built by hand
    # has none, and is then scanned directly instead of being skipped.
    first_token: str = ""
    inner: str = ""
    context: str | None = None
    form: str = "NFC"

    def spans(self, text: str):
        """Yield (start, end, matched_text) for every occurrence.

        The reference implementation: one full-text pass per entry. Correct and slow — it is
        what `entity_hits` has to reproduce, and what the equivalence test compares against.
        """
        for match in self.regex.finditer(text):
            start, end = match.span(ENTITY_GROUP)
            yield start, end, match.group(ENTITY_GROUP)


@dataclass(frozen=True)
class EntityVariant:
    """One compiled form of a dictionary entry, plus what a fast scan needs to find it."""

    regex: re.Pattern[str]
    # The same pattern WITHOUT the `(?P<entity>…)` wrapper: several entries that share a
    # `@context` are compiled into one alternation, which cannot repeat the group name.
    inner: str
    # The first name token, normalized like the pattern. Every match contains it verbatim, so a
    # text that does not contain it can skip the pattern entirely (the whole point of the scan).
    first_token: str
    # `@context` entries are NOT literal-scannable: their match starts at the context, not at the
    # first token. They are grouped by (context, normalization form) and scanned once per group.
    context: str | None = None
    form: str = "NFC"


def _name_tokens(value: str) -> list[str]:
    """The surface form split into tokens, with a trailing legal form dropped.

    `len(tokens) > 1`: a company literally named "SA"/"AG"/"AB" must stay declarable. Stripping
    its only token would produce zero regexes and silently drop the entry (a false negative).
    """
    tokens = [token for token in re.split(r"\s+", value.strip()) if token]
    while len(tokens) > 1 and LEGAL_FORM_RE.fullmatch(tokens[-1]):
        tokens.pop()
    return tokens


def _entity_regexes(
    value: str,
    *,
    stem: bool = False,
    case_sensitive: bool = False,
    context: str | None = None,
) -> list[EntityVariant]:
    """One variant per Unicode normalization form, so a macOS NFD file matches an NFC dictionary."""
    tokens = _name_tokens(value)
    if not tokens:
        return []
    core = NAME_SEPARATOR_RE.join(re.escape(token) for token in tokens) + LEGAL_SUFFIX_RE
    if stem:
        # `Pincopallino` must also cover `Pincopallino1`, `Pincopallino-DB01`, `PINCOPALLINO_srv`.
        # Opt-in per entry: on a common word a stem rule would over-redact.
        core += r"[\w\-]*"
    # Case sensitivity applies to the ENTITY only: a `@context` marker like "comune di" must
    # still match however it is capitalized in the document, while `Prato` must not match `prato`.
    inner = f"(?-i:{core})" if case_sensitive else core
    prefix = f"(?:{context})(?<!\\w)" if context else r"(?<!\w)"
    variants: list[EntityVariant] = []
    seen: set[str] = set()
    for form in ("NFC", "NFD"):
        # The first token is normalized like the pattern: an NFC token compared against an NFD
        # text (or the reverse) would make the fast scan miss a match the pattern would find.
        normalized_inner = unicodedata.normalize(form, inner)
        normalized_context = unicodedata.normalize(form, context) if context else None
        normalized_prefix = f"(?:{normalized_context})(?<!\\w)" if context else prefix
        full = f"{normalized_prefix}(?P<{ENTITY_GROUP}>{normalized_inner})(?!\\w)"
        if full in seen:  # pure-ASCII entries normalize to the same pattern twice
            continue
        seen.add(full)
        variants.append(
            EntityVariant(
                regex=re.compile(full, re.IGNORECASE),
                inner=normalized_inner,
                first_token=unicodedata.normalize(form, tokens[0]),
                context=normalized_context,
                form=form,
            )
        )
    return variants


# Directives apply to the entries that FOLLOW them, so one file can mix behaviours (a
# case-sensitive, context-gated city catalog next to plain names).
KNOWN_DIRECTIVES = ("type", "stem", "match", "context")
_TRUE = ("", "on", "true", "yes", "1")
_FALSE = ("off", "false", "no", "0")

# A stem matches any suffix (`Acme` -> `AcmeCorp`, `Acme-DB01`), so a 3-4 character stem quietly
# redacts unrelated words.
STEM_MIN_CHARS = 5

# Warned once per stem body per process: the web UI reloads the dictionary on every request, and
# an operator must not get the same line of stderr on every click.
_WARNED_STEMS: set[str] = set()


def _warn_short_stem(path: Path, lineno: int, value: str) -> None:
    """Warn — never refuse — when a `@stem` entry is short enough to over-redact.

    Refusing would be a hard error on a live dictionary, and an engine that fails to load is an
    engine the Pi guard reports as unreachable: it then turns itself OFF for the session. A
    dictionary nit must not be able to fail open a privacy control.
    """
    body = "".join(_name_tokens(value))
    if len(body) >= STEM_MIN_CHARS or body.casefold() in _WARNED_STEMS:
        return
    _WARNED_STEMS.add(body.casefold())
    print(
        f"anon: {path.name} line {lineno}: @stem on {value!r} ({len(body)} characters) — a stem "
        "this short also matches unrelated words; declare the full forms instead",
        file=sys.stderr,
    )


def load_entities(path: Path) -> list[Entity]:
    """Parse a dictionary/catalog file.

    Line syntax:
        TYPE|value                a single surface form
        TYPE|value|alias|alias    extra surface forms of the same entity
        value                     no `|` -> TYPE is ALTRO
        @type X                   type for the entries that follow
        @stem on|off              `Pincopallino` also matches `Pincopallino1`
        @match case-sensitive     do not case-fold (proper nouns: `Brescia`, not `prato`)
        @context <regex>          only match when preceded by this context
        @context off              stop requiring a context for the entries that follow (the exact
                                  token `off`; any other value is a regex, `no` included)

    ORDERING RULE: every directive applies to the ENTRIES THAT FOLLOW IT, and a later directive
    replaces the earlier one — the file is read top to bottom. So a `@context` is not a property of
    the file: it is a property of the block it opens, and `@context off` is how you close it.

    An unknown directive is a hard ERROR, never ignored: a typo in `@stem`/`@context` would
    silently change what gets redacted, and silent under-redaction is a leak.
    """
    if not path.exists():
        return []
    entries: list[Entity] = []
    seen: set[tuple[str, str]] = set()
    ptype, stem, case_sensitive, context = "ALTRO", False, False, None

    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("@"):
            name, _, argument = line[1:].partition(" ")
            name, argument = name.strip().lower(), argument.strip()
            if name not in KNOWN_DIRECTIVES:
                raise ValueError(f"{path}:{lineno}: unknown directive '@{name}'")
            if name == "type":
                if not argument:
                    raise ValueError(f"{path}:{lineno}: @type needs a value")
                ptype = argument.upper()
            elif name == "stem":
                if argument.lower() not in _TRUE + _FALSE:
                    raise ValueError(f"{path}:{lineno}: @stem takes on|off, not {argument!r}")
                stem = argument.lower() in _TRUE
            elif name == "match":
                if argument.lower() not in ("case-sensitive", "sensitive", "insensitive", "case-insensitive", "i"):
                    raise ValueError(f"{path}:{lineno}: @match takes case-sensitive|insensitive")
                case_sensitive = argument.lower() in ("case-sensitive", "sensitive")
            else:  # context
                # ONLY the exact token `off` clears the context. Deliberately not the shared
                # `_FALSE` list (`no`, `0`, `false`): those are legitimate @context REGEXES, and
                # treating them as "off" would silently drop a gate the operator wrote.
                if argument.lower() == "off":
                    context = None
                else:
                    if not argument:
                        raise ValueError(f"{path}:{lineno}: @context needs a regex (or 'off')")
                    try:
                        re.compile(argument)  # fail loudly on a broken context, not at match time
                    except re.error as exc:
                        # Normalized to ValueError so every bad-dictionary failure reaches the caller
                        # as one error type (the CLI turns it into exit 2).
                        raise ValueError(f"{path}:{lineno}: invalid @context regex: {exc}") from exc
                    context = argument
            continue

        fields = [field.strip() for field in line.split("|")]
        if len(fields) >= 2:
            line_type = (fields[0] or ptype).upper()
            forms = [form for form in fields[1:] if form]
        else:
            line_type, forms = ptype, [line]
        if not forms:
            print(f"anon: {path.name} line {lineno}: no value, skipped", file=sys.stderr)
            continue
        for value in forms:
            key = (line_type, value.casefold())
            if key in seen:
                continue
            seen.add(key)
            variants = _entity_regexes(
                value, stem=stem, case_sensitive=case_sensitive, context=context
            )
            if not variants:
                print(f"anon: {path.name} line {lineno}: '{line_type}' has no usable name, skipped", file=sys.stderr)
                continue
            if stem:
                _warn_short_stem(path, lineno, value)
            entries.extend(
                Entity(
                    line_type,
                    value,
                    variant.regex,
                    first_token=variant.first_token,
                    inner=variant.inner,
                    context=variant.context,
                    form=variant.form,
                )
                for variant in variants
            )
    # Longest surface first, so "Acme Italia" wins over "Acme".
    entries.sort(key=lambda entity: len(entity.surface), reverse=True)
    return entries


def load_entities_many(paths: Iterable[Path]) -> list[Entity]:
    """The custom dictionary plus every selected catalog, as one list."""
    merged: list[Entity] = []
    for path in paths:
        merged.extend(load_entities(Path(path).expanduser()))
    merged.sort(key=lambda entity: len(entity.surface), reverse=True)
    return merged


def _as_list(value) -> list[str]:
    """Accept both a comma-separated CLI string and a JSON array (the web UI sends arrays).

    Anything else is a malformed REQUEST: `{"catalogs": 5}` used to raise `TypeError` out of the
    iteration and become a 500. It is a `ValueError` now, which the API answers with a 400.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError(f"expected a list of names, found {type(value).__name__}")


def entity_count(entities: Iterable[Entity]) -> int:
    """Distinct declared entries — not the internal per-normalization regex variants.

    One accented or non-ASCII entry compiles to two patterns (NFC + NFD), so counting rules
    would report `100 voci` for a 50-city catalog.
    """
    return len({(entity.type, entity.surface) for entity in entities})


def catalog_path(name: str) -> Path:
    return CATALOGS_DIR / (name if name.endswith(".txt") else f"{name}.txt")


def list_catalogs() -> list[dict[str, object]]:
    """Available catalogs (name, path, entry count) for `--list-catalogs` and the web UI."""
    if not CATALOGS_DIR.is_dir():
        return []
    available = []
    for path in sorted(CATALOGS_DIR.glob("*.txt")):
        try:
            count = entity_count(load_entities(path))
        except ValueError:
            count = -1
        available.append({"name": path.stem, "path": str(path), "entries": count})
    return available


def load_allowlist(path: Path) -> list[str]:
    """Path globs whose contents are never considered sensitive (e.g. a public repo)."""
    if not path.exists():
        return []
    patterns: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            patterns.append(line)
    return patterns


def is_allowed(path: Path, patterns: Iterable[str]) -> bool:
    expanded = path.expanduser()
    # Match both the literal absolute path and the symlink-resolved one: on macOS
    # /var -> /private/var, so a glob written by the user must match either form.
    candidates = {os.path.abspath(str(expanded)), str(expanded.resolve())}
    for pattern in patterns:
        if fnmatch.fnmatch(expanded.name, pattern) or any(fnmatch.fnmatch(c, pattern) for c in candidates):
            return True
    return False


def entity_hits(text: str, entities: Iterable[Entity]):
    """Yield (entity, start, end) for every dictionary occurrence, with the same matches
    `Entity.spans` produces — but without one full-text pass per entry.

    That per-entry pass was the whole cost of a scan: a 200-entry dictionary compiles to 400
    patterns (one per normalization form) and took 13.5s of 13.7s on 2 MB (scripts/bench-check.py).
    Two stages instead:

      * entries without a `@context` are located by ONE case-insensitive scan over the first
        token of every entry, then verified ANCHORED at each candidate position — the very match
        `finditer` would have returned, because a context-free entry always starts at its first
        token;
      * entries WITH a `@context` cannot be found that way (their match starts at the context),
        so they are grouped by (context, normalization form) and scanned once per group.

    An entry built by hand (no scan metadata) is scanned directly: never skipped, so a caller
    cannot silence an entry by omitting a field.
    """
    literals: dict[str, list[Entity]] = {}
    sources: set[str] = set()
    groups: dict[tuple[str, str], list[Entity]] = {}
    direct: list[Entity] = []
    for entity in entities:
        if not entity.first_token or not entity.inner:
            direct.append(entity)
        elif entity.context:
            groups.setdefault((entity.context, entity.form), []).append(entity)
        else:
            literals.setdefault(entity.first_token.casefold(), []).append(entity)
            sources.add(entity.first_token)

    if sources:
        # Longest literal first: the alternation takes the FIRST branch that matches at a position,
        # and this way that is also the longest — the choice the overlap resolver makes anyway for
        # matches that start at the same offset.
        alternation = re.compile(
            "|".join(re.escape(source) for source in sorted(sources, key=len, reverse=True)),
            re.IGNORECASE,
        )
        for match in alternation.finditer(text):
            matched = match.group(0)
            # The alternation takes the FIRST branch that matches, so a longer literal hides a
            # shorter one starting at the same offset: with `@stem on` on `Ferretti` behind the
            # literal `Ferrettini`, `Ferrettini` wins and the stem entry — whose `[\w\-]*` DOES
            # match that word — would never be probed. Every prefix of the matched text is looked
            # up too, so the shorter entry gets its anchored chance.
            for end in range(1, len(matched) + 1):
                for entity in literals.get(matched[:end].casefold(), ()):
                    anchored = entity.regex.match(text, match.start())
                    if anchored is not None:
                        start, span_end = anchored.span(ENTITY_GROUP)
                        yield entity, start, span_end

    for (context, _form), members in groups.items():
        # Same order as the caller's list (longest surface first), so a city catalog inside one
        # context keeps the behavior of one scan per entry.
        branches = "|".join(
            f"(?P<e{index}>{member.inner})" for index, member in enumerate(members)
        )
        grouped = re.compile(f"(?:{context})(?<!\\w)(?:{branches})(?!\\w)", re.IGNORECASE)
        for match in grouped.finditer(text):
            # The group pattern only LOCATES the context positions; each member is then verified
            # anchored there, exactly as above. An alternation reports one branch per position, so
            # reading the matched group would silently drop `Roma` when `Roma Nord` is declared in
            # the same context — the reference scan yields both, and the overlap resolver decides.
            for member in members:
                anchored = member.regex.match(text, match.start())
                if anchored is not None:
                    start, end = anchored.span(ENTITY_GROUP)
                    yield member, start, end

    for entity in direct:
        for start, end, _value in entity.spans(text):
            yield entity, start, end


def detect(
    text: str,
    entities: list[Entity],
    include_heuristics: bool = True,
    families: Iterable[str] | None = None,
) -> list[tuple[int, int, str]]:
    """Return non-overlapping (start, end, TYPE) spans of sensitive content, in order.

    `families` limits which pattern groups run (None = all). The curated dictionary is always
    applied: it is the operator's own list, not a default.
    """
    claimed = bytearray(len(text))
    candidates: list[tuple[int, int, int, str, str]] = []

    # Protect existing placeholders so anonymizing twice is a no-op.
    for match in PLACEHOLDER_RE.finditer(text):
        claimed[match.start():match.end()] = b"\x01" * (match.end() - match.start())

    def collect(start: int, end: int, priority: int, ptype: str, value: str) -> None:
        if end <= start or any(claimed[start:end]) or _is_safe(value):
            return
        candidates.append((start, end, priority, ptype, value))

    def apply(rule: Rule, priority: int) -> None:
        if families is not None and rule.family not in families:
            return
        for match in rule.regex.finditer(text):
            group = rule.capture
            if group and match.group(group) is None:
                continue
            start, end = (match.start(group), match.end(group)) if group else (match.start(), match.end())
            if rule.trim_trailing:
                while end > start and text[end - 1] in rule.trim_trailing:
                    end -= 1
            value = text[start:end]
            if rule.validator is not None and not rule.validator(value):
                if not rule.shrink_labels:
                    continue
                # `db01.azienda.it.log`: drop trailing labels until a real TLD is reached.
                shrunk = value
                while "." in shrunk:
                    shrunk = shrunk.rsplit(".", 1)[0]
                    if rule.validator(shrunk):
                        end, value = start + len(shrunk), shrunk
                        break
                else:
                    continue
            collect(start, end, priority, rule.type, value)

    for index, rule in enumerate(RULES):
        apply(rule, index)
    for entity, start, end in entity_hits(text, entities):
        collect(start, end, ENTITY_PRIORITY, entity.type, text[start:end])
    if include_heuristics:
        for index, rule in enumerate(HEURISTIC_RULES):
            apply(rule, HEURISTIC_PRIORITY + index)

    # Resolve overlaps by LONGEST match, not by evaluation order: a curated name inside a hostname
    # (`srv-crm01.contoso.local`) must give way to the hostname, or the redaction would be partial
    # (`srv-crm01.[AZIENDA-1].local`) and leave the host readable.
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))
    found: list[tuple[int, int, str]] = []
    for start, end, _priority, ptype, _value in candidates:
        if any(claimed[start:end]):
            continue
        claimed[start:end] = b"\x01" * (end - start)
        found.append((start, end, ptype))
    return found


class _Placeholders:
    """Per-run placeholder allocation, shared by the text path and the container path.

    One instance per document/run: `[EMAIL-1-tag]` must mean the same value in `document.xml` and
    in `header1.xml`, or the map would describe two different things under one key. The numbering
    and the reserved-token rule live here once, so the two paths cannot drift apart.
    """

    def __init__(self, tag: str | None, reserved: Iterable[str] = ()) -> None:
        self.tag = tag or new_tag()
        self.by_key: dict[tuple[str, str], str] = {}
        self.counters: dict[str, int] = {}
        self.entries: dict[str, dict[str, str]] = {}
        # Placeholder-shaped text already in the source must not be reused as a FRESH placeholder:
        # `[EMAIL-1] e info@acme.it` would otherwise map the literal `[EMAIL-1]` to the new value,
        # and deanon would rewrite both.
        self.reserved = set(reserved)

    def for_value(self, ptype: str, value: str) -> str:
        known = self.by_key.get((ptype, value))
        if known is not None:
            return known
        suffix = f"-{self.tag}" if self.tag else ""
        while True:
            self.counters[ptype] = self.counters.get(ptype, 0) + 1
            candidate = f"[{ptype}-{self.counters[ptype]}{suffix}]"
            if candidate not in self.reserved:
                break
        self.reserved.add(candidate)
        self.by_key[(ptype, value)] = candidate
        self.entries[candidate] = {"type": ptype, "original": value}
        return candidate

    def counts(self) -> dict[str, int]:
        # Recomputed from the entries: `counters` also advances past reserved numbers, so it would
        # over-report (e.g. {"EMAIL": 2} for a single entry) in the map summary.
        counts: dict[str, int] = {}
        for entry in self.entries.values():
            counts[entry["type"]] = counts.get(entry["type"], 0) + 1
        return counts


def group_runs(offsets: Iterable[int]) -> list[list[int]]:
    """Group raw offsets into the contiguous runs they came from.

    A value (or a placeholder) that a word processor split across runs arrives as offsets with
    gaps: `"Mario "` in one `<w:t>` and `"Rossi"` in the next.
    """
    runs: list[list[int]] = []
    for position in offsets:
        if runs and position == runs[-1][-1] + 1:
            runs[-1].append(position)
        else:
            runs.append([position])
    return runs


def anonymize(
    text: str,
    entities: list[Entity],
    include_heuristics: bool = True,
    families: Iterable[str] | None = None,
    tag: str | None = None,
) -> tuple[str, dict[str, dict[str, str]], dict[str, int]]:
    """Replace sensitive spans with stable placeholders. Returns (redacted, entries, counts).

    Every placeholder carries a `tag` identifying the map it belongs to (`[EMAIL-1-a3f9]`), so a
    document can only be de-anonymized with ITS map. `tag=None` generates a fresh one.
    """
    found = detect(text, entities, include_heuristics, families)
    alloc = _Placeholders(tag, (m.group(0) for m in PLACEHOLDER_RE.finditer(text)))
    out: list[str] = []
    cursor = 0
    for start, end, ptype in found:
        out.append(text[cursor:start])
        out.append(alloc.for_value(ptype, text[start:end]))
        cursor = end
    out.append(text[cursor:])
    return "".join(out), alloc.entries, alloc.counts()


class UnreadableContainer(ValueError):
    """A `.docx`-looking file that is not a readable ZIP: fake, or corrupt.

    Refused with the same words as any other binary (`REFUSED`, `binary`), because that is what it
    is: a familiar extension must not make the refusal vaguer.
    """


# Tags a value may be split ACROSS. Word fragments a run for formatting reasons, and that split is
# internal to one text container: `</w:t></w:r><w:r><w:t>` is the same sentence. `</w:p><w:p>` or
# `</dc:title><dc:creator>` is not: joining across it would take text from a DIFFERENT element, so a
# match that needs such a boundary is refused instead of rewritten.
RUN_LEVEL_TAGS = re.compile(
    r"^</?(?:w:r|w:t|w:rPr|w:proofErr|w:noProof|w:lastRenderedPageBreak|"
    r"a:r|a:t|a:rPr|a:endParaRPr|text:span|text:s)\b"
)


def masked_index(xml: str) -> tuple[str, array.array, set[int]]:
    """(the text a reader sees, with ONE SPACE where each tag was; source offset per character, -1
    for the inserted separator; the indices of separators that are NOT a run-level boundary).

    `visible_index` CONCATENATES fragments, which is what a self-delimiting token (`[EMAIL-1]`)
    needs, and it stays that way for the restore direction. Detecting a real VALUE needs the
    opposite: `</dc:title><dc:creator>` must not merge `Contoso` and `Mario` into one word (that is
    exactly how a document property escaped redaction), while a value split inside a paragraph must
    still be found — and it is, because the entity patterns join their tokens with `[\\s...]+`, so a
    single space between two run fragments matches.

    A match reaching across one of the reported boundaries takes text from ANOTHER element, so the
    caller must refuse it instead of rewriting: that is the difference between "Word split a run"
    and "two paragraphs happened to end and start with the right words".
    """
    return _walk_parts(xml, " ")


# Decompression caps. `zipfile` inflates a part into memory before anyone can look at it, and the
# per-character index below costs one int and one 1-char str per character (~40-80x the text), so an
# unbounded part is both a memory and a time hole — reachable from `--check`, which the Pi guard
# runs on a `read`. These are the boundaries of "a document", not a policy: over them the container
# is REFUSED (fail-closed), never half-processed.
CONTAINER_MAX_PART_BYTES = 64 * 1024 * 1024
CONTAINER_MAX_TOTAL_BYTES = 256 * 1024 * 1024
CONTAINER_MAX_RATIO = 200


def _guard_part(info: zipfile.ZipInfo, running_total: int, max_total: int = CONTAINER_MAX_TOTAL_BYTES) -> int:
    """The running decompressed size, or UnreadableContainer when a part is out of bounds."""
    if info.file_size > CONTAINER_MAX_PART_BYTES:
        raise UnreadableContainer(
            f"REFUSED — part {info.filename} declares {info.file_size} uncompressed bytes "
            f"(over {CONTAINER_MAX_PART_BYTES}): binary or a bomb. Nothing was written."
        )
    if info.compress_size and info.file_size > max(4096, CONTAINER_MAX_RATIO * info.compress_size):
        raise UnreadableContainer(
            f"REFUSED — part {info.filename} expands {info.file_size // max(info.compress_size, 1)}x "
            f"its compressed size (over {CONTAINER_MAX_RATIO}x): binary or a bomb. Nothing was written."
        )
    total = running_total + info.file_size
    if total > max_total:
        raise UnreadableContainer(
            f"REFUSED — the parts expand past {max_total} bytes: binary or a bomb. "
            "Nothing was written."
        )
    return total


class UnreadablePart(UnreadableContainer):
    """A part that cannot be decoded as text although its name says it is text.

    Passing it through unscanned would deliver a document with a value still inside, and the
    verification would not see it either (same view, same blind spot). Refused instead.
    """


def open_container(src: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(src)
    except (zipfile.BadZipFile, OSError) as exc:
        raise UnreadableContainer(
            f"REFUSED — {src} looks like an office document but is not a readable ZIP "
            f"(binary or corrupt: {type(exc).__name__}). Nothing was written."
        ) from exc


def container_text(src: Path, max_total: int = CONTAINER_MAX_TOTAL_BYTES) -> str:
    """The visible text of EVERY text part, joined: what a reader of the container sees.

    `max_total` is the caller's budget. `--check` passes `SCAN_MAX_BYTES`, because that check is
    what the Pi guard runs on a `read`: beyond its own budget the honest answer is "unscannable"
    (fail-closed), not an unbounded scan.
    """
    chunks: list[str] = []
    with open_container(src) as archive:
        total = 0
        for info in archive.infolist():
            total = _guard_part(info, total, max_total)
            text, _codec = decode_part(archive.read(info))
            if text is None and is_xml_part(info.filename):
                raise UnreadablePart(
                    f"REFUSED — part {info.filename} is not decodable text: it cannot be scanned, "
                    "so a value inside it could not be found. Nothing was written."
                )
            if text is not None:
                # The SAME view the rewrite detects on: verifying on a different one is how the
                # first version reported "0 leftovers" while the document properties were intact.
                chunks.append(masked_index(text)[0])
    return "\n".join(chunks)


def _container_leftovers(path: Path, entities, include_heuristics, families) -> int:
    """How much still looks sensitive in the file we just wrote.

    The verdict comes from the OUTPUT, never from our intent: a `*.redacted.docx` that still
    carries a client name is the one outcome this feature must never produce.
    """
    return len(detect(container_text(path), entities, include_heuristics, families))


def anonymize_container(
    src: Path,
    out: Path | None,
    entities,
    include_heuristics: bool = True,
    families: Iterable[str] | None = None,
    tag: str | None = None,
    dry_run: bool = False,
) -> tuple[dict[str, dict[str, str]], dict[str, int], dict[str, int]]:
    """Redact a ZIP container PART BY PART, keeping the file type, the layout and the styles.

    This is the opposite trade from converting to Markdown: instead of losing headers, footers,
    comments and document properties (measured: 6 features of 17), it rewrites them where they
    live. Returns (entries, counts, {part: values replaced}).

    Raises ValueError — deleting the output first — when the written file still contains a detected
    value. Nothing is delivered on trust.
    """
    parts: dict[str, int] = {}
    blocks: list[tuple[zipfile.ZipInfo, bytes, str | None, str | None]] = []
    reserved: set[str] = set()
    with open_container(src) as archive:
        total = 0
        for info in archive.infolist():
            total = _guard_part(info, total)
            blob = archive.read(info)
            text, codec = decode_part(blob)
            if text is None and is_xml_part(info.filename):
                raise UnreadablePart(
                    f"REFUSED — part {info.filename} is not decodable text: it cannot be scanned, "
                    "so a value inside it could not be found. Nothing was written."
                )
            if text is not None:
                # Reserved tokens must be collected across the WHOLE container before allocating:
                # a part read later could otherwise reuse a number an earlier part already shows.
                reserved.update(m.group(0) for m in PLACEHOLDER_RE.finditer(visible_text(text)))
            blocks.append((info, blob, text, codec))

    alloc = _Placeholders(tag, reserved)
    chunks: list[tuple[zipfile.ZipInfo, bytes]] = []
    for info, blob, text, codec in blocks:
        if text is not None:
            visible, offsets, structural = masked_index(text)
            found = detect(visible, entities, include_heuristics, families)
            if found:
                edits: list[tuple[int, int, str]] = []
                for start, end, ptype in found:
                    # Fix the value BEFORE using it: `for_value` must key the map on the real text,
                    # not on the view. A split value would otherwise be stored with the separator
                    # spaces in it, and the restore would write those spaces into the document.
                    # -1 marks an inserted separator: it has no source character to rewrite.
                    raw = [position for position in offsets[start:end] if position >= 0]
                    if not raw:
                        continue
                    if any(index in structural for index in range(start, end)):
                        raise UnreadablePart(
                            f"REFUSED — a value of type {ptype} spans a structural boundary in "
                            f"{info.filename}: rewriting it would take text from another element. "
                            "Nothing was written."
                        )
                    placeholder = alloc.for_value(ptype, "".join(text[position] for position in raw))
                    runs = group_runs(raw)
                    # As in the reverse direction: the first fragment carries the whole token and
                    # the others are emptied, so no formatting moves and the part stays valid.
                    edits.append((runs[0][0], runs[0][-1] + 1, placeholder))
                    for run in runs[1:]:
                        edits.append((run[0], run[-1] + 1, ""))
                for start, end, replacement in sorted(edits, reverse=True):
                    text = text[:start] + replacement + text[end:]
                parts[info.filename] = len(found)
                blob = text.encode(codec or "utf-8", "surrogateescape")
        chunks.append((info, blob))

    entries, counts = alloc.entries, alloc.counts()
    if dry_run or out is None:
        return entries, counts, parts

    write_container_atomic(out, chunks)
    leftover = _container_leftovers(out, entities, include_heuristics, families)
    if leftover:
        out.unlink(missing_ok=True)
        raise ValueError(
            f"redaction INCOMPLETE: {leftover} value(s) still readable in the written document — "
            "nothing was written. Report this: the container has a shape this pass does not cover."
        )
    return entries, counts, parts


TAG_DIGITS = 6  # a width, NOT a guarantee: uniqueness comes from new_tag(existing_tags(...))


def new_tag(taken: Iterable[str] = ()) -> str:
    """A short, per-map identifier carried inside every placeholder.

    `taken` is the set of tags already in use (see `existing_tags`). Uniqueness is CHECKED against
    the maps that exist, not argued from the birthday bound: with 6 hex (16.7M values) the chance of
    a collision is ~3% at 1 000 maps and ~53% at 5 000 (`scripts/tag-collision.py` measures the
    curve), which a tool used daily reaches in a few years — and a collision is exactly the failure
    the tag exists to prevent, because a wrong map would then resolve the placeholders silently.

    The check is per-directory and NOT atomic: two runs that allocate at the same instant, against
    the same directory, can still draw the same tag (a declared residual race, ~1/16.7M per pair).
    Raises RuntimeError only if 128 candidates are all taken, which needs a directory holding a
    large fraction of both 16^6 and 16^8 values — unreachable in practice.
    """
    blocked = set(taken)
    for digits in (TAG_DIGITS, 8):
        # 8 is the widest the placeholder syntax accepts (`PLACEHOLDER_RE`: 4-8 hex digits), so the
        # fallback widens within what every reader already understands.
        for _ in range(64):
            candidate = os.urandom(4).hex()[:digits]
            if candidate not in blocked:
                return candidate
    raise RuntimeError("could not allocate a free placeholder tag")


def existing_tags(maps_dir: Path) -> set[str]:
    """Tags already used by the maps in `maps_dir`; unreadable ones are skipped.

    One pass over the given directory per anonymize run — a few hundred files in normal use (a
    measured 0.17 ms per map), which is the price of checking uniqueness instead of hoping for it.
    Callers that may write OUTSIDE `DEFAULT_MAPS` must pass the union of both directories, or the
    guarantee is only as wide as the scan.
    """
    tags: set[str] = set()
    try:
        paths = sorted(maps_dir.glob("*.map.json"))
    except OSError:
        return tags
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("tag"), str):
            tags.add(payload["tag"])
    return tags


def allocate_tag(destinations: Iterable[Path], pinned: str | None = None) -> str:
    """A tag no map in ANY of `destinations` uses. A `pinned` tag wins, unchecked.

    Every directory that could hold a map the next restore might confuse must be passed in: the
    caller that may write outside `DEFAULT_MAPS` (via `--map`) passes both, because a guarantee is
    only as wide as the scan behind it.
    """
    if pinned:
        return pinned
    taken: set[str] = set()
    for directory in dict.fromkeys(destinations):  # the same directory twice = one scan
        taken |= existing_tags(directory)
    return new_tag(taken)


def tag_of(entries: dict[str, dict[str, str]]) -> str | None:
    """Recover the tag from the placeholder keys (the map writer needs it)."""
    for placeholder in entries:
        match = PLACEHOLDER_RE.fullmatch(placeholder)
        if match and placeholder.count("-") >= 2:
            return placeholder.rsplit("-", 1)[-1].rstrip("]")
    return None


# --------------------------------------------------------------- containers
# Office containers (.docx/.xlsx/.pptx/.odt) are ZIPs whose text lives in XML parts, and a word
# processor splits a string across runs (`<w:t>[EMAIL-</w:t></w:r><w:r><w:t>1]</w:t>`). Both
# directions of the tool — writing a placeholder in, restoring a value out — need the same three
# primitives, so they live here and `deanon.py` imports them: two copies of "what a text part is"
# would drift, and the drift would be invisible until a document was wrong.
MARKUP_RE = re.compile(r"<[^>]*>")
XML_SUFFIXES = (".xml", ".rels")

# A ZIP part can legally be UTF-16/UTF-32 encoded (OOXML allows it); decoded as UTF-8 its
# placeholders are NUL-interleaved and would be neither replaced nor detected.
CONTAINER_BOMS = (
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


def visible_text(xml: str) -> str:
    """The text a reader of the document would see, approximated by dropping every tag."""
    return MARKUP_RE.sub("", xml)


def decode_part(blob: bytes) -> tuple[str | None, str | None]:
    """(text, codec) for a text part, or (None, None) when the part is binary.

    Binary is decided by content, not by filename: `word/embeddings/note.txt` holds text that
    must be rewritten, and an image is binary whatever it is called.
    """
    for bom, codec in CONTAINER_BOMS:
        if blob.startswith(bom):
            return blob.decode(codec, "surrogateescape"), codec
    if b"\x00" in blob[:8192]:
        # A NUL is not proof of binary. Deciding "binary" from it means a text part WITHOUT a BOM
        # is never scanned AND never verified — both sides use the same mistaken view, so the
        # check reports zero leftovers while the value sits there. The byte pattern is the
        # evidence: in UTF-16 every second byte is 0 in the ASCII range, in UTF-32 three of four.
        wide = _guess_wide_codec(blob)
        if wide is None:
            return None, None
        return blob.decode(wide), wide
    return blob.decode("utf-8", "surrogateescape"), "utf-8"


def _guess_wide_codec(blob: bytes) -> str | None:
    """The UTF-16/32 variant a BOM-less blob looks like, or None if it does not look like one.

    Ratios rather than all-or-nothing (a CJK document has non-zero high bytes), and a STRICT
    decode: a part we cannot decode cleanly must not be rewritten through lossy replacement —
    it is refused instead, which is the fail-closed direction.
    """
    sample = blob[:4096]
    if len(sample) < 16:
        return None
    for codec, stride, zero_slots in (
        ("utf-16-le", 2, (1,)), ("utf-16-be", 2, (0,)),
        ("utf-32-le", 4, (1, 2, 3)), ("utf-32-be", 4, (0, 1, 2)),
    ):
        slots = [byte for index, byte in enumerate(sample) if index % stride in zero_slots]
        others = [byte for index, byte in enumerate(sample) if index % stride not in zero_slots]
        if not slots or not others:
            continue
        if sum(1 for byte in slots if byte == 0) / len(slots) < 0.9:
            continue
        if sum(1 for byte in others if byte != 0) / len(others) < 0.8:
            continue
        try:
            text = blob.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
        if "\x00" in text:
            continue
        return codec
    return None


def is_xml_part(name: str) -> bool:
    return name == "[Content_Types].xml" or name.lower().endswith(XML_SUFFIXES)


def xml_protect(value: str) -> str:
    """Escape a value before writing it into an XML part.

    `&`, `<`, `>` are mandatory in element text; `"` and `'` matter when the value sits in an
    attribute (`w:tooltip="..."`, a `.rels` Target). Escaping quotes in element text is harmless —
    they decode back to themselves — so this is safe in both contexts.
    """
    return _sax_escape(value, {'"': "&quot;", "'": "&apos;"})


def _walk_parts(xml: str, separator: str) -> tuple[str, array.array, set[int]]:
    """(visible text, the source offset of every one of its characters, the structural separators).

    One implementation for both directions. Two details are about NOT spending the text over again:
    the visible text is built from chunks and joined ONCE (a list of single characters is a pointer
    per character), and the offsets live in an `array('i')` — a Python list of ints costs tens of
    bytes each, which is how a 64 MB part used to ask for gigabytes. `-1` marks a character that
    has no source position because it IS the separator standing for a tag.
    """
    chunks: list[str] = []
    offsets = array.array("i")
    structural: set[int] = set()
    position = 0
    length = 0
    for match in MARKUP_RE.finditer(xml):
        chunk = xml[position:match.start()]
        chunks.append(chunk)
        offsets.extend(range(position, match.start()))
        length += len(chunk)
        if separator:
            if not all(RUN_LEVEL_TAGS.match(tag) for tag in MARKUP_RE.findall(match.group(0))):
                structural.add(length)
            chunks.append(separator)
            offsets.append(-1)
            length += 1
        position = match.end()
    chunk = xml[position:]
    chunks.append(chunk)
    offsets.extend(range(position, len(xml)))
    return "".join(chunks), offsets, structural


def visible_index(xml: str) -> tuple[str, array.array]:
    """(the text a reader sees, the offset in `xml` of every one of its characters).

    This is what makes a value (or a placeholder) that a word processor split across runs findable
    as one string, and repairable without touching a single tag.
    """
    visible, offsets, _structural = _walk_parts(xml, "")
    return visible, offsets


def write_container_atomic(output: Path, chunks) -> None:
    """Write the parts to a new ZIP, then `os.replace` it into place: a half-written container
    must never be left where a caller could pick it up."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp-{os.urandom(4).hex()}")
    try:
        with zipfile.ZipFile(temporary, "w") as target:
            for info, blob in chunks:
                # A fresh ZipInfo: reusing the original object can carry a data-descriptor flag
                # bit that zipfile then rewrites inconsistently.
                clean = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                clean.compress_type = info.compress_type
                clean.external_attr = info.external_attr
                clean.internal_attr = info.internal_attr
                clean.create_system = info.create_system
                target.writestr(clean, blob)
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()


def _default_output(path: Path) -> Path:
    return path.with_name(f"{path.stem}.redacted{path.suffix}")


def _write_private(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _fold_entity(value: str) -> str:
    """Canonical key for fuzzy comparison: case-folded, legal forms dropped, non-alphanumerics
    removed. `Contoso S.r.l.` and `CONTOSO` fold to the same key."""
    tokens = _name_tokens(value)
    return "".join(char for char in "".join(tokens).casefold() if char.isalnum())


NEAR_MISS_WORD_LIMIT = 400
NEAR_MISS_VOCAB_LIMIT = 200

# The size the tool promises to scan inside its budget: the same 12 MB the Pi guard applies before
# it reads a file, sized by scripts/bench-check.py. `--batch` and `--check` on a container both
# derive from it — a folder, and a ZIP whose parts expand, are not licences to exceed it silently.
SCAN_MAX_BYTES = 12 * 1024 * 1024
BATCH_MAX_BYTES = SCAN_MAX_BYTES
# Our own outputs must never be treated as input: re-anonymizing `x.redacted.md` would nest
# placeholders and re-number them, and deleting nothing is not the fix.
OWN_OUTPUT_SUFFIXES = (".redacted", ".deanon")
# Anchored to the FINAL extension and case-insensitive: a bare substring test skipped real sources
# (`note.redacted.draft.txt`) and missed `X.REDACTED.MD`.
OWN_OUTPUT_RE = re.compile(r"\.(?:redacted|deanon)\.[^.]*$", re.IGNORECASE)
NEAR_MISS_CUTOFF = 0.86
WORD_RE = re.compile(r"[^\W\d_][\w'’\-]*", re.UNICODE)


def near_misses(
    text: str, entities: list[Entity], found: list[tuple[int, int, str]]
) -> tuple[list[dict[str, object]], bool]:
    """Dictionary entries that the text almost contains.

    Two kinds, both REPORTED for a human to declare — never used to redact, because a fuzzy rule
    that silently misses is worse than an explicit alias added once:
      * `variant` — the text writes the entity differently but it folds to the same key
        (`Con Toso` / `CONTOSO Srl` vs a declared `Contoso`); found by folding adjacent-word joins.
      * `near` — a single word that is close but not identical (a typo, another transliteration).
    Bounded on purpose (word and vocabulary caps) so the audit stays fast.
    """
    import difflib

    covered = bytearray(len(text))
    for start, end, _type in found:
        covered[start:end] = b"\x01" * (end - start)

    vocabulary: dict[str, Entity] = {}
    for entity in entities:
        key = _fold_entity(entity.surface)
        if len(key) >= 5:
            vocabulary.setdefault(key, entity)
    if not vocabulary:
        return [], False

    words: list[tuple[int, int, str]] = []
    for match in WORD_RE.finditer(text):
        if any(covered[match.start():match.end()]):
            continue
        words.append((match.start(), match.end(), match.group(0)))
        if len(words) >= NEAR_MISS_WORD_LIMIT:
            break
    if not words:
        return [], False

    hits: dict[str, tuple[str, int, str, Entity, float]] = {}
    # 1) joins of adjacent words on the same line: `Be` + `Safe` -> `contoso`
    for index in range(len(words)):
        for size in (2, 3):
            chunk = words[index:index + size]
            if len(chunk) < size:
                continue
            if any("\n" in text[chunk[k][1]:chunk[k + 1][0]] for k in range(len(chunk) - 1)):
                continue
            joined = "".join(word for _p, _e, word in chunk)
            key = _fold_entity(joined)
            entity = vocabulary.get(key)
            if entity is not None and key not in hits:
                hits[key] = ("variant", chunk[0][0], text[chunk[0][0]:chunk[-1][1]], entity, 1.0)
    # 2) single words: exact folding or a close match
    keys = list(vocabulary)[:NEAR_MISS_VOCAB_LIMIT]
    for position, _end, word in words:
        if len(word) < 5:
            continue
        key = _fold_entity(word)
        if key in hits:
            continue
        entity = vocabulary.get(key)
        if entity is not None:
            hits[key] = ("variant", position, word, entity, 1.0)
            continue
        close = difflib.get_close_matches(key, keys, n=1, cutoff=NEAR_MISS_CUTOFF)
        if close:
            hits[key] = (
                "near",
                position,
                word,
                vocabulary[close[0]],
                round(difflib.SequenceMatcher(None, key, close[0]).ratio(), 3),
            )

    out: list[dict[str, object]] = []
    for kind, position, label, entity, similarity in hits.values():
        out.append({
            "kind": kind,
            "type": entity.type,
            "line": text.count("\n", 0, position) + 1,
            "similarity": similarity,
            "token_masked": label[:1] + "\u2022" * (len(label) - 1),
            # Filled only with --reveal: the default report must be safe to hand to an agent.
            "token": label,
            "entity": entity.surface,
        })
    out.sort(key=lambda item: (-float(item["similarity"]), str(item["token_masked"])))
    return out, len(words) >= NEAR_MISS_WORD_LIMIT or len(vocabulary) > NEAR_MISS_VOCAB_LIMIT


def resolve_entities(args: argparse.Namespace) -> list[Entity]:
    """The custom dictionaries plus the selected catalogs, as one list.

    `--entities` is repeatable. When it is given, every path must exist (a typo must fail loudly,
    not silently drop a dictionary). When it is not, the default dictionaries that exist are used:
    `entities.txt` (generic), `people.txt` (people) and `clients.txt` (companies).
    """
    requested = getattr(args, "entities", None)
    if requested:
        paths = [Path(item).expanduser() for item in _as_list(requested)]
        if not paths:
            raise ValueError("--entities needs a path")
        missing = [str(path) for path in paths if not path.is_file()]
        if missing:
            raise ValueError("dictionary file not found: " + ", ".join(missing))
    else:
        paths = [path for path in DEFAULT_DICTIONARIES if path.is_file()]
        if not paths:
            # NOT fatal. A fresh install has no curated dictionary, and the engine must keep working
            # with the pattern rules alone. A hard error here would make `--check --json` print
            # nothing, and the Pi guard reads "no JSON" as "engine broken" and fails OPEN for the
            # session — a leak, and worse than having no dictionary.
            print(
                "anon: no dictionary file found; using the built-in patterns only (expected one of "
                + ", ".join(str(path) for path in DEFAULT_DICTIONARIES)
                + ")",
                file=sys.stderr,
            )
    for name in _as_list(getattr(args, "catalogs", None)):
        if True:
            path = catalog_path(name)
            if not path.is_file():
                available = ", ".join(item["name"] for item in list_catalogs()) or "(none installed)"
                raise ValueError(f"unknown catalog '{name}' — available: {available}")
            paths.append(path)
    return load_entities_many(paths)


def resolve_families(args: argparse.Namespace) -> set[str] | None:
    """`--patterns identity,network` restricts the built-in pattern groups (default: all)."""
    requested = _as_list(getattr(args, "patterns", None))
    if not requested:
        return None
    families = {item.lower() for item in requested}
    unknown = families - set(PATTERN_FAMILIES)
    if unknown:
        raise ValueError(
            f"unknown pattern group(s): {', '.join(sorted(unknown))} — "
            f"available: {', '.join(PATTERN_FAMILIES)}"
        )
    return families


# A .docx/.xlsx/.pptx/.odt is a ZIP; .doc/.xls a CFB; a PDF starts with %PDF-. Reading any of
# them as text produces mojibake: the regexes match a few lucky byte runs, almost nothing real
# is redacted, and the "redacted" copy is a corrupted file that LOOKS anonymized. That silent
# half-success is the most dangerous possible output for a privacy tool, so binary input is
# refused instead.
#
# The same sniff is what `--check` uses to protect `read` (see `cmd_check`): Pi's read tool
# sends images as attachments but decodes EVERYTHING else with `toString("utf-8")`
# (dist/core/tools/read.js) — so a .docx with stored entries, a legacy UTF-16 .doc or an
# uncompressed PDF stream would hand readable names to the model. Unscannable documents must
# therefore fail CLOSED, not "not sensitive".
CONTAINER_PREFIX_MAGIC = (
    b"PK\x03\x04",  # zip: docx, xlsx, pptx, odt, ods, odp, epub, jar...
    b"PK\x05\x06",
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",  # CFB: legacy .doc, .xls, .ppt, .msg
    b"\x1f\x8b",  # gzip
    b"Rar!",
    b"7z\xbc\xaf\x27\x1c",
    b"OggS",
    b"MZ",  # PE executable
    b"\x7fELF",
)
# Searched (not anchored): the PDF spec allows up to 1024 bytes before `%PDF-`, and a zip may
# have been appended after a preamble. These signatures contain control bytes, so text cannot
# contain them by accident.
CONTAINER_SEARCH_MAGIC = (b"PK\x03\x04", b"PK\x07\x08")
# `%PDF-` is printable ASCII, so searching for it bare would refuse any document or log that
# merely MENTIONS it. A real header carries a version (`%PDF-1.7`).
PDF_HEADER_RE = re.compile(rb"%PDF-\d\.\d")
IMAGE_PREFIX_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")


def _is_image(head: bytes) -> bool:
    if head.startswith(IMAGE_PREFIX_MAGIC):
        return True
    if head.startswith(b"RIFF"):
        return head[8:12] == b"WEBP"
    # BMP: "BM" alone would match any text starting with those letters, so require the
    # reserved field to be zero (bytes 6-9) as the format mandates.
    return head.startswith(b"BM") and head[6:10] == b"\x00\x00\x00\x00"


def sniff(path: Path, head_bytes: int = 8192) -> str | None:
    """Classify the input: 'image', 'container', 'binary', or None for scannable text."""
    try:
        with path.open("rb") as handle:
            head = handle.read(head_bytes)
    except OSError:
        return None
    if _is_image(head):
        return "image"
    if head.startswith(CONTAINER_PREFIX_MAGIC):
        return "container"
    # The PDF header may start at offset <=1024 and the signature itself is 8 bytes.
    window = head[:1032]
    if any(magic in window for magic in CONTAINER_SEARCH_MAGIC):
        return "container"
    if PDF_HEADER_RE.search(window):
        return "container"
    if b"\x00" in head:
        return "binary"
    return None


def cmd_check(args: argparse.Namespace) -> int:
    if args.file == "-":
        # stdin mode: check arbitrary text (e.g. the Markdown anon-guard just produced)
        # without writing a temp file that would hold the very data being inspected.
        entities = resolve_entities(args)
        text = sys.stdin.read()
        return _emit_check(args, detect(text, entities, families=resolve_families(args)), Path("<stdin>"), text=text)
    target = Path(args.file).expanduser()
    allow = load_allowlist(Path(args.allow) if args.allow else DEFAULT_ALLOW)
    allow += list(args.allow_glob or [])
    if is_allowed(target, allow):
        return _emit_check(args, [], target, allowed=True)
    if not target.is_file():
        if args.json:
            print(json.dumps({"sensitive": False, "error": f"not a file: {target}"}))
        else:
            print(f"anon: not a file: {target}", file=sys.stderr)
        return 0
    entities = resolve_entities(args)
    kind = sniff(target)
    if kind == "image":
        # Not scannable (it is sent as an image attachment, not as text), but reading an image
        # is a legitimate feature. Declared gap: a screenshot of a client document leaks.
        return _emit_check(args, [], target, binary=True)
    if kind is not None:
        # A container's parts ARE scannable, and now that we rewrite them in place we must be able
        # to name what would come out. `unscannable` stays, and not as a formality: it describes
        # what the `read` tool does with this file (decode it as text, which this is not), and the
        # guard's auto-remediation keys on that exact field.
        inside = ""
        found_inside: list[tuple[int, int, str]] = []
        if kind == "container":
            try:
                inside = container_text(target, max_total=SCAN_MAX_BYTES)
                found_inside = detect(inside, entities, families=resolve_families(args))
            except (UnreadableContainer, OSError, zipfile.BadZipFile):
                # A PDF, or a package we cannot open: the honest answer stays "unscannable".
                found_inside = []
        return _emit_check(args, found_inside, target, text=inside, binary=True, unscannable=True,
                           container=kind == "container")
    text = read_text(target)
    found = detect(text, entities, families=resolve_families(args))
    return _emit_check(args, found, target, text=text)


def read_json_object(text: str, what: str) -> dict:
    """Parse `text` as a JSON object, turning EVERY malformed-input failure into ValueError.

    `json.loads` raises `RecursionError` on deeply nested input — not a ValueError — so a caller
    that only catches ValueError turns a malformed file or request body into a 500 or an
    unhandled traceback instead of a clean refusal. Both `deanon` (map files) and the web API
    (request bodies) go through here, so the two cannot drift apart again.
    """
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"{what}: malformed JSON ({type(exc).__name__})") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{what}: expected a JSON object, found {type(data).__name__}")
    return data


def _envelope(tool: str) -> dict[str, object]:
    return {"schema": SCHEMA, "tool": tool, "version": VERSION}


def cmd_audit(args: argparse.Namespace) -> int:
    """Is this file REALLY anonymized? Report residual risk, without echoings any value."""
    target = Path(args.file).expanduser()
    if not target.is_file():
        print(f"anon: not a file: {target}", file=sys.stderr)
        return 2
    kind = sniff(target)
    inside_container = False
    if kind == "container":
        # A container is auditable now that its parts are scanned elsewhere: the same view the
        # rewrite uses, so "clean" here means the same thing it means there. A PDF or a legacy
        # .doc still cannot be checked without rewriting it, and saying so is the honest answer.
        try:
            text = container_text(target, max_total=SCAN_MAX_BYTES)
            inside_container = True
        except (UnreadableContainer, OSError, zipfile.BadZipFile):
            print(
                f"anon: {target.name} is binary, or a container that cannot be read as a package "
                "— convert it to Markdown first",
                file=sys.stderr,
            )
            return 2
    elif kind is not None:
        print(f"anon: {target.name} is binary — convert it to Markdown first", file=sys.stderr)
        return 2
    else:
        text = read_text(target)
    entities = resolve_entities(args)
    found = detect(text, entities, families=resolve_families(args))
    candidates, capped = near_misses(text, entities, found)
    placeholders = sum(1 for match in PLACEHOLDER_RE.finditer(text))

    by_type: dict[str, int] = {}
    findings: list[dict[str, object]] = []
    for start, _end, ptype in found:
        by_type[ptype] = by_type.get(ptype, 0) + 1
        if len(findings) < 50:
            findings.append({"type": ptype, "line": text.count("\n", 0, start) + 1})

    verdict = "sensitive" if found else ("suspicious" if candidates else "clean")
    report: dict[str, object] = {
        **_envelope("anon.py audit"),
        **({"container": True} if inside_container else {}),
        "file": str(target),
        "verdict": verdict,
        "total": len(found),
        "types": by_type,
        "findings": findings,
        "near_miss": candidates if args.reveal else [
            {k: v for k, v in item.items() if k not in ("token", "entity")} for item in candidates
        ],
        "revealed": bool(args.reveal),
        "candidates_capped": capped,
        "placeholders_present": placeholders,
    }
    if len(found) > len(findings):
        report["findings_truncated"] = True

    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    elif not args.quiet or args.reveal:
        if found:
            summary = ", ".join(f"{k}x{v}" for k, v in sorted(by_type.items()))
            lines = ", ".join(f"line {item['line']}" for item in findings[:8])
            print(f"anon: SENSITIVE content found ({summary}) at {lines}")
        if candidates:
            print(f"anon: {len(candidates)} candidate(s) — a declared entity written differently"
                  f"{'' if args.reveal else ' (masked; use --reveal to see them, they ARE the sensitive data)'}")
            for item in candidates:
                label = f"{item['token']!r} ~ {item['entity']!r}" if args.reveal else str(item["token_masked"])
                print(f"        line {item['line']} [{item['kind']}]: {label} ({item['type']}, {item['similarity']})")
        if placeholders:
            print(f"anon: {placeholders} placeholder(s) present — consistent with an already-redacted document")
        if not found and not candidates:
            print(f"anon: CLEAN — no sensitive content, no near miss ({entity_count(entities)} rules applied)")
        print(f"anon: verdict: {verdict}")

    if found:
        return 1
    return 4 if candidates else 0


def _emit_check(
    args: argparse.Namespace,
    found: list[tuple[int, int, str]],
    target: Path,
    text: str = "",
    allowed: bool = False,
    binary: bool = False,
    unscannable: bool = False,
    container: bool = False,
) -> int:
    by_type: dict[str, int] = {}
    findings: list[dict[str, object]] = []
    for start, _end, ptype in found:
        by_type[ptype] = by_type.get(ptype, 0) + 1
        if len(findings) < 20:
            findings.append({"type": ptype, "line": text.count("\n", 0, start) + 1})
    result: dict[str, object] = {
        **_envelope("anon.py --check"),
        "sensitive": bool(found) or unscannable,
        "file": str(target),
        "total": len(found),
        "types": by_type,
        "findings": findings,
    }
    if len(found) > len(findings):
        # The list is capped for output size: say so, instead of letting a truncated list read as
        # the whole picture (the near-miss cap had exactly this failure mode).
        result["findings_truncated"] = True
    if allowed:
        result["allowed"] = True
    if binary:
        result["binary"] = True
    if unscannable:
        result["unscannable"] = True
    if container:
        # The line numbers refer to the parts as a reader would concatenate them, not to a file
        # with lines: say which of the two views this is, instead of leaving it to be guessed.
        result["container"] = True
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    elif found:
        summary = ", ".join(f"{k}x{v}" for k, v in sorted(by_type.items()))
        lines = ", ".join(f"line {f['line']}" for f in findings[:5])
        print(f"anon: sensitive content found in {target} ({summary}) at {lines}")
    elif unscannable:
        print(f"anon: un-scannable binary document: {target}")
    return 1 if (found or unscannable) else 0


def _is_own_output(name: str) -> bool:
    """True for a file this tool produced: `x.redacted.md`, `X.REDACTED.MD`, `x.deanon.docx`,
    `x.map.json`.

    Re-anonymizing our own output would re-number and nest the placeholders; a map is never input.
    """
    return bool(OWN_OUTPUT_RE.search(name)) or name.lower().endswith(".map.json")


def plan_map_path(args: argparse.Namespace) -> Path:
    """Where the map will go. Planned separately from writing it, so a dry run can name the file it
    would create without creating anything."""
    selected = getattr(args, "map", None)
    if selected:
        return Path(selected).expanduser()
    return DEFAULT_MAPS / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.urandom(3).hex()}.map.json"


def write_map(map_path: Path, src: Path, out: Path | None, entries, counts) -> None:
    payload = {
        "tag": tag_of(entries),
        "version": VERSION,
        "id": map_path.stem.removesuffix(".map"),
        "source": str(src.resolve()),
        "output": str(out) if out is not None else "(stdout)",
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "counts": counts,
        "entries": entries,
    }
    _write_private(map_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def write_run(
    src: Path,
    redacted: str,
    entries: dict[str, dict[str, str]],
    counts: dict[str, int],
    tag: str,
    args: argparse.Namespace,
    *,
    dry_run: bool,
    out_path: Path | None = None,
) -> tuple[Path | None, Path | None]:
    """Write the redacted copy and its map; with `dry_run`, compute the paths and write NOTHING.

    One function for both the single-file and the batch path: two copies of "where does the output
    go, and what does the map contain" is exactly the duplication that drifts.
    """
    to_stdout = args.stdout and out_path is None
    out: Path | None = None
    if not to_stdout:
        out = out_path if out_path is not None else (Path(args.out).expanduser() if args.out else _default_output(src))
    map_path: Path | None = plan_map_path(args) if entries else None
    if dry_run:
        return out, map_path
    if to_stdout:
        # `--stdout` redirects the TEXT; it does not opt out of writing the map, or the pipeline
        # that just consumed the redacted text on stdout could never be reversed.
        sys.stdout.write(redacted)
        if not redacted.endswith("\n"):
            sys.stdout.write("\n")
    else:
        _write_private(out, redacted)
    if map_path is not None:
        write_map(map_path, src, out, entries, counts)
    return out, map_path


def _emit_dry_run(
    src: Path,
    out: Path | None,
    map_path: Path | None,
    entries: dict[str, dict[str, str]],
    counts: dict[str, int],
    args: argparse.Namespace,
) -> int:
    """`--check` answers "is this sensitive?". This answers "what would change, and where would it
    land?" — the question that comes first, and the only safe first step on a whole folder."""
    if args.json:
        print(json.dumps({
            **_envelope("anon.py --dry-run"),
            "dry_run": True,
            "file": str(src),
            "sensitive": bool(entries),
            "would_write": str(out) if out else "(stdout)",
            "would_map": str(map_path) if map_path else None,
            "entries": len(entries),
            "counts": counts,
        }, ensure_ascii=False))
        return 0
    if not args.quiet:
        if entries:
            summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            print(f"anon: dry run — nothing written; would redact {len(entries)} span(s) [{summary}]")
            if out is not None:
                print(f"anon: would write -> {out}")
            print(f"anon: would map   -> {map_path}")
        else:
            print(f"anon: dry run — nothing sensitive in {src}; nothing would be written")
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    """Anonymize a directory, one document at a time, never touching a source in place.

    What it REFUSES is the point: binary/unscannable files are reported and skipped (never copied
    under a `redacted` name), our own outputs are skipped (re-anonymizing them would nest the
    placeholders), and anything over `BATCH_MAX_BYTES` is skipped rather than half-processed. With
    `--dry-run` it reports the same plan and writes nothing; with `--check` it only gives verdicts
    and exits 1 if any file is sensitive, which is the folder-shaped gate.
    """
    root = Path(args.file).expanduser()
    if not root.is_dir():
        raise ValueError(f"--batch needs a directory, not {root}")
    if args.stdout:
        raise ValueError("--batch writes files: --stdout does not apply")
    if args.map:
        raise ValueError("--batch writes one map per document: --map does not apply")
    resolved = root.resolve()
    private = ANON_HOME.resolve()
    if resolved == private or private in resolved.parents:
        raise ValueError(f"refusing a batch inside the private store ({ANON_HOME})")
    out_dir = Path(args.out).expanduser() if args.out else None
    if out_dir is not None:
        out_resolved = out_dir.resolve()
        if out_resolved == resolved or out_resolved in resolved.parents:
            # Otherwise every candidate file looks like "inside the output directory" and the run
            # reports 0 file(s) / 0 skipped — a no-op that looks like success.
            raise ValueError("--out must not be the folder being scanned, nor a parent of it")
        if not (args.dry_run or args.check):
            out_dir.mkdir(parents=True, exist_ok=True)

    entities = resolve_entities(args)          # resolved once: the folder shares one dictionary
    families = resolve_families(args)
    rows: list[dict[str, object]] = []
    skipped: list[tuple[Path, str]] = []
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        # `is_file()` FOLLOWS a symlink, so a link inside the tree can point anywhere: at a file
        # outside the scan root, or at the private store, whose maps hold the real values. Scanning
        # it would process — and copy under a `redacted` name — something the operator never put in
        # scope. The tree is the scope, so a target outside it is skipped and said to be.
        target = path.resolve()
        if target == private or private in target.parents:
            skipped.append((path, "symlink into the private store"))
            continue
        if resolved not in target.parents and target != resolved:
            skipped.append((path, "symlink outside the scan root"))
            continue
        if out_dir is not None and out_dir.resolve() in target.parents:
            continue  # never re-scan what we just wrote
        if _is_own_output(path.name):
            skipped.append((path, "own output"))
            continue
        try:
            size = path.stat().st_size
        except OSError:
            skipped.append((path, "unreadable"))
            continue
        if size > BATCH_MAX_BYTES:
            skipped.append((path, f"over {BATCH_MAX_BYTES // (1024 * 1024)} MB"))
            continue
        kind = sniff(path)
        if kind == "container":
            try:
                text = container_text(path)
            except UnreadableContainer:
                if args.check:
                    rows.append({"file": str(path), "total": 1, "entries": 0, "counts": {},
                                 "redacted": None, "map": None, "unscannable": True})
                else:
                    skipped.append((path, "not a readable container (binary or corrupt)"))
                continue
        elif kind is not None:
            if args.check:
                # Fail CLOSED, exactly like the single-file check: "unscannable" is not "clean".
                # Reporting a folder of PDFs as clean is the one answer that must never happen.
                rows.append({"file": str(path), "total": 1, "entries": 0, "counts": {},
                             "redacted": None, "map": None, "unscannable": True})
            else:
                skipped.append((path, "not text (binary/image: convert it first)"))
            continue
        else:
            text = read_text(path)
        if args.check:
            found = detect(text, entities, include_heuristics=not args.no_hosts, families=families)
            by_type: dict[str, int] = {}
            for _start, _end, ptype in found:
                by_type[ptype] = by_type.get(ptype, 0) + 1
            rows.append({"file": str(path), "total": len(found), "entries": 0, "counts": by_type,
                         "redacted": None, "map": None})
            continue
        tag = new_tag() if args.dry_run else allocate_tag([DEFAULT_MAPS])
        # The relative path is preserved: flattening to the basename made `a/nota.txt` and
        # `b/nota.txt` overwrite each other in --out, silently, with two maps pointing at one file.
        relative = path.relative_to(root)
        planned = out_dir / relative.parent / _default_output(path).name if out_dir is not None else None
        if kind == "container":
            # Kept, not converted: the folder workflow brings documents back, not Markdown.
            out = planned if planned is not None else _default_output(path)
            try:
                entries, counts, _parts = anonymize_container(
                    path, None if args.dry_run else out, entities,
                    include_heuristics=not args.no_hosts, families=families, tag=tag, dry_run=args.dry_run,
                )
            except UnreadableContainer:
                if args.check:
                    rows.append({"file": str(path), "total": 1, "entries": 0, "counts": {},
                                 "redacted": None, "map": None, "unscannable": True})
                else:
                    skipped.append((path, "not a readable container (binary or corrupt)"))
                continue
            map_path = plan_map_path(args) if entries else None
            if not args.dry_run and map_path is not None:
                write_map(map_path, path, out, entries, counts)
        else:
            redacted, entries, counts = anonymize(
                text, entities, include_heuristics=not args.no_hosts, families=families, tag=tag,
            )
            out, map_path = write_run(
                path, redacted, entries, counts, tag, args, dry_run=args.dry_run, out_path=planned,
            )
        rows.append({
            "file": str(path), "total": len(entries), "entries": len(entries), "counts": counts,
            "redacted": str(out) if out else None, "map": str(map_path) if map_path else None,
        })

    placeholders = sum(int(row["entries"]) for row in rows)
    changed = sum(1 for row in rows if int(row["entries"]))
    if args.json:
        print(json.dumps({
            **_envelope("anon.py --batch"),
            "batch": True,
            "dry_run": bool(args.dry_run),
            "check": bool(args.check),
            "root": str(root),
            "files": rows,
            "skipped": [{"file": str(path), "reason": reason} for path, reason in skipped],
            "totals": {"scanned": len(rows), "changed": changed, "placeholders": placeholders,
                       "skipped": len(skipped)},
        }, ensure_ascii=False))
    elif not args.quiet:
        for row in rows:
            name = Path(str(row["file"])).name
            summary = ", ".join(f"{k}x{v}" for k, v in sorted(dict(row["counts"]).items()))  # type: ignore[arg-type]
            if args.check:
                if row.get("unscannable"):
                    print(f"SENSITIVE  {name}  (non scansionabile: convertilo prima, la redazione non l'ha visto)")
                else:
                    print(f"{'SENSITIVE' if row['total'] else 'clean    '}  {name}" + (f"  {summary}" if summary else ""))
            elif row["redacted"]:
                verb = "would write" if args.dry_run else "->"
                print(f"  {name}  [{summary}]  {verb} {Path(str(row['redacted'])).name}")
            else:
                print(f"  {name}  clean")
        for path, reason in skipped:
            print(f"  skipped  {path.name}  ({reason})")
        verb = "would redact" if args.dry_run else "redacted"
        print(
            f"anon: {len(rows)} file(s), {changed} {verb}, {placeholders} placeholder(s), "
            f"{len(skipped)} skipped"
        )
    if args.check and any(int(row["total"]) for row in rows):
        return 1
    return 0


def cmd_anonymize_container(args: argparse.Namespace, src: Path) -> int:
    """`anon.py verbale.docx` -> `verbale.redacted.docx`: same type, same layout, same map shape.

    The alternative (convert to Markdown, redact, hand back Markdown) is what the guard does for a
    `read`; here the operator gets the DOCUMENT back, with headers, footers, comments and properties
    rewritten where they live instead of dropped.
    """
    if args.stdout:
        print("anon: --stdout does not apply to a container: its content is not text", file=sys.stderr)
        return 2
    entities = resolve_entities(args)
    destination = Path(args.map).expanduser().parent if args.map else DEFAULT_MAPS
    tag = new_tag() if args.dry_run else allocate_tag([DEFAULT_MAPS, destination], getattr(args, "tag", None))
    out: Path | None = None if args.dry_run else (Path(args.out).expanduser() if args.out else _default_output(src))
    entries, counts, parts = anonymize_container(
        src, out, entities,
        include_heuristics=not args.no_hosts, families=resolve_families(args), tag=tag, dry_run=args.dry_run,
    )
    if args.dry_run:
        if args.json:
            print(json.dumps({
                **_envelope("anon.py --dry-run"),
                "dry_run": True, "container": True, "file": str(src), "sensitive": bool(entries),
                "would_write": str(out), "would_map": str(plan_map_path(args)) if entries else None,
                "entries": len(entries), "counts": counts, "parts": parts,
            }, ensure_ascii=False))
            return 0
        if not args.quiet:
            if entries:
                summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                print(f"anon: dry run — nothing written; would redact {len(entries)} value(s) [{summary}]")
                print(f"anon: would write -> {out}")
                print(f"anon: would map   -> {plan_map_path(args)}")
                for name, count in sorted(parts.items()):
                    print(f"       {name}: {count}")
            else:
                print(f"anon: dry run — nothing sensitive in {src}; nothing would be written")
        return 0

    map_path = plan_map_path(args) if entries else None
    if map_path is not None:
        write_map(map_path, src, out, entries, counts)
    if args.json:
        print(json.dumps({
            **_envelope("anon.py"), "container": True, "sensitive": bool(entries),
            "redacted": str(out), "map": str(map_path) if map_path else None,
            "entries": len(entries), "counts": counts, "parts": parts,
        }, ensure_ascii=False))
        return 0
    if not args.quiet:
        if not entries:
            print(f"anon: nothing sensitive found in {src}", file=sys.stderr)
        else:
            summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            print(f"anon: {len(entries)} placeholder(s) [{summary}] over {len(parts)} part(s)")
        print(f"anon: redacted -> {out}")
        if map_path is not None:
            print(f"anon: map      -> {map_path}")
    return 0


def cmd_anonymize(args: argparse.Namespace) -> int:
    src = Path(args.file).expanduser()
    if not src.is_file():
        print(f"anon: not a file: {src}", file=sys.stderr)
        return 2
    kind = sniff(src)
    if kind == "container":
        return cmd_anonymize_container(args, src)
    if kind is not None:
        print(
            f"anon: REFUSED — {src} is a binary file (PDF/image/legacy .doc/.xls/.ppt). Reading it\n"
            "      as text would redact almost nothing and leave a corrupted copy named 'redacted'.\n"
            "      Convert it to Markdown first, then anonymize that:\n"
            '        doc_to_markdown(path="…", output="…/file.md")   # writes, does not return\n'
            "        /anon …/file.md                                  # redacts the Markdown\n"
            "      Nothing was written.",
            file=sys.stderr,
        )
        return 2
    entities = resolve_entities(args)
    text = read_text(src)
    # The tag must be unique among the maps that EXIST — and a map may be redirected with --map, so
    # both the default directory and the destination are scanned: scanning only one would let a
    # --map run draw a tag that an existing ~/.anon/maps map already uses, which is the silent
    # substitution the tag exists to prevent.
    destination = Path(args.map).expanduser().parent if args.map else DEFAULT_MAPS
    # A dry run writes nothing, so it does not need a collision-free tag: the preview only shows
    # the SHAPE (`[EMAIL-1-a3f9d1]`), and the real run draws its own.
    tag = new_tag() if args.dry_run else allocate_tag([DEFAULT_MAPS, destination], getattr(args, "tag", None))
    redacted, entries, counts = anonymize(
        text,
        entities,
        include_heuristics=not args.no_hosts,
        families=resolve_families(args),
        tag=tag,
    )

    out, map_path = write_run(src, redacted, entries, counts, tag, args, dry_run=args.dry_run)

    if args.dry_run:
        return _emit_dry_run(src, out, map_path, entries, counts, args)

    if args.json:
        print(json.dumps({
            **_envelope("anon.py"),
            "sensitive": bool(entries),
            "redacted": str(out) if out else "(stdout)",
            "map": str(map_path) if map_path else None,
            "entries": len(entries),
            "counts": counts,
        }, ensure_ascii=False))
        return 0

    if not entries:
        if not args.quiet:
            print(f"anon: nothing sensitive found in {src}", file=sys.stderr)
            if out is not None:
                print(str(out))
        return 0

    if not args.quiet:
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        print(f"anon: {len(entries)} placeholder(s) [{summary}]")
        if out is not None:
            print(f"anon: redacted -> {out}")
        print(f"anon: map      -> {map_path}")
    return 0


def cmd_prune_maps(args: argparse.Namespace) -> int:
    """List the maps older than N days — and delete them only with `--yes`.

    `~/.anon/maps` otherwise grows forever: every anonymize writes a map holding the REAL values,
    and nothing ever removes one. Dry-run by default, because a deletion the operator cannot
    preview is not something this tool should do on its own.
    """
    days = args.prune_maps
    if days < 1:
        # `0` would mean "cutoff = now", i.e. delete everything including the map written a second
        # ago. Deleting is not undoable here, so the smallest accepted window is one day.
        print("anon: --prune-maps takes at least 1 day (nothing is deleted without --yes)", file=sys.stderr)
        return 2
    cutoff = time.time() - days * 86400
    candidates: list[tuple[Path, os.stat_result]] = []
    if DEFAULT_MAPS.is_dir():
        for path in sorted(DEFAULT_MAPS.glob("*.map.json")):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime < cutoff:
                candidates.append((path, stat))

    removed = 0
    freed = 0
    if args.yes:
        for path, stat in candidates:
            try:
                path.unlink()
            except OSError as exc:
                print(f"anon: could not remove {path.name}: {exc}", file=sys.stderr)
                continue
            removed += 1
            freed += stat.st_size

    report: dict[str, object] = {
        **_envelope("anon.py --prune-maps"),
        "maps_dir": str(DEFAULT_MAPS),
        "days": days,
        "candidates": len(candidates),
        "deleted": removed,
        "bytes_freed": freed,
        "applied": bool(args.yes),
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
        return 0
    for path, stat in candidates:
        age = (time.time() - stat.st_mtime) / 86400
        print(f"anon: {'removed' if args.yes else 'would remove'} {path.name} ({age:.0f} days old, {stat.st_size} bytes)")
    if not candidates:
        print(f"anon: no map older than {days} day(s) in {DEFAULT_MAPS}")
    elif args.yes:
        print(f"anon: {removed} map(s) removed, {freed} bytes freed")
    else:
        print(f"anon: {len(candidates)} map(s) older than {days} day(s) — rerun with --yes to delete them")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anon.py",
        description="Deterministic, local anonymizer: FILE -> redacted copy + reversible map.",
    )
    parser.add_argument("file", nargs="?", help="text file to anonymize ('-' with --check reads stdin)")
    parser.add_argument("--out", help="write the redacted copy here (default: <name>.redacted.<ext>)")
    parser.add_argument("--map", help="write the map here (default: ~/.anon/maps/<id>.map.json)")
    parser.add_argument(
        "--entities",
        action="append",
        metavar="PATH",
        help="dictionary file (repeatable; default: ~/.anon/entities.txt + people.txt + clients.txt, "
        "whichever exist)",
    )
    parser.add_argument("--catalogs", help="comma-separated catalog names from ~/.anon/catalogs/")
    parser.add_argument("--patterns", help=f"pattern groups to apply: {', '.join(PATTERN_FAMILIES)}")
    parser.add_argument("--list-catalogs", action="store_true", help="list the installed catalogs and exit")
    parser.add_argument(
        "--prune-maps",
        type=int,
        metavar="DAYS",
        help="list the maps in ~/.anon/maps older than DAYS (1 or more) and exit (add --yes to delete them)",
    )
    parser.add_argument(
        "--yes", action="store_true", help="with --prune-maps: actually delete the listed maps"
    )
    parser.add_argument("--allow", help="path-glob allowlist used by --check (default: ~/.anon/allow.txt)")
    parser.add_argument(
        "--allow-glob",
        action="append",
        metavar="GLOB",
        help="extra path glob treated as un-sensitive by --check (repeatable; same syntax as --allow)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what WOULD be redacted and where it would be written, writing nothing",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="treat FILE as a directory and anonymize every eligible file in it",
    )
    parser.add_argument("--check", action="store_true", help="report sensitive content, write nothing")
    parser.add_argument(
        "--audit",
        action="store_true",
        help="verify that a file is REALLY anonymized (residual content + near misses), write nothing",
    )
    parser.add_argument(
        "--reveal",
        action="store_true",
        help="with --audit: show the near-miss words and entity names (they are the sensitive data)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable JSON on stdout")
    parser.add_argument("--stdout", action="store_true", help="write the redacted text to stdout")
    parser.add_argument("--no-hosts", action="store_true", help="skip hostname/phone/IP heuristics")
    parser.add_argument(
        "--tag",
        help="pin the per-map placeholder tag, used verbatim (default: random, checked against the existing maps)",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress informational messages")
    parser.add_argument("--version", action="version", version=f"anon.py {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_catalogs:
        available = list_catalogs()
        if not available:
            print(f"anon: no catalogs installed under {CATALOGS_DIR}")
            return 0
        for item in available:
            print(f"{item['name']}\t{item['entries']} entries\t{item['path']}")
        return 0
    if args.prune_maps is not None:
        return cmd_prune_maps(args)
    if not args.file:
        print("anon: a file is required (or use --list-catalogs)", file=sys.stderr)
        return 2
    try:
        if args.audit:
            return cmd_audit(args)
        if args.batch:
            return cmd_batch(args)
        return cmd_check(args) if args.check else cmd_anonymize(args)
    except ValueError as exc:
        print(f"anon: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
