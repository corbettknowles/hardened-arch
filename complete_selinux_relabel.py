#!/usr/bin/env python3
"""
Complete the SELinux offline relabel after the policy store has already built.

The previous command incorrectly used:
    setfiles -r / ...

Inside chroot, "/" is already the target root, so -r / is invalid. This script
runs setfiles without -r, verifies labels, updates normal boot entries for a
permissive first boot, syncs efiboot.img, and rebuilds the ISO.

Close QEMU first, then run:
    sudo python3 /home/corbett/complete_selinux_relabel.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


HOME = Path("/home/corbett")
ISO_ROOT = HOME / "iso-systemd"
ROOTFS = ISO_ROOT / "rootfs.ext2"
EFI_IMAGE = ISO_ROOT / "efiboot.img"
ISO_BUILDER = HOME / "build_hardened_iso.py"


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


def find_policy(root: Path):
    policy_dir = root / "etc/selinux/refpolicy-arch/policy"
    policies = sorted(
        (
            path for path in policy_dir.glob("policy.*")
            if path.is_file() and path.name.rsplit(".", 1)[-1].isdigit()
        ),
        key=lambda path: int(path.name.rsplit(".", 1)[-1]),
        reverse=True,
    )
    if not policies:
        die("Compiled policy.N is missing.")

    contexts = root / "etc/selinux/refpolicy-arch/contexts/files/file_contexts"
    if not contexts.is_file():
        die("file_contexts is missing.")

    return policies[0], contexts


def relabel(root: Path, contexts: Path):
    setfiles = None
    for candidate in (
        "/usr/bin/setfiles",
        "/usr/sbin/setfiles",
        "/sbin/setfiles",
    ):
        if (root / candidate.lstrip("/")).is_file():
            setfiles = candidate
            break

    if not setfiles:
        die("setfiles is missing.")

    target_contexts = "/" + str(contexts.relative_to(root))

    # Correct form inside chroot: do not use -r /.
    result = run(
        [
            "chroot",
            str(root),
            setfiles,
            "-F",
            target_contexts,
            "/",
        ],
        check=False,
        capture=True,
    )

    if result.stdout:
        print(result.stdout.rstrip())

    if result.returncode != 0:
        (root / ".autorelabel").touch()
        die("setfiles failed; /.autorelabel was retained.")

    (root / ".autorelabel").unlink(missing_ok=True)
    print("Offline SELinux relabel: PASS")


def verify_labels(root: Path):
    ls_tool = None
    for candidate in ("/usr/bin/ls", "/bin/ls"):
        if (root / candidate.lstrip("/")).is_file():
            ls_tool = candidate
            break

    if not ls_tool:
        print("WARNING: target ls is missing; skipping label display.")
        return

    result = run(
        [
            "chroot",
            str(root),
            ls_tool,
            "-Zd",
            "/",
            "/etc/selinux",
            "/usr/bin/semodule",
            "/usr/bin/setfiles",
        ],
        check=False,
        capture=True,
    )
    if result.stdout:
        print(result.stdout.rstrip())

    if result.returncode != 0:
        print("WARNING: ls -Z verification was unavailable, but setfiles succeeded.")


def verify_config(root: Path):
    config = root / "etc/selinux/config"
    if not config.is_file():
        die("/etc/selinux/config is missing.")

    text = config.read_text(encoding="utf-8", errors="replace")
    if "SELINUX=permissive" not in text:
        die("First boot is not configured permissive.")
    if "SELINUXTYPE=refpolicy-arch" not in text:
        die("SELINUXTYPE is not refpolicy-arch.")

    targeted = root / "etc/selinux/targeted"
    if not targeted.is_symlink() or os.readlink(targeted) != "refpolicy-arch":
        die("/etc/selinux/targeted compatibility link is wrong.")

    print("SELinux configuration verification: PASS")


def verify_pam(root: Path):
    required = (
        "etc/pam.d/system-auth",
        "etc/pam.d/system-login",
        "etc/pam.d/other",
        "etc/pam.d/login",
        "etc/pam.d/su",
    )
    missing = [path for path in required if not (root / path).is_file()]
    if missing:
        die("Persistent PAM files missing: " + ", ".join(missing))

    login = (root / "etc/pam.d/login").read_text(
        encoding="utf-8",
        errors="replace",
    )
    if "pam_console.so" in login:
        die("pam_console.so is still present in login PAM config.")
    if "pam_selinux.so open" not in login:
        die("pam_selinux open-session hook is missing.")

    print("Persistent PAM login verification: PASS")


def patch_loader_entries():
    entries = ISO_ROOT / "loader/entries"
    if not entries.is_dir():
        die(f"Loader entries directory missing: {entries}")

    for path in sorted(entries.glob("*.conf")):
        lowered = path.name.lower()

        if any(
            token in lowered
            for token in ("selinux-off", "direct-shell", "debug-shell")
        ):
            print(f"Preserving rescue entry: {path.name}")
            continue

        output = []
        changed = False

        for raw in path.read_text(encoding="utf-8").splitlines():
            if not raw.strip().startswith("options "):
                output.append(raw)
                continue

            tokens = raw.strip()[len("options "):].split()
            tokens = [
                token for token in tokens
                if token not in {
                    "selinux=0",
                    "selinux=1",
                    "enforcing=0",
                    "enforcing=1",
                }
            ]
            tokens.extend(["selinux=1", "enforcing=0"])
            new_line = "options " + " ".join(dict.fromkeys(tokens))
            changed = changed or new_line != raw
            output.append(new_line)

        if changed:
            path.write_text(
                "\n".join(output).rstrip() + "\n",
                encoding="utf-8",
                newline="\n",
            )
            path.chmod(0o644)
            print(f"Patched normal permissive entry: {path.name}")


def sync_efi_entries():
    mountpoint = Path(
        tempfile.mkdtemp(prefix="efi-selinux-relabel-", dir="/mnt")
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

        destination = mountpoint / "loader/entries"
        destination.mkdir(parents=True, exist_ok=True)

        for source in sorted((ISO_ROOT / "loader/entries").glob("*.conf")):
            shutil.copy2(source, destination / source.name)

        loader_conf = ISO_ROOT / "loader/loader.conf"
        if loader_conf.is_file():
            target = mountpoint / "loader/loader.conf"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(loader_conf, target)

        os.sync()
        print("EFI loader entries synchronized.")

    finally:
        if mounted:
            run(["umount", str(mountpoint)], check=False)
        try:
            mountpoint.rmdir()
        except OSError:
            pass


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

    for path in (ROOTFS, EFI_IMAGE, ISO_BUILDER):
        if not path.exists():
            die(f"Missing required path: {path}")

    clear_loops()
    run(["e2fsck", "-f", "-y", str(ROOTFS)])

    loop = run(
        ["losetup", "--find", "--show", str(ROOTFS)],
        capture=True,
    ).stdout.strip()

    mountpoint = Path(
        tempfile.mkdtemp(prefix="complete-selinux-relabel-", dir="/mnt")
    )
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

        policy, contexts = find_policy(mountpoint)
        print(f"Compiled policy verified: /{policy.relative_to(mountpoint)}")
        print(f"File contexts verified: /{contexts.relative_to(mountpoint)}")

        verify_config(mountpoint)
        verify_pam(mountpoint)
        relabel(mountpoint, contexts)
        verify_labels(mountpoint)
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

    patch_loader_entries()
    sync_efi_entries()

    print("=== Rebuilding ISO ===")
    run([sys.executable, str(ISO_BUILDER)])

    print()
    print("=== SUCCESS ===")
    print("The compiled SELinux policy is installed.")
    print("The root filesystem was relabeled offline.")
    print("The persistent PAM login stack remains verified.")
    print("Normal boot is SELinux permissive.")
    print("SELinux-off and direct-shell rescue entries remain available.")


if __name__ == "__main__":
    main()
