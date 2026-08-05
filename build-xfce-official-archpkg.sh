#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

JOBS="${JOBS:-2}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_ROOT="$HOME/xfce-archpkg-build/runs/$STAMP"
REPO_ROOT="$RUN_ROOT/repos"
PACKAGE_ROOT="$RUN_ROOT/packages"
DEBUG_PACKAGE_ROOT="$RUN_ROOT/debug-packages"
PACKAGE_STAGE_ROOT="$RUN_ROOT/package-stages"
FINAL_STAGE="$RUN_ROOT/xfce-upower-stage/rootfs"
LOG="$RUN_ROOT/build.log"
CURRENT_COMPONENT="preflight"

mkdir -p \
    "$REPO_ROOT" \
    "$PACKAGE_ROOT" \
    "$DEBUG_PACKAGE_ROOT" \
    "$PACKAGE_STAGE_ROOT" \
    "$FINAL_STAGE"

exec > >(tee -a "$LOG") 2>&1

fail() {
    echo
    echo "============================================================"
    echo "XFCE/UPower CLEAN-CHROOT BUILD FAILED"
    echo "Component: $CURRENT_COMPONENT"
    echo "Run root:  $RUN_ROOT"
    echo "Log:       $LOG"
    echo "No existing Xfce stage, verified rootfs, builder, Limine,"
    echo "or initramfs files were changed."
    echo "============================================================"
    exit 1
}

trap 'status=$?; echo "ERROR: line $LINENO exited with status $status"; fail' ERR

for command_name in pkgctl git bsdtar sha512sum readelf cmp sudo zstd systemd-nspawn; do
    command -v "$command_name" >/dev/null 2>&1 || {
        echo "Missing required command: $command_name"
        echo "Install the host prerequisites with:"
        echo "  sudo pacman -S --needed devtools git libarchive binutils zstd"
        exit 1
    }
done

sudo -v

COMPONENTS=(
    xfce4-dev-tools
    upower
    libxfce4util
    xfconf
    libxfce4windowing
    libxfce4ui
    garcon
    exo
    thunar
    tumbler
    xfce4-power-manager
    xfce4-panel
    xfce4-settings
    xfce4-session
    xfdesktop
    xfwm4
    xfce4-appfinder
    thunar-volman
)

printf '%s\n' "===== BUILD PLAN ====="
printf '  %s\n' "${COMPONENTS[@]}"
echo "Parallel jobs: $JOBS"
echo "Run root:      $RUN_ROOT"
echo

CURRENT_COMPONENT="repository-clone"
cd "$REPO_ROOT"
pkgctl repo clone \
    --protocol=https \
    --jobs "$JOBS" \
    "${COMPONENTS[@]}"

BUILT_PACKAGES=()

for component in "${COMPONENTS[@]}"; do
    CURRENT_COMPONENT="$component"
    component_root="$REPO_ROOT/$component"

    [[ -d "$component_root" ]] || {
        echo "Missing cloned packaging repository: $component_root"
        fail
    }

    echo
    echo "============================================================"
    echo "BUILDING: $component"
    echo "============================================================"

    install_args=()
    for local_package in "${BUILT_PACKAGES[@]}"; do
        install_args+=(--install-to-chroot "$local_package")
    done

    cd "$component_root"
    pkgctl build \
        --nocheck \
        "${install_args[@]}"

    mapfile -d '' component_artifacts < <(
        find "$component_root" \
            -maxdepth 2 \
            -type f \
            -name '*.pkg.tar.zst' \
            -print0 |
            sort -z
    )

    (( ${#component_artifacts[@]} > 0 )) || {
        echo "No package artifacts were produced for $component"
        fail
    }

    for artifact in "${component_artifacts[@]}"; do
        artifact_name="$(basename "$artifact")"

        if [[ "$artifact_name" == *-debug-* ]]; then
            destination="$DEBUG_PACKAGE_ROOT/$artifact_name"
            cp -a --no-clobber "$artifact" "$destination"
            echo "DEBUG PACKAGE: $destination"
            continue
        fi

        destination="$PACKAGE_ROOT/$artifact_name"
        cp -a --no-clobber "$artifact" "$destination"
        cmp -s "$artifact" "$destination" || {
            echo "Artifact copy verification failed: $artifact_name"
            fail
        }

        BUILT_PACKAGES+=("$destination")
        sha512sum "$destination" >> "$RUN_ROOT/packages.sha512"
        echo "PACKAGE: $destination"
    done

done

merge_tree() {
    local source_root="$1"
    local destination_root="$2"
    local source_path
    local relative_path
    local destination_path

    while IFS= read -r -d '' source_path; do
        relative_path="${source_path#"$source_root"/}"
        destination_path="$destination_root/$relative_path"

        if [[ -d "$source_path" && ! -L "$source_path" ]]; then
            if [[ -e "$destination_path" || -L "$destination_path" ]]; then
                [[ -d "$destination_path" && ! -L "$destination_path" ]] || {
                    echo "Directory collision: $destination_path"
                    fail
                }
            fi
            continue
        fi

        if [[ -L "$source_path" ]]; then
            if [[ -e "$destination_path" || -L "$destination_path" ]]; then
                [[ -L "$destination_path" ]] || {
                    echo "Symlink collision with non-symlink: $destination_path"
                    fail
                }
                [[ "$(readlink "$source_path")" == "$(readlink "$destination_path")" ]] || {
                    echo "Different symlink already exists: $destination_path"
                    fail
                }
            fi
            continue
        fi

        if [[ -f "$source_path" ]]; then
            if [[ -e "$destination_path" || -L "$destination_path" ]]; then
                [[ -f "$destination_path" && ! -L "$destination_path" ]] || {
                    echo "File collision with non-file: $destination_path"
                    fail
                }
                cmp -s "$source_path" "$destination_path" || {
                    echo "Different file already exists: $destination_path"
                    fail
                }
            fi
            continue
        fi

        echo "Unsupported staged object: $source_path"
        fail
    done < <(find "$source_root" -mindepth 1 -print0 | sort -z)

    cp -a --no-clobber "$source_root"/. "$destination_root"/
}

CURRENT_COMPONENT="package-extraction"

while IFS= read -r -d '' package_file; do
    package_name="$(
        bsdtar -xOf "$package_file" .PKGINFO |
            awk -F' = ' '$1 == "pkgname" {print $2; exit}'
    )"

    [[ -n "$package_name" ]] || {
        echo "Could not read pkgname from: $package_file"
        fail
    }

    package_stage="$PACKAGE_STAGE_ROOT/$package_name"
    mkdir -p "$package_stage"

    bsdtar -xpf "$package_file" \
        -C "$package_stage" \
        --exclude .BUILDINFO \
        --exclude .MTREE \
        --exclude .PKGINFO

    if find "$package_stage" -type f -name '*.la' -print -quit | grep -q .; then
        echo "Unexpected libtool archive in packaged payload:"
        find "$package_stage" -type f -name '*.la' -print
        fail
    fi

    merge_tree "$package_stage" "$FINAL_STAGE"
    echo "STAGED PACKAGE: $package_name"
done < <(
    find "$PACKAGE_ROOT" -maxdepth 1 -type f -name '*.pkg.tar.zst' -print0 |
        sort -z
)

CURRENT_COMPONENT="final-audit"

if find "$FINAL_STAGE" -type f -name '*.la' -print -quit | grep -q .; then
    echo "FINAL AUDIT FAIL: libtool archives remain"
    find "$FINAL_STAGE" -type f -name '*.la' -print
    fail
fi

if grep -RIlF \
    --binary-files=without-match \
    "$RUN_ROOT" \
    "$FINAL_STAGE" |
    grep -q .; then
    echo "FINAL AUDIT FAIL: build path leaked into staged text files"
    grep -RIlF \
        --binary-files=without-match \
        "$RUN_ROOT" \
        "$FINAL_STAGE"
    fail
fi

rpath_failure=0
while IFS= read -r -d '' candidate; do
    readelf -h "$candidate" >/dev/null 2>&1 || continue
    if readelf -d "$candidate" 2>/dev/null |
        grep -Eq '[(](RPATH|RUNPATH)[)]'; then
        echo "RPATH/RUNPATH: $candidate"
        readelf -d "$candidate" |
            grep -E '[(](RPATH|RUNPATH)[)]' || true
        rpath_failure=1
    fi
done < <(find "$FINAL_STAGE" -type f -print0)

(( rpath_failure == 0 )) || fail

(
    cd "$FINAL_STAGE"
    find . -type f -print0 |
        sort -z |
        xargs -0 -r sha512sum
) > "$RUN_ROOT/xfce-upower-stage.files.sha512"

tar --zstd -cpf \
    "$RUN_ROOT/xfce-upower-stage.tar.zst" \
    -C "$FINAL_STAGE" \
    .

sha512sum "$RUN_ROOT/xfce-upower-stage.tar.zst" \
    > "$RUN_ROOT/xfce-upower-stage.tar.zst.sha512"

trap - ERR

echo
echo "============================================================"
echo "XFCE/UPower CLEAN-CHROOT BUILD COMPLETE"
echo "Packages:  $PACKAGE_ROOT"
echo "Stage:     $FINAL_STAGE"
echo "Archive:   $RUN_ROOT/xfce-upower-stage.tar.zst"
echo "Manifest:  $RUN_ROOT/xfce-upower-stage.files.sha512"
echo "Log:       $LOG"
echo "Nothing was merged into the existing Xfce stage or rootfs."
echo "============================================================"
