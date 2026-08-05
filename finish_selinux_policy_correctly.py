#!/usr/bin/env python3
"""
Finish the SELinux policy build correctly.

Fixes both remaining blockers:
  1. Stage Arch's matching PCRE2 shared libraries into flat /usr/lib so
     libselinux 3.10 no longer loads the old Debian-layout PCRE2.
  2. Build all refpolicy-arch .pp modules in ONE semodule transaction, exactly
     like the package's official post_install script. The previous batched
     install split policy dependencies and caused unresolved booleanif errors.

The script then verifies policy.N + file_contexts, performs an offline relabel,
keeps the first boot permissive, syncs loader entries into efiboot.img, and
rebuilds the ISO.

Close QEMU first, then run:
    sudo python3 /home/corbett/finish_selinux_policy_correctly.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path


HOME = Path("/home/corbett")
ISO_ROOT = HOME / "iso-systemd"
ROOTFS = ISO_ROOT / "rootfs.ext2"
EFI_IMAGE = ISO_ROOT / "efiboot.img"
ISO_BUILDER = HOME / "build_hardened_iso.py"
BACKUP_ROOT = HOME / "rootfs-backups"

PCRE2_URL = "https://archlinux.org/packages/core/x86_64/pcre2/download/"

PAM_REQUIRED = (
    "etc/pam.d/system-auth",
    "etc/pam.d/system-login",
    "etc/pam.d/other",
    "etc/pam.d/login",
    "etc/pam.d/su",
)


def die(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


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
        die(
            f"Command failed ({exc.returncode}): "
            + " ".join(str(item) for item in args)
        )


def require_root() -> None:
    if os.geteuid() != 0:
        die("Run this script with sudo.")


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        die(f"Required host command not found: {name}")


def ensure_qemu_closed() -> None:
    pgrep = shutil.which("pgrep")
    if pgrep is None:
        return

    result = subprocess.run(
        [pgrep, "-f", "qemu-system-x86_64"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        die("QEMU is still running. Shut it down first.")


def download(url: str, destination: Path) -> None:
    print(f"Downloading {url}")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Hardened-Arch-SELinux-finisher/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            with destination.open("wb") as stream:
                shutil.copyfileobj(response, stream)
    except Exception as exc:
        die(f"Download failed: {url}\n{exc}")


def package_info(package: Path) -> dict[str, list[str]]:
    result = run(
        ["tar", "--zstd", "-xOf", str(package), ".PKGINFO"],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        die(f"Could not read .PKGINFO:\n{result.stdout}")

    fields: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        if " = " not in line:
            continue
        key, value = line.split(" = ", 1)
        fields.setdefault(key.strip(), []).append(value.strip())
    return fields


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
        targets = run(
            ["findmnt", "-rn", "-S", loop, "-o", "TARGET"],
            check=False,
            capture=True,
        ).stdout.splitlines()

        for target in sorted(
            {line.strip() for line in targets if line.strip()},
            key=len,
            reverse=True,
        ):
            result = run(["umount", target], check=False, capture=True)
            if result.returncode != 0:
                die(f"Could not unmount stale mount {target}:\n{result.stdout}")

        run(["losetup", "--detach", loop], check=False, capture=True)

    remaining = associated_loops(image)
    if remaining:
        die("rootfs.ext2 is still attached to: " + ", ".join(remaining))


def backup_file(root: Path, path: Path, backup_dir: Path) -> None:
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


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def stage_arch_pcre2(
    root: Path,
    package: Path,
    workdir: Path,
    backup_dir: Path,
) -> None:
    info = package_info(package)
    names = info.get("pkgname", [])
    arches = info.get("arch", [])
    versions = info.get("pkgver", [])

    if names != ["pcre2"]:
        die(f"Downloaded package is not pcre2: {names}")
    if arches and arches[0] != "x86_64":
        die(f"Downloaded pcre2 has wrong architecture: {arches[0]}")

    print(
        "Verified Arch package:",
        names[0],
        versions[0] if versions else "unknown",
    )

    extracted = workdir / "pcre2-package"
    extracted.mkdir()

    run(
        [
            "tar",
            "--zstd",
            "--same-owner",
            "--same-permissions",
            "-xpf",
            str(package),
            "-C",
            str(extracted),
        ]
    )

    source_directory = extracted / "usr/lib"
    if not source_directory.is_dir():
        die("The Arch pcre2 package has no /usr/lib payload.")

    families = (
        "libpcre2-8.so",
        "libpcre2-16.so",
        "libpcre2-32.so",
        "libpcre2-posix.so",
    )

    copied = 0
    target_directory = root / "usr/lib"
    target_directory.mkdir(parents=True, exist_ok=True)

    for source in sorted(source_directory.iterdir()):
        if not any(
            source.name == family or source.name.startswith(family + ".")
            for family in families
        ):
            continue

        destination = target_directory / source.name

        if destination.exists() or destination.is_symlink():
            backup_file(root, destination, backup_dir)
            remove_path(destination)

        if source.is_symlink():
            destination.symlink_to(os.readlink(source))
        elif source.is_file():
            shutil.copy2(source, destination, follow_symlinks=False)
        else:
            continue

        copied += 1

    if copied == 0:
        die("No PCRE2 libraries were copied from the Arch package.")

    # Replace stale Debian-layout PCRE2 runtime copies with relative links to
    # Arch's canonical flat /usr/lib SONAMEs.
    compatibility = {
        "lib/x86_64-linux-gnu": "../../usr/lib/{soname}",
        "usr/lib/x86_64-linux-gnu": "../{soname}",
        "lib64": "../usr/lib/{soname}",
    }

    sonames = (
        "libpcre2-8.so.0",
        "libpcre2-16.so.0",
        "libpcre2-32.so.0",
        "libpcre2-posix.so.3",
    )

    for soname in sonames:
        canonical = root / "usr/lib" / soname
        if not canonical.exists() and not canonical.is_symlink():
            die(f"Arch PCRE2 package did not provide /usr/lib/{soname}")

        try:
            canonical.resolve(strict=True)
        except FileNotFoundError:
            die(f"Broken Arch PCRE2 SONAME link: /usr/lib/{soname}")

        for relative_directory, target_template in compatibility.items():
            directory = root / relative_directory
            directory.mkdir(parents=True, exist_ok=True)

            if directory.is_symlink():
                continue

            # Remove only this PCRE2 library family from the compatibility dir.
            for candidate in list(directory.glob(soname)) + list(
                directory.glob(soname + ".*")
            ):
                if not (candidate.exists() or candidate.is_symlink()):
                    continue
                backup_file(root, candidate, backup_dir)
                remove_path(candidate)

            link = directory / soname
            link.symlink_to(target_template.format(soname=soname))

    config = root / "etc/ld.so.conf.d/00-arch-selinux.conf"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "# Prefer Arch SELinux and PCRE2 libraries.\n/usr/lib\n",
        encoding="utf-8",
        newline="\n",
    )
    config.chmod(0o644)

    (root / "etc/ld.so.cache").unlink(missing_ok=True)
    print(f"Staged {copied} Arch PCRE2 library files and links.")


def bind_runtime_filesystems(root: Path) -> list[Path]:
    mounted: list[Path] = []

    for source, relative in (
        ("/dev", "dev"),
        ("/proc", "proc"),
        ("/sys", "sys"),
        ("/run", "run"),
    ):
        destination = root / relative
        destination.mkdir(parents=True, exist_ok=True)
        run(["mount", "--bind", source, str(destination)])
        mounted.append(destination)

    return mounted


def unmount_runtime_filesystems(paths: list[Path]) -> None:
    for path in reversed(paths):
        run(["umount", str(path)], check=False)


def target_tool(root: Path, candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        if (root / candidate.lstrip("/")).is_file():
            return candidate
    return None


def rebuild_linker_cache(root: Path) -> None:
    ldconfig = target_tool(
        root,
        (
            "/usr/sbin/ldconfig",
            "/sbin/ldconfig",
            "/usr/bin/ldconfig",
            "/bin/ldconfig",
        ),
    )
    if ldconfig is None:
        die("Target ldconfig is missing.")

    run(["chroot", str(root), ldconfig])


def verify_pcre2_runtime(root: Path) -> None:
    ldd = target_tool(root, ("/usr/bin/ldd", "/bin/ldd"))
    if ldd is None:
        die("Target ldd is missing.")

    for executable in ("/usr/bin/semodule", "/usr/bin/setfiles"):
        if not (root / executable.lstrip("/")).is_file():
            die(f"Target executable is missing: {executable}")

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
        if "no version information available" in result.stdout:
            die(f"{executable} still loads the stale unversioned PCRE2 library.")
        if "libpcre2-8.so.0 => /usr/lib/libpcre2-8.so.0" not in result.stdout:
            die(f"{executable} is not loading Arch PCRE2 from /usr/lib.")

    print("Arch PCRE2 runtime verification: PASS")


def verify_pam(root: Path) -> None:
    missing = [relative for relative in PAM_REQUIRED if not (root / relative).is_file()]
    if missing:
        die("Persistent PAM files are missing: " + ", ".join(missing))

    login_text = (root / "etc/pam.d/login").read_text(
        encoding="utf-8", errors="replace"
    )
    if "pam_console.so" in login_text:
        die("Dead pam_console.so reference returned to /etc/pam.d/login.")
    if "pam_selinux.so open" not in login_text:
        die("PAM SELinux open-session hook is missing.")

    print("Persistent PAM login stack verification: PASS")


def reset_policy_store(root: Path, backup_dir: Path) -> None:
    for relative in (
        "etc/selinux/refpolicy-arch",
        "var/lib/selinux/refpolicy-arch",
    ):
        path = root / relative

        if path.exists() or path.is_symlink():
            # Stores are large; keep a timestamp marker rather than duplicating
            # hundreds of megabytes of failed temporary CIL.
            marker = backup_dir / (relative.replace("/", "_") + ".removed.txt")
            marker.write_text(
                f"Removed stale/failed policy store: /{relative}\n",
                encoding="utf-8",
            )
            remove_path(path)

    (root / "var/lib/selinux").mkdir(parents=True, exist_ok=True)


def build_policy_one_transaction(root: Path) -> None:
    modules = sorted(
        (root / "usr/share/selinux/refpolicy-arch").glob("*.pp")
    )
    if not modules:
        die("No refpolicy-arch .pp modules are installed.")

    print(f"Building {len(modules)} modules in ONE semodule transaction.")

    shell = target_tool(root, ("/bin/bash", "/usr/bin/bash", "/bin/sh"))
    if shell is None:
        die("No target shell is available.")

    # This intentionally matches the package's official post_install behavior:
    # semodule -s refpolicy-arch -i /usr/share/selinux/refpolicy-arch/*.pp
    command = (
        "set -eu; "
        "exec /usr/bin/semodule -s refpolicy-arch -i "
        "/usr/share/selinux/refpolicy-arch/*.pp"
    )

    result = run(
        ["chroot", str(root), shell, "-c", command],
        check=False,
        capture=True,
    )
    if result.stdout:
        print(result.stdout.rstrip())

    if result.returncode != 0:
        die("The single-transaction refpolicy build failed.")

    listing = run(
        [
            "chroot",
            str(root),
            "/usr/bin/semodule",
            "-s",
            "refpolicy-arch",
            "-l",
        ],
        check=False,
        capture=True,
    )
    if listing.stdout:
        print(
            "Installed policy modules reported by semodule:",
            len(listing.stdout.splitlines()),
        )

    if listing.returncode != 0 or not listing.stdout.strip():
        die("The refpolicy-arch module store did not verify.")


def verify_compiled_policy(root: Path) -> tuple[Path, Path]:
    policy_directory = root / "etc/selinux/refpolicy-arch/policy"
    policies = sorted(
        (
            path
            for path in policy_directory.glob("policy.*")
            if path.is_file()
            and path.name.rsplit(".", 1)[-1].isdigit()
        ),
        key=lambda path: int(path.name.rsplit(".", 1)[-1]),
        reverse=True,
    )

    if not policies:
        die("No compiled policy.N was generated.")

    contexts = (
        root
        / "etc/selinux/refpolicy-arch/contexts/files/file_contexts"
    )
    if not contexts.is_file():
        die("The generated policy store is missing file_contexts.")

    print(f"Compiled policy: /{policies[0].relative_to(root)}")
    print(f"File contexts: /{contexts.relative_to(root)}")
    return policies[0], contexts


def configure_selinux(root: Path) -> None:
    config = root / "etc/selinux/config"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "# First fully labeled boot remains permissive.\n"
        "SELINUX=permissive\n"
        "SELINUXTYPE=refpolicy-arch\n",
        encoding="utf-8",
        newline="\n",
    )
    config.chmod(0o644)

    targeted = root / "etc/selinux/targeted"
    if targeted.exists() or targeted.is_symlink():
        remove_path(targeted)
    targeted.symlink_to("refpolicy-arch")


def relabel_filesystem(root: Path, contexts: Path) -> None:
    setfiles = target_tool(
        root,
        (
            "/usr/bin/setfiles",
            "/usr/sbin/setfiles",
            "/sbin/setfiles",
        ),
    )
    if setfiles is None:
        die("setfiles is missing.")

    target_contexts = "/" + str(contexts.relative_to(root))

    result = run(
        [
            "chroot",
            str(root),
            setfiles,
            "-F",
            "-r",
            "/",
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
        die(
            "The policy compiled, but the offline filesystem relabel failed. "
            "Boot remains permissive and rescue entries remain available."
        )

    (root / ".autorelabel").unlink(missing_ok=True)
    print("Offline SELinux filesystem relabel: PASS")


def patch_normal_loader_entries() -> None:
    entries = ISO_ROOT / "loader/entries"
    if not entries.is_dir():
        die(f"Loader entries directory not found: {entries}")

    for path in sorted(entries.glob("*.conf")):
        lowered = path.name.lower()

        if any(
            token in lowered
            for token in ("selinux-off", "direct-shell", "debug-shell")
        ):
            print(f"Preserving rescue entry: {path.name}")
            continue

        output: list[str] = []
        changed = False

        for raw in path.read_text(
            encoding="utf-8", errors="strict"
        ).splitlines():
            stripped = raw.strip()

            if not stripped.startswith("options "):
                output.append(raw)
                continue

            tokens = stripped[len("options "):].split()
            tokens = [
                token
                for token in tokens
                if token
                not in {
                    "selinux=0",
                    "selinux=1",
                    "enforcing=0",
                    "enforcing=1",
                }
            ]
            tokens.extend(["selinux=1", "enforcing=0"])

            new_line = "options " + " ".join(dict.fromkeys(tokens))
            output.append(new_line)
            changed = changed or new_line != raw

        if changed:
            path.write_text(
                "\n".join(output).rstrip() + "\n",
                encoding="utf-8",
                newline="\n",
            )
            path.chmod(0o644)
            print(f"Patched normal permissive entry: {path.name}")


def sync_loader_entries_to_efi() -> None:
    mountpoint = Path(
        tempfile.mkdtemp(prefix="efi-finish-selinux-", dir="/mnt")
    )
    mounted = False

    try:
        result = run(
            [
                "mount",
                "-o",
                "loop,rw,sync",
                str(EFI_IMAGE),
                str(mountpoint),
            ],
            check=False,
            capture=True,
        )
        if result.returncode != 0:
            die(f"Could not mount efiboot.img:\n{result.stdout}")
        mounted = True

        destination = mountpoint / "loader/entries"
        destination.mkdir(parents=True, exist_ok=True)

        for source in sorted(
            (ISO_ROOT / "loader/entries").glob("*.conf")
        ):
            shutil.copy2(source, destination / source.name)

        loader_conf = ISO_ROOT / "loader/loader.conf"
        if loader_conf.is_file():
            target = mountpoint / "loader/loader.conf"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(loader_conf, target)

        os.sync()
        print("Synchronized loader entries into efiboot.img.")

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
    ensure_qemu_closed()

    for command in (
        "tar",
        "losetup",
        "findmnt",
        "mount",
        "umount",
        "e2fsck",
        "chroot",
        "sync",
    ):
        require_command(command)

    for path in (ROOTFS, EFI_IMAGE, ISO_BUILDER):
        if not path.exists():
            die(f"Required path not found: {path}")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = BACKUP_ROOT / f"finish-selinux-{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    workdir = Path(tempfile.mkdtemp(prefix="finish-selinux-work-"))
    pcre2_package = workdir / "pcre2.pkg.tar.zst"
    download(PCRE2_URL, pcre2_package)

    clear_stale_loops(ROOTFS)
    run(["e2fsck", "-f", "-y", str(ROOTFS)])

    loop = run(
        ["losetup", "--find", "--show", str(ROOTFS)],
        capture=True,
    ).stdout.strip()

    mountpoint = Path(
        tempfile.mkdtemp(prefix="finish-selinux-root-", dir="/mnt")
    )
    mounted = False
    bindings: list[Path] = []

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

        verify_pam(mountpoint)
        stage_arch_pcre2(
            mountpoint,
            pcre2_package,
            workdir,
            backup_dir,
        )

        bindings = bind_runtime_filesystems(mountpoint)
        rebuild_linker_cache(mountpoint)
        verify_pcre2_runtime(mountpoint)

        reset_policy_store(mountpoint, backup_dir)
        build_policy_one_transaction(mountpoint)
        policy, contexts = verify_compiled_policy(mountpoint)
        configure_selinux(mountpoint)

        unmount_runtime_filesystems(bindings)
        bindings = []

        relabel_filesystem(mountpoint, contexts)
        run(["sync"])

        print(f"Final compiled policy verified: /{policy.relative_to(mountpoint)}")

    finally:
        if bindings:
            unmount_runtime_filesystems(bindings)
        if mounted:
            run(["umount", str(mountpoint)], check=False)
        run(["losetup", "--detach", loop], check=False)
        shutil.rmtree(workdir, ignore_errors=True)

        try:
            mountpoint.rmdir()
        except OSError:
            pass

    run(["e2fsck", "-f", "-y", str(ROOTFS)])
    run(["e2fsck", "-fn", str(ROOTFS)])

    patch_normal_loader_entries()
    sync_loader_entries_to_efi()

    print("=== Rebuilding ISO ===")
    run([sys.executable, str(ISO_BUILDER)])

    print()
    print("=== SUCCESS ===")
    print("Arch PCRE2 now matches libselinux 3.10.")
    print("All refpolicy modules were built in one transaction.")
    print("Compiled policy and file contexts verified.")
    print("The root filesystem was relabeled offline.")
    print("The persistent PAM login stack remains verified.")
    print("Normal boot is SELinux permissive; rescue entries remain.")
    print(f"Backups: {backup_dir}")


if __name__ == "__main__":
    main()
