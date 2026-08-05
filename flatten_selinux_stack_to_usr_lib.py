#!/usr/bin/env python3
"""
Normalize only the SELinux/audit runtime stack to Arch's flat /usr/lib layout.

This does NOT rebuild the whole distro and does NOT touch glibc's existing
multiarch layout. It removes stale duplicate SELinux libraries from Debian-style
paths, replaces them with relative compatibility symlinks into /usr/lib, runs
ldconfig, and verifies the actual target runtime inside chroot.

Close QEMU first, then run:
    sudo python3 /home/corbett/flatten_selinux_stack_to_usr_lib.py
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

SONAMES = (
    "libselinux.so.1",
    "libsepol.so.2",
    "libsemanage.so.2",
    "libaudit.so.1",
    "libcap-ng.so.0",
)

COMPAT_DIRS = {
    "lib/x86_64-linux-gnu": "../../usr/lib/{soname}",
    "usr/lib/x86_64-linux-gnu": "../{soname}",
    "lib64": "../usr/lib/{soname}",
}

CHECK_EXECUTABLES = (
    "/usr/bin/semodule",
    "/usr/bin/setfiles",
    "/usr/bin/restorecon",
    "/usr/sbin/restorecon",
    "/usr/bin/getenforce",
    "/usr/sbin/getenforce",
    "/usr/bin/setenforce",
    "/usr/sbin/setenforce",
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


def require_root():
    if os.geteuid() != 0:
        die("Run with sudo.")


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


def loops():
    result = run(
        ["losetup", "--noheadings", "-O", "NAME", "--associated", str(ROOTFS)],
        check=False,
        capture=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def clear_loops():
    for loop in loops():
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
    if loops():
        die("rootfs.ext2 is still attached to a loop device.")


def backup_path(root: Path, path: Path, backup_dir: Path):
    relative = path.relative_to(root)
    destination = backup_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)

    if path.is_symlink():
        destination.with_suffix(destination.suffix + ".symlink.txt").write_text(
            os.readlink(path), encoding="utf-8"
        )
    elif path.is_file():
        shutil.copy2(path, destination, follow_symlinks=False)


def canonical_arch_library(root: Path, soname: str) -> Path:
    soname_path = root / "usr/lib" / soname
    if not soname_path.exists() and not soname_path.is_symlink():
        die(f"Missing Arch library: /usr/lib/{soname}")

    try:
        real = soname_path.resolve(strict=True)
    except FileNotFoundError:
        die(f"Broken Arch SONAME link: /usr/lib/{soname}")

    if not real.is_file():
        die(f"Arch library is not a file: {real}")

    return real


def normalize_one(root: Path, soname: str, backup_dir: Path):
    real = canonical_arch_library(root, soname)
    print(f"{soname}: canonical Arch file is /{real.relative_to(root)}")

    for relative_dir, target_template in COMPAT_DIRS.items():
        directory = root / relative_dir
        directory.mkdir(parents=True, exist_ok=True)

        # Avoid modifying a directory that is itself a symlink into /usr/lib.
        if directory.is_symlink():
            continue

        for candidate in list(directory.glob(soname)) + list(directory.glob(soname + ".*")):
            if not (candidate.exists() or candidate.is_symlink()):
                continue

            # Keep the exact compatibility symlink only if it already points
            # where we want it to point.
            desired = target_template.format(soname=soname)
            if candidate.name == soname and candidate.is_symlink():
                if os.readlink(candidate) == desired:
                    continue

            backup_path(root, candidate, backup_dir)
            if candidate.is_symlink() or candidate.is_file():
                candidate.unlink()

        link = directory / soname
        desired = target_template.format(soname=soname)
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(desired)
        print(f"Created /{link.relative_to(root)} -> {desired}")


def write_linker_config(root: Path):
    conf = root / "etc/ld.so.conf.d/00-arch-selinux.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(
        "# Prefer Arch SELinux userspace libraries.\n/usr/lib\n",
        encoding="utf-8",
        newline="\n",
    )
    conf.chmod(0o644)

    cache = root / "etc/ld.so.cache"
    cache.unlink(missing_ok=True)


def target_tool(root: Path, candidates):
    for candidate in candidates:
        if (root / candidate.lstrip("/")).is_file():
            return candidate
    return None


def run_ldconfig(root: Path):
    ldconfig = target_tool(
        root,
        ("/usr/sbin/ldconfig", "/sbin/ldconfig", "/usr/bin/ldconfig", "/bin/ldconfig"),
    )
    if not ldconfig:
        die("Target ldconfig is missing.")
    run(["chroot", str(root), ldconfig])


def verify_runtime(root: Path):
    ldd = target_tool(root, ("/usr/bin/ldd", "/bin/ldd"))
    if not ldd:
        die("Target ldd is missing.")

    checked = 0
    for executable in CHECK_EXECUTABLES:
        if not (root / executable.lstrip("/")).is_file():
            continue

        checked += 1
        result = run(
            ["chroot", str(root), ldd, executable],
            check=False,
            capture=True,
        )
        print(result.stdout.rstrip())

        if result.returncode != 0:
            die(f"{executable} returned a linker error.")
        if "not found" in result.stdout:
            die(f"{executable} still has a missing library.")
        if "LIBSELINUX_3.10" in result.stdout:
            die(f"{executable} still loads the wrong libselinux ABI.")

    if checked == 0:
        die("No SELinux executables were found to verify.")

    semodule = target_tool(
        root, ("/usr/bin/semodule", "/usr/sbin/semodule", "/sbin/semodule")
    )
    setfiles = target_tool(
        root, ("/usr/bin/setfiles", "/usr/sbin/setfiles", "/sbin/setfiles")
    )
    if not semodule or not setfiles:
        die("semodule or setfiles is missing.")

    probe = run(
        ["chroot", str(root), semodule, "--help"],
        check=False,
        capture=True,
    )
    if "error while loading shared libraries" in probe.stdout or "LIBSELINUX_" in probe.stdout:
        print(probe.stdout, file=sys.stderr)
        die("semodule still has a loader failure.")

    probe = run(
        ["chroot", str(root), setfiles, "-h"],
        check=False,
        capture=True,
    )
    if "error while loading shared libraries" in probe.stdout or "LIBSELINUX_" in probe.stdout:
        print(probe.stdout, file=sys.stderr)
        die("setfiles still has a loader failure.")

    pam_candidates = (
        "usr/lib/security/pam_selinux.so",
        "usr/lib64/security/pam_selinux.so",
        "usr/lib/x86_64-linux-gnu/security/pam_selinux.so",
        "lib/security/pam_selinux.so",
        "lib/x86_64-linux-gnu/security/pam_selinux.so",
    )
    found_pam = False
    for relative in pam_candidates:
        if not (root / relative).is_file():
            continue
        found_pam = True
        result = run(
            ["chroot", str(root), ldd, "/" + relative],
            check=False,
            capture=True,
        )
        print(result.stdout.rstrip())
        if result.returncode != 0 or "not found" in result.stdout or "LIBSELINUX_" in result.stdout:
            die(f"/{relative} still has a loader/ABI failure.")

    if not found_pam:
        die("pam_selinux.so is missing.")

    print("SELinux flat-/usr runtime verification: PASS")


def main():
    require_root()
    ensure_qemu_closed()

    for command in ("losetup", "findmnt", "mount", "umount", "e2fsck", "chroot", "sync"):
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

    mountpoint = Path(tempfile.mkdtemp(prefix="selinux-flat-usr-", dir="/mnt"))
    backup_dir = BACKUP_ROOT / (
        "selinux-flat-usr-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    backup_dir.mkdir(parents=True, exist_ok=True)
    mounted = False

    try:
        run(
            ["mount", "-t", "ext2", "-o", "rw,acl,user_xattr", loop, str(mountpoint)]
        )
        mounted = True

        for soname in SONAMES:
            normalize_one(mountpoint, soname, backup_dir)

        write_linker_config(mountpoint)
        run_ldconfig(mountpoint)
        verify_runtime(mountpoint)
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
    print("SELinux/audit runtime now uses Arch's flat /usr/lib layout.")
    print("Debian-style paths are compatibility symlinks only.")
    print(f"Backups: {backup_dir}")
    print()
    print("Now rerun:")
    print("sudo python3 /home/corbett/stage_selinux_userspace_and_policy.py")


if __name__ == "__main__":
    main()
