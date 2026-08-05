#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

# Hardened Arch: build UPower plus the complete current stable Xfce 4.20 core
# into an isolated staging tree. This script does NOT merge that tree
# into the live rootfs and does NOT edit the ISO builder, Limine,
# initramfs, Plasma, SDDM, or GDM.
#
# Stable point-release manifest locked on 2026-07-29 from:
#   https://archive.xfce.org/src/xfce/<component>/4.20/
#
# Run as the normal user:
#   chmod 0755 ~/build-xfce-4.20-current-all.sh
#   ~/build-xfce-4.20-current-all.sh
#
# Optional:
#   JOBS=1 ~/build-xfce-4.20-current-all.sh
#   RUN_TESTS=1 ~/build-xfce-4.20-current-all.sh

XFCE_BASE="${XFCE_BASE:-/home/corbett/xfce-source-build}"
XFCE_STAGE="${XFCE_STAGE:-/home/corbett/xfce-source-stage/rootfs}"
TARGET_ROOT="${TARGET_ROOT:-/home/corbett/xorg-source-stage/rootfs}"
JOBS="${JOBS:-2}"
RUN_TESTS="${RUN_TESTS:-0}"

DOWNLOADS="$XFCE_BASE/downloads/stable-4.20-current"
LOGS="$XFCE_BASE/logs"
STAMPS="$XFCE_BASE/stamps/current-stable-4.20"
RUNS="$XFCE_BASE/runs"

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
RUN_ROOT="$RUNS/$RUN_ID"
RUN_SOURCES="$RUN_ROOT/sources"
RUN_BUILDS="$RUN_ROOT/build"
RUN_STAGES="$RUN_ROOT/package-stages"
MASTER_LOG="$LOGS/xfce-4.20-current-all-$RUN_ID.log"
MANIFEST_FILE="$XFCE_BASE/xfce-4.20-current.manifest"

CURRENT_PACKAGE="preflight"

fail() {
    printf '\nFAIL: %s\n' "$*" >&2
    exit 1
}

on_error() {
    local rc=$?
    printf '\n============================================================\n' >&2
    printf 'XFCE BATCH BUILD FAILED\n' >&2
    printf 'Package: %s\n' "$CURRENT_PACKAGE" >&2
    printf 'Line:    %s\n' "${BASH_LINENO[0]:-unknown}" >&2
    printf 'Status:  %s\n' "$rc" >&2
    printf 'Log:     %s\n' "$MASTER_LOG" >&2
    printf 'Run dir: %s\n' "$RUN_ROOT" >&2
    printf 'No builder, rootfs, Limine, or initramfs files were changed.\n' >&2
    printf '============================================================\n' >&2
    exit "$rc"
}
trap on_error ERR

[[ "$EUID" -ne 0 ]] || fail "Run this as corbett, not with sudo."
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || fail "JOBS must be a positive integer."
[[ "$RUN_TESTS" == 0 || "$RUN_TESTS" == 1 ]] ||
    fail "RUN_TESTS must be 0 or 1."

mkdir -p \
    "$DOWNLOADS" \
    "$LOGS" \
    "$STAMPS" \
    "$RUN_SOURCES" \
    "$RUN_BUILDS" \
    "$RUN_STAGES" \
    "$XFCE_STAGE"

exec > >(tee "$MASTER_LOG") 2>&1

cat > "$MANIFEST_FILE" <<'MANIFEST'
# Current stable UPower/Xfce releases, locked 2026-07-29.
# UPower is built first because xfce4-power-manager requires upower-glib.
# The remaining order follows the Xfce dependency chain.
upower               1.91.3
libxfce4util          4.20.1
xfconf                4.20.0
libxfce4windowing     4.20.6
libxfce4ui            4.20.2
garcon                4.20.0
exo                   4.20.0
thunar                4.20.9
tumbler               4.20.1
xfce4-panel           4.20.7
xfce4-settings        4.20.4
xfce4-session         4.20.4
xfwm4                 4.20.0
xfdesktop             4.20.2
xfce4-appfinder       4.20.0
thunar-volman         4.20.0
xfce4-power-manager   4.20.0
MANIFEST

COMPONENTS=(
    "libxfce4util|4.20.1"
    "xfconf|4.20.0"
    "libxfce4windowing|4.20.6"
    "libxfce4ui|4.20.2"
    "garcon|4.20.0"
    "exo|4.20.0"
    "thunar|4.20.9"
    "tumbler|4.20.1"
    "xfce4-panel|4.20.7"
    "xfce4-settings|4.20.4"
    "xfce4-session|4.20.4"
    "xfwm4|4.20.0"
    "xfdesktop|4.20.2"
    "xfce4-appfinder|4.20.0"
    "thunar-volman|4.20.0"
    "xfce4-power-manager|4.20.0"
)

echo "============================================================"
echo " UPOWER + XFCE 4.20 CURRENT-STABLE COMPLETE CORE BUILD"
echo "============================================================"
echo "Base:        $XFCE_BASE"
echo "Target root: $TARGET_ROOT"
echo "Xfce stage:  $XFCE_STAGE"
echo "Run root:    $RUN_ROOT"
echo "Jobs:        $JOBS"
echo "Run tests:   $RUN_TESTS"
echo "Master log:  $MASTER_LOG"
echo
cat "$MANIFEST_FILE"

echo
echo "========== REQUIRED HOST COMMANDS =========="

REQUIRED_COMMANDS=(
    awk
    bzip2
    cmp
    cp
    curl
    date
    file
    find
    gcc
    gdbus-codegen
    gettext
    glib-compile-resources
    glib-compile-schemas
    grep
    install
    make
    md5sum
    meson
    msgfmt
    ninja
    perl
    pkg-config
    python3
    readelf
    sed
    sha256sum
    sha512sum
    sort
    stat
    tar
    tee
)

for command_name in "${REQUIRED_COMMANDS[@]}"; do
    command_path="$(command -v "$command_name" 2>/dev/null || true)"
    [[ -n "$command_path" ]] ||
        fail "Required host command is missing: $command_name"
    printf 'PASS  %-28s %s\n' "$command_name" "$command_path"
done

echo
echo "========== DIRECTORY SAFETY =========="

[[ -d "$TARGET_ROOT/usr" ]] ||
    fail "Target root is missing or invalid: $TARGET_ROOT"

[[ -d "$XFCE_STAGE" && -w "$XFCE_STAGE" ]] ||
    fail "Xfce stage is missing or not writable: $XFCE_STAGE"

case "$XFCE_STAGE" in
    /home/corbett/xfce-source-stage/rootfs)
        ;;
    *)
        fail "Refusing unexpected XFCE_STAGE path: $XFCE_STAGE"
        ;;
esac

case "$TARGET_ROOT" in
    /home/corbett/xorg-source-stage/rootfs)
        ;;
    *)
        fail "Refusing unexpected TARGET_ROOT path: $TARGET_ROOT"
        ;;
esac

echo "TARGET ROOT SAFETY: PASS"
echo "XFCE STAGE SAFETY: PASS"

TARGET_PC_DIRS=(
    "$TARGET_ROOT/usr/lib/pkgconfig"
    "$TARGET_ROOT/usr/share/pkgconfig"
    "$TARGET_ROOT/usr/lib64/pkgconfig"
)

STAGE_PC_DIRS=(
    "$XFCE_STAGE/usr/lib/pkgconfig"
    "$XFCE_STAGE/usr/share/pkgconfig"
    "$XFCE_STAGE/usr/lib64/pkgconfig"
)

join_colon_existing() {
    local output=""
    local item
    for item in "$@"; do
        [[ -d "$item" ]] || continue
        if [[ -n "$output" ]]; then
            output+=":"
        fi
        output+="$item"
    done
    printf '%s' "$output"
}

TARGET_PC_LIBDIR="$(join_colon_existing "${TARGET_PC_DIRS[@]}")"
[[ -n "$TARGET_PC_LIBDIR" ]] ||
    fail "No target pkg-config directories were found."

echo
echo "========== TARGET BASE DEPENDENCIES =========="

# These are the base libraries required to produce the complete core set.
# Optional integrations such as libnotify, GStreamer, Poppler, and FFmpeg
# are detected automatically and are not required for the desktop to build.
REQUIRED_TARGET_MODULES=(
    glib-2.0
    gio-2.0
    gio-unix-2.0
    gobject-2.0
    gmodule-2.0
    gthread-2.0
    gtk+-3.0
    gdk-pixbuf-2.0
    cairo
    pango
    dbus-1
    x11
    ice
    sm
    xext
    xrender
    xrandr
    xi
    xcursor
    xfixes
    xcomposite
    xdamage
    xkbcommon
    epoxy
    libwnck-3.0
    libdisplay-info
    libudev
    gudev-1.0
    libusb-1.0
    polkit-gobject-1
)

MISSING_TARGET_MODULES=()

for module in "${REQUIRED_TARGET_MODULES[@]}"; do
    module_version="$(
        PKG_CONFIG_PATH= \
        PKG_CONFIG_LIBDIR="$TARGET_PC_LIBDIR" \
        pkg-config --modversion "$module" 2>/dev/null || true
    )"

    if [[ -n "$module_version" ]]; then
        printf 'PASS  %-28s %s\n' "$module" "$module_version"
    else
        printf 'MISS  %s\n' "$module"
        MISSING_TARGET_MODULES+=("$module")
    fi
done

if (( ${#MISSING_TARGET_MODULES[@]} > 0 )); then
    printf '\nMissing required target pkg-config modules:\n' >&2
    printf '  %s\n' "${MISSING_TARGET_MODULES[@]}" >&2
    fail "Stage the missing base dependencies before building Xfce."
fi

echo
echo "========== OPTIONAL TARGET INTEGRATIONS =========="

OPTIONAL_TARGET_MODULES=(
    upower-glib
    libnotify
    libcanberra
    libcanberra-gtk3
    libinput
    libxklavier
    polkit-gobject-1
    libsystemd
    libstartup-notification-1.0
    libexif
    poppler-glib
    gstreamer-1.0
    libffmpegthumbnailer
    libgsf-1
)

for module in "${OPTIONAL_TARGET_MODULES[@]}"; do
    module_version="$(
        PKG_CONFIG_PATH= \
        PKG_CONFIG_LIBDIR="$TARGET_PC_LIBDIR" \
        pkg-config --modversion "$module" 2>/dev/null || true
    )"

    if [[ -n "$module_version" ]]; then
        printf 'FOUND %-28s %s\n' "$module" "$module_version"
    else
        printf 'SKIP  %s\n' "$module"
    fi
done

# Only stage + target pkg-config files are visible. This prevents a host-only
# optional library from being detected and accidentally linked into the target.
refresh_build_environment() {
    local stage_pc_libdir
    stage_pc_libdir="$(join_colon_existing "${STAGE_PC_DIRS[@]}")"

    if [[ -n "$stage_pc_libdir" ]]; then
        export PKG_CONFIG_LIBDIR="$stage_pc_libdir:$TARGET_PC_LIBDIR"
    else
        export PKG_CONFIG_LIBDIR="$TARGET_PC_LIBDIR"
    fi

    export PKG_CONFIG_PATH=
    unset PKG_CONFIG_SYSROOT_DIR

    export PATH="$XFCE_STAGE/usr/bin:$PATH"
    export CPPFLAGS="-I$XFCE_STAGE/usr/include -I$XFCE_STAGE/usr/include/xfce4 -I$TARGET_ROOT/usr/include${ORIGINAL_CPPFLAGS:+ $ORIGINAL_CPPFLAGS}"
    export LDFLAGS="-L$XFCE_STAGE/usr/lib -Wl,-rpath-link,$XFCE_STAGE/usr/lib -L$TARGET_ROOT/usr/lib -Wl,-rpath-link,$TARGET_ROOT/usr/lib${ORIGINAL_LDFLAGS:+ $ORIGINAL_LDFLAGS}"
    export LD_LIBRARY_PATH="$XFCE_STAGE/usr/lib:$TARGET_ROOT/usr/lib${ORIGINAL_LD_LIBRARY_PATH:+:$ORIGINAL_LD_LIBRARY_PATH}"
    export GI_TYPELIB_PATH="$XFCE_STAGE/usr/lib/girepository-1.0:$TARGET_ROOT/usr/lib/girepository-1.0${ORIGINAL_GI_TYPELIB_PATH:+:$ORIGINAL_GI_TYPELIB_PATH}"
    export XDG_DATA_DIRS="$XFCE_STAGE/usr/share:$TARGET_ROOT/usr/share${ORIGINAL_XDG_DATA_DIRS:+:$ORIGINAL_XDG_DATA_DIRS}"
    export ACLOCAL_PATH="$XFCE_STAGE/usr/share/aclocal${ORIGINAL_ACLOCAL_PATH:+:$ORIGINAL_ACLOCAL_PATH}"
}

ORIGINAL_CPPFLAGS="${CPPFLAGS:-}"
ORIGINAL_LDFLAGS="${LDFLAGS:-}"
ORIGINAL_LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}"
ORIGINAL_GI_TYPELIB_PATH="${GI_TYPELIB_PATH:-}"
ORIGINAL_XDG_DATA_DIRS="${XDG_DATA_DIRS:-}"
ORIGINAL_ACLOCAL_PATH="${ACLOCAL_PATH:-}"

download_and_verify() {
    local package="$1"
    local version="$2"
    local archive="${package}-${version}.tar.bz2"
    local url="https://archive.xfce.org/src/xfce/${package}/4.20/${archive}"
    local destination="$DOWNLOADS/$archive"
    local partial="$destination.part"
    local checksum_record="$destination.sha256"
    local checksum_response
    local expected_sha256
    local actual_sha256

    [[ ! -e "$partial" ]] ||
        fail "Partial download already exists: $partial"

    if [[ ! -f "$destination" ]]; then
        echo "Downloading: $url"
        curl \
            --fail \
            --location \
            --proto '=https' \
            --tlsv1.2 \
            --retry 3 \
            --retry-all-errors \
            --output "$partial" \
            "$url"

        [[ -s "$partial" ]] ||
            fail "Downloaded archive is empty: $partial"

        mv --no-clobber "$partial" "$destination"
    else
        echo "Using existing archive after verification: $destination"
    fi

    checksum_response="$(
        curl \
            --fail \
            --silent \
            --show-error \
            --location \
            --proto '=https' \
            --tlsv1.2 \
            --retry 3 \
            --retry-all-errors \
            "${url}?sha256"
    )"

    expected_sha256="$(
        awk '
            match($0, /[0-9a-fA-F]{64}/) {
                print tolower(substr($0, RSTART, RLENGTH))
                exit
            }
        ' <<< "$checksum_response"
    )"

    [[ "$expected_sha256" =~ ^[0-9a-f]{64}$ ]] ||
        fail "Could not parse the official SHA-256 for $archive"

    actual_sha256="$(sha256sum "$destination" | awk '{print $1}')"

    echo "Official SHA-256: $expected_sha256"
    echo "Actual SHA-256:   $actual_sha256"

    [[ "$actual_sha256" == "$expected_sha256" ]] ||
        fail "SHA-256 mismatch for $archive"

    if [[ -e "$checksum_record" ]]; then
        recorded_sha256="$(awk 'NR == 1 {print $1}' "$checksum_record")"
        [[ "$recorded_sha256" == "$expected_sha256" ]] ||
            fail "Existing checksum record disagrees for $archive"
    else
        printf '%s  %s\n' "$expected_sha256" "$archive" > "$checksum_record"
    fi

    bzip2 -t "$destination" ||
        fail "Bzip2 integrity check failed for $archive"

    DOWNLOADED_ARCHIVE="$destination"
    DOWNLOADED_SHA256="$actual_sha256"
}

stamp_path_for() {
    local package="$1"
    local version="$2"
    printf '%s/%s-%s.done' "$STAMPS" "$package" "$version"
}

valid_stamp() {
    local stamp="$1"
    local package="$2"
    local version="$3"

    [[ -f "$stamp" ]] || return 1
    grep -Fxq "package=$package" "$stamp" || return 1
    grep -Fxq "version=$version" "$stamp" || return 1
    return 0
}

import_existing_libxfce4util() {
    local package="libxfce4util"
    local version="4.20.1"
    local stamp
    local pc_file="$XFCE_STAGE/usr/lib/pkgconfig/libxfce4util-1.0.pc"
    local library="$XFCE_STAGE/usr/lib/libxfce4util.so.7.0.0"
    local staged_version

    stamp="$(stamp_path_for "$package" "$version")"

    if valid_stamp "$stamp" "$package" "$version"; then
        return 0
    fi

    [[ -f "$pc_file" && -f "$library" ]] || return 0

    staged_version="$(
        awk -F': *' '$1 == "Version" {print $2; exit}' "$pc_file"
    )"

    [[ "$staged_version" == "$version" ]] || return 0

    {
        echo "package=$package"
        echo "version=$version"
        echo "origin=preexisting-verified-stage"
        echo "library_sha512=$(sha512sum "$library" | awk '{print $1}')"
        echo "recorded_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$stamp"

    echo "Imported existing verified libxfce4util 4.20.1 into batch state."
}

check_package_stage_collisions() {
    local package_stage="$1"
    local source_path
    local relative_path
    local destination_path

    while IFS= read -r -d '' source_path; do
        relative_path="${source_path#"$package_stage"/}"
        destination_path="$XFCE_STAGE/$relative_path"

        if [[ -d "$source_path" && ! -L "$source_path" ]]; then
            if [[ -e "$destination_path" || -L "$destination_path" ]]; then
                [[ -d "$destination_path" && ! -L "$destination_path" ]] ||
                    fail "Directory collision: $destination_path"
            fi
            continue
        fi

        if [[ -L "$source_path" ]]; then
            if [[ -e "$destination_path" || -L "$destination_path" ]]; then
                [[ -L "$destination_path" ]] ||
                    fail "Symlink collision with non-symlink: $destination_path"

                [[ "$(readlink "$source_path")" == "$(readlink "$destination_path")" ]] ||
                    fail "Different symlink already exists: $destination_path"
            fi
            continue
        fi

        if [[ -f "$source_path" ]]; then
            if [[ -e "$destination_path" || -L "$destination_path" ]]; then
                [[ -f "$destination_path" && ! -L "$destination_path" ]] ||
                    fail "File collision with non-file: $destination_path"

                cmp -s "$source_path" "$destination_path" ||
                    fail "Different staged file already exists: $destination_path"
            fi
            continue
        fi

        fail "Unsupported staged object: $source_path"
    done < <(find "$package_stage" -mindepth 1 -print0 | sort -z)
}

verify_package_stage() {
    local package_stage="$1"
    local elf_file
    local object_count=0

    while IFS= read -r -d '' staged_object; do
        ((object_count += 1))
    done < <(find "$package_stage" -mindepth 1 -print0)

    (( object_count > 0 )) ||
        fail "Package installation produced an empty stage: $package_stage"

    while IFS= read -r -d '' elf_file; do
        if readelf -h "$elf_file" >/dev/null 2>&1; then
            if readelf -d "$elf_file" 2>/dev/null |
                grep -Eq '[(](RPATH|RUNPATH)[)]'; then
                readelf -d "$elf_file" |
                    grep -E '[(](RPATH|RUNPATH)[)]' || true
                fail "Unexpected RPATH/RUNPATH in $elf_file"
            fi
        fi
    done < <(find "$package_stage" -type f -print0)

    echo "PACKAGE-STAGE OBJECTS: $object_count"
    echo "RPATH/RUNPATH AUDIT: PASS"
}

merge_package_stage() {
    local package_stage="$1"

    check_package_stage_collisions "$package_stage"

    # --no-clobber preserves all preexisting staged files. The collision scan
    # above has already proved any duplicates are byte-identical.
    cp -a --no-clobber "$package_stage"/. "$XFCE_STAGE"/
}

build_component() {
    local package="$1"
    local version="$2"
    local stamp
    local archive
    local archive_sha256
    local source_dir="$RUN_SOURCES/${package}-${version}"
    local build_dir="$RUN_BUILDS/${package}-${version}"
    local package_stage="$RUN_STAGES/${package}-${version}"
    local configure_help
    local -a configure_args
    local package_manifest

    CURRENT_PACKAGE="$package-$version"
    stamp="$(stamp_path_for "$package" "$version")"

    echo
    echo "============================================================"
    echo " BUILDING $package $version"
    echo "============================================================"

    if valid_stamp "$stamp" "$package" "$version"; then
        echo "STAMP VERIFIED: already complete; skipping."
        return 0
    fi

    download_and_verify "$package" "$version"
    archive="$DOWNLOADED_ARCHIVE"
    archive_sha256="$DOWNLOADED_SHA256"

    mkdir -p "$source_dir" "$build_dir" "$package_stage"

    tar -xjf "$archive" \
        --strip-components=1 \
        -C "$source_dir"

    [[ -x "$source_dir/configure" ]] ||
        fail "Released source is missing an executable configure script: $package"

    refresh_build_environment

    configure_args=(
        "--prefix=/usr"
        "--exec-prefix=/usr"
        "--bindir=/usr/bin"
        "--sbindir=/usr/bin"
        "--libdir=/usr/lib"
        "--libexecdir=/usr/lib"
        "--includedir=/usr/include"
        "--datadir=/usr/share"
        "--sysconfdir=/etc"
        "--localstatedir=/var"
    )

    configure_help="$("$source_dir/configure" --help)"

    if grep -Fq -- '--disable-static' <<< "$configure_help"; then
        configure_args+=("--disable-static")
    fi

    if grep -Fq -- '--enable-shared' <<< "$configure_help"; then
        configure_args+=("--enable-shared")
    fi

    if grep -Fq -- '--disable-gtk-doc' <<< "$configure_help"; then
        configure_args+=("--disable-gtk-doc")
    fi

    # Build the X11 desktop. Disable Wayland wherever that released
    # component exposes a supported switch.
    if grep -Fq -- '--enable-x11' <<< "$configure_help"; then
        configure_args+=("--enable-x11")
    fi

    if grep -Fq -- '--disable-wayland' <<< "$configure_help"; then
        configure_args+=("--disable-wayland")
    fi

    echo "Configure arguments:"
    printf '  %q\n' "${configure_args[@]}"

    (
        cd "$build_dir"
        "$source_dir/configure" "${configure_args[@]}"
        make -j"$JOBS"

        if [[ "$RUN_TESTS" == 1 ]]; then
            if grep -Eq '^check:' Makefile 2>/dev/null; then
                if command -v dbus-run-session >/dev/null 2>&1; then
                    dbus-run-session -- make -j"$JOBS" check
                else
                    make -j"$JOBS" check
                fi
            else
                echo "No check target advertised; skipping tests."
            fi
        fi

        make DESTDIR="$package_stage" install
    )

    verify_package_stage "$package_stage"

    package_manifest="$RUN_ROOT/${package}-${version}.files.sha512"
    (
        cd "$package_stage"
        find . -type f -print0 |
            sort -z |
            xargs -0 -r sha512sum
    ) > "$package_manifest"

    merge_package_stage "$package_stage"

    {
        echo "package=$package"
        echo "version=$version"
        echo "archive=$archive"
        echo "archive_sha256=$archive_sha256"
        echo "package_manifest=$package_manifest"
        echo "completed_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "run_root=$RUN_ROOT"
    } > "$stamp"

    echo "$package $version: PASS"
}


build_upower() {
    local package="upower"
    local version="1.91.3"
    local archive="upower-v${version}.tar.bz2"
    local url="https://gitlab.freedesktop.org/upower/upower/-/archive/v${version}/${archive}"
    local expected_md5="bf09da2eb695224e249e38077460414c"
    local destination="$DOWNLOADS/$archive"
    local partial="$destination.part"
    local stamp
    local source_dir="$RUN_SOURCES/${package}-${version}"
    local build_dir="$RUN_BUILDS/${package}-${version}"
    local package_stage="$RUN_STAGES/${package}-${version}"
    local package_manifest
    local actual_md5
    local detected_version

    CURRENT_PACKAGE="$package-$version"
    stamp="$(stamp_path_for "$package" "$version")"

    echo
    echo "============================================================"
    echo " BUILDING UPOWER $version"
    echo "============================================================"

    if valid_stamp "$stamp" "$package" "$version"; then
        refresh_build_environment
        detected_version="$(pkg-config --modversion upower-glib 2>/dev/null || true)"
        [[ "$detected_version" == "$version" ]] ||
            fail "UPower stamp exists, but staged upower-glib is $detected_version"
        echo "STAMP VERIFIED: UPower $version already complete; skipping."
        return 0
    fi

    [[ ! -e "$partial" ]] ||
        fail "Partial UPower download already exists: $partial"

    if [[ ! -f "$destination" ]]; then
        echo "Downloading: $url"
        curl \
            --fail \
            --location \
            --proto '=https' \
            --tlsv1.2 \
            --retry 3 \
            --retry-all-errors \
            --output "$partial" \
            "$url"

        [[ -s "$partial" ]] ||
            fail "Downloaded UPower archive is empty"

        mv --no-clobber "$partial" "$destination"
    else
        echo "Using existing UPower archive after verification: $destination"
    fi

    actual_md5="$(md5sum "$destination" | awk '{print $1}')"

    echo "Expected MD5: $expected_md5"
    echo "Actual MD5:   $actual_md5"

    [[ "$actual_md5" == "$expected_md5" ]] ||
        fail "UPower archive MD5 mismatch"

    bzip2 -t "$destination" ||
        fail "UPower bzip2 integrity check failed"

    mkdir -p "$source_dir" "$build_dir" "$package_stage"

    tar -xjf "$destination" \
        --strip-components=1 \
        -C "$source_dir"

    [[ -f "$source_dir/meson.build" ]] ||
        fail "UPower source is missing meson.build"

    refresh_build_environment

    meson setup "$build_dir" "$source_dir" \
        --prefix=/usr \
        --libdir=lib \
        --buildtype=release \
        -Dgtk-doc=false \
        -Dman=false \
        -Dsystemdsystemunitdir=/usr/lib/systemd/system \
        -Dudevrulesdir=/usr/lib/udev/rules.d

    meson compile -C "$build_dir" -j "$JOBS"

    if [[ "$RUN_TESTS" == 1 ]]; then
        LC_ALL=C meson test \
            -C "$build_dir" \
            --print-errorlogs
    fi

    DESTDIR="$package_stage" \
        meson install \
            -C "$build_dir" \
            --no-rebuild

    verify_package_stage "$package_stage"

    REQUIRED_UPOWER_OUTPUTS=(
        "$package_stage/usr/bin/upower"
        "$package_stage/usr/lib/upowerd"
        "$package_stage/usr/lib/pkgconfig/upower-glib.pc"
        "$package_stage/usr/lib/systemd/system/upower.service"
        "$package_stage/usr/share/dbus-1/system-services/org.freedesktop.UPower.service"
    )

    for required_output in "${REQUIRED_UPOWER_OUTPUTS[@]}"; do
        [[ -e "$required_output" || -L "$required_output" ]] ||
            fail "Required UPower output is missing: $required_output"
        echo "PASS: $required_output"
    done

    package_manifest="$RUN_ROOT/${package}-${version}.files.sha512"
    (
        cd "$package_stage"
        find . -type f -print0 |
            sort -z |
            xargs -0 -r sha512sum
    ) > "$package_manifest"

    merge_package_stage "$package_stage"
    refresh_build_environment

    detected_version="$(pkg-config --modversion upower-glib)"

    [[ "$detected_version" == "$version" ]] ||
        fail "Staged upower-glib version is $detected_version; expected $version"

    {
        echo "package=$package"
        echo "version=$version"
        echo "archive=$destination"
        echo "archive_md5=$actual_md5"
        echo "package_manifest=$package_manifest"
        echo "completed_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "run_root=$RUN_ROOT"
    } > "$stamp"

    echo "UPOWER $version BUILD: PASS"
}

build_upower

import_existing_libxfce4util

for component in "${COMPONENTS[@]}"; do
    IFS='|' read -r package version <<< "$component"
    build_component "$package" "$version"
done

CURRENT_PACKAGE="final-stage-audit"

echo
echo "============================================================"
echo " FINAL XFCE STAGE AUDIT"
echo "============================================================"

REQUIRED_OUTPUTS=(
    "$XFCE_STAGE/usr/bin/upower"
    "$XFCE_STAGE/usr/lib/upowerd"
    "$XFCE_STAGE/usr/lib/pkgconfig/upower-glib.pc"
    "$XFCE_STAGE/usr/lib/systemd/system/upower.service"
    "$XFCE_STAGE/usr/bin/startxfce4"
    "$XFCE_STAGE/usr/bin/xfce4-session"
    "$XFCE_STAGE/usr/bin/xfwm4"
    "$XFCE_STAGE/usr/bin/xfce4-panel"
    "$XFCE_STAGE/usr/bin/xfdesktop"
    "$XFCE_STAGE/usr/bin/xfsettingsd"
    "$XFCE_STAGE/usr/bin/xfce4-settings-manager"
    "$XFCE_STAGE/usr/bin/xfconf-query"
    "$XFCE_STAGE/usr/bin/thunar"
    "$XFCE_STAGE/usr/bin/xfce4-appfinder"
    "$XFCE_STAGE/usr/bin/xfce4-power-manager"
    "$XFCE_STAGE/usr/share/xsessions/xfce.desktop"
    "$XFCE_STAGE/usr/lib/pkgconfig/libxfce4util-1.0.pc"
    "$XFCE_STAGE/usr/lib/pkgconfig/libxfconf-0.pc"
)

for required_output in "${REQUIRED_OUTPUTS[@]}"; do
    [[ -e "$required_output" || -L "$required_output" ]] ||
        fail "Required final Xfce output is missing: $required_output"
    echo "PASS: $required_output"
done

FINAL_FILE_MANIFEST="$XFCE_BASE/xfce-4.20-current-stage-files.sha512"
FINAL_SYMLINK_MANIFEST="$XFCE_BASE/xfce-4.20-current-stage-symlinks.txt"

(
    cd "$XFCE_STAGE"
    find . -type f -print0 |
        sort -z |
        xargs -0 -r sha512sum
) > "$FINAL_FILE_MANIFEST"

(
    cd "$XFCE_STAGE"
    find . -type l -printf '%p -> %l\n' |
        sort
) > "$FINAL_SYMLINK_MANIFEST"

FINAL_FILE_COUNT="$(find "$XFCE_STAGE" -type f | wc -l)"
FINAL_SYMLINK_COUNT="$(find "$XFCE_STAGE" -type l | wc -l)"

echo
echo "Regular files: $FINAL_FILE_COUNT"
echo "Symlinks:      $FINAL_SYMLINK_COUNT"
echo "File manifest: $FINAL_FILE_MANIFEST"
echo "Link manifest: $FINAL_SYMLINK_MANIFEST"
echo "Master log:    $MASTER_LOG"
echo "Run archive:   $RUN_ROOT"

echo
echo "============================================================"
echo "UPOWER + XFCE 4.20 CURRENT-STABLE CORE BUILD: PASS"
echo "============================================================"
echo
echo "The isolated Xfce stage is ready at:"
echo "  $XFCE_STAGE"
echo
echo "Nothing was merged into hardened-rootfs-verified."
echo "No builder, Limine, initramfs, Plasma, SDDM, or GDM files were changed."
