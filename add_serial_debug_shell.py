#!/usr/bin/env python3
"""
Add a test-only unauthenticated root shell on ttyS0 and rebuild the ISO.

This does NOT replace or modify the normal login entry. It adds a separate
systemd-boot entry whose kernel command line contains:

    hardened.debug_shell=1 selinux=0

Only that entry starts the direct serial root shell. The normal boot entries
continue to use agetty/login/PAM.

Close QEMU before running:

    sudo python3 /home/corbett/add_serial_debug_shell.py
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


HOME = Path("/home/corbett")
ISO_ROOT = HOME / "iso-systemd"
ROOTFS = ISO_ROOT / "rootfs.ext2"
EFI_IMAGE = ISO_ROOT / "efiboot.img"
ISO_BUILDER = HOME / "build_hardened_iso.py"
KERNEL_VERSION = "7.1.2"

ENTRY_NAME = "hardened-debug-shell.conf"
ENTRY_TITLE = "Hardened Arch TEST Root Shell on ttyS0"
SERVICE_NAME = "hardened-serial-debug-shell.service"


def die(message: str, code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def run(
    args: list[str],
    *,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    print("+", " ".join(str(item) for item in args))
    try:
        return subprocess.run(
            [str(item) for item in args],
            check=check,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
        )
    except subprocess.CalledProcessError as exc:
        if capture and exc.stdout:
            print(exc.stdout, file=sys.stderr)
        die(f"Command failed ({exc.returncode}): {' '.join(map(str, args))}")


def require_root() -> None:
    if os.geteuid() != 0:
        die("Run this script with sudo.")


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        die(f"Required command not found: {name}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def associated_loops(image: Path) -> list[str]:
    result = run(
        [
            "losetup",
            "--noheadings",
            "--output",
            "NAME",
            "--associated",
            str(image),
        ],
        check=False,
        capture=True,
    )
    return sorted(
        {
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip().startswith("/dev/loop")
        }
    )


def clear_stale_loops(image: Path) -> None:
    for loop in associated_loops(image):
        targets_result = run(
            ["findmnt", "-rn", "-S", loop, "-o", "TARGET"],
            check=False,
            capture=True,
        )
        targets = sorted(
            {
                line.strip()
                for line in targets_result.stdout.splitlines()
                if line.strip()
            },
            key=len,
            reverse=True,
        )

        for target in targets:
            result = run(["umount", target], check=False, capture=True)
            if result.returncode != 0:
                die(
                    f"Could not unmount stale rootfs mount {target}:\n"
                    + result.stdout
                )

        run(["losetup", "--detach", loop], check=False, capture=True)

    remaining = associated_loops(image)
    if remaining:
        die("rootfs.ext2 is still attached to: " + ", ".join(remaining))


def choose_shell(root: Path) -> tuple[str, str]:
    bash = root / "bin/bash"
    if bash.is_file() and os.access(bash, os.X_OK):
        return "/bin/bash", "--noprofile --norc"

    shell = root / "bin/sh"
    if shell.is_file() and os.access(shell, os.X_OK):
        return "/bin/sh", ""

    die("Neither /bin/bash nor /bin/sh is executable in the target root.")
    raise AssertionError


def install_debug_units(root: Path) -> None:
    shell, shell_args = choose_shell(root)

    unit = root / "etc/systemd/system" / SERVICE_NAME
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        "[Unit]\n"
        "Description=Hardened Arch TEST unauthenticated serial root shell\n"
        "ConditionKernelCommandLine=hardened.debug_shell=1\n"
        "After=systemd-remount-fs.service local-fs.target\n"
        "Before=getty.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "User=root\n"
        "Environment=HOME=/root\n"
        "Environment=TERM=linux\n"
        "Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n"
        f"ExecStart={shell}{(' ' + shell_args) if shell_args else ''}\n"
        "StandardInput=tty-force\n"
        "StandardOutput=tty\n"
        "StandardError=tty\n"
        "TTYPath=/dev/ttyS0\n"
        "TTYReset=yes\n"
        "TTYVHangup=yes\n"
        "TTYVTDisallocate=no\n"
        "Restart=always\n"
        "RestartSec=1\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n",
        encoding="utf-8",
        newline="\n",
    )
    unit.chmod(0o644)

    wants = root / "etc/systemd/system/multi-user.target.wants"
    wants.mkdir(parents=True, exist_ok=True)
    link = wants / SERVICE_NAME
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to("../" + SERVICE_NAME)

    # Suppress the normal ttyS0 login service only on the explicit debug-shell
    # kernel command line. Normal boots are unaffected.
    dropin = (
        root
        / "etc/systemd/system/serial-getty@ttyS0.service.d"
        / "90-hardened-debug-shell.conf"
    )
    dropin.parent.mkdir(parents=True, exist_ok=True)
    dropin.write_text(
        "[Unit]\n"
        "ConditionKernelCommandLine=!hardened.debug_shell=1\n",
        encoding="utf-8",
        newline="\n",
    )
    dropin.chmod(0o644)

    marker = root / "etc/hardened-debug-shell-warning"
    marker.write_text(
        "TEST-ONLY unauthenticated root shell is available when the kernel\n"
        "command line contains hardened.debug_shell=1. Remove this service,\n"
        "drop-in, loader entry, and enablement link before a release build.\n",
        encoding="utf-8",
        newline="\n",
    )
    marker.chmod(0o600)

    print(f"Installed {SERVICE_NAME} using {shell}.")
    print("Normal serial-getty remains enabled for every non-debug boot.")


def write_loader_entry() -> Path:
    candidates = (
        ISO_ROOT / "loader/entries/hardened-debug.conf",
        ISO_ROOT / "loader/entries/hardened-selinux-off.conf",
        ISO_ROOT / "loader/entries/hardened.conf",
    )
    source = next((path for path in candidates if path.is_file()), None)
    if source is None:
        die("No existing loader entry was found to clone.")

    text = source.read_text(encoding="utf-8", errors="strict")
    output: list[str] = []
    saw_title = False
    saw_options = False

    for raw in text.splitlines():
        stripped = raw.strip()

        if stripped.startswith("title "):
            output.append(f"title {ENTRY_TITLE}")
            saw_title = True
            continue

        if stripped.startswith("options "):
            tokens = stripped[len("options "):].split()

            # Remove older values that conflict with this explicit test entry.
            filtered = [
                token
                for token in tokens
                if token not in {
                    "selinux=0",
                    "enforcing=0",
                    "enforcing=1",
                    "hardened.debug_shell=1",
                    "quiet",
                    "splash",
                }
            ]

            filtered.extend(
                [
                    "hardened.debug_shell=1",
                    "selinux=0",
                    "systemd.unit=multi-user.target",
                    "loglevel=7",
                    "systemd.show_status=yes",
                ]
            )

            deduplicated: list[str] = []
            for token in filtered:
                if token not in deduplicated:
                    deduplicated.append(token)

            output.append("options " + " ".join(deduplicated))
            saw_options = True
            continue

        output.append(raw)

    if not saw_title:
        output.insert(0, f"title {ENTRY_TITLE}")

    if not saw_options:
        output.append(
            "options rw console=tty0 console=ttyS0,115200 "
            "hardened.rootfs=/rootfs.ext2 hardened.overlay=tmpfs "
            "hardened.mode=debug hardened.debug_shell=1 selinux=0 "
            "systemd.unit=multi-user.target loglevel=7 "
            "systemd.show_status=yes"
        )

    rendered = "\n".join(output).rstrip() + "\n"

    required = (
        f"linux /boot/vmlinuz-{KERNEL_VERSION}",
        f"initrd /boot/initramfs-{KERNEL_VERSION}.cpio.gz",
        "hardened.debug_shell=1",
        "selinux=0",
    )
    for item in required:
        if item not in rendered:
            die(f"Generated loader entry is missing: {item}")

    destination = ISO_ROOT / "loader/entries" / ENTRY_NAME
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(rendered, encoding="utf-8", newline="\n")
    destination.chmod(0o644)

    loader_conf = ISO_ROOT / "loader/loader.conf"
    if loader_conf.is_file():
        lines = loader_conf.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
        output_lines: list[str] = []
        editor_seen = False

        for line in lines:
            if line.strip().startswith("editor "):
                output_lines.append("editor no")
                editor_seen = True
            else:
                output_lines.append(line)

        if not editor_seen:
            output_lines.append("editor no")

        loader_conf.write_text(
            "\n".join(output_lines).rstrip() + "\n",
            encoding="utf-8",
            newline="\n",
        )

    print(f"Created loader entry: {destination}")
    return destination


def sync_efi_entry(entry: Path) -> None:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = EFI_IMAGE.with_name(
        EFI_IMAGE.name + f".bak-debug-shell-{timestamp}"
    )
    shutil.copy2(EFI_IMAGE, backup)
    print(f"Backed up EFI image: {backup}")

    mountpoint = Path(
        tempfile.mkdtemp(prefix="efi-debug-shell-", dir="/mnt")
    )
    mounted = False

    try:
        result = run(
            ["mount", "-o", "loop,rw,sync", str(EFI_IMAGE), str(mountpoint)],
            check=False,
            capture=True,
        )
        if result.returncode != 0:
            die(f"Could not mount efiboot.img:\n{result.stdout}")
        mounted = True

        destination = mountpoint / "loader/entries" / ENTRY_NAME
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(entry, destination)

        loader_source = ISO_ROOT / "loader/loader.conf"
        loader_destination = mountpoint / "loader/loader.conf"
        shutil.copy2(loader_source, loader_destination)

        os.sync()

        if sha256(entry) != sha256(destination):
            die("EFI debug-shell entry hash mismatch.")

        if sha256(loader_source) != sha256(loader_destination):
            die("EFI loader.conf hash mismatch.")

    finally:
        if mounted:
            result = run(
                ["umount", str(mountpoint)],
                check=False,
                capture=True,
            )
            if result.returncode != 0:
                die(f"Could not unmount efiboot.img:\n{result.stdout}")

        try:
            mountpoint.rmdir()
        except OSError:
            pass


def main() -> None:
    require_root()

    for command in (
        "losetup",
        "findmnt",
        "mount",
        "umount",
        "e2fsck",
        "sync",
    ):
        require_command(command)

    for path in (ROOTFS, EFI_IMAGE, ISO_BUILDER):
        if not path.exists():
            die(f"Required path not found: {path}")

    print("=== Clearing stale rootfs mounts ===")
    clear_stale_loops(ROOTFS)

    print("=== Checking rootfs.ext2 ===")
    run(["e2fsck", "-f", "-y", str(ROOTFS)])

    loop_result = run(
        ["losetup", "--find", "--show", str(ROOTFS)],
        capture=True,
    )
    loop_device = loop_result.stdout.strip()
    mountpoint = Path(
        tempfile.mkdtemp(prefix="hardened-debug-shell-root-", dir="/mnt")
    )
    mounted = False

    try:
        run(
            ["mount", "-t", "ext2", "-o", "rw", loop_device, str(mountpoint)]
        )
        mounted = True
        install_debug_units(mountpoint)
        run(["sync"])
    finally:
        if mounted:
            run(["umount", str(mountpoint)], check=False)
        run(["losetup", "--detach", loop_device], check=False)
        try:
            mountpoint.rmdir()
        except OSError:
            pass

    run(["e2fsck", "-f", "-y", str(ROOTFS)])
    run(["e2fsck", "-fn", str(ROOTFS)])

    entry = write_loader_entry()
    sync_efi_entry(entry)

    print("=== Rebuilding verified outer ISO ===")
    run([sys.executable, str(ISO_BUILDER)])

    print()
    print("=== SUCCESS ===")
    print("A test-only unauthenticated root shell entry was added.")
    print("Normal login/PAM boot entries were not replaced.")
    print()
    print(f"Select: {ENTRY_TITLE}")
    print("The shell will appear directly on ttyS0 without a login prompt.")
    print()
    print("SECURITY: remove this diagnostic entry and service before release.")


if __name__ == "__main__":
    main()
