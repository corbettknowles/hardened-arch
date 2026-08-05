#!/usr/bin/env python3
"""
Fix libsemanage helper paths in the Hardened Arch rootfs.

The Arch policycoreutils payload currently provides helpers under /usr/bin,
while libsemanage's default helper lookup expects /sbin or /usr/sbin.
Create compatibility links for:
  setfiles
  sefcontext_compile
  load_policy

Then verify each helper executes inside the target root.

Close QEMU first, then run:
    sudo python3 /home/corbett/fix_libsemanage_helper_paths.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


HOME = Path("/home/corbett")
ROOTFS = HOME / "iso-systemd/rootfs.ext2"
BACKUP_ROOT = HOME / "rootfs-backups"

HELPERS = (
    "setfiles",
    "sefcontext_compile",
    "load_policy",
)


def die(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def run(args, *, check=True, capture=False):
    print("+", " ".join(map(str, args)))
    try:
        return subprocess.run(
            [str(x) for x in args],
            check=check,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
        )
    except subprocess.CalledProcessError as exc:
        if capture and exc.stdout:
            print(exc.stdout, file=sys.stderr)
        die(f"Command failed ({exc.returncode})")


def ensure_qemu_closed():
    pgrep = shutil.which("pgrep")
    if not pgrep:
        return
    result = subprocess.run(
        [pgrep, "-f", "qemu-system-x86_64"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        die("QEMU is still running. Shut it down first.")


def associated_loops():
    result = run(
        ["losetup", "--noheadings", "-O", "NAME", "--associated", str(ROOTFS)],
        check=False,
        capture=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def clear_loops():
    for loop in associated_loops():
        result = run(
            ["findmnt", "-rn", "-S", loop, "-o", "TARGET"],
            check=False,
            capture=True,
        )
        for target in sorted(
            [x.strip() for x in result.stdout.splitlines() if x.strip()],
            key=len,
            reverse=True,
        ):
            run(["umount", target], check=False)
        run(["losetup", "-d", loop], check=False)

    if associated_loops():
        die("rootfs.ext2 is still attached to a loop device.")


def find_helper(root: Path, name: str) -> str | None:
    for candidate in (
        f"/usr/bin/{name}",
        f"/usr/sbin/{name}",
        f"/bin/{name}",
        f"/sbin/{name}",
    ):
        path = root / candidate.lstrip("/")
        if path.is_file():
            return candidate
    return None


def backup_link(path: Path, root: Path, backup_dir: Path):
    if not path.exists() and not path.is_symlink():
        return

    relative = path.relative_to(root)
    destination = backup_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)

    if path.is_symlink():
        destination.with_suffix(destination.suffix + ".symlink.txt").write_text(
            os.readlink(path),
            encoding="utf-8",
        )
    elif path.is_file():
        shutil.copy2(path, destination, follow_symlinks=False)


def create_compat_links(root: Path, backup_dir: Path):
    found = {}

    for helper in HELPERS:
        source = find_helper(root, helper)
        if not source:
            print(f"WARNING: helper not installed: {helper}")
            continue

        found[helper] = source
        print(f"{helper}: canonical target path is {source}")

        for destination_string in (
            f"/usr/sbin/{helper}",
            f"/sbin/{helper}",
        ):
            if destination_string == source:
                continue

            destination = root / destination_string.lstrip("/")
            destination.parent.mkdir(parents=True, exist_ok=True)

            backup_link(destination, root, backup_dir)

            if destination.exists() or destination.is_symlink():
                destination.unlink()

            relative_target = os.path.relpath(
                root / source.lstrip("/"),
                start=destination.parent,
            )
            destination.symlink_to(relative_target)
            print(
                f"Created {destination_string} -> {relative_target}"
            )

    if "setfiles" not in found:
        die("setfiles is missing from the target.")
    if "sefcontext_compile" not in found:
        die("sefcontext_compile is missing from the target.")

    return found


def verify_helpers(root: Path, found: dict[str, str]):
    for helper, source in found.items():
        result = run(
            ["chroot", str(root), source, "-h"],
            check=False,
            capture=True,
        )

        output = result.stdout or ""
        if (
            "No such file or directory" in output
            or "error while loading shared libraries" in output
        ):
            print(output, file=sys.stderr)
            die(f"{helper} cannot execute inside the target.")

        print(f"Verified target helper: {source}")

    for required in (
        "/usr/sbin/setfiles",
        "/sbin/setfiles",
        "/usr/sbin/sefcontext_compile",
        "/sbin/sefcontext_compile",
    ):
        path = root / required.lstrip("/")
        if not path.exists():
            die(f"Required compatibility path is missing: {required}")


def main():
    if os.geteuid() != 0:
        die("Run with sudo.")

    ensure_qemu_closed()

    for command in (
        "losetup",
        "findmnt",
        "mount",
        "umount",
        "e2fsck",
        "chroot",
        "sync",
    ):
        if not shutil.which(command):
            die(f"Missing host command: {command}")

    if not ROOTFS.is_file():
        die(f"Missing {ROOTFS}")

    clear_loops()
    run(["e2fsck", "-f", "-y", str(ROOTFS)])

    loop = run(
        ["losetup", "--find", "--show", str(ROOTFS)],
        capture=True,
    ).stdout.strip()

    mountpoint = Path(
        tempfile.mkdtemp(prefix="libsemanage-helper-fix-", dir="/mnt")
    )
    backup_dir = BACKUP_ROOT / (
        "libsemanage-helper-paths-"
        + datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    backup_dir.mkdir(parents=True, exist_ok=True)

    mounted = False

    try:
        run(
            [
                "mount",
                "-t",
                "ext2",
                "-o",
                "rw,acl,user_xattr",
                loop,
                str(mountpoint),
            ]
        )
        mounted = True

        found = create_compat_links(mountpoint, backup_dir)
        verify_helpers(mountpoint, found)
        run(["sync"])

    finally:
        if mounted:
            run(["umount", str(mountpoint)], check=False)
        run(["losetup", "-d", loop], check=False)
        try:
            mountpoint.rmdir()
        except OSError:
            pass

    run(["e2fsck", "-f", "-y", str(ROOTFS)])
    run(["e2fsck", "-fn", str(ROOTFS)])

    print()
    print("=== SUCCESS ===")
    print("libsemanage helper compatibility paths are installed.")
    print(f"Backups: {backup_dir}")
    print()
    print("Now rerun:")
    print(
        "sudo python3 "
        "/home/corbett/finish_selinux_policy_correctly.py"
    )


if __name__ == "__main__":
    main()
