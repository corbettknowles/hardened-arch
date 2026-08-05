#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL=/home/corbett/hardened-clean-builder
BUILDER="$INSTALL/hardened_clean_iso_builder.py"
APPROVER="$INSTALL/approve_clean_builder_change.py"
RUNNER="$INSTALL/run_clean_builder.py"

OLD_LABEL='HARDENED_EFI'
NEW_LABEL='HARDENEFI'
REASON="Fix the Limine ESP FAT32 volume label: HARDENED_EFI is 12 characters, exceeding the FAT 11-character limit; use HARDENEFI and keep all other approved builder behavior unchanged."

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
ARCHIVE="/home/corbett/hardened-builder-archives/esp-label-fix-$STAMP"

fail() {
    printf 'FAIL: %s\n' "$*" >&2
    exit 1
}

[[ $EUID -eq 0 ]] || fail "Run with sudo."
[[ -f "$BUILDER" ]] || fail "Builder is missing: $BUILDER"
[[ -x "$APPROVER" ]] || fail "Approval tool is missing: $APPROVER"
[[ -x "$RUNNER" ]] || fail "Clean-builder launcher is missing: $RUNNER"

if (( ${#NEW_LABEL} > 11 )); then
    fail "Replacement FAT label is too long: $NEW_LABEL"
fi

echo '===== CURRENT LABEL REFERENCES ====='
grep -nF "$OLD_LABEL" "$BUILDER" || true

COUNT=$(grep -oF "$OLD_LABEL" "$BUILDER" | wc -l)
[[ "$COUNT" -eq 1 ]] || fail \
    "Expected exactly one $OLD_LABEL reference in the builder; found $COUNT."

mkdir -p "$ARCHIVE"
cp -a "$BUILDER" "$ARCHIVE/"
sha512sum "$ARCHIVE/$(basename "$BUILDER")" \
    > "$ARCHIVE/SHA512SUMS.before"

chmod u+w "$BUILDER"

python3 - "$BUILDER" "$OLD_LABEL" "$NEW_LABEL" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
old = sys.argv[2]
new = sys.argv[3]

text = path.read_text(encoding="utf-8")
count = text.count(old)

if count != 1:
    raise SystemExit(
        f"FAIL: Expected exactly one {old!r} reference; found {count}."
    )

path.write_text(text.replace(old, new, 1), encoding="utf-8")
PY

echo
echo '===== PYTHON SYNTAX CHECK ====='
python3 -m py_compile "$BUILDER"
echo 'PASS: builder syntax'

echo
echo '===== PATCHED LABEL REFERENCE ====='
grep -nF "$NEW_LABEL" "$BUILDER"

if grep -qF "$OLD_LABEL" "$BUILDER"; then
    fail "Old invalid FAT label is still present."
fi

echo
echo '===== MKFS.FAT LABEL PREFLIGHT ====='
TMP_IMG=$(mktemp /tmp/hardened-efi-label-test.XXXXXX.img)
trap 'rm -f "$TMP_IMG"' EXIT
truncate -s 64M "$TMP_IMG"
mkfs.fat -F 32 -n "$NEW_LABEL" "$TMP_IMG"
fatlabel "$TMP_IMG" | grep -Fx "$NEW_LABEL"
echo "PASS: mkfs.fat accepted label $NEW_LABEL"

echo
echo '===== EXPLICIT APPROVAL ====='
"$APPROVER" --reason "$REASON"

echo
echo '===== TRUSTED CLEAN-BUILDER AUDIT ====='
"$RUNNER" build --audit-only

echo
echo 'ESP FAT LABEL FIX COMPLETE'
echo "Old label: $OLD_LABEL (${#OLD_LABEL} characters)"
echo "New label: $NEW_LABEL (${#NEW_LABEL} characters)"
echo "Archive:   $ARCHIVE"
echo
echo 'The failed build run was retained for evidence and was not deleted.'
