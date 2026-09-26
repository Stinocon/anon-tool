#!/usr/bin/env bash
#
# anon-home-backup.sh — the encrypted copy of the private store, and the way back from it.
#
# `~/.anon` holds the maps (the REAL value behind every placeholder) and the private dictionary.
# None of it is in a repository, so an encrypted archive is the only copy that survives a lost disk.
# The live store itself stays plaintext with mode 0600 (DEC-0033): the engine writes and rereads a
# map with no key, so a passphrase cannot be part of that path.
#
#   bash scripts/anon-home-backup.sh backup  [DEST]         # default ~/private-backups/anon-home-<date>.tar.gz.enc
#   bash scripts/anon-home-backup.sh restore ARCHIVE [DIR]  # default target ~/.anon
#
# Both directions verify BEFORE they report success: the archive is decrypted again in the same run
# and every file is compared with a sha256 manifest kept inside it. An unverified backup is a hope,
# and a restore that writes before it checks can destroy the state it was meant to replace.
#
# What goes in: every file under the home EXCEPT `models/` (2 GB, re-downloadable with `make model`),
# `__pycache__/`, `*.pyc` and `.DS_Store`. Code and data both — this is the live tree, and the live
# tree is what a restore has to return. An empty directory is not archived; the engine creates the
# directories it writes into (`anon.py::_write_private`, mode 0700). Contents, paths and modes
# travel; the filesystem's own bookkeeping (extended attributes, ACLs) does not, so an archive made
# on a Mac extracts cleanly with GNU tar on Linux.
#
# Exits 1 on a refusal (empty or mismatched passphrase, missing tool, existing destination, a restore
# target that is not empty) and 2 when a verification fails. It writes nothing in either case.
#
# The passphrase is read once, from `$ANON_BACKUP_PASSPHRASE` when set and otherwise from a prompt,
# and the variable is removed before the script starts any other program. The prompt is still the
# default: a value in the environment is readable by any same-user process while the script runs (on
# Linux by reading the process's initial environment block, which no `unset` can reach).
set -uo pipefail
umask 077

PROG="${0##*/}"
WORK=""
PARTIAL=""

cleanup() {
  [ -n "$WORK" ] && rm -rf "$WORK"
  [ -n "$PARTIAL" ] && rm -f "$PARTIAL"
  return 0
}
trap cleanup EXIT

die() { printf '%s: %s\n' "$PROG" "$*" >&2; exit "${2:-1}"; }

usage() {
  cat <<'EOF'
usage: anon-home-backup.sh backup  [DEST]         encrypted copy of the private store
       anon-home-backup.sh restore ARCHIVE [DIR]   write it back, verified before anything is written

  --home=DIR   the store itself (default ~/.anon)
  --out=FILE   where the archive goes (default ~/private-backups/anon-home-<date>.tar.gz.enc)
  --force      backup: overwrite an existing archive. restore: move the current tree aside first.

The passphrase is read from $ANON_BACKUP_PASSPHRASE when set, otherwise from a prompt. It is never an
argument (arguments are visible in `ps`) and it is not stored anywhere. Without it the archive cannot
be opened, by design. The variable is removed before the script starts any other program; the prompt
is the safe path, because a same-user process can read the environment while the script is starting.
EOF
}

# ---- tools -------------------------------------------------------------------------------------
# One check per tool, up front: a missing `tar` discovered halfway through leaves an encrypted
# archive of an incomplete tree, which is worse than no archive.

if command -v sha256sum >/dev/null 2>&1; then SHA=(sha256sum)
elif command -v shasum >/dev/null 2>&1; then SHA=(shasum -a 256)
else die "neither sha256sum nor shasum is in PATH"
fi
command -v tar >/dev/null 2>&1 || die "tar is not in PATH"
command -v openssl >/dev/null 2>&1 || die "openssl is not in PATH"

ITER=600000
hash_of() { "${SHA[@]}" "$1" | awk '{print $1}'; }

passphrase() {  # $1 = "confirm" to ask twice (backup); restore asks once
  # Set-but-empty is a REFUSAL, not a request to prompt: a caller that set the variable to an
  # empty string meant to run non-interactively, and prompting instead hides that mistake.
  if [ "${ANON_BACKUP_PASSPHRASE+x}" = x ]; then
    [ -n "$ANON_BACKUP_PASSPHRASE" ] || die "empty passphrase — refusing"
    printf '%s' "$ANON_BACKUP_PASSPHRASE"; return 0
  fi
  # A prompt needs a terminal to read from. With stdin on a pipe nobody closes — a test harness, a
  # cron job, a service — `read` blocks forever: the run neither finishes nor fails. Refuse
  # instead; the passphrase has an explicit channel, `ANON_BACKUP_PASSPHRASE`.
  [ -t 0 ] || die "no terminal to prompt on: set ANON_BACKUP_PASSPHRASE"
  local p='' p2=''
  printf 'Passphrase: ' >&2
  IFS= read -rs p || die "no passphrase read"
  printf '\n' >&2
  [ -n "$p" ] || die "empty passphrase — refusing"
  if [ "$1" = confirm ]; then
    printf 'Again:      ' >&2
    IFS= read -rs p2 || die "no passphrase read"
    printf '\n' >&2
    [ "$p" = "$p2" ] || die "the two passphrases differ — refusing"
  fi
  printf '%s' "$p"
}

encrypt() { openssl enc    -aes-256-cbc -pbkdf2 -iter "$ITER" -salt -pass stdin -in "$1" -out "$2"; }
decrypt() { openssl enc -d -aes-256-cbc -pbkdf2 -iter "$ITER"         -pass stdin -in "$1" -out "$2"; }

# The archive is an input a restore has to trust. Before a single byte reaches the target, every
# member must be a plain relative file: a `../` name would write outside the home, a leading `/`
# would write to an absolute path, and a symlink member could turn the next entry into either.
audit_members() {  # $1 = plaintext tar
  local names
  names="$(tar tzf "$1")" || { echo "the payload is not a readable tar" >&2; return 1; }
  [ -n "$names" ] || { echo "the payload holds no member" >&2; return 1; }
  if printf '%s\n' "$names" | grep -qE '(^|/)\.\.(/|$)|^/'; then
    printf 'the payload holds an absolute or parent-relative name:\n' >&2
    printf '%s\n' "$names" | grep -E '(^|/)\.\.(/|$)|^/' | head -5 >&2
    return 1
  fi
  # `tar tv` distinguishes the member types: only a regular FILE is allowed. Our own archive holds
  # no directory member — `find -type f` is what builds the list — and a directory is not harmless: a
  # member named `.` with mode 0777 is applied by `-p` to the restore TARGET itself, so a crafted
  # archive could take the store from 0700 to 0777 (measured; the audit used to accept `d`).
  if tar tvzf "$1" | grep -qvE '^-'; then
    printf 'the payload holds a member that is not a plain file:\n' >&2
    tar tvzf "$1" | grep -vE '^-' | head -5 >&2
    return 1
  fi
  return 0
}

# The manifest is what makes a wrong passphrase and a tampered payload visible: openssl exits 0 on
# bytes it can decrypt into garbage, and CBC without AEAD has no tag to check.
#
# The file COUNT is not redundant, and it compares the wrong thing if it excludes a name rather than
# a position: `-c` verifies only what the manifest lists, so a file slipped into the archive would
# otherwise pass unnoticed — and `! -name MANIFEST.sha256` excludes that name ANYWHERE, so a planted
# `sub/MANIFEST.sha256` passed both checks and was written into the restored store. Every regular
# file is counted, the archive's own manifest subtracted, and nothing else.
tree_files() {  # $1 = extracted tree; the files it holds, minus the manifest at its root
  echo $(( $(find "$1" -type f | wc -l | tr -d ' ') - 1 ))
}

check_tree() {  # $1 = extracted tree, $2 = directory for the log
  local log="$2/check.log" listed actual
  ( cd "$1" && "${SHA[@]}" -c MANIFEST.sha256 ) >"$log" 2>&1 || {
    grep -v ': OK$' "$log" | head -20 >&2
    echo "the content does not match its own manifest" >&2
    return 1
  }
  listed="$(grep -c '' "$1/MANIFEST.sha256")"
  actual="$(tree_files "$1")"
  if [ "$listed" != "$actual" ]; then
    printf 'the archive holds %s file(s), the manifest lists %s\n' "$actual" "$listed" >&2
    return 1
  fi
  [ "$actual" -gt 0 ] || { echo "the archive is empty" >&2; return 1; }
  return 0
}

# ---- backup ------------------------------------------------------------------------------------

cmd_backup() {  # $1 = destination (optional; --out=FILE wins)
  local home="$HOME_DIR" out="$1" tar back ex pass f files=() operands=()
  [ -d "$home" ] || die "$home does not exist"   # a builtin: nothing runs before the passphrase
  [ -n "$OUT" ] && out="$OUT"
  # Read first, and drop the inherited copy before starting any other program: everything below runs
  # without it, so no child of this script — openssl included — carries the passphrase.
  pass="$(passphrase confirm)" || exit $?
  unset ANON_BACKUP_PASSPHRASE
  [ -n "$out" ] || out="$HOME/private-backups/anon-home-$(date +%Y%m%d-%H%M).tar.gz.enc"
  if [ -e "$out" ] && [ "$FORCE" != 1 ]; then die "$out exists — use --out=FILE or --force"; fi
  mkdir -p "$(dirname "$out")" || die "cannot create $(dirname "$out")"
  # the private partial is created 0600 by the umask, and the directory may be wider than that
  PARTIAL="$out.partial"

  # The list is built once and used for both the manifest and the tar: a list computed twice is two
  # descriptions of a tree that may have changed in between.
  while IFS= read -r -d '' f; do
    case "$f" in
      *$'\n'*|*$'\r'*)
        # the manifest is line-based, so such a name splits its own line. Refused here, by name,
        # rather than later as "the manifest lists more files than the archive holds".
        die "a file name holds a newline or a carriage return, which the manifest cannot carry: $(printf '%q' "$f")" ;;
      */._*)
        # bsdtar reads a `._name` member as AppleDouble metadata for `name`. Measured: with such a
        # file in the store the archive bsdtar writes is CORRUPT ("Truncated input file") and cannot
        # be extracted — with `COPYFILE_DISABLE=1`, with `--no-xattrs`, with `--no-mac-metadata`, and
        # with every pair of them. A name the tool cannot archive faithfully is refused here, saying
        # which file, rather than writing a broken archive or dropping the file silently.
        die "a file name starts with '._', which bsdtar reserves for macOS metadata and cannot archive: rename or remove $(printf '%q' "$f")" ;;
    esac
    files+=("${f#./}")
  done < <(
    cd "$home" && find . -type f \
      ! -path './models/*' ! -path '*/__pycache__/*' ! -name '*.pyc' ! -name '.DS_Store' -print0
  )
  [ "${#files[@]}" -gt 0 ] || die "$home holds no file to back up"

  WORK="$(mktemp -d)" || die "mktemp failed"
  tar="$WORK/anon-home.tar.gz"
  back="$WORK/back.tar.gz"
  ex="$WORK/extract"

  # one line per file, in the `sha256sum -c` shape (`hash` two spaces `path`)
  : > "$WORK/MANIFEST.sha256"
  for f in "${files[@]}"; do
    printf '%s  %s\n' "$(hash_of "$home/$f")" "$f" >> "$WORK/MANIFEST.sha256"
  done

  # The manifest is the first member and comes from a directory of its own: writing it into the home
  # to archive it with the tree would put a file in the store that no run of the tool put there.
  #
  # Every operand is `./`-prefixed, and that is load-bearing: bsdtar does not accept `--` after the
  # first operand, so a file named `-C.redacted` (a download keeps the name it was uploaded with)
  # would be read as an option and silently DROPPED. `-C` is positional and honoured after operands
  # on both tar implementations, which is what lets the manifest and the tree share one archive.
  #
  # `--no-xattrs` and `COPYFILE_DISABLE=1` together, and both are needed: bsdtar records the macOS
  # `com.apple.*` xattrs twice over — as a generated `._name` MEMBER (which GNU tar extracts as a real
  # file, measured on Linux) and as PAX records — and each flag removes one of the two. With
  # `--no-xattrs` alone the leftover member is emitted carrying nothing, and with `COPYFILE_DISABLE`
  # alone a real file NAMED `._something` truncates the archive; a name that cannot be archived is
  # refused above, where it can be named. GNU tar accepts `--no-xattrs` and ignores the variable.
  for f in "${files[@]}"; do operands+=("./$f"); done
  COPYFILE_DISABLE=1 tar czf "$tar" --no-xattrs -C "$WORK" MANIFEST.sha256 -C "$home" "${operands[@]}" \
    || die "tar failed"

  printf '%s' "$pass" | encrypt "$tar" "$PARTIAL" || die "openssl failed"
  chmod 600 "$PARTIAL" || die "cannot restrict the permissions of $PARTIAL"
  [ -f "$PARTIAL" ] || die "openssl wrote no archive"

  printf '%s' "$pass" | decrypt "$PARTIAL" "$back" \
    || die "VERIFY FAILED: the archive does not decrypt back" 2
  [ "$(hash_of "$tar")" = "$(hash_of "$back")" ] \
    || die "VERIFY FAILED: the decrypted bytes are not what was encrypted" 2

  audit_members "$back" || die "VERIFY FAILED: see above" 2
  mkdir -p "$ex"
  tar xzf "$back" -C "$ex" || die "VERIFY FAILED: the payload is not a readable tar" 2
  check_tree "$ex" "$WORK" || die "VERIFY FAILED: see above" 2
  unset pass

  mv "$PARTIAL" "$out" || die "cannot move the archive into place"
  chmod 600 "$out" || die "cannot restrict the permissions of $out"
  PARTIAL=""

  printf 'VERIFIED: %s file(s), %s -> %s\n' "${#files[@]}" "$(ls -lh "$out" | awk '{print $5}')" "$out"
  printf 'Restore:  bash %s restore %s\n' "$0" "$out"
  printf 'The passphrase is nowhere in the archive: without it the archive is lost, by design.\n'
}

# ---- restore -----------------------------------------------------------------------------------

cmd_restore() {  # $1 = archive, $2 = target directory (optional)
  local archive="$1" target count back ex pass safe
  target="${2:-$HOME_DIR}"
  [ -n "$archive" ] || die "usage: $PROG restore ARCHIVE [DIR]"
  [ -f "$archive" ] || die "$archive does not exist"

  # read and drop the inherited copy before any other program runs (`mktemp` below is the first)
  pass="$(passphrase)" || exit $?
  unset ANON_BACKUP_PASSPHRASE

  WORK="$(mktemp -d)" || die "mktemp failed"
  back="$WORK/plain.tar.gz"
  ex="$WORK/extract"

  printf '%s' "$pass" | decrypt "$archive" "$back" \
    || die "the archive does not decrypt — wrong passphrase, or a damaged file" 2
  unset pass

  # Verified into a scratch tree first: nothing is written to the target until the payload has been
  # shown to be a plain-file archive whose content matches its own manifest. A wrong passphrase is
  # caught here rather than by a failed extraction into the tree it was about to overwrite.
  audit_members "$back" || die "the archive does not open: wrong passphrase, or a damaged file (see above)" 2
  mkdir -p "$ex"
  tar xzf "$back" -C "$ex" || die "the archive does not open: wrong passphrase, or a damaged file" 2
  check_tree "$ex" "$WORK" || die "REFUSING: the content does not match its own manifest (wrong passphrase, or a damaged file)" 2
  count="$(tree_files "$ex")"

  if [ -e "$target" ] && [ -n "$(ls -A "$target" 2>/dev/null)" ]; then
    if [ "$FORCE" != 1 ]; then
      die "$target is not empty — refusing. Use --force (the current tree is MOVED aside first, nothing is deleted), or restore into a fresh directory: $PROG restore $archive /some/empty/dir"
    fi
    # Nothing is deleted: the tree about to be replaced is renamed out of the way, on the same
    # filesystem, so restoring the wrong archive costs one `mv` back. `$$` keeps two restores in the
    # same second apart — with a colliding name `mv` would move the tree INTO the previous one.
    safe="$target.pre-restore-$(date +%Y%m%d-%H%M%S)-$$"
    mv "$target" "$safe" || die "could not move $target aside"
    printf 'current tree kept in %s\n' "$safe"
  fi
  mkdir -p -m 700 "$target" || die "cannot create $target"

  # `-p`: tar applies each member's mode masked by the umask, and this script runs under 077 —
  # without it a 0644 file in the store would come back 0600. The promise is that the store returns
  # as it was, and the modes are part of that.
  tar xpzf "$back" -C "$target" || die "extraction failed"
  # the manifest is the archive's own bookkeeping, not a file of the store
  rm -f "$target/MANIFEST.sha256"
  printf 'RESTORED: %s file(s) into %s\n' "$count" "$target"
  printf 'The model is not in the archive: run `make model` if you need it.\n'
}

# ---- entry point -------------------------------------------------------------------------------

SUB="${1:-}"
if [ $# -gt 0 ]; then shift; fi

HOME_DIR="${ANON_HOME:-$HOME/.anon}"
OUT=""
FORCE=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --home=*) HOME_DIR="${a#--home=}" ;;
    --out=*)  OUT="${a#--out=}" ;;
    --force)  FORCE=1 ;;
    -h|--help) usage; exit 0 ;;
    -*) die "unknown option: $a" ;;
    *) ARGS+=("$a") ;;
  esac
done

case "$SUB" in
  backup)  cmd_backup "${ARGS[0]:-}" ;;
  restore) cmd_restore "${ARGS[0]:-}" "${ARGS[1]:-}" ;;
  ''|help|-h|--help) usage ;;
  *) usage >&2; die "unknown command: $SUB" ;;
esac
