#!/usr/bin/env python3
"""Build a UEFI-only Hardened Arch live/install/update ISO.

This script is tailored to Corbett's existing tree layout but every important
path can be overridden on the command line.

Run as root, for example:

  sudo python3 build_hardened_arch_iso.py \
      --version 1.10-alpha \
      --repo-url 'https://sourceforge.net/projects/PROJECT/files/updates/manifest.json/download' \
      --install-tools

The generated ISO is intended for UEFI systems. Secure Boot must be disabled
unless the Limine binary and kernel are signed with a key trusted by firmware.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import getpass
import hashlib
import json
import os
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Iterable, Sequence


HOST_PACKAGES = [
    "squashfs-tools",
    "libisoburn",
    "dosfstools",
    "mtools",
    "rsync",
    "zstd",
    "cpio",
    "btrfs-progs",
    "gptfdisk",
    "parted",
    "curl",
    "jq",
]

HOST_COMMANDS = [
    "rsync",
    "mksquashfs",
    "xorriso",
    "mkfs.fat",
    "mcopy",
    "zstd",
    "cpio",
    "find",
]

LIVE_TOOLS = [
    "sgdisk",
    "partprobe",
    "mkfs.fat",
    "mkfs.btrfs",
    "btrfs",
    "rsync",
    "lsblk",
    "blkid",
    "findmnt",
    "mount",
    "umount",
    "chroot",
    "udevadm",
    "systemctl",
    "useradd",
    "chpasswd",
    "curl",
    "jq",
    "sha512sum",
    "sort",
    "sed",
    "awk",
    "grep",
    "mountpoint",
    "readlink",
    "head",
    "tail",
    "tr",
    "mktemp",
    "mv",
    "sync",
    "reboot",
    "chmod",
    "rm",
    "mkdir",
    "cp",
]

REQUIRED_KERNEL_CONFIG = {
    "CONFIG_EFI_STUB": "y",
    "CONFIG_BLK_DEV_INITRD": "y",
    "CONFIG_DEVTMPFS": "y",
    "CONFIG_DEVTMPFS_MOUNT": "y",
    "CONFIG_BLK_DEV_LOOP": "y",
    "CONFIG_ISO9660_FS": "y",
    "CONFIG_SQUASHFS": "y",
    "CONFIG_SQUASHFS_ZSTD": "y",
    "CONFIG_OVERLAY_FS": "y",
    "CONFIG_TMPFS": "y",
    "CONFIG_RD_ZSTD": "y",
    "CONFIG_EFI_PARTITION": "y",
    "CONFIG_SQUASHFS_XATTR": "y",
    "CONFIG_SCSI": "y",
    "CONFIG_BLK_DEV_SD": "y",
    "CONFIG_USB_SUPPORT": "y",
    "CONFIG_USB": "y",
    "CONFIG_USB_XHCI_HCD": "y",
    "CONFIG_USB_XHCI_PCI": "y",
    "CONFIG_USB_STORAGE": "y",
}


class BuildError(RuntimeError):
    pass


def original_user_home() -> Path:
    user = os.environ.get("SUDO_USER")
    if user:
        return Path(pwd.getpwnam(user).pw_dir)
    return Path.home()


def run(
    cmd: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    argv = [os.fspath(x) for x in cmd]
    print("+", shlex.join(argv), flush=True)
    return subprocess.run(
        argv,
        cwd=os.fspath(cwd) if cwd else None,
        env=env,
        text=True,
        capture_output=capture,
        check=check,
    )


def require_root() -> None:
    if os.geteuid() != 0:
        raise BuildError(
            "Run this builder as root, for example: sudo python3 build_hardened_arch_iso.py ..."
        )


def safe_remove_tree(path: Path, home: Path) -> None:
    resolved = path.resolve()
    forbidden = {Path("/"), home.resolve(), Path("/home"), Path("/root"), Path("/mnt")}
    if resolved in forbidden or len(resolved.parts) < 4:
        raise BuildError(f"Refusing to remove unsafe path: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


def write_text(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def copy_file(src: Path, dst: Path, mode: int | None = None) -> None:
    if not src.exists():
        raise BuildError(f"Required file not found: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst, follow_symlinks=False)
    if mode is not None:
        dst.chmod(mode)


def sha512_file(path: Path) -> str:
    h = hashlib.sha512()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def command_in_root(root: Path, name: str) -> Path | None:
    for rel in (
        f"usr/local/sbin/{name}",
        f"usr/local/bin/{name}",
        f"usr/sbin/{name}",
        f"usr/bin/{name}",
        f"sbin/{name}",
        f"bin/{name}",
    ):
        p = root / rel
        # Absolute symlinks are valid inside the future initramfs even
        # though they can appear broken when followed from the host.
        if p.exists() or p.is_symlink():
            return p
    return None


def copy_path_preserving_links(src: Path, root: Path) -> None:
    """Copy src and its symlink target chain to the same absolute paths below root."""
    src = Path(src)
    seen: set[Path] = set()
    current = src
    while True:
        if current in seen:
            break
        seen.add(current)
        dst = root / current.relative_to("/")
        dst.parent.mkdir(parents=True, exist_ok=True)
        if current.is_symlink():
            target = os.readlink(current)
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            os.symlink(target, dst)
            next_path = (current.parent / target).resolve() if not target.startswith("/") else Path(target)
            current = next_path
            continue
        shutil.copy2(current, dst)
        break


def ldd_dependencies(binary: Path) -> list[Path]:
    cp = run(["ldd", binary], capture=True, check=False)
    if cp.returncode != 0:
        # Static binaries commonly produce a non-zero exit code.
        return []
    deps: list[Path] = []
    for line in cp.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        match = re.search(r"=>\s+(/[^\s]+)", line)
        if match:
            deps.append(Path(match.group(1)))
            continue
        match = re.match(r"(/[^\s]+)\s+\(", line)
        if match:
            deps.append(Path(match.group(1)))
    return deps


def stage_host_tool(root: Path, name: str) -> None:
    if command_in_root(root, name):
        return
    found = shutil.which(name)
    if not found:
        raise BuildError(f"Host command is missing and cannot be staged into the live root: {name}")
    binary = Path(found).resolve()
    print(f"Staging missing live tool: {name} ({binary})")
    copy_path_preserving_links(Path(found), root)
    if Path(found).is_symlink():
        copy_path_preserving_links(binary, root)
    for dep in ldd_dependencies(binary):
        copy_path_preserving_links(dep, root)


def install_host_tools() -> None:
    if not shutil.which("pacman"):
        raise BuildError("--install-tools currently supports an Arch host with pacman.")
    run(["pacman", "-S", "--needed", "--noconfirm", *HOST_PACKAGES])


def check_host_tools() -> None:
    missing = [name for name in HOST_COMMANDS if not shutil.which(name)]
    if missing:
        packages = " ".join(HOST_PACKAGES)
        raise BuildError(
            "Missing host commands: "
            + ", ".join(missing)
            + f"\nInstall them with:\n  sudo pacman -S --needed {packages}\n"
            + "or rerun this builder with --install-tools."
        )


def kernel_release(kernel_src: Path, kernel_build: Path) -> str:
    cp = run(
        ["make", "-s", "-C", kernel_src, f"O={kernel_build}", "kernelrelease"],
        capture=True,
    )
    value = cp.stdout.strip()
    if not value:
        raise BuildError("Could not determine the kernel release.")
    return value


def parse_kernel_config(config_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in config_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def check_kernel_config(config_path: Path) -> None:
    cfg = parse_kernel_config(config_path)
    bad: list[str] = []
    for key, wanted in REQUIRED_KERNEL_CONFIG.items():
        actual = cfg.get(key, "not set")
        if actual != wanted:
            bad.append(f"{key}={actual} (need {wanted})")
    if bad:
        raise BuildError(
            "The live ISO initramfs requires these features built into the kernel:\n  "
            + "\n  ".join(bad)
            + "\nRebuild the kernel with them set to =y, then rerun the builder."
        )


def ensure_toybox_applets(initramfs_root: Path) -> None:
    toybox_candidates = [
        initramfs_root / "bin/toybox",
        initramfs_root / "usr/bin/toybox",
    ]
    toybox = next((p for p in toybox_candidates if p.exists()), None)
    required = [
        "sh",
        "mount",
        "umount",
        "mkdir",
        "cat",
        "sleep",
        "switch_root",
        "losetup",
        "blkid",
        "head",
        "grep",
    ]
    if toybox is None:
        missing = [x for x in required if command_in_root(initramfs_root, x) is None]
        if missing:
            raise BuildError(
                "The base initramfs has no Toybox binary and is missing: " + ", ".join(missing)
            )
        return

    cp = run([toybox], capture=True, check=False)
    # Toybox versions may print their applet list to stdout or stderr and
    # may return nonzero when invoked without an applet.
    listing = (cp.stdout or "") + "\n" + (cp.stderr or "")
    supported = set(re.findall(r"[A-Za-z0-9_.+-]+", listing))
    for name in required:
        if command_in_root(initramfs_root, name):
            continue
        if name not in supported:
            raise BuildError(f"Toybox in the initramfs does not provide required applet: {name}")
        link = initramfs_root / "bin" / name
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink("toybox", link)


@dataclasses.dataclass
class BuildPaths:
    home: Path
    runtime_root: Path
    kernel_src: Path
    kernel_build: Path
    kernel_artifacts: Path
    initramfs_stage: Path
    theme_dir: Path
    limine_dir: Path
    work: Path
    output: Path

    @property
    def live_root(self) -> Path:
        return self.work / "live-root"

    @property
    def iso_root(self) -> Path:
        return self.work / "iso-root"

    @property
    def initramfs_root(self) -> Path:
        return self.work / "live-initramfs"

    @property
    def esp_root(self) -> Path:
        return self.work / "esp-root"

    @property
    def esp_image(self) -> Path:
        return self.work / "hardened-arch-esp.img"

    @property
    def live_initrd(self) -> Path:
        return self.work / "initramfs-live.img.zst"


@dataclasses.dataclass
class BuildConfig:
    version: str
    release_channel: str
    repo_url: str
    volume_label: str
    jobs: int
    force: bool
    keep_work: bool
    public_build: bool


INSTALLER_SCRIPT = r'''#!/usr/bin/env bash
set -Eeuo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
KVER=__KVER__
SOURCE_ROOT=/run/hardened-live/lower
SOURCE_DEVICE_FILE=/run/hardened-live/source-device
TARGET_MNT=/mnt/hardened-target
TARGET_TOP=/mnt/hardened-target-top

red=$'\033[1;31m'
green=$'\033[1;32m'
yellow=$'\033[1;33m'
reset=$'\033[0m'

cleanup() {
    set +e
    for p in "$TARGET_MNT/run" "$TARGET_MNT/sys" "$TARGET_MNT/proc" "$TARGET_MNT/dev"; do
        mountpoint -q "$p" && umount -R "$p"
    done
    mountpoint -q "$TARGET_MNT/boot" && umount "$TARGET_MNT/boot"
    mountpoint -q "$TARGET_MNT/var/log" && umount "$TARGET_MNT/var/log"
    mountpoint -q "$TARGET_MNT/home" && umount "$TARGET_MNT/home"
    mountpoint -q "$TARGET_MNT" && umount "$TARGET_MNT"
    mountpoint -q "$TARGET_TOP" && umount "$TARGET_TOP"
}
trap cleanup EXIT

partition_path() {
    local disk=$1 number=$2
    case "$disk" in
        *[0-9]) printf '%sp%s\n' "$disk" "$number" ;;
        *)     printf '%s%s\n' "$disk" "$number" ;;
    esac
}

live_parent_disk() {
    local dev pk
    [[ -r "$SOURCE_DEVICE_FILE" ]] || return 0
    dev=$(cat "$SOURCE_DEVICE_FILE")
    pk=$(lsblk -no PKNAME "$dev" 2>/dev/null | head -n1 || true)
    [[ -n "$pk" ]] && printf '/dev/%s\n' "$pk"
}

clear
printf '%s\n' "${green}Hardened Arch Linux Disk Installer${reset}"
printf '%s\n\n' "This installs a UEFI Limine system with a Btrfs root and @ subvolume."

if [[ ! -d "$SOURCE_ROOT/usr" ]]; then
    printf '%s\n' "${red}Live source root is unavailable at $SOURCE_ROOT.${reset}" >&2
    exit 1
fi

LIVE_DISK=$(live_parent_disk || true)

# Found via live testing on 2026-08-03: the previous version filtered with
# awk '$5 == "disk"', assuming lsblk always emits 5 fields. VirtualBox (and
# other virtual/NVMe disks) report empty MODEL and TRAN values, and lsblk
# omits empty columns rather than padding them -- so a VBox disk comes back
# as only "NAME SIZE TYPE" and the $5 test never matched, leaving the disk
# list completely empty and making installation impossible in a VM.
# Filtering on TYPE directly via -I/-n avoids depending on field position.
mapfile -t DISK_LIST < <(
    lsblk -dpno NAME,TYPE \
    | awk '$NF == "disk" { print $1 }' \
    | while read -r dev; do
        size=$(lsblk -dno SIZE "$dev" 2>/dev/null | head -n1)
        model=$(lsblk -dno MODEL "$dev" 2>/dev/null | head -n1)
        tran=$(lsblk -dno TRAN "$dev" 2>/dev/null | head -n1)
        [[ -z "$model" ]] && model="(no model)"
        [[ -z "$tran" ]] && tran="virtual"
        printf '%s  %s  %s  %s\n' "$dev" "$size" "$model" "$tran"
    done
)

if [[ ${#DISK_LIST[@]} -eq 0 ]]; then
    printf '%s\n' "${red}No disks were found.${reset}" >&2
    exit 1
fi

printf '%s\n\n' "Available whole disks:"
for i in "${!DISK_LIST[@]}"; do
    entry="${DISK_LIST[$i]}"
    entry_disk=$(awk '{print $1}' <<< "$entry")
    marker=""
    if [[ -n "$LIVE_DISK" && "$entry_disk" == "$LIVE_DISK" ]]; then
        marker="  ${yellow}(this is the USB/live boot disk -- cannot be selected)${reset}"
    fi
    printf '  [%d] %s%s\n' "$((i + 1))" "$entry" "$marker"
done

printf '\nSelect a disk by number: '
read -r SELECTION

if [[ ! "$SELECTION" =~ ^[0-9]+$ ]] || (( SELECTION < 1 || SELECTION > ${#DISK_LIST[@]} )); then
    printf '%s\n' "${red}Invalid selection: $SELECTION${reset}" >&2
    exit 1
fi

TARGET_DISK=$(awk '{print $1}' <<< "${DISK_LIST[$((SELECTION - 1))]}")
TARGET_DISK=$(readlink -f "$TARGET_DISK")

printf '\nSelected: %s\n' "$TARGET_DISK"

if [[ ! -b "$TARGET_DISK" ]]; then
    printf '%s\n' "${red}Not a block device: $TARGET_DISK${reset}" >&2
    exit 1
fi
if [[ $(lsblk -dno TYPE "$TARGET_DISK") != disk ]]; then
    printf '%s\n' "${red}Choose the whole disk, not a partition.${reset}" >&2
    exit 1
fi

if [[ -n "$LIVE_DISK" && "$TARGET_DISK" == "$LIVE_DISK" ]]; then
    printf '%s\n' "${red}Refusing to erase the USB/live boot disk: $TARGET_DISK${reset}" >&2
    exit 1
fi

if findmnt -rn -S "$TARGET_DISK" >/dev/null 2>&1 || lsblk -nrpo MOUNTPOINTS "$TARGET_DISK" | grep -q '[^[:space:]]'; then
    printf '%s\n' "${red}The target disk or one of its partitions is mounted. Unmount it first.${reset}" >&2
    exit 1
fi

printf '\n%s\n' "${red}EVERYTHING on $TARGET_DISK will be destroyed.${reset}"
printf 'Type exactly: ERASE %s\n> ' "$TARGET_DISK"
read -r CONFIRM
[[ "$CONFIRM" == "ERASE $TARGET_DISK" ]] || { echo "Cancelled."; exit 1; }

ESP=$(partition_path "$TARGET_DISK" 1)
ROOT_PART=$(partition_path "$TARGET_DISK" 2)

printf '%s\n' "${yellow}Partitioning $TARGET_DISK...${reset}"
sgdisk --zap-all "$TARGET_DISK"
sgdisk --clear \
    --new=1:1MiB:+1GiB --typecode=1:EF00 --change-name=1:HARDENEFI \
    --new=2:0:0       --typecode=2:8300 --change-name=2:HARDENED_ROOT \
    "$TARGET_DISK"
partprobe "$TARGET_DISK"
udevadm settle

printf '%s\n' "${yellow}Formatting filesystems...${reset}"
mkfs.fat -F 32 -n HARDENEFI "$ESP"
mkfs.btrfs -f -L HARDENED_ROOT "$ROOT_PART"

mkdir -p "$TARGET_TOP" "$TARGET_MNT"
mount "$ROOT_PART" "$TARGET_TOP"
btrfs subvolume create "$TARGET_TOP/@"
btrfs subvolume create "$TARGET_TOP/@home"
btrfs subvolume create "$TARGET_TOP/@var_log"
btrfs subvolume create "$TARGET_TOP/@snapshots"
btrfs subvolume set-default "$TARGET_TOP/@"
umount "$TARGET_TOP"

mount -o subvol=@,compress=zstd:3,noatime "$ROOT_PART" "$TARGET_MNT"
mkdir -p "$TARGET_MNT/home" "$TARGET_MNT/var/log" "$TARGET_MNT/.snapshots" "$TARGET_MNT/boot"
mount -o subvol=@home,compress=zstd:3,noatime "$ROOT_PART" "$TARGET_MNT/home"
mount -o subvol=@var_log,compress=zstd:3,noatime "$ROOT_PART" "$TARGET_MNT/var/log"
mount -o subvol=@snapshots,compress=zstd:3,noatime "$ROOT_PART" "$TARGET_MNT/.snapshots"
mount "$ESP" "$TARGET_MNT/boot"

printf '%s\n' "${yellow}Copying the operating system...${reset}"
rsync -aHAX --numeric-ids \
    --exclude='/boot/EFI/***' \
    --exclude='/dev/***' --exclude='/proc/***' --exclude='/sys/***' \
    --exclude='/run/***' --exclude='/tmp/***' --exclude='/mnt/***' \
    "$SOURCE_ROOT/" "$TARGET_MNT/"
mkdir -p "$TARGET_MNT/boot/EFI"
rsync -aHAX --numeric-ids "$SOURCE_ROOT/boot/EFI/" "$TARGET_MNT/boot/EFI/"
# The disk installer is a live-media feature; keep the updater, remove the destructive installer from the installed root.
rm -f "$TARGET_MNT/usr/local/sbin/hardened-install" \
      "$TARGET_MNT/usr/lib/systemd/system/hardened-installer.service" \
      "$TARGET_MNT/usr/lib/systemd/system/hardened-installer.target"

ROOT_UUID=$(blkid -s UUID -o value "$ROOT_PART")
ESP_UUID=$(blkid -s UUID -o value "$ESP")

cat > "$TARGET_MNT/etc/fstab" <<EOF
UUID=$ROOT_UUID  /           btrfs  rw,noatime,compress=zstd:3,subvol=@          0 0
UUID=$ROOT_UUID  /home       btrfs  rw,noatime,compress=zstd:3,subvol=@home      0 0
UUID=$ROOT_UUID  /var/log    btrfs  rw,noatime,compress=zstd:3,subvol=@var_log   0 0
UUID=$ROOT_UUID  /.snapshots btrfs  rw,noatime,compress=zstd:3,subvol=@snapshots 0 0
UUID=$ESP_UUID   /boot       vfat   rw,umask=0077                              0 2
EOF
mkdir -p "$TARGET_MNT/etc/kernel"
printf '%s\n' "root=UUID=$ROOT_UUID rootfstype=btrfs rootflags=subvol=@ rw quiet splash" > "$TARGET_MNT/etc/kernel/cmdline"

cat > "$TARGET_MNT/boot/EFI/BOOT/limine.conf" <<EOF
# Hardened Arch installed-system Limine configuration
timeout: 8
default_entry: 1
graphics: yes
interface_resolution: 1920x1080
interface_branding: HARDENED ARCH LINUX
interface_branding_colour: ff3038
interface_help_hidden: no
interface_help_colour: d8dde8
interface_help_colour_bright: ff3a40
wallpaper: boot():/EFI/BOOT/limine-bg.png
wallpaper_style: stretched
term_font_scale: 2x2
term_font_spacing: 1
term_margin: 250
term_margin_gradient: 28
term_background: 980b1321
term_foreground: f4f6fb
term_background_bright: 301018
term_foreground_bright: ffffff
term_palette: 10131a;d92b38;3da85b;d6a83d;4d78d8;a858c7;45a8b8;d8dde8
term_palette_bright: 4b5262;ff3646;56d878;ffd35a;6e9cff;dc79f2;67d7e8;ffffff
editor_enabled: no

/Hardened Arch Linux (default)
    comment: Start the installed graphical system
    protocol: linux
    path: boot():/EFI/Linux/vmlinuz-$KVER.efi
    module_path: boot():/EFI/Linux/initramfs-$KVER.img.zst
    cmdline: root=UUID=$ROOT_UUID rootfstype=btrfs rootflags=subvol=@ rw quiet splash

/+Advanced options for Hardened Arch Linux
    comment: Recovery and diagnostic modes

    //Check for Software Updates / Recovery
        protocol: linux
        path: boot():/EFI/Linux/vmlinuz-$KVER.efi
        module_path: boot():/EFI/Linux/initramfs-$KVER.img.zst
        cmdline: root=UUID=$ROOT_UUID rootfstype=btrfs rootflags=subvol=@ rw systemd.unit=hardened-update.target

    //Verbose diagnostic boot
        protocol: linux
        path: boot():/EFI/Linux/vmlinuz-$KVER.efi
        module_path: boot():/EFI/Linux/initramfs-$KVER.img.zst
        cmdline: root=UUID=$ROOT_UUID rootfstype=btrfs rootflags=subvol=@ rw loglevel=7 systemd.log_level=debug systemd.show_status=yes
EOF

printf '\nHostname [hardened-arch]: '
read -r HOSTNAME_VALUE
HOSTNAME_VALUE=${HOSTNAME_VALUE:-hardened-arch}
printf '%s\n' "$HOSTNAME_VALUE" > "$TARGET_MNT/etc/hostname"

for tree in dev proc sys run; do
    mkdir -p "$TARGET_MNT/$tree"
    mount --rbind "/$tree" "$TARGET_MNT/$tree"
    mount --make-rslave "$TARGET_MNT/$tree"
done

if [[ -x "$TARGET_MNT/usr/bin/systemd-machine-id-setup" ]]; then
    rm -f "$TARGET_MNT/etc/machine-id"
    chroot "$TARGET_MNT" systemd-machine-id-setup
fi

# Strip the live-media autologin config so the installed system always
# requires real authentication. Autologin is correct for the live/demo
# boot session (physical possession of the media already grants full
# access there) but must never carry over onto a persistent installed
# system.
rm -f "$TARGET_MNT/etc/sddm.conf.d/30-hardened-xfce-session.conf"
rm -f "$TARGET_MNT/etc/sddm.conf.d/10-hardened-live.conf"

# Strip live-media TTY autologin (added 2026-08-03) for the same reason as
# the SDDM autologin above: correct on live/demo boot media, must never
# carry over onto a persistent installed system.
for tty in tty1 tty2 tty3 tty4 tty5 tty6; do
    rm -f "$TARGET_MNT/etc/systemd/system/getty@${tty}.service.d/zz-hardened-live-autologin.conf"
done

if [[ -x "$TARGET_MNT/usr/sbin/useradd" && -x "$TARGET_MNT/usr/sbin/chpasswd" ]]; then
    printf '\nAdministrator username [corbett]: '
    read -r ADMIN_USER
    ADMIN_USER=${ADMIN_USER:-corbett}
    if [[ ! "$ADMIN_USER" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
        printf '%s\n' "${red}Invalid username.${reset}" >&2
        exit 1
    fi
    chroot "$TARGET_MNT" useradd -m -G wheel -s /usr/bin/zsh "$ADMIN_USER" 2>/dev/null || true
    chroot "$TARGET_MNT" usermod -s /usr/bin/zsh "$ADMIN_USER" 2>/dev/null || true
    for shell in /usr/bin/zsh /usr/bin/bash /bin/bash; do
        if [[ -x "$TARGET_MNT$shell" ]] && ! grep -qxF "$shell" "$TARGET_MNT/etc/shells" 2>/dev/null; then
            echo "$shell" >> "$TARGET_MNT/etc/shells"
        fi
    done
    while true; do
        read -rsp "Password for $ADMIN_USER: " P1; echo
        read -rsp "Repeat password: " P2; echo
        [[ -n "$P1" && "$P1" == "$P2" ]] && break
        echo "Passwords did not match."
    done
    printf '%s:%s\n' "$ADMIN_USER" "$P1" | chroot "$TARGET_MNT" chpasswd
    unset P1 P2
    if [[ -d "$TARGET_MNT/etc/sudoers.d" ]]; then
        printf '%%wheel ALL=(ALL:ALL) ALL\n' > "$TARGET_MNT/etc/sudoers.d/10-wheel"
        chmod 0440 "$TARGET_MNT/etc/sudoers.d/10-wheel"
    fi
fi

systemctl --root="$TARGET_MNT" set-default graphical.target 2>/dev/null || true
systemctl --root="$TARGET_MNT" enable sddm.service 2>/dev/null || true
systemctl --root="$TARGET_MNT" disable gdm.service 2>/dev/null || true
for unit in systemd-networkd.service systemd-resolved.service; do
    systemctl --root="$TARGET_MNT" enable "$unit" 2>/dev/null || true
done
if [[ -e "$TARGET_MNT/usr/lib/systemd/system/systemd-resolved.service" ]]; then
    rm -f "$TARGET_MNT/etc/resolv.conf"
    ln -s ../run/systemd/resolve/stub-resolv.conf "$TARGET_MNT/etc/resolv.conf"
fi

sync
printf '\n%s\n' "${green}Installation complete.${reset}"
printf '%s\n' "Remove the USB drive and reboot when ready."
read -r -p 'Press Enter for a shell, or type reboot: ' ACTION
if [[ "$ACTION" == reboot ]]; then
    trap - EXIT
    cleanup
    reboot
fi
exec /bin/bash
'''


UPDATE_SCRIPT = r'''#!/usr/bin/env bash
set -Eeuo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

CONFIG=/etc/hardened-arch/update.conf
[[ -r "$CONFIG" ]] && source "$CONFIG"
MANIFEST_URL=${MANIFEST_URL:-}
LOCAL_VERSION=$(sed -n 's/^VERSION_ID=//p' /usr/lib/os-release 2>/dev/null | tr -d '"' | head -n1)
LOCAL_VERSION=${LOCAL_VERSION:-unknown}

usage() {
    echo "Usage: hardened-update [--download DIRECTORY] [--manifest URL] [--interactive]"
}

DOWNLOAD_DIR=
INTERACTIVE=0
while (($#)); do
    case "$1" in
        --download) DOWNLOAD_DIR=${2:?missing directory}; shift 2 ;;
        --manifest) MANIFEST_URL=${2:?missing URL}; shift 2 ;;
        --interactive) INTERACTIVE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

if [[ -z "$MANIFEST_URL" || "$MANIFEST_URL" == __REPO_PLACEHOLDER__ ]]; then
    echo "Update repository is not configured."
    echo "Set MANIFEST_URL in $CONFIG after the SourceForge manifest is published."
    (( INTERACTIVE )) && exec /bin/bash
    exit 1
fi
case "$MANIFEST_URL" in
    https://*) ;;
    *) echo "Refusing a non-HTTPS update manifest: $MANIFEST_URL" >&2; exit 1 ;;
esac

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
MANIFEST="$TMP/manifest.json"

echo "Checking $MANIFEST_URL"
if ! curl -fL --retry 3 --connect-timeout 15 --max-time 120 \
        -o "$MANIFEST" "$MANIFEST_URL"; then
    echo "Update check failed. Configure networking first (nmtui is useful when available)." >&2
    (( INTERACTIVE )) && exec /bin/bash
    exit 1
fi

jq -e '.schema == 1 and (.version|type == "string") and (.iso.url|type == "string") and (.iso.sha512|type == "string")' \
    "$MANIFEST" >/dev/null
REMOTE_VERSION=$(jq -r '.version' "$MANIFEST")
ISO_URL=$(jq -r '.iso.url' "$MANIFEST")
ISO_SHA=$(jq -r '.iso.sha512' "$MANIFEST" | tr 'A-F' 'a-f')
ISO_NAME=$(jq -r '.iso.filename // ("hardened-arch-" + .version + "-x86_64.iso")' "$MANIFEST")
NOTES_URL=$(jq -r '.notes_url // empty' "$MANIFEST")

printf '\nInstalled version: %s\nAvailable version: %s\n' "$LOCAL_VERSION" "$REMOTE_VERSION"
if [[ "$LOCAL_VERSION" == "$REMOTE_VERSION" ]]; then
    echo "This system matches the published version."
elif [[ "$LOCAL_VERSION" == unknown ]] || [[ $(printf '%s\n%s\n' "$LOCAL_VERSION" "$REMOTE_VERSION" | sort -V | tail -n1) == "$REMOTE_VERSION" ]]; then
    echo "An update is available."
else
    echo "The installed build is newer than the published manifest."
fi
[[ -n "$NOTES_URL" ]] && echo "Release notes: $NOTES_URL"
echo "ISO SHA-512: $ISO_SHA"

if [[ -n "$DOWNLOAD_DIR" ]]; then
    mkdir -p "$DOWNLOAD_DIR"
    DEST="$DOWNLOAD_DIR/$ISO_NAME"
    PART="$DEST.part"
    echo "Downloading to $DEST"
    curl -fL --retry 3 -C - -o "$PART" "$ISO_URL"
    ACTUAL=$(sha512sum "$PART" | awk '{print $1}')
    if [[ "$ACTUAL" != "$ISO_SHA" ]]; then
        echo "SHA-512 verification failed." >&2
        echo "Expected: $ISO_SHA" >&2
        echo "Actual:   $ACTUAL" >&2
        exit 1
    fi
    mv -f "$PART" "$DEST"
    echo "Verified download: $DEST"
fi

if (( INTERACTIVE )); then
    echo
    echo "Use: hardened-update --download /path/to/writable/storage"
    exec /bin/bash
fi
'''


VERIFY_SCRIPT = r'''#!/usr/bin/env bash
set -Eeuo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

BASE=/run/hardened-live
SOURCE_DEVICE_FILE="$BASE/source-device"
MEDIA_MNT="$BASE/medium"

echo "Hardened Arch installation media verifier"
echo

if [[ ! -r "$SOURCE_DEVICE_FILE" ]]; then
    echo "Could not determine the boot device: $SOURCE_DEVICE_FILE is missing." >&2
    echo "The early-boot media discovery step did not record its source." >&2
    exec /bin/bash
fi
SOURCE_DEVICE=$(cat "$SOURCE_DEVICE_FILE")
echo "Boot device:    $SOURCE_DEVICE"

if ! mountpoint -q "$MEDIA_MNT"; then
    echo "Media is not mounted at $MEDIA_MNT." >&2
    echo "Attempting to remount $SOURCE_DEVICE read-only for verification..." >&2
    mkdir -p "$MEDIA_MNT"
    mount -t iso9660 -o ro "$SOURCE_DEVICE" "$MEDIA_MNT" 2>/dev/null \
        || mount -o ro "$SOURCE_DEVICE" "$MEDIA_MNT" 2>/dev/null \
        || {
            echo "Could not mount $SOURCE_DEVICE for verification." >&2
            exec /bin/bash
        }
fi

MANIFEST="$MEDIA_MNT/hardened/build-manifest.json"
ROOTFS="$MEDIA_MNT/hardened/rootfs.sfs"

if [[ ! -f "$MANIFEST" ]]; then
    echo "Build manifest is missing from the media: $MANIFEST" >&2
    exec /bin/bash
fi
if [[ ! -f "$ROOTFS" ]]; then
    echo "Root filesystem image is missing from the media: $ROOTFS" >&2
    exec /bin/bash
fi

EXPECTED=$(jq -r '.rootfs_sha512' "$MANIFEST" | tr 'A-F' 'a-f')
if [[ -z "$EXPECTED" || "$EXPECTED" == null ]]; then
    echo "Build manifest does not contain a rootfs_sha512 field." >&2
    exec /bin/bash
fi

echo "Manifest version: $(jq -r '.version' "$MANIFEST")"
echo "Built:            $(jq -r '.built_utc' "$MANIFEST")"
echo
echo "Hashing $ROOTFS ..."
ACTUAL=$(sha512sum "$ROOTFS" | awk '{print $1}')

echo
echo "Expected SHA-512: $EXPECTED"
echo "Actual SHA-512:   $ACTUAL"
echo

if [[ "$ACTUAL" == "$EXPECTED" ]]; then
    echo "VERIFICATION PASSED: this media matches the recorded build."
else
    echo "VERIFICATION FAILED: this media does not match the recorded build." >&2
    echo "Do not install from this media. Re-download or re-write it." >&2
    exec /bin/bash
fi

echo
echo "Press Enter to reboot, or Ctrl+C for a shell."
read -r _ || true
reboot -f
'''


LIVE_INIT = '#!/bin/sh\n# Hardened Arch physical-hardware-safe live-media initramfs.\nPATH=/bin:/sbin:/usr/bin:/usr/sbin\nexport PATH\n\nmkdir -p /proc /sys /dev /dev/pts /run /newroot\nmount -t proc proc /proc 2>/dev/null || true\nmount -t sysfs sysfs /sys 2>/dev/null || true\nmount -t devtmpfs devtmpfs /dev 2>/dev/null || true\nmkdir -p /dev/pts /run /newroot\nmount -t devpts devpts /dev/pts 2>/dev/null || true\nmount -t tmpfs -o mode=0755,nosuid,nodev tmpfs /run 2>/dev/null || true\n\nBASE=/run/hardened-live\nLOG=$BASE/early-init.log\nMEDIA_MNT=$BASE/medium\nLOWER=$BASE/lower\nRW=$BASE/rw\nmkdir -p "$BASE" "$MEDIA_MNT" "$LOWER" "$RW" /newroot\n: > "$LOG"\n\nlog() {\n    echo "[hardened-init] $*"\n    echo "[hardened-init] $*" >> "$LOG" 2>/dev/null || true\n}\n\nshow_diagnostics() {\n    echo\n    echo "===== /proc/cmdline ====="\n    cat /proc/cmdline 2>/dev/null || true\n    echo\n    echo "===== /proc/filesystems ====="\n    cat /proc/filesystems 2>/dev/null || true\n    echo\n    echo "===== blkid ====="\n    blkid 2>/dev/null || true\n    echo\n    echo "===== candidate block devices ====="\n    for dev in \\\n        /dev/disk/by-label/* \\\n        /dev/sd[a-z][0-9]* /dev/sd[a-z] \\\n        /dev/nvme[0-9]n[0-9]p[0-9]* /dev/nvme[0-9]n[0-9] \\\n        /dev/mmcblk[0-9]p[0-9]* /dev/mmcblk[0-9] \\\n        /dev/vd[a-z][0-9]* /dev/vd[a-z] \\\n        /dev/xvd[a-z][0-9]* /dev/xvd[a-z] \\\n        /dev/sr[0-9] /dev/cdrom\n    do\n        [ -e "$dev" ] && echo "$dev"\n    done\n    if command -v dmesg >/dev/null 2>&1; then\n        echo\n        echo "===== recent kernel messages ====="\n        if command -v tail >/dev/null 2>&1; then\n            dmesg 2>/dev/null | tail -n 120\n        else\n            dmesg 2>/dev/null\n        fi\n    fi\n}\n\nfail() {\n    if [ "${PLYMOUTH_ACTIVE:-0}" -eq 1 ] && [ -x /usr/bin/plymouth ]; then\n        /usr/bin/plymouth quit >/dev/null 2>&1 || true\n    fi\n    log "FATAL: $*"\n    show_diagnostics\n    echo\n    echo "Early boot failed; rebooting."\n    echo "Early-boot log: $LOG"\n    sleep 8; reboot -f\n}\n\nISO_LABEL="__ISO_LABEL__"\nMODE="live"\nMEDIA_HINT=""\nWAIT_SECONDS=120\nDEBUG_SHELL=0\nPLYMOUTH_ACTIVE=0\n\nfor word in $(cat /proc/cmdline 2>/dev/null); do\n    case "$word" in\n        iso_label=*) ISO_LABEL="${word#iso_label=}" ;;\n        hardened.label=*) ISO_LABEL="${word#hardened.label=}" ;;\n        hardened.media=*) MEDIA_HINT="${word#hardened.media=}" ;;\n        hardened.mode=*) MODE="${word#hardened.mode=}" ;;\n        hardened.wait=*) WAIT_SECONDS="${word#hardened.wait=}" ;;\n        hardened.debug=1) DEBUG_SHELL=1 ;;\n        rd.shell|rd.shell=1) DEBUG_SHELL=1 ;;\n    esac\ndone\n\nif [ "$MODE" = "live" ] || \\\n   [ "$MODE" = "install" ] || \\\n   [ "$MODE" = "update" ]; then\n\n    case " $(cat /proc/cmdline 2>/dev/null) " in\n        *" splash "*)\n            if [ -x /usr/bin/plymouthd ] && \\\n               [ -x /usr/bin/plymouth ]; then\n\n                mkdir -p /run/plymouth\n\n                /usr/bin/plymouthd \\\n                    --mode=boot \\\n                    --pid-file=/run/plymouth/pid \\\n                    >/dev/null 2>&1 || true\n\n                /usr/bin/plymouth show-splash \\\n                    >/dev/null 2>&1 || true\n\n                /usr/bin/plymouth display-message \\\n                    --text="Starting Hardened Arch Linux" \\\n                    >/dev/null 2>&1 || true\n\n                PLYMOUTH_ACTIVE=1\n            fi\n            ;;\n    esac\nfi\n\ncase "$WAIT_SECONDS" in\n    \'\'|*[!0-9]*) WAIT_SECONDS=120 ;;\nesac\n\nlog "starting physical-media discovery"\nlog "label=$ISO_LABEL mode=$MODE wait=${WAIT_SECONDS}s"\n[ -n "$MEDIA_HINT" ] && log "explicit media hint=$MEDIA_HINT"\n\n# Ask Toybox mdev to rescan hardware when that applet is available.\nif command -v mdev >/dev/null 2>&1; then\n    echo /sbin/mdev > /proc/sys/kernel/hotplug 2>/dev/null || true\n    mdev -s >> "$LOG" 2>&1 || true\nelif [ -x /bin/toybox ]; then\n    /bin/toybox mdev -s >> "$LOG" 2>&1 || true\nfi\n\nMEDIUM=""\nSFS=""\n\ntry_medium() {\n    dev="$1"\n    [ -n "$dev" ] || return 1\n    [ -b "$dev" ] || return 1\n\n    umount "$MEDIA_MNT" >/dev/null 2>&1 || true\n\n    # DD-written hybrid media normally mounts as ISO9660. The automatic\n    # fallback also handles firmware layouts exposing the files on a partition.\n    if mount -t iso9660 -o ro "$dev" "$MEDIA_MNT" >> "$LOG" 2>&1 || \\\n       mount -o ro "$dev" "$MEDIA_MNT" >> "$LOG" 2>&1\n    then\n        if [ -f "$MEDIA_MNT/hardened/rootfs.sfs" ]; then\n            MEDIUM="$dev"\n            SFS="$MEDIA_MNT/hardened/rootfs.sfs"\n            log "live medium found on $MEDIUM"\n            return 0\n        fi\n        umount "$MEDIA_MNT" >/dev/null 2>&1 || true\n    fi\n    return 1\n}\n\nscan_media_once() {\n    # Highest-confidence paths first.\n    [ -n "$MEDIA_HINT" ] && try_medium "$MEDIA_HINT" && return 0\n\n    LABEL_PATH="/dev/disk/by-label/$ISO_LABEL"\n    [ -e "$LABEL_PATH" ] && try_medium "$LABEL_PATH" && return 0\n\n    LABEL_DEVICE=$(blkid -L "$ISO_LABEL" 2>/dev/null | head -n 1)\n    [ -n "$LABEL_DEVICE" ] && try_medium "$LABEL_DEVICE" && return 0\n\n    # Partitions first, then whole devices. Optical devices are last so an\n    # empty sr0 cannot distract from the USB stick.\n    for dev in \\\n        /dev/sd[a-z][0-9]* \\\n        /dev/nvme[0-9]n[0-9]p[0-9]* \\\n        /dev/mmcblk[0-9]p[0-9]* \\\n        /dev/vd[a-z][0-9]* \\\n        /dev/xvd[a-z][0-9]* \\\n        /dev/sd[a-z] \\\n        /dev/nvme[0-9]n[0-9] \\\n        /dev/mmcblk[0-9] \\\n        /dev/vd[a-z] \\\n        /dev/xvd[a-z] \\\n        /dev/sr[0-9] /dev/cdrom\n    do\n        try_medium "$dev" && return 0\n    done\n    return 1\n}\n\nelapsed=0\nwhile [ "$elapsed" -lt "$WAIT_SECONDS" ]; do\n    scan_media_once && break\n    if command -v mdev >/dev/null 2>&1; then\n        mdev -s >> "$LOG" 2>&1 || true\n    elif [ -x /bin/toybox ]; then\n        /bin/toybox mdev -s >> "$LOG" 2>&1 || true\n    fi\n    if [ $((elapsed % 10)) -eq 0 ]; then\n        log "waiting for physical boot media (${elapsed}/${WAIT_SECONDS}s)"\n    fi\n    sleep 1\n    elapsed=$((elapsed + 1))\ndone\n\n[ -n "$MEDIUM" ] || fail "could not locate media containing /hardened/rootfs.sfs"\n[ -f "$SFS" ] || fail "live root image disappeared: $SFS"\nlog "rootfs image=$SFS"\n\n# Some minimal initramfs/devtmpfs combinations expose loop-control but not\n# numbered loop nodes. Create a small set if Toybox can provide mknod.\nLOOP=$(losetup -f 2>/dev/null || true)\nif [ -z "$LOOP" ]; then\n    if command -v mknod >/dev/null 2>&1; then\n        MKNOD=mknod\n    elif [ -x /bin/toybox ]; then\n        MKNOD="/bin/toybox mknod"\n    else\n        MKNOD=""\n    fi\n    if [ -n "$MKNOD" ]; then\n        [ -e /dev/loop-control ] || $MKNOD /dev/loop-control c 10 237 2>/dev/null || true\n        i=0\n        while [ "$i" -lt 16 ]; do\n            [ -e "/dev/loop$i" ] || $MKNOD "/dev/loop$i" b 7 "$i" 2>/dev/null || true\n            i=$((i + 1))\n        done\n        LOOP=$(losetup -f 2>/dev/null || true)\n    fi\nfi\n[ -n "$LOOP" ] || fail "no usable loop device"\n\nlosetup -r "$LOOP" "$SFS" >> "$LOG" 2>&1 || \\\n    fail "could not attach rootfs.sfs to $LOOP"\nmount -t squashfs -o ro "$LOOP" "$LOWER" >> "$LOG" 2>&1 || \\\n    fail "could not mount SquashFS root"\n\n[ -x "$LOWER/sbin/init" ] || [ -x "$LOWER/usr/lib/systemd/systemd" ] || \\\n    fail "SquashFS root has no executable systemd init"\n\nmount -t tmpfs -o mode=0755,nosuid,nodev tmpfs "$RW" >> "$LOG" 2>&1 || \\\n    fail "could not create writable tmpfs layer"\nmkdir -p "$RW/upper" "$RW/work"\nmount -t overlay overlay \\\n    -o lowerdir="$LOWER",upperdir="$RW/upper",workdir="$RW/work" \\\n    /newroot >> "$LOG" 2>&1 || fail "could not mount OverlayFS live root"\n\nINIT=/sbin/init\n[ -x "/newroot$INIT" ] || INIT=/usr/lib/systemd/systemd\n[ -x "/newroot$INIT" ] || fail "systemd is not executable in the overlay root"\n\nprintf \'%s\\n\' "$MEDIUM" > "$BASE/source-device"\nprintf \'%s\\n\' "$MODE" > "$BASE/mode"\nprintf \'%s\\n\' "$ISO_LABEL" > "$BASE/iso-label"\n\nif [ "$DEBUG_SHELL" -eq 1 ]; then\n    log "debug shell requested; type exit to continue into systemd"\n    sh\nfi\n\nlog "switching to $INIT on $MEDIUM"\nif [ "${PLYMOUTH_ACTIVE:-0}" -eq 1 ] && [ -x /usr/bin/plymouth ]; then\n    /usr/bin/plymouth display-message --text="Handing off to Hardened Arch" >/dev/null 2>&1 || true\nfi\n\nmkdir -p /newroot/proc /newroot/sys /newroot/dev /newroot/run\nmount --move /proc /newroot/proc || fail "could not move /proc"\nmount --move /sys /newroot/sys || fail "could not move /sys"\nmount --move /dev /newroot/dev || fail "could not move /dev"\nmount --move /run /newroot/run || fail "could not move /run"\n\nexec switch_root /newroot "$INIT"\n\necho "switch_root returned unexpectedly"\nsleep 8; reboot -f\n'

DRM_TRACE_SCRIPT = r'''#!/usr/bin/env bash
# Continuous DRM/display initialization tracer. Started as early as
# multi-user.target allows, runs for the life of the session, and gives a
# single timestamped record of everything from kernel DRM readiness through
# a confirmed-stable XFCE session. Intended for alpha/beta diagnostic use
# only; strip before a production release (see build script --public-build
# notes).
set -u
LOG=/var/log/hardened-drm-trace.log
mkdir -p /var/log 2>/dev/null

ts() { date +%s.%N; }

{
    echo "===================================================="
    echo "[$(ts)] hardened-drm-trace starting"
    echo "[$(ts)] kernel: $(uname -r 2>/dev/null)"
    for card in /sys/class/drm/card*; do
        [ -e "$card" ] || continue
        name=$(basename "$card")
        status_file="$card/status"
        if [ -r "$status_file" ]; then
            echo "[$(ts)] $name status: $(cat "$status_file" 2>/dev/null)"
        fi
    done
} >> "$LOG" 2>/dev/null

# Real-time DRM subsystem events, timestamped by udev itself as they occur.
# This is the actual evidence trail for a hardware-timing race: it shows the
# exact moment DRM devices/connectors come and go, independent of anything
# userspace decides to log on its own.
if command -v udevadm >/dev/null 2>&1; then
    (
        udevadm monitor --udev --subsystem-match=drm 2>/dev/null \
            | while IFS= read -r line; do
                echo "[$(ts)] udev: $line" >> "$LOG" 2>/dev/null
            done
    ) &
fi

# Kernel-ring-buffer DRM/GPU messages in follow mode, same timestamping
# discipline, catches driver-level detail udev alone will not show.
if command -v dmesg >/dev/null 2>&1; then
    (
        dmesg -w 2>/dev/null \
            | grep -i -E "drm|gpu|vgaarb|becoming.*master|nomodeset" \
            | while IFS= read -r line; do
                echo "[$(ts)] dmesg: $line" >> "$LOG" 2>/dev/null
            done
    ) &
fi

wait
'''


SESSION_TRACE_SCRIPT = r'''#!/usr/bin/env bash
# Wraps the real startxfce4 so every launch attempt is timestamped against
# the same trace log the DRM monitor writes to, and confirms the session
# actually reached a stable, running state rather than just having started.
set -u
LOG=/var/log/hardened-drm-trace.log
mkdir -p /var/log 2>/dev/null

ts() { date +%s.%N; }

{
    echo "[$(ts)] startxfce4 wrapper: launch requested"
    for card in /sys/class/drm/card*; do
        [ -e "$card" ] || continue
        status_file="$card/status"
        if [ -r "$status_file" ]; then
            echo "[$(ts)] $(basename "$card") status at launch: $(cat "$status_file" 2>/dev/null)"
        fi
    done
} >> "$LOG" 2>/dev/null

/usr/bin/startxfce4.real "$@" &
XFCE_PID=$!

# Stability check: wait a short grace period, then confirm the core XFCE
# processes are actually still alive, not just that startxfce4 returned.
# A crash-and-restart loop or an immediate death both need to be visible
# here, not just a bare exit code.
(
    sleep 5
    core_up=1
    for proc in xfwm4 xfce4-panel xfdesktop; do
        pgrep -x "$proc" >/dev/null 2>&1 || core_up=0
    done
    if [ "$core_up" -eq 1 ]; then
        echo "[$(ts)] STABLE: xfwm4, xfce4-panel, and xfdesktop all confirmed running 5s after launch" >> "$LOG" 2>/dev/null
    else
        echo "[$(ts)] UNSTABLE: one or more of xfwm4/xfce4-panel/xfdesktop not running 5s after launch" >> "$LOG" 2>/dev/null
    fi
) &

wait "$XFCE_PID"
RC=$?
echo "[$(ts)] startxfce4 wrapper: session process exited with code $RC" >> "$LOG" 2>/dev/null
exit "$RC"
'''


def os_release(version: str, repo_url: str) -> str:
    home_url = "https://sourceforge.net/"
    if repo_url.startswith("https://sourceforge.net/projects/"):
        m = re.match(r"https://sourceforge\.net/projects/([^/]+)", repo_url)
        if m:
            home_url = f"https://sourceforge.net/projects/{m.group(1)}/"
    return textwrap.dedent(
        f'''\
        NAME="Hardened Arch Linux"
        PRETTY_NAME="Hardened Arch Linux {version}"
        ID=hardened-arch
        ID_LIKE=arch
        VERSION_ID="{version}"
        VERSION="{version}"
        BUILD_ID="{version}"
        ANSI_COLOR="38;2;180;20;35"
        HOME_URL="{home_url}"
        DOCUMENTATION_URL="{home_url}"
        SUPPORT_URL="{home_url}"
        BUG_REPORT_URL="{home_url}"
        LOGO=hardened-arch-logo
        '''
    )


def systemd_units(root: Path) -> None:
    unit_dir = root / "usr/lib/systemd/system"
    installer_service = textwrap.dedent(
        """\
        [Unit]
        Description=Hardened Arch Disk Installer
        After=systemd-user-sessions.service
        Conflicts=getty@tty1.service

        [Service]
        Type=idle
        ExecStart=/usr/local/sbin/hardened-install
        StandardInput=tty
        StandardOutput=tty
        StandardError=tty
        TTYPath=/dev/tty1
        TTYReset=yes
        TTYVHangup=yes

        [Install]
        WantedBy=hardened-installer.target
        """
    )
    installer_target = textwrap.dedent(
        """\
        [Unit]
        Description=Hardened Arch Installer Mode
        Requires=multi-user.target hardened-installer.service
        After=multi-user.target
        AllowIsolate=yes
        """
    )
    update_service = textwrap.dedent(
        """\
        [Unit]
        Description=Hardened Arch Software Update Check
        Wants=network-online.target
        After=network-online.target systemd-user-sessions.service
        Conflicts=getty@tty1.service

        [Service]
        Type=idle
        ExecStart=/usr/local/sbin/hardened-update --interactive
        StandardInput=tty
        StandardOutput=tty
        StandardError=tty
        TTYPath=/dev/tty1
        TTYReset=yes
        TTYVHangup=yes

        [Install]
        WantedBy=hardened-update.target
        """
    )
    update_target = textwrap.dedent(
        """\
        [Unit]
        Description=Hardened Arch Update and Recovery Mode
        Requires=multi-user.target hardened-update.service
        After=multi-user.target
        AllowIsolate=yes
        """
    )
    verify_service = textwrap.dedent(
        """\
        [Unit]
        Description=Hardened Arch Installation Media Verifier
        After=systemd-user-sessions.service
        Conflicts=getty@tty1.service

        [Service]
        Type=idle
        ExecStart=/usr/local/sbin/hardened-verify
        StandardInput=tty
        StandardOutput=tty
        StandardError=tty
        TTYPath=/dev/tty1
        TTYReset=yes
        TTYVHangup=yes

        [Install]
        WantedBy=hardened-verify.target
        """
    )
    verify_target = textwrap.dedent(
        """\
        [Unit]
        Description=Hardened Arch Installation Media Verification Mode
        Requires=multi-user.target hardened-verify.service
        After=multi-user.target
        AllowIsolate=yes
        """
    )
    # UPDATED 2026-08-03 per explicit direction: no password prompt anywhere
    # on the live boot media, TTY or graphical -- physical possession of the
    # media already grants full access, so a console login prompt is exactly
    # as redundant as a graphical one. This supersedes the earlier design
    # note below, which intentionally required a real TTY login for
    # recovery mode. That design is gone; recovery now autologins the same
    # as every other TTY on live media. This is stripped entirely on the
    # installed system, same as SDDM's autologin -- see
    # _strip_live_autologin_for_install / the installer script's shell
    # scrubbing of etc/sddm.conf.d and this override.
    getty_autologin_override = textwrap.dedent(
        """\
        [Unit]
        # tty1 shares its physical console with the Plymouth splash. Without
        # this ordering, agetty's own startup/autologin output can print
        # over or interrupt the splash instead of a clean handoff.
        After=plymouth-quit.service
        Wants=plymouth-quit.service

        [Service]
        ExecStart=
        ExecStart=-/usr/bin/agetty --autologin hardened --noclear %I $TERM
        """
    )
    recovery_target = textwrap.dedent(
        """\
        [Unit]
        Description=Hardened Arch Recovery / Debug Console (autologin, live media only)
        Requires=multi-user.target getty@tty1.service
        After=multi-user.target
        AllowIsolate=yes
        """
    )
    write_text(unit_dir / "hardened-installer.service", installer_service)
    write_text(unit_dir / "hardened-installer.target", installer_target)
    write_text(unit_dir / "hardened-update.service", update_service)
    write_text(unit_dir / "hardened-update.target", update_target)
    write_text(unit_dir / "hardened-verify.service", verify_service)
    write_text(unit_dir / "hardened-verify.target", verify_target)
    write_text(unit_dir / "hardened-recovery.target", recovery_target)

    # Filename deliberately sorts after "hardened.conf" (which
    # install_hardened_qt_security_payload overlays later in the pipeline
    # with plain, non-autologin agetty) so this override always wins
    # regardless of call order, rather than depending on execution order
    # the way the SDDM autologin bug did earlier tonight.
    for tty in ("tty1", "tty2", "tty3", "tty4", "tty5", "tty6"):
        write_text(
            root / f"etc/systemd/system/getty@{tty}.service.d/zz-hardened-live-autologin.conf",
            getty_autologin_override,
        )
    print(
        "XFCE CHECKPOINT: TTY autologin configured for hardened user "
        "(live media only, tty1-tty6)",
        flush=True,
    )

    drm_trace_service = textwrap.dedent(
        """\
        [Unit]
        Description=Hardened Arch DRM/display initialization tracer (alpha/beta diagnostic only)
        After=basic.target
        Before=graphical.target sddm.service

        [Service]
        Type=simple
        ExecStart=/usr/local/libexec/hardened-drm-trace
        Restart=no

        [Install]
        WantedBy=graphical.target
        """
    )
    write_text(unit_dir / "hardened-drm-trace.service", drm_trace_service)



def _configure_hardened_live_desktop(root: Path, public_build: bool = False) -> None:
    password_line = "printf '%s\\n' 'hardened:hardened' | chpasswd\n"
    if public_build:
        # Public builds must never ship a known default credential. Set the
        # convenience password as the initial hash (chpasswd needs *some*
        # value to set), then immediately mark it expired so PAM forces the
        # user to choose their own password the very first time they log in,
        # before a shell is ever granted.
        password_line += "chage -d 0 hardened\n"

    account_script = (
        "set -eu\n"
        "\n"
        "if ! id hardened >/dev/null 2>&1; then\n"
        "    useradd -m -U -u 1000 -s /usr/bin/zsh "
        "-c 'Hardened Arch Live User' hardened\n"
        "fi\n"
        "\n"
        # Found via live testing on 2026-08-02: this useradd's -s flag only
        # ever applies at account creation time. If the "hardened" account
        # already existed (inherited from the base runtime_root copy, which
        # is plausible since runtime_root is a full prior-stage system, not
        # a blank one) the block above is skipped entirely and the account
        # keeps whatever shell it already had -- in this case /bin/bash,
        # which was never added to /etc/shells. pam_shells.so's first-line
        # `auth required` check then rejected every login attempt on every
        # environment tested (SDDM, getty, real hardware, VirtualBox) with
        # a generic "incorrect password" message, even though the password
        # itself was proven correct via a direct crypt() comparison. This
        # forces the shell unconditionally, whether the account was just
        # created or already existed.\n"
        "usermod -s /usr/bin/zsh hardened\n"
        "\n"
        "for group in wheel video audio input storage network lp optical; do\n"
        "    if getent group \"$group\" >/dev/null 2>&1; then\n"
        "        usermod -a -G \"$group\" hardened\n"
        "    fi\n"
        "done\n"
        "\n"
        # Defense in depth: whatever shell any account on this system ends
        # up with, pam_shells.so must never be able to block login over an
        # unregistered shell again.\n"
        "for shell in /usr/bin/zsh /usr/bin/bash /bin/bash; do\n"
        "    if [ -x \"$shell\" ] && ! grep -qxF \"$shell\" /etc/shells 2>/dev/null; then\n"
        "        echo \"$shell\" >> /etc/shells\n"
        "    fi\n"
        "done\n"
        "\n"
        f"{password_line}"
    )

    run([
        "chroot",
        root,
        "/bin/bash",
        "-lc",
        account_script,
    ])

    write_text(
        root / "etc/sudoers.d/10-hardened-live",
        "%wheel ALL=(ALL:ALL) ALL\n",
        0o440,
    )

    write_text(
        root / "etc/sddm.conf.d/10-hardened-live.conf",
        "[General]\n"
        "InputMethod=qtvirtualkeyboard\n"
        "GreeterEnvironment="
        "QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1,QT_ACCESSIBILITY=1\n"
        "\n"
        "[Theme]\n"
        "Current=breeze\n"
        "\n"
        "[Users]\n"
        "MinimumUid=1000\n"
        "MaximumUid=60000\n"
        "RememberLastUser=false\n"
        "RememberLastSession=true\n"
        "HideShells=/sbin/nologin,/usr/sbin/nologin,"
        "/bin/false,/usr/bin/false\n"
        "\n"
        "[Autologin]\n"
        "Relogin=false\n"
        "Session=\n"
        "User=\n"
        "\n"
        "[X11]\n"
        "DisplayCommand=/usr/local/libexec/hardened-sddm-xsetup\n",
    )

    write_text(
        root / "usr/local/libexec/hardened-sddm-xsetup",
        "#!/bin/sh\n"
        "export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1\n"
        "export QT_ACCESSIBILITY=1\n"
        "\n"
        "if command -v xkbset >/dev/null 2>&1; then\n"
        "    xkbset sticky -twokey latchlock "
        ">/dev/null 2>&1 || true\n"
        "    xkbset exp 64 =sticky "
        ">/dev/null 2>&1 || true\n"
        "fi\n"
        "\n"
        "exit 0\n",
        0o755,
    )

    write_text(
        root / "etc/environment.d/90-hardened-accessibility.conf",
        "QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1\n"
        "QT_ACCESSIBILITY=1\n",
    )

    home = root / "home/hardened"
    config = home / ".config"
    config.mkdir(parents=True, exist_ok=True)

    # Sticky Keys via XFCE's actual accessibility mechanism (AccessX, backed
    # by xfconf), not KDE's kaccess daemon. The previous kaccessrc/kwinrc/
    # kglobalshortcutsrc block below configured KDE-specific components that
    # do not exist in this XFCE build -- kaccess was never staged, so the
    # autostart entry that would have launched it never got written, and
    # none of Sticky Keys, the zoom plugin, or the zoom shortcuts ever
    # actually functioned despite the accessibility-ready message printing
    # unconditionally. Found via manual inspection on 2026-07-31.
    xfconf_dir = config / "xfce4/xfconf/xfce-perchannel-xml"
    xfconf_dir.mkdir(parents=True, exist_ok=True)
    write_text(
        xfconf_dir / "accessibility.xml",
        '<?xml version="1.0" encoding="UTF-8"?>\n\n'
        '<channel name="accessibility" version="1.0">\n'
        '  <property name="StickyKeys" type="empty">\n'
        '    <property name="Enabled" type="bool" value="true"/>\n'
        '    <property name="Latch" type="bool" value="true"/>\n'
        '    <property name="TwoKeys" type="bool" value="false"/>\n'
        '  </property>\n'
        '  <property name="SlowKeys" type="empty">\n'
        '    <property name="Enabled" type="bool" value="false"/>\n'
        '  </property>\n'
        '  <property name="BounceKeys" type="empty">\n'
        '    <property name="Enabled" type="bool" value="false"/>\n'
        '  </property>\n'
        '</channel>\n',
    )
    # No fix for zoom yet: XFCE/xfwm4 has no built-in screen magnifier
    # equivalent to KWin's zoom compositor plugin. Genuinely not addressed
    # here -- would need a separate magnifier tool staged and wired up, not
    # a config-file swap. Tracked as a known gap, not silently claimed as
    # working.

    run([
        "chroot",
        root,
        "/bin/chown",
        "-R",
        "hardened:hardened",
        "/home/hardened",
    ])

    if public_build:
        print(
            "LIVE LOGIN READY: username=hardened "
            "(password expired \u2014 you will be prompted to set a new one on first login)"
        )
    else:
        print(
            "LIVE LOGIN READY: "
            "username=hardened password=hardened"
        )

    print(
        "ACCESSIBILITY READY: virtual keyboard, Sticky Keys (XFCE AccessX). "
        "Zoom/magnifier not yet implemented for XFCE -- known gap."
    )

    if not (root / "usr/bin/orca").exists():
        print(
            "WARNING: Orca is not present; "
            "speech output is not available yet."
        )



def _configure_hardened_pacman_repos(root: Path) -> None:
    # Added 2026-08-03 at direct request before public release. Nothing had
    # ever configured pacman.conf/mirrorlist at all since pacman was staged
    # -- whatever was present was just whatever happened to be in the
    # runtime_root snapshot's own config, unaudited.
    print("\n== Configuring pacman repositories and mirrors ==", flush=True)

    mirrorlist_content = (
        "# Hardened Arch default mirrorlist -- curated official Arch\n"
        "# mirrors, HTTPS only, geographically diverse. Generated as a\n"
        "# static starting list; users should run reflector post-install\n"
        "# for mirrors ranked by their own actual location/speed.\n"
        "\n"
        "Server = https://geo.mirror.pkgbuild.com/$repo/os/$arch\n"
        "Server = https://america.mirror.pkgbuild.com/$repo/os/$arch\n"
        "Server = https://europe.mirror.pkgbuild.com/$repo/os/$arch\n"
        "Server = https://asia.mirror.pkgbuild.com/$repo/os/$arch\n"
        "Server = https://mirrors.kernel.org/archlinux/$repo/os/$arch\n"
    )
    write_text(root / "etc/pacman.d/mirrorlist", mirrorlist_content)

    blackarch_mirrorlist_content = (
        "# BlackArch -- penetration testing / security research tools.\n"
        "# https://blackarch.org -- legitimate, well-established Arch-based\n"
        "# security repository, not affiliated with this project.\n"
        "Server = https://www.blackarch.org/blackarch/$repo/os/$arch\n"
        "Server = https://mirror.cyberbits.eu/blackarch/$repo/os/$arch\n"
    )
    write_text(
        root / "etc/pacman.d/blackarch-mirrorlist",
        blackarch_mirrorlist_content,
    )

    pacman_conf_content = (
        "[options]\n"
        "HoldPkg     = pacman glibc\n"
        "Architecture = auto\n"
        "CheckSpace\n"
        "SigLevel    = Required DatabaseOptional\n"
        "LocalFileSigLevel = Optional\n"
        "ParallelDownloads = 5\n"
        "\n"
        "[core]\n"
        "Include = /etc/pacman.d/mirrorlist\n"
        "\n"
        "[extra]\n"
        "Include = /etc/pacman.d/mirrorlist\n"
        "\n"
        "[multilib]\n"
        "Include = /etc/pacman.d/mirrorlist\n"
        "\n"
        "# BlackArch -- security/pentesting tools, added 2026-08-03.\n"
        "# Verify the signing key before first use:\n"
        "#   curl -O https://blackarch.org/keyring/blackarch-keyring.pkg.tar.xz\n"
        "#   pacman-key --lsign-key <official BlackArch key, verify on blackarch.org>\n"
        "[blackarch]\n"
        "Include = /etc/pacman.d/blackarch-mirrorlist\n"
    )
    write_text(root / "etc/pacman.conf", pacman_conf_content)

    print(
        "PACMAN CHECKPOINT: wrote pacman.conf with core/extra/multilib + "
        "BlackArch, and a curated default mirrorlist",
        flush=True,
    )


def _configure_hardened_plymouth(root: Path) -> None:
    artwork = Path("/home/corbett/boot_logo2.png")

    if not artwork.is_file():
        raise BuildError(
            f"custom Plymouth artwork is missing: {artwork}"
        )

    theme = (
        root
        / "usr/share/plymouth/themes/hardened-arch"
    )

    theme.mkdir(parents=True, exist_ok=True)

    copy_file(
        artwork,
        theme / "boot_logo2.png",
    )

    write_text(
        theme / "hardened-arch.plymouth",
        "[Plymouth Theme]\n"
        "Name=Hardened Arch\n"
        "Description=Hardened Arch custom boot splash\n"
        "ModuleName=script\n"
        "\n"
        "[script]\n"
        "ImageDir=/usr/share/plymouth/themes/hardened-arch\n"
        "ScriptFile=/usr/share/plymouth/themes/"
        "hardened-arch/hardened-arch.script\n",
    )

    write_text(
        theme / "hardened-arch.script",
        "Window.SetBackgroundTopColor(0.0, 0.0, 0.0);\n"
        "Window.SetBackgroundBottomColor(0.0, 0.0, 0.0);\n"
        "\n"
        "logo.image = Image(\"boot_logo2.png\");\n"
        "logo.sprite = Sprite(logo.image);\n"
        "logo.sprite.SetX("
        "Window.GetWidth() / 2 "
        "- logo.image.GetWidth() / 2"
        ");\n"
        "logo.sprite.SetY("
        "Window.GetHeight() / 2 "
        "- logo.image.GetHeight() / 2"
        ");\n"
        "\n"
        "# Real functional progress bar, added 2026-08-03. Tied to\n"
        "# Plymouth's actual boot-progress callback, which reflects genuine\n"
        "# systemd unit startup timing -- not a decorative animation.\n"
        "progress_bar.original_width = 320;\n"
        "progress_bar.height = 6;\n"
        "progress_bar.x = Window.GetWidth() / 2 - progress_bar.original_width / 2;\n"
        "progress_bar.y = "
        "Window.GetHeight() / 2 + logo.image.GetHeight() / 2 + 40;\n"
        "\n"
        "progress_bar.track = Rectangle("
        "progress_bar.original_width, progress_bar.height, "
        "0.15, 0.15, 0.15, 1);\n"
        "progress_bar.track_sprite = Sprite(progress_bar.track);\n"
        "progress_bar.track_sprite.SetX(progress_bar.x);\n"
        "progress_bar.track_sprite.SetY(progress_bar.y);\n"
        "\n"
        "progress_bar.fill = Rectangle("
        "1, progress_bar.height, 0.55, 0.35, 0.85, 1);\n"
        "progress_bar.fill_sprite = Sprite(progress_bar.fill);\n"
        "progress_bar.fill_sprite.SetX(progress_bar.x);\n"
        "progress_bar.fill_sprite.SetY(progress_bar.y);\n"
        "\n"
        "status_text.sprite = Sprite();\n"
        "status_text.sprite.SetX(progress_bar.x);\n"
        "status_text.sprite.SetY(progress_bar.y + progress_bar.height + 10);\n"
        "\n"
        "fun refresh_status_text(message) {\n"
        "    status_text.image = Image.Text("
        "message, 0.7, 0.7, 0.7, 1, \"Sans 9\");\n"
        "    status_text.sprite.SetImage(status_text.image);\n"
        "}\n"
        "\n"
        "fun boot_progress_callback(duration, progress) {\n"
        "    width = progress_bar.original_width * progress;\n"
        "    if (width < 1) {\n"
        "        width = 1;\n"
        "    }\n"
        "    progress_bar.fill = Rectangle("
        "width, progress_bar.height, 0.55, 0.35, 0.85, 1);\n"
        "    progress_bar.fill_sprite.SetImage(progress_bar.fill);\n"
        "    refresh_status_text("
        "\"Starting Hardened Arch Linux -- \" "
        "+ Math.Int(progress * 100) + \"%\");\n"
        "}\n"
        "Plymouth.SetBootProgressFunction(boot_progress_callback);\n"
        "\n"
        "fun display_normal_callback() {\n"
        "    refresh_status_text(\"Starting Hardened Arch Linux\");\n"
        "}\n"
        "Plymouth.SetDisplayNormalFunction(display_normal_callback);\n"
        "\n"
        "fun quit_callback() {\n"
        "    refresh_status_text(\"Ready\");\n"
        "}\n"
        "Plymouth.SetQuitFunction(quit_callback);\n",
    )

    write_text(
        root / "etc/plymouth/plymouthd.conf",
        "[Daemon]\n"
        "Theme=hardened-arch\n"
        "ShowDelay=0\n"
        "DeviceTimeout=8\n",
    )

    write_text(
        root
        / "usr/local/libexec/hardened-plymouth-start",
        "#!/bin/sh\n"
        "set -u\n"
        "\n"
        "[ -x /usr/bin/plymouthd ] || exit 0\n"
        "[ -x /usr/bin/plymouth ] || exit 0\n"
        "\n"
        "mkdir -p /run/plymouth\n"
        "\n"
        "if [ -s /run/plymouth/pid ]; then\n"
        "    pid=$(cat /run/plymouth/pid "
        "2>/dev/null || true)\n"
        "    [ -n \"$pid\" ] "
        "&& kill -0 \"$pid\" 2>/dev/null "
        "&& exit 0\n"
        "fi\n"
        "\n"
        "/usr/bin/plymouthd "
        "--mode=boot "
        "--pid-file=/run/plymouth/pid "
        ">/dev/null 2>&1 || exit 0\n"
        "\n"
        "/usr/bin/plymouth show-splash "
        ">/dev/null 2>&1 || true\n"
        "\n"
        "/usr/bin/plymouth display-message "
        "--text=\"Starting Hardened Arch Linux\" "
        ">/dev/null 2>&1 || true\n"
        "\n"
        "exit 0\n",
        0o755,
    )

    write_text(
        root
        / "usr/local/libexec/hardened-plymouth-quit",
        "#!/bin/sh\n"
        "[ -x /usr/bin/plymouth ] || exit 0\n"
        "/usr/bin/plymouth quit "
        ">/dev/null 2>&1 || true\n"
        "exit 0\n",
        0o755,
    )

    unit_dir = root / "usr/lib/systemd/system"

    write_text(
        unit_dir
        / "hardened-plymouth-start.service",
        "[Unit]\n"
        "Description=Start Hardened Arch boot splash\n"
        "DefaultDependencies=no\n"
        "ConditionKernelCommandLine=splash\n"
        "After=systemd-udev-trigger.service\n"
        "Before=basic.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/local/libexec/"
        "hardened-plymouth-start\n"
        "RemainAfterExit=yes\n"
        "\n"
        "[Install]\n"
        "WantedBy=sysinit.target\n",
    )

    write_text(
        unit_dir
        / "hardened-plymouth-quit.service",
        "[Unit]\n"
        "Description=Quit Hardened Arch boot splash "
        "before user interface\n"
        "ConditionKernelCommandLine=splash\n"
        "After=basic.target\n"
        "Before=sddm.service "
        "hardened-installer.service "
        "hardened-update.service "
        "hardened-verify.service "
        "getty@tty1.service\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        "ExecStart=/usr/local/libexec/"
        "hardened-plymouth-quit\n"
        "\n"
        "[Install]\n"
        "WantedBy=graphical.target "
        "hardened-installer.target "
        "hardened-update.target "
        "hardened-verify.target "
        "hardened-recovery.target\n",
    )

    run(
        [
            "systemctl",
            f"--root={root}",
            "enable",
            "hardened-plymouth-start.service",
        ],
        check=False,
    )

    run(
        [
            "systemctl",
            f"--root={root}",
            "enable",
            "hardened-plymouth-quit.service",
        ],
        check=False,
    )


def _stage_hardened_plymouth(
    live_root: Path,
    initramfs_root: Path,
) -> None:
    for tool in ("plymouthd", "plymouth"):
        host_tool = shutil.which(tool)

        if host_tool:
            stage_host_tool(initramfs_root, tool)
            continue

        source_tool = command_in_root(
            live_root,
            tool,
        )

        if source_tool is None:
            raise BuildError(
                "Plymouth tool is missing from host "
                f"and live root: {tool}"
            )

        destination = (
            initramfs_root
            / source_tool.relative_to(live_root)
        )

        copy_file(
            source_tool,
            destination,
            0o755,
        )

        for dependency in ldd_dependencies(
            source_tool
        ):
            copy_path_preserving_links(
                dependency,
                initramfs_root,
            )

    plugin_sources: list[Path] = []

    for relative in (
        Path("usr/lib/plymouth"),
        Path("usr/lib64/plymouth"),
        Path("usr/share/plymouth"),
        Path("etc/plymouth"),
    ):
        source_path = live_root / relative

        if not source_path.exists():
            continue

        if (
            relative.name == "plymouth"
            and "lib" in relative.parts
        ):
            plugin_sources.append(source_path)

        destination = initramfs_root / relative

        if (
            destination.is_symlink()
            or destination.is_file()
        ):
            destination.unlink()

        elif destination.is_dir():
            shutil.rmtree(destination)

        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        shutil.copytree(
            source_path,
            destination,
            symlinks=True,
        )

    for plugin_root in plugin_sources:
        for plugin in plugin_root.rglob("*.so"):
            for dependency in ldd_dependencies(
                plugin
            ):
                copy_path_preserving_links(
                    dependency,
                    initramfs_root,
                )

    for required in (
        initramfs_root
        / "usr/bin/plymouthd",

        initramfs_root
        / "usr/bin/plymouth",

        initramfs_root
        / "usr/share/plymouth/themes/"
        "hardened-arch/hardened-arch.plymouth",

        initramfs_root
        / "usr/share/plymouth/themes/"
        "hardened-arch/hardened-arch.script",

        initramfs_root
        / "usr/share/plymouth/themes/"
        "hardened-arch/boot_logo2.png",
    ):
        if not required.exists():
            raise BuildError(
                "live initramfs Plymouth payload "
                f"is missing: {required}"
            )



def _configure_hardened_wayland_login(root: Path) -> None:
    # Replace the Plasma live session with the audited Xfce stage.
    #
    # The disposable live-root may contain stripped or otherwise transformed
    # KDE files whose bytes differ from the saved KDE install prefix. The
    # saved prefix is therefore used as an explicit path allow-list. Every
    # existing destination is archived and verified before it is removed.
    import filecmp
    import hashlib
    import os
    from pathlib import Path
    import shutil

    print("\n== Preparing clean Xfce live desktop ==", flush=True)

    xfce_stage = Path(
        os.environ.get(
            "HARDENED_XFCE_STAGE",
            "/home/corbett/xfce-source-stage/rootfs",
        )
    ).resolve()

    kde_prefix = Path(
        os.environ.get(
            "HARDENED_KDE_PREFIX",
            "/home/corbett/kde/usr",
        )
    )

    print("XFCE CHECKPOINT: validating source stages", flush=True)

    if not xfce_stage.is_dir():
        raise BuildError(f"Audited Xfce stage is missing: {xfce_stage}")

    if kde_prefix.is_symlink() or not kde_prefix.is_dir():
        raise BuildError(
            "KDE source-install prefix must be a real directory so its exact "
            f"path manifest can be excluded safely: {kde_prefix}"
        )

    resolved_kde_prefix = kde_prefix.resolve()
    if resolved_kde_prefix == Path("/usr") or not str(resolved_kde_prefix).startswith(
        "/home/corbett/kde/"
    ):
        raise BuildError(
            f"Refusing unsafe KDE exclusion prefix: {resolved_kde_prefix}"
        )

    required_xfce = (
        "usr/bin/startxfce4",
        "usr/bin/xfce4-session",
        "usr/bin/xfce4-panel",
        "usr/bin/xfce4-settings-manager",
        "usr/bin/xfdesktop",
        "usr/bin/xfwm4",
        "usr/bin/Thunar",
        "usr/bin/xfce4-power-manager",
        "usr/bin/upower",
        "usr/share/xsessions/xfce.desktop",
        # Discovered via ldd against xfce4-session on 2026-07-30: xfce4-panel's
        # layer-shell support and the taskbar/window-list code pull these in,
        # but neither is an Xfce-authored component, so nothing else in this
        # script would ever check for them without this explicit entry.
        "usr/lib/libgtk-layer-shell.so.0",
        "usr/lib/libwnck-3.so.0",
        # Discovered via live testing on 2026-07-30: the sudoers.d grants
        # above are useless if the sudo binary itself was never staged into
        # the build. Nothing previously checked for this, so a build could
        # silently ship with the wheel-group permission configured but no
        # way to actually invoke it, leaving the live/hardened user with no
        # privilege escalation path at all despite the config implying one
        # exists.
        "usr/bin/sudo",
        # Signed Arch Firefox package staged into the Xfce overlay.
        "usr/bin/firefox",
        "usr/lib/firefox/firefox",
        "usr/lib/firefox/firefox-bin",
        "usr/lib/firefox/libxul.so",
        "usr/lib/firefox/libmozsandbox.so",
        # Signed Arch Chromium package staged into the Xfce overlay.
        "usr/bin/chromium",
        "usr/lib/chromium/chromium",
        "usr/lib/chromium/chrome-sandbox",
        "usr/lib/chromium/chrome_crashpad_handler",
        "usr/lib/chromium/icudtl.dat",
        "usr/lib/chromium/resources.pak",
        "usr/lib/chromium/locales/en-US.pak",
        "usr/share/applications/chromium.desktop",
        # Signed Arch Xfce Terminal and essential runtime libraries.
        "usr/bin/xfce4-terminal",
        # Signed Arch Zsh interactive shell and Snapper Btrfs tools.
        "usr/bin/zsh",
        "etc/skel/.zshrc",
        "usr/bin/snapper",
        "usr/bin/snapperd",
        "usr/lib/libsnapper.so.8",
        "usr/lib/systemd/system/snapper-cleanup.timer",
        "usr/lib/systemd/system/snapper-timeline.timer",
        "usr/lib/systemd/system/snapperd.service",
        "usr/share/zsh/site-functions/_snapper",
        "usr/share/applications/xfce4-terminal.desktop",
        "usr/lib/libvte-2.91.so.0",
        "usr/lib/libutempter.so.0",
        "usr/share/fonts/liberation/LiberationSans-Regular.ttf",
        # Hardened Xfce desktop layout: wallpaper, panel config, start icon,
        # first-login helper. Never made it into this tuple originally
        # because the patch script that added them searched for
        # "required_xfce = [" (a list) when this is actually a tuple using
        # "(" -- the search silently found nothing every time it ran, so
        # these files were staged in the rootfs but never enforced by the
        # build. Found and fixed on 2026-08-02.
        "usr/share/backgrounds/hardened/hardened-purple-default.png",
        "usr/share/pixmaps/hardened-start.png",
        "usr/share/icons/hicolor/48x48/apps/hardened-start.png",
        "usr/share/icons/hicolor/64x64/apps/hardened-start.png",
        "usr/share/icons/hicolor/128x128/apps/hardened-start.png",
        "usr/share/icons/hicolor/256x256/apps/hardened-start.png",
        "etc/skel/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-panel.xml",
        "etc/skel/.config/xfce4/xfconf/xfce-perchannel-xml/xsettings.xml",
        "etc/skel/.config/xfce4/xfconf/xfce-perchannel-xml/xfwm4.xml",
        "etc/skel/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml",
        "etc/skel/.config/xfce4/panel/launcher-3/firefox.desktop",
        "etc/skel/.config/xfce4/panel/launcher-4/chromium.desktop",
        "etc/skel/.config/xfce4/panel/launcher-5/xfce4-terminal.desktop",
        "etc/skel/.config/xfce4/panel/launcher-6/thunar.desktop",
        "etc/skel/.config/gtk-3.0/gtk.css",
        "etc/skel/.config/autostart/hardened-xfce-first-login.desktop",
        "usr/local/bin/hardened-xfce-first-login",
        # Base package manager infrastructure, staged from system-stage on
        # 2026-08-01. Nothing previously verified pacman itself existed on
        # the final image -- its presence was purely incidental, inherited
        # unverified from whatever base tree happened to contain it.
        "usr/bin/pacman",
        "usr/lib/libalpm.so.15",
        # Found via live testing on 2026-08-03: xfdesktop and other XFCE
        # components link against libnotify for desktop notification
        # popups (battery warnings, etc). It was fixed live in the stage
        # multiple times tonight but nothing in the build script ever
        # verified it actually landed in a given build -- meaning a build
        # could silently ship without it again with no warning, only
        # discoverable hours later via a runtime crash. Hard-required now.
        "usr/lib/libnotify.so.4",
        # Same reasoning: the dynamic linker itself, the full Samba chain,
        # and zlib were all found missing live tonight with zero build-time
        # verification catching any of it beforehand.
        "usr/lib/ld-linux-x86-64.so.2",
        "usr/lib/libz.so.1",
        "usr/lib/samba/libsamba-security-private-samba.so",
    )

    for relative in required_xfce:
        candidate = xfce_stage / relative
        if not candidate.exists():
            raise BuildError(f"Required Xfce stage output is missing: {candidate}")

    staged_la = list(xfce_stage.rglob("*.la"))
    if staged_la:
        raise BuildError(
            "Audited Xfce stage unexpectedly contains libtool archives: "
            + ", ".join(str(item) for item in staged_la[:10])
        )

    print("XFCE CHECKPOINT: enumerating KDE path allow-list", flush=True)

    source_objects = sorted(
        (
            item
            for item in resolved_kde_prefix.rglob("*")
            if item.is_file() or item.is_symlink()
        ),
        key=lambda item: len(item.parts),
        reverse=True,
    )

    # Preflight the complete mutation set before creating or deleting anything.
    removals: dict[Path, tuple[Path | None, str]] = {}
    matching_files = 0
    transformed_files = 0

    for source in source_objects:
        relative = source.relative_to(resolved_kde_prefix)
        destination = root / "usr" / relative

        if not os.path.lexists(destination):
            continue

        if destination.is_dir() and not destination.is_symlink():
            raise BuildError(
                "KDE path allow-list resolves to a directory where a file or "
                f"symlink was expected: {destination}"
            )

        if not destination.is_file() and not destination.is_symlink():
            raise BuildError(
                f"Refusing to remove unsupported filesystem object: {destination}"
            )

        identical = False
        if source.is_symlink() and destination.is_symlink():
            identical = os.readlink(source) == os.readlink(destination)
        elif (
            source.is_file()
            and not source.is_symlink()
            and destination.is_file()
            and not destination.is_symlink()
        ):
            identical = filecmp.cmp(source, destination, shallow=False)

        if identical:
            matching_files += 1
            reason = "byte-identical-or-same-link"
        else:
            transformed_files += 1
            reason = "path-allowlisted-transformed-copy"

        removals[destination] = (source, reason)

    stale_files = (
        "usr/bin/startplasma-wayland",
        "usr/bin/startplasma-x11",
        "usr/bin/plasmashell",
        "usr/bin/kwin_wayland",
        "usr/bin/kwin_x11",
        "usr/share/wayland-sessions/plasma.desktop",
        "usr/share/xsessions/plasma.desktop",
        "usr/share/xsessions/plasmax11.desktop",
        # 10-hardened-live.conf is intentionally kept: it configures
        # accessibility/theme settings for SDDM itself, not Plasma, and SDDM
        # remains the active display manager for this build.
        "etc/sddm.conf.d/20-hardened-wayland.conf",
        "etc/sddm.conf.d/20-plasma-x11.conf",
        "var/lib/sddm/state.conf",
        "home/hardened/.config/kwinrc",
        "home/hardened/.config/plasmashellrc",
        "home/hardened/.config/plasma-org.kde.plasma.desktop-appletsrc",
        "home/hardened/.config/kdeglobals",
        "home/hardened/.config/kscreenlockerrc",
        "home/hardened/.config/ksmserverrc",
        "home/hardened/.config/startkderc",
        # Found via manual inspection on 2026-07-31: home-directory KDE config
        # files are only ever removed if explicitly listed here -- the
        # automatic byte-comparison exclusion above only scans paths under
        # /usr, never /home. These two were missing entirely.
        "home/hardened/.config/kaccessrc",
        "home/hardened/.config/kglobalshortcutsrc",
        "home/hardened/.config/kglobalshortcutsrc.notify",
        "home/hardened/.config/kcminputrc",
        "home/hardened/.config/kxkbrc",
        # flatpak was pulled in as a transitive dependency of KDE Discover's
        # flatpak backend during the original KDE build. It's not part of the
        # KDE application prefix path structure, so the byte-for-byte KDE
        # comparison above never catches it, and its own dependency
        # (libappstream) was never staged, leaving a broken
        # "cannot open shared object file" hook with nothing in this XFCE
        # build that actually needs it. Stripped outright rather than fixed.
        # Directory trees (usr/lib/flatpak, etc/flatpak, var/lib/flatpak) are
        # handled separately below since this list only accepts files.
        "usr/bin/flatpak",
        "usr/libexec/flatpak-system-helper",
        "usr/lib/systemd/system/flatpak-system-helper.service",
        "usr/lib/systemd/system/multi-user.target.wants/flatpak-system-helper.service",
        "usr/share/dbus-1/system-services/org.freedesktop.Flatpak.SystemHelper.service",
        "usr/share/dbus-1/system.d/org.freedesktop.Flatpak.service.conf",
    )

    for relative in stale_files:
        destination = root / relative
        if not os.path.lexists(destination):
            continue
        if destination.is_dir() and not destination.is_symlink():
            raise BuildError(
                f"Refusing to remove unexpected directory at stale Plasma path: {destination}"
            )
        if not destination.is_file() and not destination.is_symlink():
            raise BuildError(
                f"Refusing to remove unsupported stale Plasma object: {destination}"
            )
        removals.setdefault(destination, (None, "explicit-stale-plasma-path"))

    # flatpak's directory trees are removed explicitly and intentionally here,
    # separate from the single-file stale_files mechanism above (which
    # deliberately refuses to touch real directories as a safety guard).
    flatpak_dirs = (
        root / "usr/lib/flatpak",
        root / "etc/flatpak",
        root / "var/lib/flatpak",
    )
    for flatpak_dir in flatpak_dirs:
        if flatpak_dir.is_dir() and not flatpak_dir.is_symlink():
            print(f"XFCE CHECKPOINT: removing stale flatpak directory {flatpak_dir}", flush=True)
            shutil.rmtree(flatpak_dir)
        elif flatpak_dir.is_symlink() or flatpak_dir.is_file():
            flatpak_dir.unlink()

    compatibility = root / "home/corbett/kde/usr"
    if compatibility.is_symlink():
        removals.setdefault(
            compatibility,
            (None, "explicit-kde-prefix-compatibility-link"),
        )
    elif compatibility.exists():
        raise BuildError(
            "KDE compatibility path exists but is not a symlink: "
            f"{compatibility}"
        )

    # Protect deliberately-added standalone applications and their runtime
    # dependencies from this exclusion pass. These were pulled in on purpose
    # via pacman/system-stage as standalone components, not inherited from
    # the archived KDE prefix -- but several (Kate, KMag, Dolphin, pacman,
    # the KF6/Qt6 libraries, Breeze icons/theme) may exist at the same
    # relative path in that prefix too, since the original KDE build had its
    # own copies. Without this filter the path-overlap comparison above
    # would remove them as if they were leftover KDE cruft. This filter was
    # present in an earlier build script lineage and is being restored here
    # after being found missing on 2026-08-02.
    protected_prefixes = (
        "usr/bin/kate",
        "usr/bin/kwrite",
        "usr/bin/kmag",
        "usr/bin/dolphin",
        "usr/bin/magnus",
        "usr/bin/pacman",
        "usr/bin/firefox",
        "usr/bin/chromium",
        "usr/bin/xfce4-terminal",
        "usr/bin/zsh",
        "usr/bin/snapper",
        "usr/bin/snapperd",
        "usr/lib/libKF6",
        "usr/lib/libdolphin",
        "usr/lib/libkateprivate",
        "usr/lib/libalpm",
        "usr/lib/libsnapper",
        "usr/lib/libvte",
        "usr/lib/libutempter",
        "usr/lib/firefox",
        "usr/lib/chromium",
        "usr/lib/qt6",
        "usr/lib/libQt6",
        "usr/lib/systemd/system/snapper-",
        "usr/share/kf6",
        "usr/share/kxmlgui6",
        "usr/share/icons/breeze",
        "usr/share/themes/Breeze",
        "usr/share/applications/firefox.desktop",
        "usr/share/applications/chromium.desktop",
        "usr/share/applications/xfce4-terminal.desktop",
        "usr/share/backgrounds/hardened",
        "usr/share/pixmaps/hardened-start.png",
        "usr/share/icons/hicolor/48x48/apps/hardened-start.png",
        "usr/share/icons/hicolor/64x64/apps/hardened-start.png",
        "usr/share/icons/hicolor/128x128/apps/hardened-start.png",
        "usr/share/icons/hicolor/256x256/apps/hardened-start.png",
        "etc/skel",
        "etc/zsh",
        "etc/shells",
    )
    protected_hits = [
        dest for dest in removals
        if any(str(dest.relative_to(root)).startswith(p) for p in protected_prefixes)
    ]
    for dest in protected_hits:
        del removals[dest]
    if protected_hits:
        print(
            f"XFCE CHECKPOINT: protected {len(protected_hits)} paths from "
            "KDE exclusion (standalone apps, browsers, shell tooling, "
            "desktop layout)",
            flush=True,
        )

    print(
        "XFCE CHECKPOINT: preflight complete "
        f"allowlisted={len(removals)} "
        f"identical={matching_files} transformed={transformed_files}",
        flush=True,
    )

    backup_root = root.parent / "xfce-kde-exclusion-backup"
    if backup_root.exists() or backup_root.is_symlink():
        raise BuildError(
            f"Refusing to overwrite an existing KDE exclusion backup: {backup_root}"
        )

    backup_root.mkdir(parents=True, mode=0o700)
    manifest_lines = [
        "# Hardened Arch Xfce candidate KDE/Plasma exclusion backup",
        f"# live_root={root}",
        f"# kde_prefix={resolved_kde_prefix}",
        f"# entries={len(removals)}",
    ]

    print(
        f"XFCE CHECKPOINT: archiving {len(removals)} candidate paths",
        flush=True,
    )

    for destination, (source, reason) in sorted(
        removals.items(),
        key=lambda item: str(item[0]),
    ):
        relative = destination.relative_to(root)
        backup = backup_root / relative
        backup.parent.mkdir(parents=True, exist_ok=True)

        if destination.is_symlink():
            link_target = os.readlink(destination)
            os.symlink(link_target, backup)
            if not backup.is_symlink() or os.readlink(backup) != link_target:
                raise BuildError(f"Failed to verify archived symlink: {backup}")
            descriptor = f"symlink={link_target}"
        else:
            shutil.copy2(destination, backup, follow_symlinks=False)
            if not filecmp.cmp(destination, backup, shallow=False):
                raise BuildError(f"Failed to verify archived file: {backup}")
            digest = hashlib.sha512()
            with backup.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            descriptor = f"sha512={digest.hexdigest()}"

        source_text = str(source) if source is not None else "-"
        manifest_lines.append(
            f"{relative}\t{reason}\t{descriptor}\tsource={source_text}"
        )

    write_text(
        backup_root / "EXCLUSION-MANIFEST.txt",
        "\n".join(manifest_lines) + "\n",
        0o600,
    )

    print("XFCE CHECKPOINT: backup verified; removing candidate paths", flush=True)

    for destination in sorted(
        removals,
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        destination.unlink()

    remaining_removed_paths = [
        str(destination)
        for destination in removals
        if os.path.lexists(destination)
    ]
    if remaining_removed_paths:
        raise BuildError(
            "KDE/Plasma candidate paths remained after exclusion:\n  "
            + "\n  ".join(remaining_removed_paths[:40])
        )

    source_directories = sorted(
        (
            item
            for item in resolved_kde_prefix.rglob("*")
            if item.is_dir() and not item.is_symlink()
        ),
        key=lambda item: len(item.parts),
        reverse=True,
    )

    for source_dir in source_directories:
        destination_dir = root / "usr" / source_dir.relative_to(resolved_kde_prefix)
        try:
            destination_dir.rmdir()
        except (FileNotFoundError, OSError):
            pass

    print("XFCE CHECKPOINT: overlaying audited Xfce stage", flush=True)

    run(
        [
            "rsync",
            "-aHAX",
            "--force",
            "--numeric-ids",
            f"{xfce_stage}/",
            f"{root}/",
        ]
    )

    wayland_session = root / "usr/share/wayland-sessions/xfce-wayland.desktop"
    if wayland_session.is_file() or wayland_session.is_symlink():
        wayland_session.unlink()

    write_text(
        root / "etc/sddm.conf.d/30-hardened-xfce-session.conf",
        "[Autologin]\n"
        "User=hardened\n"
        "Session=xfce\n"
        "Relogin=false\n",
    )

    write_text(
        root / "var/lib/AccountsService/users/hardened",
        "[User]\n"
        "Session=xfce\n"
        "XSession=xfce\n"
        "SystemAccount=false\n",
        0o644,
    )

    write_text(
        root / "home/hardened/.dmrc",
        "[Desktop]\n"
        "Session=xfce\n",
        0o644,
    )

    print(f"KDE/PLASMA CANDIDATE PATHS EXCLUDED: {len(removals)}", flush=True)
    print(f"KDE/PLASMA EXCLUSION BACKUP: {backup_root}", flush=True)
    print(f"XFCE STAGE OVERLAY: {xfce_stage}", flush=True)
    print("XFCE SESSION: X11 (xfce.desktop)", flush=True)




def _repair_hardened_wayland_runtime(root: Path) -> None:
    # Finalize and verify the Xfce/SDDM runtime after live-root assembly.
    # GDM stays present in the tree (staged for the future beta release) but
    # explicitly disabled; SDDM is the active display manager for alpha.
    import os

    print("\n== Finalizing Xfce/SDDM runtime ==", flush=True)

    required = (
        root / "usr/bin/startxfce4",
        root / "usr/bin/xfce4-session",
        root / "usr/bin/xfce4-panel",
        root / "usr/bin/xfdesktop",
        root / "usr/bin/xfwm4",
        root / "usr/share/xsessions/xfce.desktop",
        root / "usr/lib/systemd/system/sddm.service",
    )

    for item in required:
        if not item.exists():
            raise BuildError(f"Required Xfce/SDDM runtime file is missing: {item}")

    forbidden_exact = (
        root / "usr/bin/startplasma-wayland",
        root / "usr/bin/startplasma-x11",
        root / "usr/bin/plasmashell",
        root / "usr/bin/kwin_wayland",
        root / "usr/bin/kwin_x11",
        root / "usr/share/wayland-sessions/plasma.desktop",
        root / "usr/share/xsessions/plasma.desktop",
        root / "usr/share/xsessions/plasmax11.desktop",
    )

    remaining = [str(item) for item in forbidden_exact if os.path.lexists(item)]
    if remaining:
        raise BuildError(
            "Plasma runtime paths remain in the Xfce candidate:\n  "
            + "\n  ".join(remaining)
        )

    dm_link = root / "etc/systemd/system/display-manager.service"
    dm_link.parent.mkdir(parents=True, exist_ok=True)

    if dm_link.is_symlink() or dm_link.is_file():
        dm_link.unlink()
    elif dm_link.exists():
        raise BuildError(
            f"Display-manager alias exists but is not a file/symlink: {dm_link}"
        )

    dm_link.symlink_to("/usr/lib/systemd/system/sddm.service")

    # GDM is required to be present in the tree (staged for the future beta
    # swap) but must stay explicitly disabled for this alpha build; SDDM is
    # the active display manager.
    gdm_symlink = root / "etc/systemd/system/multi-user.target.wants/gdm.service"
    if gdm_symlink.is_symlink() or gdm_symlink.is_file():
        gdm_symlink.unlink()

    write_text(
        root / "var/lib/AccountsService/users/hardened",
        "[User]\n"
        "Session=xfce\n"
        "XSession=xfce\n"
        "SystemAccount=false\n",
        0o644,
    )

    write_text(
        root / "home/hardened/.dmrc",
        "[Desktop]\n"
        "Session=xfce\n",
        0o644,
    )

    # Regenerate every merged/compiled metadata cache now that KDE files have
    # been excluded. These caches (glib schemas, desktop database, mime
    # database, icon theme cache) are single compiled blobs built by scanning
    # whatever source files were present at generation time; removing the
    # source .xml/.desktop files afterward does not retroactively purge
    # already-baked entries from the compiled cache. Left stale, they can
    # carry KDE-era GSettings/session metadata forward into an XFCE-only
    # build silently. Failures here are non-fatal (some caches may not exist
    # in a minimal build) but every attempt is logged.
    print("XFCE CHECKPOINT: regenerating metadata caches post-KDE-exclusion", flush=True)
    cache_regen_commands = (
        ["find", "/usr/share/glib-2.0/schemas", "-name", "gschemas.compiled", "-delete"],
        ["glib-compile-schemas", "/usr/share/glib-2.0/schemas"],
        ["update-desktop-database", "/usr/share/applications"],
        ["update-mime-database", "/usr/share/mime"],
        ["gtk-update-icon-cache", "-f", "/usr/share/icons/hicolor"],
    )
    for command in cache_regen_commands:
        run(["chroot", str(root), *command], check=False)

    print("DISPLAY MANAGER: SDDM (alpha)", flush=True)
    print("DISPLAY MANAGER: GDM present but explicitly disabled (staged for beta)", flush=True)
    print("DEFAULT LIVE SESSION: Xfce X11", flush=True)
    print("PLASMA RUNTIME AUDIT: ABSENT", flush=True)



def _configure_hardened_drm_trace(root: Path, release_channel: str) -> None:
    if release_channel == "stable":
        print(
            f"DRM/SESSION TRACE: skipped (release_channel={release_channel!r}, "
            "diagnostic tooling is alpha/beta only)",
            flush=True,
        )
        return

    write_text(
        root / "usr/local/libexec/hardened-drm-trace",
        DRM_TRACE_SCRIPT,
        0o755,
    )

    startxfce4 = root / "usr/bin/startxfce4"
    startxfce4_real = root / "usr/bin/startxfce4.real"
    if startxfce4.is_file() and not startxfce4_real.exists():
        startxfce4.rename(startxfce4_real)
        write_text(startxfce4, SESSION_TRACE_SCRIPT, 0o755)

    print(
        "DRM/SESSION TRACE: enabled (alpha/beta) -- "
        "log at /var/log/hardened-drm-trace.log",
        flush=True,
    )


def prepare_live_root(paths: BuildPaths, cfg: BuildConfig, kver: str) -> None:



    print("\n== Preparing live root ==")
    paths.live_root.mkdir(parents=True, exist_ok=True)
    run(
        [
            "rsync",
            "-aHAX",
            "--numeric-ids",
            "--delete",
            f"{paths.runtime_root}/",
            f"{paths.live_root}/",
        ]
    )

    # Found via a real question on 2026-08-03: runtime_root is a snapshot of
    # an actual live development system, not a blank template. That means
    # the real developer's home directory -- complete with whatever build
    # staging trees happened to be sitting in it at snapshot time -- comes
    # along for the ride on every single build, unless it is explicitly
    # stripped out. Only one narrow symlink under home/corbett/kde/usr was
    # ever being checked; the rest of home/corbett (including entire
    # xfce-source-stage copies) was never touched at all. This build must
    # never ship a real developer's username or their build artifacts.
    live_home = paths.live_root / "home"
    if live_home.is_dir():
        allowed_home_dirs = {"hardened"}
        stray_dirs = [
            entry for entry in live_home.iterdir()
            if entry.is_dir() and entry.name not in allowed_home_dirs
        ]
        for stray in stray_dirs:
            print(
                f"XFCE CHECKPOINT: removing stray developer home directory "
                f"inherited from runtime_root snapshot: {stray}",
                flush=True,
            )
            if stray.is_symlink():
                stray.unlink()
            else:
                shutil.rmtree(stray)

        remaining = sorted(p.name for p in live_home.iterdir() if p.is_dir())
        if remaining and remaining != ["hardened"]:
            raise BuildError(
                "Live root /home still contains unexpected entries after "
                f"stray-home cleanup: {remaining}. This build must only "
                "ever ship the hardened account's home directory."
            )
        print(
            f"XFCE CHECKPOINT: /home verified clean, contains only: {remaining}",
            flush=True,
        )

    # Found via a real question on 2026-08-03: the stray-home cleanup above
    # only ever removed the directory. The actual account definitions
    # (/etc/passwd, /etc/shadow, /etc/group) for the real developer's
    # username were never touched -- the same runtime_root snapshot that
    # contained the stray home directory also contains a real user/group
    # entry for it (uid/gid 1000 in this case), and that carries through
    # into every build. This shows up anywhere the system displays account
    # info: `id`, file ownership listings, user-switcher prompts, etc.
    # Confirmed via the live system's own account listing showing a real
    # developer username as both a UID and GID entry alongside "hardened".
    print("\n== Removing stray developer account entries ==", flush=True)

    expected_accounts = {"root", "hardened"}
    system_service_prefixes = (
        "systemd-", "dbus", "polkitd", "colord", "geoclue", "tss",
        "saned", "utmp", "nobody", "sync", "shutdown", "halt", "mail",
        "ftp", "http", "uuidd", "dhcpcd", "avahi", "rtkit", "pipewire",
        "sddm",
    )

    def _is_expected_account(name: str) -> bool:
        if name in expected_accounts:
            return True
        return any(name.startswith(prefix) for prefix in system_service_prefixes)

    passwd_file = root / "etc/passwd"
    group_file = root / "etc/group"
    shadow_file = root / "etc/shadow"

    stray_usernames: set[str] = set()

    if passwd_file.exists():
        lines = passwd_file.read_text().splitlines()
        kept_lines = []
        for line in lines:
            if not line.strip():
                continue
            name = line.split(":", 1)[0]
            uid_field = line.split(":")[2] if line.count(":") >= 2 else ""
            try:
                uid = int(uid_field)
            except ValueError:
                uid = None
            # Only ever consider real human-range accounts (uid >= 1000,
            # matching standard Arch convention) for removal. System/
            # service accounts below that range are never touched here.
            if uid is not None and uid >= 1000 and not _is_expected_account(name):
                stray_usernames.add(name)
                print(f"XFCE CHECKPOINT: removing stray account: {name} (uid {uid})", flush=True)
                continue
            kept_lines.append(line)
        passwd_file.write_text("\n".join(kept_lines) + "\n")

    if shadow_file.exists() and stray_usernames:
        lines = shadow_file.read_text().splitlines()
        kept_lines = [
            line for line in lines
            if line.strip() and line.split(":", 1)[0] not in stray_usernames
        ]
        shadow_file.write_text("\n".join(kept_lines) + "\n")

    if group_file.exists() and stray_usernames:
        lines = group_file.read_text().splitlines()
        kept_lines = [
            line for line in lines
            if not (line.strip() and line.split(":", 1)[0] in stray_usernames)
        ]
        group_file.write_text("\n".join(kept_lines) + "\n")

    if passwd_file.exists():
        remaining_names = {
            line.split(":", 1)[0]
            for line in passwd_file.read_text().splitlines()
            if line.strip()
        }
        remaining_human = {
            line.split(":", 1)[0]
            for line in passwd_file.read_text().splitlines()
            if line.strip() and line.count(":") >= 2
            and line.split(":")[2].isdigit()
            and int(line.split(":")[2]) >= 1000
        }
        unexpected_human = remaining_human - expected_accounts
        if unexpected_human:
            raise BuildError(
                "Unexpected non-service accounts remain in /etc/passwd "
                f"after stray-account cleanup: {unexpected_human}. This "
                "build must never ship a real developer's account."
            )
        print(
            f"XFCE CHECKPOINT: account list verified clean, human-range "
            f"accounts present: {sorted(remaining_human)}",
            flush=True,
        )

    print("\n== Staging universal GPU modules and firmware ==")

    module_candidates = [
        paths.initramfs_stage / "rootfs/usr/lib/modules" / kver,
        paths.initramfs_stage / "rootfs/lib/modules" / kver,
    ]
    modules_src = next((path for path in module_candidates if path.is_dir()), None)
    if modules_src is None:
        raise BuildError(
            "Kernel modules were not found for "
            + kver
            + " under "
            + str(paths.initramfs_stage / "rootfs")
        )

    modules_dst = paths.live_root / "usr/lib/modules" / kver
    modules_dst.mkdir(parents=True, exist_ok=True)
    run(
        [
            "rsync",
            "-aHAX",
            "--numeric-ids",
            "--delete",
            f"{modules_src}/",
            f"{modules_dst}/",
        ]
    )

    firmware_src = Path("/usr/lib/firmware")
    if not firmware_src.is_dir():
        raise BuildError("Host firmware tree is missing: /usr/lib/firmware")

    firmware_dst = paths.live_root / "usr/lib/firmware"
    firmware_dst.mkdir(parents=True, exist_ok=True)
    run(
        [
            "rsync",
            "-aHAX",
            "--numeric-ids",
            "--delete",
            f"{firmware_src}/",
            f"{firmware_dst}/",
        ]
    )

    depmod = shutil.which("depmod")
    if not depmod:
        raise BuildError("depmod is required to index the staged GPU modules")
    run([depmod, "-b", paths.live_root, kver])

    release = os_release(cfg.version, cfg.repo_url)
    write_text(paths.live_root / "usr/lib/os-release", release)
    etc_release = paths.live_root / "etc/os-release"
    if etc_release.exists() or etc_release.is_symlink():
        etc_release.unlink()
    etc_release.parent.mkdir(parents=True, exist_ok=True)
    os.symlink("../usr/lib/os-release", etc_release)
    write_text(paths.live_root / "etc/hardened-arch-release", cfg.version + "\n")

    config_repo = cfg.repo_url or "__REPO_PLACEHOLDER__"
    write_text(
        paths.live_root / "etc/hardened-arch/update.conf",
        f"MANIFEST_URL={shlex.quote(config_repo)}\n",
        0o600,
    )
    installer = INSTALLER_SCRIPT.replace("__KVER__", shlex.quote(kver))
    updater = UPDATE_SCRIPT.replace("__REPO_PLACEHOLDER__", shlex.quote("__REPO_PLACEHOLDER__"))
    write_text(paths.live_root / "usr/local/sbin/hardened-install", installer, 0o755)
    write_text(paths.live_root / "usr/local/sbin/hardened-update", updater, 0o755)
    write_text(paths.live_root / "usr/local/sbin/hardened-verify", VERIFY_SCRIPT, 0o755)
    systemd_units(paths.live_root)

    # Live systems must not attempt to mount a previously configured physical root.
    write_text(paths.live_root / "etc/fstab", "# Populated by the disk installer.\n")
    machine_id = paths.live_root / "etc/machine-id"
    if machine_id.exists() or machine_id.is_symlink():
        machine_id.unlink()
    machine_id.touch(mode=0o444)

    for name in LIVE_TOOLS:
        stage_host_tool(paths.live_root, name)

    # TLS trust for SourceForge update checks.
    # Use rsync because the runtime root may already contain certificate
    # symlinks that shutil.copytree cannot safely overwrite.
    for src in [Path("/etc/ssl/certs"), Path("/etc/ca-certificates"), Path("/usr/share/ca-certificates")]:
        if src.exists():
            dst = paths.live_root / src.relative_to("/")
            dst.mkdir(parents=True, exist_ok=True)
            run([
                "rsync",
                "-aHAX",
                "--delete",
                f"{src}/",
                f"{dst}/",
            ])

    _configure_hardened_live_desktop(paths.live_root, cfg.public_build)
    _configure_hardened_wayland_login(paths.live_root)
    _repair_hardened_wayland_runtime(paths.live_root)
    _configure_hardened_drm_trace(paths.live_root, cfg.release_channel)

    drm_trace_script_path = paths.live_root / "usr/local/libexec/hardened-drm-trace"
    if cfg.release_channel == "stable":
        if drm_trace_script_path.exists():
            raise BuildError(
                "DRM trace script exists on a stable-channel build; it should "
                "have been skipped entirely. _configure_hardened_drm_trace "
                "wrote something it should not have for this release channel."
            )
    else:
        if not drm_trace_script_path.exists():
            raise BuildError(
                "hardened-drm-trace script is missing after "
                "_configure_hardened_drm_trace ran for a non-stable release "
                "channel. It should always be written for alpha/beta builds; "
                "something is silently failing before or during that write."
            )

    _configure_hardened_pacman_repos(paths.live_root)

    for required_pacman_file in (
        "etc/pacman.conf",
        "etc/pacman.d/mirrorlist",
        "etc/pacman.d/blackarch-mirrorlist",
    ):
        candidate = paths.live_root / required_pacman_file
        if not candidate.exists():
            raise BuildError(
                f"Required pacman repo config is missing after "
                f"_configure_hardened_pacman_repos ran: {candidate}"
            )

    _configure_hardened_plymouth(paths.live_root)

    # Prepare graphical live boot and networking. Failures are non-fatal because
    # some custom roots use alternate unit names.
    run(["systemctl", f"--root={paths.live_root}", "set-default", "graphical.target"], check=False)
    run(
        ["systemctl", f"--root={paths.live_root}", "enable", "sddm.service"],
        check=False,
    )
    if (paths.live_root / "usr/local/libexec/hardened-drm-trace").exists():
        # systemctl --root= enable proved unreliable here for reasons not yet
        # understood (unit and script both exist, but no wants symlink was
        # ever created). Creating the symlink directly is deterministic and
        # matches the same approach already used for display-manager.service
        # in _repair_hardened_wayland_runtime, rather than trusting a
        # subprocess call's side effect in an offline --root context.
        wants_dir = paths.live_root / "usr/lib/systemd/system/graphical.target.wants"
        wants_dir.mkdir(parents=True, exist_ok=True)
        drm_trace_link = wants_dir / "hardened-drm-trace.service"
        if drm_trace_link.is_symlink() or drm_trace_link.exists():
            drm_trace_link.unlink()
        drm_trace_link.symlink_to("../hardened-drm-trace.service")
        if not drm_trace_link.is_symlink():
            raise BuildError(
                f"Failed to create hardened-drm-trace.service wants symlink: {drm_trace_link}"
            )
        target = paths.live_root / "usr/lib/systemd/system/hardened-drm-trace.service"
        if not target.exists():
            raise BuildError(
                f"hardened-drm-trace.service wants symlink points at a missing unit: {target}"
            )
    run(
        ["systemctl", f"--root={paths.live_root}", "disable", "gdm.service"],
        check=False,
    )
    for unit in (
        "systemd-networkd.service",
        "systemd-resolved.service",
    ):
        run(
            ["systemctl", f"--root={paths.live_root}", "enable", unit],
            check=False,
        )
    if (paths.live_root / "usr/lib/systemd/system/systemd-resolved.service").exists():
        resolv = paths.live_root / "etc/resolv.conf"
        if resolv.exists() or resolv.is_symlink():
            resolv.unlink()
        os.symlink("../run/systemd/resolve/stub-resolv.conf", resolv)

    # Syntax-check scripts before packing them.
    run(["bash", "-n", paths.live_root / "usr/local/sbin/hardened-install"])
    run(["bash", "-n", paths.live_root / "usr/local/sbin/hardened-update"])
    run(["bash", "-n", paths.live_root / "usr/local/sbin/hardened-verify"])
    if (paths.live_root / "usr/local/libexec/hardened-drm-trace").exists():
        run(["bash", "-n", paths.live_root / "usr/local/libexec/hardened-drm-trace"])
    if (paths.live_root / "usr/bin/startxfce4.real").exists():
        run(["bash", "-n", paths.live_root / "usr/bin/startxfce4"])


def prepare_live_initramfs(paths: BuildPaths, cfg: BuildConfig) -> None:
    print("\n== Building live initramfs ==")
    base = paths.initramfs_stage / "initramfs"
    if not base.is_dir():
        raise BuildError(f"Initramfs source tree not found: {base}")
    run(["rsync", "-aHAX", "--delete", f"{base}/", f"{paths.initramfs_root}/"])
    _stage_hardened_plymouth(paths.live_root, paths.initramfs_root)
    ensure_toybox_applets(paths.initramfs_root)
    init_script = LIVE_INIT.replace("__ISO_LABEL__", cfg.volume_label)
    if cfg.release_channel == "stable":
        init_script = init_script.replace(
            "hardened.debug=1) DEBUG_SHELL=1 ;;",
            "hardened.debug=1) DEBUG_SHELL=0 ;;",
        )
        init_script = init_script.replace(
            "rd.shell|rd.shell=1) DEBUG_SHELL=1 ;;",
            "rd.shell|rd.shell=1) DEBUG_SHELL=0 ;;",
        )
        init_script = init_script.replace(
            'if [ "$DEBUG_SHELL" -eq 1 ]; then\n'
            '    log "debug shell requested; type exit to continue into systemd"\n'
            "    sh\n"
            "fi",
            "# Pre-handoff rescue shell is disabled for stable releases.",
        )
        if "exec sh" in init_script:
            raise BuildError("Stable live initramfs still contains an exec sh path")
    write_text(paths.initramfs_root / "init", init_script, 0o755)
    run(["sh", "-n", paths.initramfs_root / "init"])

    cmd = (
        "find . -print0 | cpio --null -o --format=newc --owner=0:0 "
        f"| zstd -T0 -19 -f -o {shlex.quote(str(paths.live_initrd))}"
    )
    run(["bash", "-o", "pipefail", "-c", cmd], cwd=paths.initramfs_root)
    run(["zstd", "-t", paths.live_initrd])


def copy_theme(theme_src: Path, dst: Path) -> None:
    if not theme_src.is_dir():
        raise BuildError(f"Theme directory not found: {theme_src}")
    shutil.copytree(theme_src, dst, symlinks=True, dirs_exist_ok=True)



def limine_config(kver: str, label: str) -> str:
    kernel_path = f"boot():/EFI/Linux/vmlinuz-{kver}.efi"
    initrd_path = f"boot():/EFI/Linux/initramfs-live-{kver}.img.zst"
    common = f"iso_label={label} rootfstype=btrfs rd.shell=0"
    return f"""timeout: 8
editor_enabled: no
graphics: yes
wallpaper: boot():/EFI/BOOT/limine-bg.png

/Hardened Arch Linux
    protocol: linux
    path: {kernel_path}
    module_path: {initrd_path}
    cmdline: {common} hardened.mode=live systemd.unit=graphical.target quiet splash rd.plymouth=1 loglevel=3 systemd.show_status=false

/Install Hardened Arch Linux (Qt)
    protocol: linux
    path: {kernel_path}
    module_path: {initrd_path}
    cmdline: {common} hardened.mode=install systemd.unit=hardened-installer.target quiet splash rd.plymouth=1 loglevel=3 systemd.show_status=false

/Software Update Fetcher (Qt)
    protocol: linux
    path: {kernel_path}
    module_path: {initrd_path}
    cmdline: {common} hardened.mode=update systemd.unit=hardened-update.target quiet splash rd.plymouth=1 loglevel=3 systemd.show_status=false

/Verify Installation Media (SHA-512)
    protocol: linux
    path: {kernel_path}
    module_path: {initrd_path}
    cmdline: {common} hardened.mode=verify systemd.unit=hardened-verify.target loglevel=4 systemd.show_status=yes

/Recovery / Debug Console (password required)
    protocol: linux
    path: {kernel_path}
    module_path: {initrd_path}
    cmdline: {common} hardened.mode=recovery systemd.unit=hardened-recovery.target loglevel=7 systemd.log_level=debug systemd.show_status=yes
"""


def find_limine_efi(limine_dir: Path) -> Path:
    candidates = (
        limine_dir / "BOOTX64.EFI",
        limine_dir / "bootx64.efi",
        limine_dir / "limine_x64.efi",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise BuildError(
        "Source-built Limine UEFI binary was not found. Expected one of:\n  "
        + "\n  ".join(str(path) for path in candidates)
    )


def find_limine_background(theme_dir: Path) -> Path:
    candidates = (
        theme_dir / "limine-bg.png",
        theme_dir / "refind_bg.png",
        theme_dir / "background.png",
        theme_dir / "refind_bg_png",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise BuildError(
        "No Hardened Arch boot background was found. Expected limine-bg.png "
        "or refind_bg.png in " + str(theme_dir)
    )


def prepare_esp(paths: BuildPaths, cfg: BuildConfig, kver: str, kernel: Path, installed_initrd: Path) -> None:
    print("\n== Preparing source-built Limine ESP ==")
    efi_boot = paths.esp_root / "EFI/BOOT"
    efi_linux = paths.esp_root / "EFI/Linux"
    for directory in (efi_boot, efi_linux):
        directory.mkdir(parents=True, exist_ok=True)

    limine_efi = find_limine_efi(paths.limine_dir)
    background = find_limine_background(paths.theme_dir)

    copy_file(limine_efi, efi_boot / "BOOTX64.EFI")
    copy_file(background, efi_boot / "limine-bg.png")
    write_text(efi_boot / "limine.conf", limine_config(kver, cfg.volume_label))

    source_record = paths.limine_dir / "source-build.txt"
    if source_record.is_file():
        copy_file(source_record, efi_boot / "limine-source-build.txt")

    copy_file(kernel, efi_linux / f"vmlinuz-{kver}.efi")
    copy_file(paths.live_initrd, efi_linux / f"initramfs-live-{kver}.img.zst")
    copy_file(installed_initrd, efi_linux / f"initramfs-{kver}.img.zst")

    # Put the exact installable ESP tree in the live root.
    live_efi = paths.live_root / "boot/EFI"
    if live_efi.exists():
        shutil.rmtree(live_efi)
    shutil.copytree(paths.esp_root / "EFI", live_efi, symlinks=True)


def build_squashfs(paths: BuildPaths, cfg: BuildConfig) -> Path:
    print("\n== Building SquashFS live root ==")
    out = paths.iso_root / "hardened/rootfs.sfs"
    out.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "mksquashfs",
            paths.live_root,
            out,
            "-noappend",
            "-comp",
            "zstd",
            "-Xcompression-level",
            "15",
            "-b",
            "1M",
            "-processors",
            str(cfg.jobs),
        ]
    )
    return out


def make_esp_image(paths: BuildPaths) -> None:
    print("\n== Building FAT32 EFI System Partition image ==")
    size_bytes = sum(p.stat().st_size for p in paths.esp_root.rglob("*") if p.is_file())
    # Leave ample room for firmware FAT implementations and future additions.
    size_mib = max(128, ((size_bytes * 3 + (1024**2 - 1)) // 1024**2 + 31) // 32 * 32)
    run(["truncate", "-s", f"{size_mib}M", paths.esp_image])
    run(["mkfs.fat", "-F", "32", "-n", "HARDENEFI", paths.esp_image])
    run(["mcopy", "-i", paths.esp_image, "-s", paths.esp_root / "EFI", "::/"])


def build_iso(paths: BuildPaths, cfg: BuildConfig, kver: str, rootfs_sfs: Path) -> None:
    print("\n== Building hybrid UEFI ISO ==")
    # A visible mirror is useful to the installer and for manual inspection.
    iso_efi = paths.iso_root / "EFI"
    if iso_efi.exists():
        shutil.rmtree(iso_efi)
    shutil.copytree(paths.esp_root / "EFI", iso_efi, symlinks=True)

    # Store the ESP as a normal ISO file for reliable El Torito UEFI
    # optical booting in QEMU, VirtualBox, and physical firmware.
    iso_boot = paths.iso_root / "boot"
    iso_boot.mkdir(parents=True, exist_ok=True)
    iso_esp_image = iso_boot / "efiboot.img"
    shutil.copy2(paths.esp_image, iso_esp_image)

    manifest = {
        "schema": 1,
        "name": "Hardened Arch Linux",
        "version": cfg.version,
        "architecture": "x86_64",
        "kernel": kver,
        "release_channel": cfg.release_channel,
        "initramfs_debug_shell": False,
        "volume_label": cfg.volume_label,
        "built_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "rootfs_sha512": sha512_file(rootfs_sfs),
        "update_manifest_url": cfg.repo_url or None,
    }
    write_text(paths.iso_root / "hardened/build-manifest.json", json.dumps(manifest, indent=2) + "\n")

    paths.output.parent.mkdir(parents=True, exist_ok=True)
    if paths.output.exists():
        if not cfg.force:
            raise BuildError(f"Output already exists (use --force): {paths.output}")
        paths.output.unlink()

    run(
        [
            "xorriso",
            "-as",
            "mkisofs",
            "-iso-level",
            "3",
            "-full-iso9660-filenames",
            "-volid",
            cfg.volume_label,
            "-output",
            paths.output,
            "-e",
            "boot/efiboot.img",
            "-no-emul-boot",
            "-append_partition",
            "2",
            "0xef",
            paths.esp_image,
            "-appended_part_as_gpt",
            paths.iso_root,
        ]
    )

    run(
        [
            "xorriso",
            "-indev",
            paths.output,
            "-report_el_torito",
            "plain",
            "-report_system_area",
            "plain",
        ]
    )
    checksum = sha512_file(paths.output)
    write_text(paths.output.with_suffix(paths.output.suffix + ".sha512"), f"{checksum}  {paths.output.name}\n")


def sourceforge_manifest_example(paths: BuildPaths, cfg: BuildConfig) -> Path:
    example = paths.output.with_name("sourceforge-manifest.example.json")
    data = {
        "schema": 1,
        "channel": cfg.release_channel,
        "initramfs_debug_shell": False,
        "version": cfg.version,
        "published": dt.datetime.now(dt.timezone.utc).isoformat(),
        "notes_url": "https://sourceforge.net/projects/YOUR-PROJECT/files/",
        "iso": {
            "filename": paths.output.name,
            "url": f"https://sourceforge.net/projects/YOUR-PROJECT/files/releases/{cfg.version}/{paths.output.name}/download",
            "sha512": sha512_file(paths.output),
            "size": paths.output.stat().st_size,
        },
    }
    write_text(example, json.dumps(data, indent=2) + "\n")
    return example


def chown_artifacts(paths: Iterable[Path], home: Path) -> None:
    try:
        st = home.stat()
    except OSError:
        return
    for path in paths:
        try:
            os.chown(path, st.st_uid, st.st_gid)
            path.chmod(0o644)
        except OSError:
            pass


def parse_args() -> argparse.Namespace:
    home = original_user_home()
    parser = argparse.ArgumentParser(
        description="Build the Hardened Arch UEFI live/install/update ISO.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--home", type=Path, default=home)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--kernel-source", type=Path)
    parser.add_argument("--kernel-build", type=Path)
    parser.add_argument("--kernel-artifacts", type=Path)
    parser.add_argument("--initramfs-stage", type=Path)
    parser.add_argument("--theme-dir", type=Path)
    parser.add_argument("--limine-dir", type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--version", default="1.10-alpha")
    parser.add_argument(
        "--release-channel",
        choices=("alpha", "beta", "stable"),
        default="alpha",
        help="alpha/beta keep the pre-handoff rescue shell; stable disables it",
    )
    parser.add_argument(
        "--repo-url",
        default="https://sourceforge.net/projects/hardened-software-update/files/updates/manifest.json/download",
        help="HTTPS URL of the SourceForge manifest.json download endpoint. "
        "Edit the default above once the real SourceForge project slug is known; "
        "the build refuses to proceed with the placeholder or an empty value.",
    )
    parser.add_argument("--volume-label", default="HARDENED_ARCH")
    parser.add_argument("--jobs", type=int, default=max(1, min(4, os.cpu_count() or 1)))
    parser.add_argument("--install-tools", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--public-build",
        action="store_true",
        help="Force the live/hardened account's password to expire immediately, "
        "requiring a fresh password to be set on first login, instead of "
        "shipping the hardened:hardened convenience credential. Use this for "
        "any ISO that will be posted publicly; leave it off for personal "
        "working copies.",
    )
    parser.add_argument("--keep-work", action="store_true")
    return parser.parse_args()


def build_paths(args: argparse.Namespace) -> BuildPaths:
    home = args.home.resolve()
    runtime_root = (args.runtime_root or Path('/home/corbett/xorg-source-stage/rootfs')).resolve()
    kernel_src = (args.kernel_source or home / "linux-7.1.2").resolve()
    kernel_build = (args.kernel_build or home / "linux-7.1.2-build").resolve()
    kernel_artifacts = (args.kernel_artifacts or home / "linux-7.1.2-artifacts").resolve()
    initramfs_stage = (args.initramfs_stage or home / "linux-7.1.2-stage").resolve()
    theme_dir = (args.theme_dir or home / "boot-theme-build/refind/hardened-arch").resolve()
    limine_dir = (args.limine_dir or Path('/home/corbett/bootloader-build/limine')).resolve()
    work = (args.work_dir or home / "hardened-arch-iso-build").resolve()
    output = (args.output or home / f"hardened-arch-{args.version}-x86_64.iso").resolve()
    return BuildPaths(
        home=home,
        runtime_root=runtime_root,
        kernel_src=kernel_src,
        kernel_build=kernel_build,
        kernel_artifacts=kernel_artifacts,
        initramfs_stage=initramfs_stage,
        theme_dir=theme_dir,
        limine_dir=limine_dir,
        work=work,
        output=output,
    )


def validate_inputs(paths: BuildPaths, cfg: BuildConfig, kver: str) -> tuple[Path, Path]:
    required_dirs = [
        paths.runtime_root,
        paths.kernel_src,
        paths.kernel_build,
        paths.kernel_artifacts,
        paths.initramfs_stage,
        paths.theme_dir,
        paths.limine_dir,
    ]
    missing_dirs = [str(p) for p in required_dirs if not p.is_dir()]
    if missing_dirs:
        raise BuildError("Missing required directories:\n  " + "\n  ".join(missing_dirs))
    if not re.fullmatch(r"[A-Z0-9_]{1,32}", cfg.volume_label):
        raise BuildError("--volume-label must use only A-Z, 0-9, underscore, maximum 32 characters.")
    if not cfg.repo_url or "CHANGE-ME" in cfg.repo_url:
        raise BuildError(
            "--repo-url is not configured (still the placeholder or empty). "
            "Set the real SourceForge manifest.json URL, either by editing the "
            "default in parse_args() or passing --repo-url explicitly."
        )
    if not cfg.repo_url.startswith("https://"):
        raise BuildError("--repo-url must be HTTPS.")

    kernel = paths.kernel_build / "arch/x86/boot/bzImage"
    installed_initrd = paths.kernel_artifacts / f"initramfs-{kver}.img.zst"
    if not kernel.is_file():
        raise BuildError(f"Bootable kernel not found: {kernel}")
    if not installed_initrd.is_file():
        raise BuildError(f"Installed-system initramfs not found: {installed_initrd}")
    check_kernel_config(paths.kernel_build / ".config")
    return kernel, installed_initrd




HARDENED_QT_PAYLOAD_ROOT = Path("/home/corbett/hardened-qt-tools/install")


def _harden_root_account_in_tree(root: Path) -> None:
    shadow = root / "etc/shadow"
    if shadow.is_file():
        lines = shadow.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
        rewritten = []
        found = False
        for line in lines:
            fields = line.split(":")
            if fields and fields[0] == "root":
                found = True
                if len(fields) < 2:
                    fields.append("!")
                elif fields[1] == "":
                    fields[1] = "!"
                line = ":".join(fields)
            rewritten.append(line)
        if not found:
            raise BuildError(f"root account is missing from {shadow}")
        shadow.write_text("\n".join(rewritten) + "\n", encoding="utf-8", errors="surrogateescape")
        shadow.chmod(0o600)

    systemd_dir = root / "etc/systemd/system"
    if systemd_dir.exists():
        for path in systemd_dir.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            data = path.read_text(encoding="utf-8", errors="ignore")
            # Fixed 2026-08-03: this previously matched the bare substring
            # "agetty --autologin" with no username check at all, meaning
            # it would silently delete ANY autologin config -- including
            # the legitimate hardened-user TTY autologin added this same
            # session -- not just root autologin, which is what this
            # function is actually supposed to be hardening against. Now
            # scoped specifically to root.
            if "--autologin root" in data:
                path.unlink()


def _patch_hardened_live_backends(root: Path) -> None:
    update = root / "usr/local/sbin/hardened-update"
    if update.is_file():
        data = update.read_text(encoding="utf-8", errors="surrogateescape")
        for old, new in (
            ("sha256sum", "sha512sum"),
            ("sha256_file", "sha512_file"),
            ("sha256", "sha512"),
            ("SHA-256", "SHA-512"),
            ("SHA256", "SHA512"),
            (".sha256", ".sha512"),
        ):
            data = data.replace(old, new)
        update.write_text(data, encoding="utf-8", errors="surrogateescape")
        update.chmod(0o755)

    installer = root / "usr/local/sbin/hardened-install"
    if not installer.is_file():
        raise BuildError(f"installer backend is missing: {installer}")
    data = installer.read_text(encoding="utf-8", errors="surrogateescape")
    marker = 'rm -f "$TARGET_MNT/usr/local/sbin/hardened-install"'
    transfer_marker = "HARDENED_ROOT_PASSWORD_TRANSFER"
    transfer_block = r'''# HARDENED_ROOT_PASSWORD_TRANSFER
if [[ -s /run/hardened-live/root-password.hash && -f "$TARGET_MNT/etc/shadow" ]]; then
    root_hash=$(cat /run/hardened-live/root-password.hash)
    shadow_tmp="$TARGET_MNT/etc/.shadow.hardened-new"
    awk -F: -v OFS=: -v hash="$root_hash" '
        $1 == "root" { $2 = hash }
        { print }
    ' "$TARGET_MNT/etc/shadow" > "$shadow_tmp"
    chmod 0600 "$shadow_tmp"
    chown 0:0 "$shadow_tmp"
    mv -f "$shadow_tmp" "$TARGET_MNT/etc/shadow"
    echo "Installed system root password configured."
else
    echo "ERROR: no authenticated root password was supplied by the Qt installer." >&2
    exit 1
fi'''
    if transfer_marker not in data:
        if marker not in data:
            raise BuildError("could not locate installer cleanup marker for root-password transfer")
        data = data.replace(marker, transfer_block + "\n\n" + marker, 1)
        installer.write_text(data, encoding="utf-8", errors="surrogateescape")
        installer.chmod(0o755)


def install_hardened_qt_security_payload(live_root: Path, cfg: BuildConfig) -> None:
    payload = HARDENED_QT_PAYLOAD_ROOT
    if not (payload / "usr/local/bin/hardened-live-qt").is_file():
        raise BuildError(f"missing prebuilt Qt payload: {payload}")

    # Make repeated --keep-work ISO builds idempotent. shutil.copytree cannot
    # replace an existing symlink even with dirs_exist_ok=True.
    for relative in (
        "usr/local/bin/hardened-installer-qt",
        "usr/local/bin/hardened-update-qt",
    ):
        destination = live_root / relative
        if destination.is_symlink() or destination.is_file():
            destination.unlink()

    for top in ("usr", "etc"):
        source = payload / top
        if source.exists():
            shutil.copytree(source, live_root / top, dirs_exist_ok=True, symlinks=True)

    update_url = cfg.repo_url or ""
    if update_url and not update_url.lower().startswith("https://"):
        raise BuildError("the update manifest URL must use HTTPS")
    write_text(
        live_root / "etc/hardened-arch/update.conf",
        f"MANIFEST_URL={update_url}\n",
        mode=0o600,
    )
    _harden_root_account_in_tree(live_root)
    _patch_hardened_live_backends(live_root)


def verify_iso_sha512(output: Path) -> None:
    sidecar = output.with_suffix(output.suffix + ".sha512")
    if not sidecar.is_file():
        raise BuildError(f"missing SHA-512 sidecar: {sidecar}")
    fields = sidecar.read_text(encoding="utf-8").strip().split()
    if not fields or len(fields[0]) != 128:
        raise BuildError(f"invalid SHA-512 sidecar: {sidecar}")
    expected = fields[0].lower()
    actual = sha512_file(output).lower()
    if expected != actual:
        raise BuildError(
            f"ISO SHA-512 verification failed: expected {expected}, got {actual}"
        )
    print(f"ISO SHA-512 VERIFIED: {output}")


def main() -> int:
    args = parse_args()
    paths = build_paths(args)
    cfg = BuildConfig(
        version=args.version,
        release_channel=args.release_channel,
        repo_url=args.repo_url,
        volume_label=args.volume_label,
        jobs=max(1, args.jobs),
        force=args.force,
        keep_work=args.keep_work,
        public_build=args.public_build,
    )
    try:
        require_root()
        if args.install_tools:
            install_host_tools()
        check_host_tools()
        kver = kernel_release(paths.kernel_src, paths.kernel_build)
        kernel, installed_initrd = validate_inputs(paths, cfg, kver)

        print("\nHardened Arch ISO build")
        print(f"  Home:          {paths.home}")
        print(f"  Runtime root:  {paths.runtime_root}")
        print(f"  Kernel:        {kernel}")
        print(f"  Kernel release:{kver}")
        print(f"  Channel:       {cfg.release_channel}")
        print("  Debug shell:   disabled")
        print(f"  Work:          {paths.work}")
        print(f"  Output:        {paths.output}")
        print(f"  Update repo:   {cfg.repo_url or '(not configured yet)'}")

        safe_remove_tree(paths.work, paths.home)
        paths.work.mkdir(parents=True)
        paths.iso_root.mkdir(parents=True)
        paths.esp_root.mkdir(parents=True)

        prepare_live_root(paths, cfg, kver)
        install_hardened_qt_security_payload(paths.live_root, cfg)
        prepare_live_initramfs(paths, cfg)
        prepare_esp(paths, cfg, kver, kernel, installed_initrd)
        rootfs_sfs = build_squashfs(paths, cfg)
        make_esp_image(paths)
        build_iso(paths, cfg, kver, rootfs_sfs)
        verify_iso_sha512(paths.output)
        manifest_example = sourceforge_manifest_example(paths, cfg)
        chown_artifacts(
            [paths.output, paths.output.with_suffix(paths.output.suffix + ".sha512"), manifest_example],
            paths.home,
        )

        iso_size = paths.output.stat().st_size
        print("\nBUILD COMPLETE")
        print(f"ISO:             {paths.output}")
        print(f"ISO size:        {iso_size / (1024**3):.2f} GiB")
        print(f"SHA-512 file:    {paths.output}.sha512")
        print(f"Manifest sample: {manifest_example}")
        print("\nWrite the ISO to the whole USB device, not a partition.")
        print("Example: sudo dd if=IMAGE.iso of=/dev/sdX bs=16M status=progress oflag=sync")
        print("Secure Boot must remain disabled until the EFI binaries are signed and trusted.")

        if not cfg.keep_work:
            safe_remove_tree(paths.work, paths.home)
        return 0
    except (BuildError, subprocess.CalledProcessError, OSError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        print(f"Work directory retained for inspection: {paths.work}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
