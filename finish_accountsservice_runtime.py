#!/usr/bin/env python3
"""
Finish the AccountsService/Polkit runtime after the previous staging pass
stopped on libjson-c.so.5.

This script:
  * stages official Arch json-c
  * checks AccountsService and polkitd with ldd
  * stages duktape or expat only if their SONAME is missing
  * re-runs systemd-sysusers safely
  * verifies AccountsService D-Bus activation files
  * patches the SELinux relabel launcher to use supported setfiles options

It does NOT rebuild the ISO. After success, run:
    sudo /home/corbett/run_target_kernel_selinux_relabel.sh
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path


HOME = Path("/home/corbett")
ROOTFS = HOME / "iso-systemd/rootfs.ext2"
RELABEL = HOME / "run_target_kernel_selinux_relabel.sh"

PACKAGES = {
    "json-c": "https://archlinux.org/packages/core/x86_64/json-c/download/",
    "duktape": "https://archlinux.org/packages/extra/x86_64/duktape/download/",
    "expat": "https://archlinux.org/packages/core/x86_64/expat/download/",
}

SONAME_TO_PACKAGE = {
    "libjson-c.so.5": "json-c",
    "libduktape.so.207": "duktape",
    "libexpat.so.1": "expat",
}

BINARIES = (
    "/usr/lib/accounts-daemon",
    "/usr/lib/polkit-1/polkitd",
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


def require_root() -> None:
    if os.geteuid() != 0:
        die("Run this script with sudo.")


def ensure_qemu_closed() -> None:
    result = subprocess.run(
        ["pgrep", "-f", "qemu-system-x86_64"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        die("QEMU is still running. Shut it down cleanly first.")


def associated_loops() -> list[str]:
    result = run(
        [
            "losetup",
            "--noheadings",
            "--output",
            "NAME",
            "--associated",
            str(ROOTFS),
        ],
        check=False,
        capture=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def clear_stale_loops() -> None:
    for loop in associated_loops():
        mounts = run(
            ["findmnt", "-rn", "-S", loop, "-o", "TARGET"],
            check=False,
            capture=True,
        )
        for target in sorted(
            [line.strip() for line in mounts.stdout.splitlines() if line.strip()],
            key=len,
            reverse=True,
        ):
            run(["umount", target], check=False)
        run(["losetup", "--detach", loop], check=False)

    if associated_loops():
        die("Could not clear all loop associations for rootfs.ext2.")


def download(url: str, destination: Path) -> None:
    print(f"Downloading {url}")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Hardened-Arch-runtime-stager/1.0"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        with destination.open("wb") as output:
            shutil.copyfileobj(response, output)


def stage_package(root: Path, work: Path, package_name: str) -> None:
    if package_name not in PACKAGES:
        die(f"No approved package mapping for {package_name}.")

    archive = work / f"{package_name}.pkg.tar.zst"
    download(PACKAGES[package_name], archive)

    info = run(
        ["tar", "--zstd", "-xOf", str(archive), ".PKGINFO"],
        capture=True,
    ).stdout

    name_match = re.search(r"^pkgname\s*=\s*(.+)$", info, re.MULTILINE)
    version_match = re.search(r"^pkgver\s*=\s*(.+)$", info, re.MULTILINE)

    if not name_match or name_match.group(1).strip() != package_name:
        die(f"Downloaded archive is not the expected {package_name} package.")

    version = version_match.group(1).strip() if version_match else "unknown"
    print(f"Verified Arch package: {package_name} {version}")

    run(
        [
            "tar",
            "--zstd",
            "--same-owner",
            "--same-permissions",
            "-xpf",
            str(archive),
            "-C",
            str(root),
            "--exclude=.PKGINFO",
            "--exclude=.BUILDINFO",
            "--exclude=.MTREE",
        ]
    )


def ldd_missing(root: Path, binary: str) -> set[str]:
    target = root / binary.lstrip("/")
    if not target.is_file():
        die(f"Required binary is missing: {binary}")

    result = run(
        ["chroot", str(root), "/usr/bin/ldd", binary],
        check=False,
        capture=True,
    )
    print(result.stdout.rstrip())

    missing = set(
        re.findall(r"^\s*(\S+)\s+=>\s+not found\s*$", result.stdout, re.MULTILINE)
    )

    hard_failures = (
        "version `GLIBC_",
        "error while loading shared libraries",
        "No such file or directory",
    )
    if any(marker in result.stdout for marker in hard_failures):
        die(f"ABI/runtime failure while checking {binary}.")

    return missing


def resolve_runtime(root: Path, work: Path) -> None:
    staged = set()

    stage_package(root, work, "json-c")
    staged.add("json-c")
    run(["chroot", str(root), "/usr/sbin/ldconfig"])

    for _ in range(3):
        missing_by_binary = {}
        all_missing = set()

        for binary in BINARIES:
            missing = ldd_missing(root, binary)
            missing_by_binary[binary] = missing
            all_missing.update(missing)

        if not all_missing:
            for binary in BINARIES:
                print(f"Runtime dependency verification: PASS ({binary})")
            return

        print("Missing SONAMEs:")
        for binary, missing in missing_by_binary.items():
            if missing:
                print(f"  {binary}: {', '.join(sorted(missing))}")

        unknown = sorted(
            soname for soname in all_missing if soname not in SONAME_TO_PACKAGE
        )
        if unknown:
            die(
                "Unmapped missing libraries remain: "
                + ", ".join(unknown)
                + ". No package was guessed."
            )

        packages_needed = {
            SONAME_TO_PACKAGE[soname] for soname in all_missing
        }

        progress = False
        for package_name in sorted(packages_needed):
            if package_name in staged:
                continue
            stage_package(root, work, package_name)
            staged.add(package_name)
            progress = True

        if not progress:
            die("Libraries remain missing even after their packages were staged.")

        run(["chroot", str(root), "/usr/sbin/ldconfig"])

    die("Runtime dependency resolution did not converge.")


def verify_sysusers(root: Path) -> None:
    result = run(
        ["chroot", str(root), "/usr/bin/systemd-sysusers"],
        check=False,
        capture=True,
    )
    if result.stdout:
        print(result.stdout.rstrip())
    if result.returncode != 0:
        die("systemd-sysusers failed inside the target.")

    passwd = (root / "etc/passwd").read_text(
        encoding="utf-8",
        errors="replace",
    )
    if not re.search(r"^polkitd:", passwd, re.MULTILINE):
        die("The polkitd account is missing.")

    print("polkitd account verification: PASS")


def verify_accountsservice_files(root: Path) -> None:
    required = (
        "/usr/lib/accounts-daemon",
        "/usr/share/dbus-1/system-services/org.freedesktop.Accounts.service",
        "/usr/share/dbus-1/system.d/org.freedesktop.Accounts.conf",
        "/usr/lib/systemd/system/accounts-daemon.service",
    )

    for item in required:
        if not (root / item.lstrip("/")).is_file():
            die(f"AccountsService file is missing: {item}")

    print("AccountsService D-Bus activation files: PASS")


def patch_relabel_launcher() -> None:
    if not RELABEL.is_file():
        die(f"Missing relabel launcher: {RELABEL}")

    text = RELABEL.read_text(encoding="utf-8", errors="replace")

    corrected = (
        "/usr/bin/setfiles -F "
        "-e /proc -e /sys -e /dev -e /run "
        "/etc/selinux/refpolicy-arch/contexts/files/file_contexts /"
    )

    text = text.replace(
        "/usr/bin/setfiles -F -x "
        "/etc/selinux/refpolicy-arch/contexts/files/file_contexts /",
        corrected,
    )
    text = text.replace(
        "setfiles -F -x "
        "/etc/selinux/refpolicy-arch/contexts/files/file_contexts /",
        corrected,
    )

    if "-F -x" in text:
        die("Unsupported setfiles -x remains in the relabel launcher.")

    if corrected not in text:
        die("The corrected setfiles command could not be verified.")

    RELABEL.write_text(text, encoding="utf-8", newline="\n")
    RELABEL.chmod(0o755)
    print("SELinux relabel launcher verification: PASS")


def main() -> None:
    require_root()
    ensure_qemu_closed()

    for command in (
        "pgrep",
        "losetup",
        "findmnt",
        "mount",
        "umount",
        "e2fsck",
        "tar",
        "chroot",
        "sync",
    ):
        if not shutil.which(command):
            die(f"Missing host command: {command}")

    if not ROOTFS.is_file():
        die(f"Missing root filesystem: {ROOTFS}")

    clear_stale_loops()
    run(["e2fsck", "-f", "-y", str(ROOTFS)])

    loop = run(
        ["losetup", "--find", "--show", str(ROOTFS)],
        capture=True,
    ).stdout.strip()

    mountpoint = Path(
        tempfile.mkdtemp(prefix="finish-accounts-runtime-", dir="/mnt")
    )
    work = Path(tempfile.mkdtemp(prefix="accounts-runtime-work-"))
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

        resolve_runtime(mountpoint, work)
        verify_sysusers(mountpoint)
        verify_accountsservice_files(mountpoint)
        run(["sync"])

    finally:
        if mounted:
            run(["umount", str(mountpoint)], check=False)
        run(["losetup", "--detach", loop], check=False)
        shutil.rmtree(mountpoint, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)

    run(["e2fsck", "-f", "-y", str(ROOTFS)])
    run(["e2fsck", "-f", "-n", str(ROOTFS)])

    patch_relabel_launcher()

    print()
    print("=== SUCCESS ===")
    print("AccountsService and polkit runtime dependencies are complete.")
    print("The polkitd service account is verified.")
    print("The relabel launcher uses supported setfiles exclusions.")
    print()
    print("Next command:")
    print("sudo /home/corbett/run_target_kernel_selinux_relabel.sh")


if __name__ == "__main__":
    main()
