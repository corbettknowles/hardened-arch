#!/usr/bin/env bash
set -Eeuo pipefail

ISO="${1:-$HOME/hardened-arch-1.10-alpha-x86_64.iso}"
KCONFIG="${2:-}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$HOME/hardened-iso-diagnostics-$STAMP"
WORK="$OUT/work"
REPORT="$OUT/diagnostic-report.txt"
FILES="$OUT/collected-files"

mkdir -p "$WORK" "$FILES"
exec > >(tee -a "$REPORT") 2>&1
trap 'rm -rf "$WORK"' EXIT

section() { printf '\n\n===== %s =====\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

for cmd in xorriso unsquashfs grep awk sed sha256sum readelf; do
    if ! have "$cmd"; then
        echo "ERROR: missing command: $cmd"
        echo "Install the inspection tools with:"
        echo "  sudo pacman -S --needed xorriso squashfs-tools binutils"
        exit 1
    fi
done

[[ -f "$ISO" ]] || { echo "ERROR: ISO not found: $ISO"; exit 1; }

section "INPUT"
echo "ISO:      $ISO"
echo "Size:     $(stat -c '%s bytes' "$ISO")"
echo "Modified: $(stat -c '%y' "$ISO")"
echo "SHA-256:  $(sha256sum "$ISO" | awk '{print $1}')"
[[ -n "$KCONFIG" ]] && echo "Kconfig:  $KCONFIG"

section "ISO / EL TORITO STRUCTURE"
xorriso -indev "$ISO" -report_el_torito plain || true
for d in / /hardened /boot /EFI; do
    echo
    echo "--- $d ---"
    xorriso -indev "$ISO" -ls "$d" || true
done

ROOTFS="$WORK/rootfs.sfs"
section "EXTRACT ROOTFS"
xorriso -osirrox on -indev "$ISO" \
    -extract /hardened/rootfs.sfs "$ROOTFS"
echo "Extracted: $ROOTFS"
echo "Size:      $(stat -c '%s bytes' "$ROOTFS")"
echo "SHA-256:   $(sha256sum "$ROOTFS" | awk '{print $1}')"

SQUASH_LIST="$WORK/rootfs-list.txt"
unsquashfs -lls "$ROOTFS" > "$SQUASH_LIST"

exists_sfs() {
    local p="${1#/}"
    grep -Eq "squashfs-root/${p}([[:space:]]|$| -> )" "$SQUASH_LIST"
}

show_entry() {
    local p="${1#/}"
    grep -E "squashfs-root/${p}([[:space:]]|$| -> )" "$SQUASH_LIST" || echo "MISSING: /$p"
}

cat_sfs() {
    local p="${1#/}"
    if exists_sfs "$p"; then
        unsquashfs -cat "$ROOTFS" "$p" 2>/dev/null
    else
        echo "MISSING: /$p"
    fi
}

save_sfs() {
    local p="${1#/}"
    local dest="$FILES/$p"
    if exists_sfs "$p"; then
        mkdir -p "$(dirname "$dest")"
        unsquashfs -cat "$ROOTFS" "$p" > "$dest" 2>/dev/null || true
    fi
}

section "MERGED-/USR LAYOUT"
for p in bin sbin lib lib64 usr/bin usr/sbin usr/lib usr/lib64; do
    show_entry "$p"
done

echo
for p in bin sbin lib lib64 usr/sbin usr/lib64; do
    printf '%-12s ' "/$p"
    line="$(grep -E "squashfs-root/${p}([[:space:]]|$| -> )" "$SQUASH_LIST" | head -n1 || true)"
    if [[ -z "$line" ]]; then
        echo "MISSING"
    elif [[ "$line" == l* ]]; then
        echo "$line"
    else
        echo "NOT A SYMLINK: $line"
    fi
done

section "SYSTEMD AND DISPLAY MANAGER"
for p in \
    etc/systemd/system/default.target \
    etc/systemd/system/display-manager.service \
    usr/lib/systemd/system/sddm.service \
    usr/lib/systemd/system/graphical.target \
    usr/lib/systemd/system/systemd-logind.service \
    usr/lib/systemd/system/systemd-udevd.service \
    usr/lib/systemd/system/dbus.service \
    usr/lib/systemd/system/polkit.service
 do
    show_entry "$p"
 done

echo
echo "--- sddm.service ---"
cat_sfs usr/lib/systemd/system/sddm.service
save_sfs usr/lib/systemd/system/sddm.service

echo
echo "--- default.target ---"
grep -E 'squashfs-root/etc/systemd/system/default.target' "$SQUASH_LIST" || true

echo
echo "--- display-manager.service ---"
grep -E 'squashfs-root/etc/systemd/system/display-manager.service' "$SQUASH_LIST" || true

section "SDDM CONFIGURATION"
for p in \
    etc/sddm.conf \
    etc/sddm.conf.d/10-wayland.conf \
    etc/sddm.conf.d/10-x11.conf \
    usr/lib/sddm/sddm.conf.d/default.conf \
    usr/share/wayland-sessions/plasma.desktop \
    usr/share/xsessions/plasmax11.desktop
 do
    if exists_sfs "$p"; then
        echo
        echo "--- /$p ---"
        cat_sfs "$p"
        save_sfs "$p"
    else
        echo "MISSING: /$p"
    fi
 done

section "GRAPHICAL BINARIES"
BINS=(
    usr/bin/sddm
    usr/bin/sddm-greeter-qt6
    usr/lib/sddm-helper
    usr/bin/Xorg
    usr/bin/Xwayland
    usr/bin/kwin_wayland
    usr/bin/startplasma-wayland
    usr/bin/startplasma-x11
    usr/bin/dbus-run-session
)
for p in "${BINS[@]}"; do show_entry "$p"; done

section "XORG MODULES AND VIDEO DRIVERS"
grep -E 'squashfs-root/usr/lib/xorg/modules/(drivers|extensions|libglamoregl|modesetting)' "$SQUASH_LIST" | head -n 250 || true
for drv in \
    usr/lib/xorg/modules/drivers/modesetting_drv.so \
    usr/lib/xorg/modules/drivers/vboxvideo_drv.so \
    usr/lib/xorg/modules/drivers/vmware_drv.so \
    usr/lib/xorg/modules/drivers/vesa_drv.so \
    usr/lib/xorg/modules/drivers/fbdev_drv.so
 do
    show_entry "$drv"
 done

section "KERNEL DRM / VIRTUALBOX SUPPORT"
CONFIG_SOURCE=""
if [[ -n "$KCONFIG" && -f "$KCONFIG" ]]; then
    CONFIG_SOURCE="$KCONFIG"
else
    cfg_path="$(awk '{print $NF}' "$SQUASH_LIST" | sed 's#^squashfs-root/##' | grep -E '^boot/config-[^/]+$' | head -n1 || true)"
    if [[ -n "$cfg_path" ]]; then
        CONFIG_SOURCE="$WORK/kernel.config"
        unsquashfs -cat "$ROOTFS" "$cfg_path" > "$CONFIG_SOURCE" 2>/dev/null || true
    fi
fi

if [[ -n "$CONFIG_SOURCE" && -s "$CONFIG_SOURCE" ]]; then
    echo "Config source: $CONFIG_SOURCE"
    grep -E '^(CONFIG_DRM|CONFIG_DRM_KMS_HELPER|CONFIG_DRM_FBDEV_EMULATION|CONFIG_DRM_SIMPLEDRM|CONFIG_DRM_VBOXVIDEO|CONFIG_DRM_VMWGFX|CONFIG_DRM_VIRTIO_GPU|CONFIG_FB|CONFIG_FRAMEBUFFER_CONSOLE|CONFIG_VT|CONFIG_VT_CONSOLE|CONFIG_INPUT_EVDEV|CONFIG_HID_GENERIC)=' "$CONFIG_SOURCE" || true
    cp -f "$CONFIG_SOURCE" "$FILES/kernel.config"
else
    echo "No kernel config was embedded in the ISO."
    echo "Re-run with the build config as the second argument:"
    echo "  $0 \"$ISO\" \"$HOME/linux-7.1.2-build/.config\""
fi

echo
echo "--- DRM modules present in rootfs ---"
grep -E 'squashfs-root/usr/lib/modules/.*/kernel/drivers/gpu/drm/(vboxvideo|vmwgfx|virtio|tiny|drm).*\.ko' "$SQUASH_LIST" | head -n 250 || true

section "USERS, GROUPS, AND PASSWORD STATE"
echo "--- root account ---"
cat_sfs etc/passwd | awk -F: '$1=="root"{print "passwd:",$1,"uid="$3,"gid="$4,"home="$6,"shell="$7}'
cat_sfs etc/shadow | awk -F: '$1=="root"{state="password-set"; if ($2=="" || $2=="!" || $2=="*" || $2 ~ /^!/) state="locked-or-empty"; print "shadow:",$1,state,"hash-length=" length($2)}'

echo
echo "--- sddm account and device groups ---"
cat_sfs etc/passwd | awk -F: '$1=="sddm"{print "passwd:",$1,"uid="$3,"gid="$4,"home="$6,"shell="$7}'
cat_sfs etc/group | awk -F: '$1=="sddm" || $1=="video" || $1=="render" || $1=="input"{print}'

section "PAM FILES"
for p in etc/pam.d/sddm etc/pam.d/sddm-autologin etc/pam.d/login etc/pam.d/system-login; do
    echo
    echo "--- /$p ---"
    cat_sfs "$p"
    save_sfs "$p"
done

section "SELECTED SHARED-LIBRARY CHECKS"
TMPBIN="$WORK/binaries"
mkdir -p "$TMPBIN"

check_needed() {
    local p="${1#/}"
    local out="$TMPBIN/$(basename "$p")"
    echo
    echo "--- /$p ---"
    if ! exists_sfs "$p"; then
        echo "MISSING BINARY"
        return
    fi
    unsquashfs -cat "$ROOTFS" "$p" > "$out" 2>/dev/null || { echo "Could not extract binary"; return; }
    have file && file "$out" || true
    mapfile -t libs < <(readelf -d "$out" 2>/dev/null | sed -n 's/.*Shared library: \[\(.*\)\]/\1/p')
    if ((${#libs[@]} == 0)); then
        echo "No dynamic NEEDED entries found."
        return
    fi
    for lib in "${libs[@]}"; do
        if grep -Eq "squashfs-root/(usr/)?lib(64)?/.*/?${lib}$|squashfs-root/(usr/)?lib(64)?/${lib}$" "$SQUASH_LIST"; then
            echo "OK      $lib"
        else
            echo "MISSING $lib"
        fi
    done
}

for p in usr/bin/sddm usr/bin/sddm-greeter-qt6 usr/lib/sddm-helper usr/bin/Xorg usr/bin/kwin_wayland; do
    check_needed "$p"
done

section "LIKELY BLOCKERS SUMMARY"
problem=0
check_required() {
    local p="$1" label="$2"
    if exists_sfs "$p"; then
        echo "PASS: $label"
    else
        echo "FAIL: $label is missing (/$p)"
        problem=1
    fi
}

check_required usr/bin/sddm "SDDM daemon"
check_required usr/bin/sddm-greeter-qt6 "Qt 6 SDDM greeter"
check_required usr/lib/sddm-helper "SDDM helper"
check_required usr/bin/Xorg "Xorg server"
check_required usr/share/wayland-sessions/plasma.desktop "Plasma Wayland session"
check_required usr/bin/kwin_wayland "KWin Wayland compositor"
check_required usr/bin/startplasma-wayland "Plasma Wayland launcher"
check_required etc/systemd/system/display-manager.service "display-manager service link"
check_required usr/lib/systemd/system/systemd-logind.service "systemd-logind"
check_required usr/lib/systemd/system/systemd-udevd.service "systemd-udevd"
check_required usr/lib/systemd/system/dbus.service "D-Bus service"

if grep -Eq 'squashfs-root/usr/lib/xorg/modules/drivers/(modesetting|vboxvideo|vmware)_drv\.so$' "$SQUASH_LIST"; then
    echo "PASS: at least one useful Xorg video driver is present"
else
    echo "WARN: no modesetting, vboxvideo, or vmware Xorg driver was found"
    problem=1
fi

if grep -Eq 'squashfs-root/etc/systemd/system/display-manager\.service .* -> .*sddm\.service' "$SQUASH_LIST"; then
    echo "PASS: display-manager.service points to SDDM"
else
    echo "WARN: display-manager.service does not visibly point to SDDM"
    problem=1
fi

if [[ -n "$CONFIG_SOURCE" && -s "$CONFIG_SOURCE" ]]; then
    if grep -Eq '^CONFIG_DRM_(VBOXVIDEO|VMWGFX|VIRTIO_GPU)=(y|m)$' "$CONFIG_SOURCE"; then
        echo "PASS: kernel has at least one VM DRM driver enabled"
    else
        echo "WARN: kernel config shows no VBox/VMware/VirtIO DRM driver enabled"
        problem=1
    fi
fi

if (( problem )); then
    echo
    echo "One or more structural problems or warnings were found above."
else
    echo
    echo "Offline filesystem checks passed. Remaining likely causes are runtime-only:"
    echo "seat0 creation, /dev/dri availability, device permissions, or the VirtualBox graphics controller."
fi

section "OUTPUT"
echo "Report directory: $OUT"
echo "Report file:      $REPORT"
ARCHIVE="${OUT}.tar.gz"
tar -C "$(dirname "$OUT")" -czf "$ARCHIVE" "$(basename "$OUT")"
echo "Upload archive:   $ARCHIVE"
echo "Done."
