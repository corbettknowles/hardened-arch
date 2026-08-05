#!/usr/bin/env bash
set -Eeuo pipefail

INSTALL=/home/corbett/hardened-clean-builder
BUILDER="$INSTALL/hardened_clean_iso_builder.py"
APPROVER="$INSTALL/approve_clean_builder_change.py"
RUNNER="$INSTALL/run_clean_builder.py"

CHECK_NAME='ESP contains BOOTX64.EFI'
REASON="Fix the Limine ESP verifier so it recognizes mtools FAT directory output, which renders BOOTX64.EFI as separate 8.3 name and extension columns (BOOTX64  EFI), while still requiring the actual file to be present."

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
ARCHIVE="/home/corbett/hardened-builder-archives/esp-bootx64-verifier-fix-$STAMP"

fail() {
    printf 'FAIL: %s\n' "$*" >&2
    exit 1
}

[[ $EUID -eq 0 ]] || fail "Run with sudo."
[[ -f "$BUILDER" ]] || fail "Builder is missing: $BUILDER"
[[ -x "$APPROVER" ]] || fail "Approval tool is missing: $APPROVER"
[[ -x "$RUNNER" ]] || fail "Clean-builder launcher is missing: $RUNNER"

for tool in python3 mkfs.fat mcopy mdir truncate; do
    command -v "$tool" >/dev/null 2>&1 || fail "Required tool is missing: $tool"
done

echo '===== CURRENT ESP VERIFIER ====='
grep -n -A8 -B3 -F "\"$CHECK_NAME\"" "$BUILDER" || \
    fail "Could not find the ESP BOOTX64 verifier."

mkdir -p "$ARCHIVE"
cp -a "$BUILDER" "$ARCHIVE/"
sha512sum "$ARCHIVE/$(basename "$BUILDER")" \
    > "$ARCHIVE/SHA512SUMS.before"

chmod u+w "$BUILDER"

python3 - "$BUILDER" "$CHECK_NAME" <<'PY'
from pathlib import Path
import ast
import sys

path = Path(sys.argv[1])
check_name = sys.argv[2]
source = path.read_text(encoding="utf-8")
tree = ast.parse(source)

matches = []

for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    if len(node.args) < 3:
        continue
    first = node.args[0]
    if isinstance(first, ast.Constant) and first.value == check_name:
        matches.append(node)

if len(matches) != 1:
    raise SystemExit(
        f"FAIL: Expected exactly one {check_name!r} record_check call; "
        f"found {len(matches)}."
    )

call = matches[0]
condition = call.args[1]
detail = call.args[2]

if not (
    isinstance(detail, ast.Attribute)
    and detail.attr == "stdout"
):
    detail_source = ast.get_source_segment(source, detail)
    raise SystemExit(
        "FAIL: The verifier detail argument is not a simple .stdout expression: "
        f"{detail_source!r}"
    )

stdout_source = ast.get_source_segment(source, detail)
if not stdout_source:
    raise SystemExit("FAIL: Could not recover the ESP listing expression.")

replacement = (
    f'"BOOTX64 EFI" in " ".join({stdout_source}.split())'
)

lines = source.splitlines(keepends=True)
offsets = [0]
for line in lines:
    offsets.append(offsets[-1] + len(line))

start = offsets[condition.lineno - 1] + condition.col_offset
end = offsets[condition.end_lineno - 1] + condition.end_col_offset

old_condition = source[start:end]
if "BOOTX64.EFI" not in old_condition:
    raise SystemExit(
        "FAIL: The old condition does not contain the expected literal "
        f"'BOOTX64.EFI': {old_condition!r}"
    )

patched = source[:start] + replacement + source[end:]
path.write_text(patched, encoding="utf-8")

print("Old condition:")
print(old_condition)
print("New condition:")
print(replacement)
PY

echo
echo '===== PYTHON SYNTAX CHECK ====='
python3 -m py_compile "$BUILDER"
echo 'PASS: builder syntax'

echo
echo '===== PATCHED ESP VERIFIER ====='
grep -n -A8 -B3 -F "\"$CHECK_NAME\"" "$BUILDER"

echo
echo '===== REAL MTOOLS PREFLIGHT ====='
TMPDIR=$(mktemp -d /tmp/hardened-esp-verifier.XXXXXX)
trap 'rm -rf "$TMPDIR"' EXIT

IMG="$TMPDIR/esp.img"
PAYLOAD="$TMPDIR/BOOTX64.EFI"
LISTING="$TMPDIR/mdir.txt"

truncate -s 64M "$IMG"
mkfs.fat -F 32 -n HARDENEFI "$IMG"
printf 'Hardened BOOTX64 verifier test\n' > "$PAYLOAD"

mmd -i "$IMG" ::/EFI
mmd -i "$IMG" ::/EFI/BOOT
mcopy -i "$IMG" "$PAYLOAD" ::/EFI/BOOT/BOOTX64.EFI
mdir -i "$IMG" ::/EFI/BOOT > "$LISTING"

cat "$LISTING"

python3 - "$LISTING" <<'PY'
from pathlib import Path
import sys

listing = Path(sys.argv[1]).read_text(
    encoding="utf-8",
    errors="replace",
)
normalized = " ".join(listing.split())

if "BOOTX64 EFI" not in normalized:
    raise SystemExit(
        "FAIL: Normalized mdir output did not contain 'BOOTX64 EFI'."
    )

print("PASS: verifier recognizes the real mtools directory format")
PY

echo
echo '===== EXPLICIT APPROVAL ====='
"$APPROVER" --reason "$REASON"

echo
echo '===== TRUSTED CLEAN-BUILDER AUDIT ====='
"$RUNNER" build --audit-only

echo
echo 'ESP BOOTX64 VERIFIER FIX COMPLETE'
echo "Archive: $ARCHIVE"
echo
echo 'The failed build run was retained for evidence and was not deleted.'
