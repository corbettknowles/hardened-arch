#!/usr/bin/env bash
# Build Limine's x86_64 UEFI boot manager from official source.
#
# Usage:
#   ./build_limine_from_source.sh
#
# Overrides:
#   LIMINE_REF=v12.3.2
#   WORK_ROOT=~/bootloader-build
#   JOBS=4

set -Eeuo pipefail

die() {
    printf '\nERROR: %s\n' "$*" >&2
    exit 1
}

log() {
    printf '\n==> %s\n' "$*"
}

OWNER_HOME="${HOME}"
WORK_ROOT="${WORK_ROOT:-$OWNER_HOME/bootloader-build}"
SRC="$WORK_ROOT/limine-src"
OUT="$WORK_ROOT/limine"
LIMINE_REF="${LIMINE_REF:-v12.3.2}"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 2)}"
LOG="$WORK_ROOT/limine-build.log"

for cmd in git make find sha256sum autoconf automake nasm awk grep sed m4 ld ar objcopy; do
    command -v "$cmd" >/dev/null 2>&1 ||
        die "Missing command: $cmd"
done

compiler="${CC:-cc}"
compiler_bin="${compiler%% *}"
command -v "$compiler_bin" >/dev/null 2>&1 ||
    die "Missing C compiler: $compiler_bin"

mkdir -p "$WORK_ROOT"

log "Fetching Limine source at $LIMINE_REF"
if [[ ! -d "$SRC/.git" ]]; then
    git clone --recursive \
        https://github.com/limine-bootloader/limine.git "$SRC"
else
    git -C "$SRC" fetch --all --tags --prune
fi

git -C "$SRC" checkout --detach --force "$LIMINE_REF"
git -C "$SRC" submodule sync --recursive
git -C "$SRC" submodule update --init --recursive
git -C "$SRC" clean -ffdqx

cd "$SRC"

if [[ -x ./bootstrap ]]; then
    log "Bootstrapping build system"
    ./bootstrap
fi

[[ -x ./configure ]] ||
    die "Limine source tree did not produce ./configure."

log "Configuring source build"
configure_help="$(./configure --help 2>&1 || true)"
configure_args=()

if grep -q -- '--enable-uefi-x86_64' <<<"$configure_help"; then
    configure_args+=(--enable-uefi-x86_64)
elif grep -q -- '--enable-uefi-x86-64' <<<"$configure_help"; then
    configure_args+=(--enable-uefi-x86-64)
else
    die "This Limine revision exposes no x86_64 UEFI configure port."
fi
if grep -q -- '--disable-bios' <<<"$configure_help"; then
    configure_args+=(--disable-bios)
fi

./configure "${configure_args[@]}"

log "Compiling Limine"
set +e
make -j"$JOBS" 2>&1 | tee "$LOG"
status=${PIPESTATUS[0]}
set -e
((status == 0)) || die "Limine build failed. See $LOG"

mapfile -t efi_candidates < <(
    find "$SRC" -type f \
        \( -iname 'BOOTX64.EFI' -o \
           -iname 'limine_x64.efi' \) \
        -print | sort
)

((${#efi_candidates[@]})) || {
    find "$SRC" -type f -iname '*.efi' -print >&2 || true
    die "Could not locate the x86_64 UEFI executable after building."
}

chosen=""
for candidate in "${efi_candidates[@]}"; do
    case "$(basename "$candidate")" in
        BOOTX64.EFI|bootx64.efi)
            chosen="$candidate"
            break
            ;;
    esac
done
[[ -n "$chosen" ]] || chosen="${efi_candidates[0]}"

rm -rf "$OUT"
mkdir -p "$OUT"
install -m 0644 "$chosen" "$OUT/BOOTX64.EFI"

commit="$(git -C "$SRC" rev-parse HEAD)"
describe="$(
    git -C "$SRC" describe --always --tags --dirty 2>/dev/null ||
    echo "$LIMINE_REF"
)"
sha="$(sha256sum "$OUT/BOOTX64.EFI" | awk '{print $1}')"

cat > "$OUT/source-build.txt" <<EOF
project=Limine
source=https://github.com/limine-bootloader/limine.git
requested_ref=$LIMINE_REF
commit=$commit
describe=$describe
built_utc=$(date -u +%FT%TZ)
sha256=$sha
artifact=BOOTX64.EFI
EOF

printf '\nLIMINE SOURCE BUILD COMPLETE\n'
printf 'EFI binary:   %s\n' "$OUT/BOOTX64.EFI"
printf 'Build record: %s\n' "$OUT/source-build.txt"
printf 'SHA-256:      %s\n' "$sha"
