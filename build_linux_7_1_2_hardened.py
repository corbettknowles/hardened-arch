#!/usr/bin/env python3
"""
Build Linux 7.1.2 for x86_64 with a comprehensive hardened configuration.

Target design
-------------
* x86_64 UEFI firmware
* systemd-compatible kernel feature set and cgroup v2 support
* Btrfs root filesystem support built into the kernel
* SELinux as the selected major LSM, with Lockdown, Yama, Landlock,
  Integrity/IMA/EVM, and BPF LSM support
* signed, compressed kernel modules
* static Toybox rescue/early-userspace initramfs
* systemd and Toybox source repositories cloned beside the kernel sources
* systemd-boot ESP staging tree, using an installed systemd-boot binary when
  available or an optional source build

This script intentionally does not install an SELinux policy. Enforcing SELinux
requires SELinux userspace, a policy, filesystem labels, and an appropriate
/etc/selinux/config in the target root filesystem.

The script is conservative with host resources by default: two parallel jobs,
nice level 15, and idle-class I/O priority when ionice is available.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Mapping, Sequence

KERNEL_VERSION = "7.1.2"
KERNEL_TAG = f"v{KERNEL_VERSION}"
DEFAULT_LINUX_REPO = Path.home() / "linux"
DEFAULT_SOURCE_DIR = Path.home() / f"linux-{KERNEL_VERSION}"
DEFAULT_BUILD_DIR = Path.home() / f"linux-{KERNEL_VERSION}-build"
DEFAULT_STAGE_DIR = Path.home() / f"linux-{KERNEL_VERSION}-stage"
DEFAULT_USERSPACE_ROOT = Path.home() / "userspace-src"
DEFAULT_ARTIFACT_DIR = Path.home() / f"linux-{KERNEL_VERSION}-artifacts"

LINUX_REPO_URL = "https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git"
TOYBOX_REPO_URL = "https://codeberg.org/landley/toybox.git"
SYSTEMD_REPO_URL = "https://github.com/systemd/systemd.git"

ARCH_BUILD_DEPENDENCIES = [
    "base-devel",
    "bc",
    "bison",
    "cpio",
    "flex",
    "git",
    "kmod",
    "libelf",
    "meson",
    "ninja",
    "openssl",
    "pahole",
    "perl",
    "python",
    "python-jinja",
    "python-lxml",
    "python-pefile",
    "python-pyelftools",
    "rsync",
    "xz",
    "zstd",
    "gperf",
]

# Required symbols are checked after olddefconfig. If one is unavailable or
# rejected because of unmet dependencies, the build stops before compilation.
REQUIRED_ENABLE = {
    # UEFI and console
    "EFI",
    "EFI_STUB",
    "EFI_PARTITION",
    "EFIVAR_FS",
    "FB_EFI",
    "FRAMEBUFFER_CONSOLE",
    # Root filesystem and early userspace
    "BTRFS_FS",
    "BTRFS_FS_POSIX_ACL",
    "DEVTMPFS",
    "DEVTMPFS_MOUNT",
    "TMPFS",
    "TMPFS_XATTR",
    "TMPFS_POSIX_ACL",
    # Security baseline
    "SECURITY",
    "SECURITYFS",
    "SECURITY_SELINUX",
    "AUDIT",
    "AUDITSYSCALL",
    "SECCOMP",
    "SECCOMP_FILTER",
    # systemd fundamentals
    "CGROUPS",
    "NAMESPACES",
    "NET_NS",
    "USER_NS",
    "PID_NS",
    "UTS_NS",
    "IPC_NS",
}

# Comprehensive hardened profile. Some entries are architecture-, compiler-,
# or version-dependent. The script reports every requested setting that the
# kernel ultimately drops or changes.
ENABLE_SYMBOLS = [
    # Core platform / boot
    "64BIT",
    "X86_64",
    "SMP",
    "ACPI",
    "PCI",
    "EFI",
    "EFI_STUB",
    "EFI_MIXED",
    "EFI_PARTITION",
    "EFIVAR_FS",
    "EFI_VARS_PSTORE",
    "PSTORE",
    "PSTORE_EFI",
    "FB_EFI",
    "FRAMEBUFFER_CONSOLE",
    "DRM",
    "DRM_SIMPLEDRM",
    "VT",
    "VT_CONSOLE",
    "RTC_CLASS",
    "RTC_HCTOSYS",
    # Early userspace and filesystems
    "DEVTMPFS",
    "DEVTMPFS_MOUNT",
    "PROC_FS",
    "SYSFS",
    "TMPFS",
    "TMPFS_XATTR",
    "TMPFS_POSIX_ACL",
    "BTRFS_FS",
    "BTRFS_FS_POSIX_ACL",
    "EXT4_FS",
    "EXT4_FS_POSIX_ACL",
    "FAT_FS",
    "VFAT_FS",
    "AUTOFS_FS",
    "OVERLAY_FS",
    "INOTIFY_USER",
    "FANOTIFY",
    "FHANDLE",
    # Generic storage needed for ordinary x86_64 UEFI machines
    "BLOCK",
    "PARTITION_ADVANCED",
    "SCSI",
    "BLK_DEV_SD",
    "BLK_DEV_BSG",
    "ATA",
    "SATA_AHCI",
    "BLK_DEV_NVME",
    "USB",
    "USB_XHCI_HCD",
    "USB_EHCI_HCD",
    "USB_OHCI_HCD",
    "USB_UHCI_HCD",
    "USB_STORAGE",
    "HID",
    "HID_GENERIC",
    "USB_HID",
    "INPUT_EVDEV",
    # systemd-required/recommended namespaces, cgroups, IPC and networking
    "CGROUPS",
    "CGROUP_SCHED",
    "FAIR_GROUP_SCHED",
    "CFS_BANDWIDTH",
    "CPUSETS",
    "MEMCG",
    "BLK_CGROUP",
    "CGROUP_PIDS",
    "CGROUP_FREEZER",
    "CGROUP_HUGETLB",
    "CGROUP_RDMA",
    "CGROUP_BPF",
    "PSI",
    "NAMESPACES",
    "UTS_NS",
    "IPC_NS",
    "USER_NS",
    "PID_NS",
    "NET_NS",
    "TIME_NS",
    "SECCOMP",
    "SECCOMP_FILTER",
    "KCMP",
    "DMI",
    "DMIID",
    "DMI_SYSFS",
    "NET",
    "PACKET",
    "UNIX",
    "INET",
    "IPV6",
    "NET_SCHED",
    "NET_SCH_FQ_CODEL",
    "SYN_COOKIES",
    # BPF features used by current systemd sandboxing and BPF LSM
    "BPF",
    "BPF_SYSCALL",
    "BPF_JIT",
    "BPF_JIT_ALWAYS_ON",
    "BPF_UNPRIV_DEFAULT_OFF",
    "BPF_LSM",
    # LSM / SELinux / integrity
    "SECURITY",
    "SECURITYFS",
    "SECURITY_NETWORK",
    "SECURITY_NETWORK_XFRM",
    "SECURITY_PATH",
    "SECURITY_SELINUX",
    "SECURITY_SELINUX_BOOTPARAM",
    "SECURITY_SELINUX_AVC_STATS",
    "SECURITY_YAMA",
    "SECURITY_LANDLOCK",
    "SECURITY_LOCKDOWN_LSM",
    "SECURITY_LOCKDOWN_LSM_EARLY",
    "LOCK_DOWN_KERNEL_FORCE_CONFIDENTIALITY",
    "INTEGRITY",
    "INTEGRITY_SIGNATURE",
    "INTEGRITY_ASYMMETRIC_KEYS",
    "INTEGRITY_TRUSTED_KEYRING",
    "INTEGRITY_PLATFORM_KEYRING",
    "INTEGRITY_MACHINE_KEYRING",
    "IMA",
    "IMA_APPRAISE",
    "IMA_ARCH_POLICY",
    "IMA_SECURE_AND_OR_TRUSTED_BOOT",
    "EVM",
    "EVM_ATTR_FSUUID",
    # Compile alternate major LSMs, but do not select them in CONFIG_LSM.
    # SELinux remains the selected major policy LSM.
    "SECURITY_TOMOYO",
    "SECURITY_SMACK",
    # Auditing
    "AUDIT",
    "AUDITSYSCALL",
    # Memory, structure, and attack-surface hardening
    "STACKPROTECTOR",
    "STACKPROTECTOR_STRONG",
    "FORTIFY_SOURCE",
    "HARDENED_USERCOPY",
    "HARDENED_USERCOPY_PAGESPAN",
    "INIT_ON_ALLOC_DEFAULT_ON",
    "INIT_ON_FREE_DEFAULT_ON",
    "SLAB_FREELIST_HARDENED",
    "SLAB_FREELIST_RANDOM",
    "RANDOM_KMALLOC_CACHES",
    "SHUFFLE_PAGE_ALLOCATOR",
    "VMAP_STACK",
    "SCHED_STACK_END_CHECK",
    "LIST_HARDENED",
    "BUG_ON_DATA_CORRUPTION",
    "STRICT_KERNEL_RWX",
    "STRICT_MODULE_RWX",
    "RANDOMIZE_BASE",
    "RANDOMIZE_MEMORY",
    "PAGE_TABLE_ISOLATION",
    "RETPOLINE",
    "CPU_MITIGATIONS",
    "ZERO_CALL_USED_REGS",
    "SECURITY_DMESG_RESTRICT",
    "SECURITY_PERF_EVENTS_RESTRICT",
    "SECURITY_TIOCSTI_RESTRICT",
    # GCC-plugin hardening (silently skipped when unsupported)
    "GCC_PLUGINS",
    "GCC_PLUGIN_LATENT_ENTROPY",
    "GCC_PLUGIN_STRUCTLEAK",
    "GCC_PLUGIN_STRUCTLEAK_BYREF_ALL",
    "GCC_PLUGIN_RANDSTRUCT",
    "RANDSTRUCT_FULL",
    "GCC_PLUGIN_STACKLEAK",
    # Kernel/module integrity
    "MODULES",
    "MODULE_SIG",
    "MODULE_SIG_ALL",
    "MODULE_SIG_FORCE",
    "MODULE_SIG_SHA512",
    "MODULE_COMPRESS_ZSTD",
    "CRYPTO_SHA512",
    "KEXEC_FILE",
    "KEXEC_SIG",
    "KEXEC_SIG_FORCE",
    "DM_VERITY",
    "DM_VERITY_VERIFY_ROOTHASH_SIG",
    "DM_VERITY_VERIFY_ROOTHASH_SIG_SECONDARY_KEYRING",
    "DM_VERITY_VERIFY_ROOTHASH_SIG_PLATFORM_KEYRING",
    # Useful diagnosis without enabling broad live tracing interfaces
    "IKCONFIG",
    "IKCONFIG_PROC",
    "KALLSYMS",
    "DEBUG_INFO",
    "DEBUG_INFO_DWARF5",
    "DEBUG_INFO_BTF",
    # Desktop compatibility
    "IA32_EMULATION",
    "BINFMT_ELF",
    "BINFMT_SCRIPT",
    "BINFMT_MISC",
]

DISABLE_SYMBOLS = [
    # systemd explicitly recommends these disabled
    "SYSFS_DEPRECATED",
    "UEVENT_HELPER",
    "FW_LOADER_USER_HELPER",
    "RT_GROUP_SCHED",
    # SELinux must not be runtime-disableable; development mode off
    "SECURITY_SELINUX_DEVELOP",
    "SECURITY_SELINUX_DISABLE",
    # Prevent permissive fallback in hardened usercopy
    "HARDENED_USERCOPY_FALLBACK",
    # Legacy/raw kernel memory interfaces
    "DEVKMEM",
    "DEVMEM",
    "DEVPORT",
    "PROC_KCORE",
    "ACPI_CUSTOM_METHOD",
    # Legacy or dangerous execution paths
    "KEXEC",  # Keep KEXEC_FILE with signature enforcement instead.
    "HIBERNATION",
    "COMPAT_BRK",
    "X86_X32",
    "X86_16BIT",
    "MODIFY_LDT_SYSCALL",
    "LEGACY_PTYS",
    # Module bypasses
    "MODULE_FORCE_LOAD",
    "MODULE_FORCE_UNLOAD",
    # Build reliability
    "WERROR",
    # Broad runtime instrumentation attack surface; BTF remains enabled.
    "DEBUG_FS",
    "KPROBES",
    "UPROBES",
    "FTRACE",
    "FUNCTION_TRACER",
    "DYNAMIC_FTRACE",
]

STRING_SYMBOLS = {
    "LOCALVERSION": f"-corbett-hardened-{KERNEL_VERSION}",
    "DEFAULT_HOSTNAME": "hardened-arch",
    "UEVENT_HELPER_PATH": "",
    "SYSTEM_TRUSTED_KEYS": "",
    "SYSTEM_REVOCATION_KEYS": "",
    # Minor LSMs plus SELinux and BPF. TOMOYO/SMACK are compiled but inactive
    # unless the boot-time lsm= list is deliberately changed.
    "LSM": "landlock,lockdown,yama,integrity,selinux,bpf",
}

VALUE_SYMBOLS = {
    "PANIC_TIMEOUT": "10",
    "SECURITY_SELINUX_CHECKREQPROT_VALUE": "0",
    "LEGACY_PTY_COUNT": "0",
}

CLANG_ENABLE = [
    "LTO_CLANG_THIN",
    "CFI_CLANG",
]

CLANG_DISABLE = [
    "CFI_PERMISSIVE",
]


class BuildError(RuntimeError):
    """Expected build/setup failure with a user-readable message."""


def qcmd(cmd: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(x)) for x in cmd)


def log(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def run(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    capture: bool = False,
    check: bool = True,
    low_priority: bool = False,
) -> subprocess.CompletedProcess[str]:
    actual = [str(x) for x in cmd]
    if low_priority:
        prefix: list[str] = []
        if shutil.which("ionice"):
            prefix += ["ionice", "-c3"]
        if shutil.which("nice"):
            prefix += ["nice", "-n", "15"]
        actual = prefix + actual

    print(f"+ {qcmd(actual)}", flush=True)
    result = subprocess.run(
        actual,
        cwd=str(cwd) if cwd else None,
        env=dict(env) if env else None,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
    )
    if check and result.returncode != 0:
        detail = ""
        if capture:
            detail = f"\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        raise BuildError(f"Command failed ({result.returncode}): {qcmd(actual)}{detail}")
    return result


def require_tools(names: Iterable[str]) -> None:
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise BuildError(
            "Missing required tools: "
            + ", ".join(missing)
            + "\nOn Arch, run this script with --install-deps or install the listed packages."
        )


def install_arch_dependencies() -> None:
    if shutil.which("pacman") is None:
        raise BuildError("--install-deps is implemented for Arch/pacman only.")
    run(["sudo", "pacman", "-Syu", "--needed", *ARCH_BUILD_DEPENDENCIES])


def git_output(repo: Path, *args: str) -> str:
    return run(["git", "-C", str(repo), *args], capture=True).stdout.strip()


def ensure_git_repo(path: Path, url: str, ref: str | None, shallow: bool = False) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        clone_cmd = ["git", "clone"]
        if shallow and ref:
            clone_cmd += ["--depth", "1", "--branch", ref]
        clone_cmd += [url, str(path)]
        run(clone_cmd)
    elif not (path / ".git").exists():
        raise BuildError(f"{path} exists but is not a Git repository.")

    current_url = git_output(path, "remote", "get-url", "origin")
    if current_url != url:
        log(f"Repository {path} uses origin {current_url}; leaving it unchanged")

    run(["git", "-C", str(path), "fetch", "--tags", "--prune", "origin"])
    if ref:
        run(["git", "-C", str(path), "checkout", "--detach", ref])


def ensure_linux_source(linux_repo: Path, source_dir: Path, reset_source: bool) -> str:
    ensure_git_repo(linux_repo, LINUX_REPO_URL, None)
    run(["git", "-C", str(linux_repo), "fetch", "origin", "tag", KERNEL_TAG])
    tag_commit = git_output(linux_repo, "rev-parse", f"{KERNEL_TAG}^{{commit}}")

    if source_dir.exists():
        try:
            existing_commit = git_output(source_dir, "rev-parse", "HEAD")
        except BuildError as exc:
            raise BuildError(f"{source_dir} exists but is not a usable Git worktree") from exc
        if existing_commit != tag_commit:
            if not reset_source:
                raise BuildError(
                    f"{source_dir} is at {existing_commit[:12]}, not {KERNEL_TAG} "
                    f"({tag_commit[:12]}). Use --reset-source only if discarding changes is safe."
                )
            run(["git", "-C", str(source_dir), "reset", "--hard", tag_commit])
            run(["git", "-C", str(source_dir), "clean", "-fdx"])
    else:
        source_dir.parent.mkdir(parents=True, exist_ok=True)
        run(
            [
                "git",
                "-C",
                str(linux_repo),
                "worktree",
                "add",
                "--detach",
                str(source_dir),
                tag_commit,
            ]
        )

    actual = git_output(source_dir, "describe", "--tags", "--exact-match", "HEAD")
    if actual != KERNEL_TAG:
        raise BuildError(f"Kernel source verification failed: expected {KERNEL_TAG}, got {actual}")
    return tag_commit


def build_env(compiler: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "KBUILD_BUILD_USER": "corbett",
            "KBUILD_BUILD_HOST": "hardened-arch-builder",
            "KBUILD_BUILD_TIMESTAMP": "1970-01-01 00:00:00 UTC",
            "KCONFIG_NOTIMESTAMP": "1",
        }
    )
    if compiler == "clang":
        env.update({"LLVM": "1", "LLVM_IAS": "1"})
    return env


def config_tool(source_dir: Path) -> Path:
    tool = source_dir / "scripts" / "config"
    if not tool.exists():
        raise BuildError(f"Kernel configuration helper not found: {tool}")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return tool


def config_cmd(tool: Path, config: Path, *args: str) -> None:
    run([str(tool), "--file", str(config), *args])


def apply_kernel_config(source_dir: Path, build_dir: Path, compiler: str) -> dict[str, str]:
    cfg = build_dir / ".config"
    tool = config_tool(source_dir)

    log("Applying hardened kernel configuration")
    for symbol in ENABLE_SYMBOLS:
        config_cmd(tool, cfg, "--enable", symbol)
    for symbol in DISABLE_SYMBOLS:
        config_cmd(tool, cfg, "--disable", symbol)
    for symbol, value in STRING_SYMBOLS.items():
        config_cmd(tool, cfg, "--set-str", symbol, value)
    for symbol, value in VALUE_SYMBOLS.items():
        config_cmd(tool, cfg, "--set-val", symbol, value)

    if compiler == "clang":
        for symbol in CLANG_ENABLE:
            config_cmd(tool, cfg, "--enable", symbol)
        for symbol in CLANG_DISABLE:
            config_cmd(tool, cfg, "--disable", symbol)

    # Resolve dependencies/defaults, then apply once more so options whose
    # parents became visible get another chance to stick.
    env = build_env(compiler)
    run(["make", f"O={build_dir}", "olddefconfig"], cwd=source_dir, env=env)

    for symbol in ENABLE_SYMBOLS:
        config_cmd(tool, cfg, "--enable", symbol)
    for symbol in DISABLE_SYMBOLS:
        config_cmd(tool, cfg, "--disable", symbol)
    for symbol, value in STRING_SYMBOLS.items():
        config_cmd(tool, cfg, "--set-str", symbol, value)
    for symbol, value in VALUE_SYMBOLS.items():
        config_cmd(tool, cfg, "--set-val", symbol, value)
    if compiler == "clang":
        for symbol in CLANG_ENABLE:
            config_cmd(tool, cfg, "--enable", symbol)
        for symbol in CLANG_DISABLE:
            config_cmd(tool, cfg, "--disable", symbol)

    run(["make", f"O={build_dir}", "olddefconfig"], cwd=source_dir, env=env)
    return parse_dot_config(cfg)


def parse_dot_config(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    unset_re = re.compile(r"^# CONFIG_([A-Za-z0-9_]+) is not set$")
    set_re = re.compile(r"^CONFIG_([A-Za-z0-9_]+)=(.*)$")
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = unset_re.match(line)
        if match:
            values[match.group(1)] = "n"
            continue
        match = set_re.match(line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def write_config_report(
    values: Mapping[str, str], artifact_dir: Path, compiler: str
) -> tuple[list[str], list[dict[str, str | None]]]:
    requested: dict[str, str] = {symbol: "y" for symbol in ENABLE_SYMBOLS}
    requested.update({symbol: "n" for symbol in DISABLE_SYMBOLS})
    requested.update({symbol: json.dumps(value) for symbol, value in STRING_SYMBOLS.items()})
    requested.update(VALUE_SYMBOLS)
    if compiler == "clang":
        requested.update({symbol: "y" for symbol in CLANG_ENABLE})
        requested.update({symbol: "n" for symbol in CLANG_DISABLE})

    mismatches: list[dict[str, str | None]] = []
    for symbol, wanted in sorted(requested.items()):
        actual = values.get(symbol)
        if actual != wanted:
            mismatches.append({"symbol": symbol, "requested": wanted, "actual": actual})

    missing_required = [symbol for symbol in sorted(REQUIRED_ENABLE) if values.get(symbol) != "y"]
    report = {
        "kernel_version": KERNEL_VERSION,
        "compiler": compiler,
        "required_missing": missing_required,
        "requested_but_changed_or_unavailable": mismatches,
        "selected_lsm": values.get("LSM"),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "kernel-config-report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )

    if mismatches:
        log(f"Kconfig report: {len(mismatches)} requested symbols were unavailable or changed")
        for item in mismatches[:30]:
            print(
                f"  {item['symbol']}: requested={item['requested']} actual={item['actual']}",
                flush=True,
            )
        if len(mismatches) > 30:
            print(f"  ...and {len(mismatches) - 30} more; see kernel-config-report.json")

    return missing_required, mismatches


def seed_kernel_config(
    source_dir: Path,
    build_dir: Path,
    base_config: Path | None,
    compiler: str,
) -> None:
    build_dir.mkdir(parents=True, exist_ok=True)
    env = build_env(compiler)
    if base_config:
        if not base_config.is_file():
            raise BuildError(f"Base config not found: {base_config}")
        shutil.copy2(base_config, build_dir / ".config")
        run(["make", f"O={build_dir}", "olddefconfig"], cwd=source_dir, env=env)
    else:
        log("No hardware-specific base config supplied; using x86_64_defconfig")
        run(["make", f"O={build_dir}", "x86_64_defconfig"], cwd=source_dir, env=env)


def build_kernel(
    source_dir: Path,
    build_dir: Path,
    stage_dir: Path,
    artifact_dir: Path,
    jobs: int,
    compiler: str,
) -> str:
    env = build_env(compiler)
    log(f"Building Linux {KERNEL_VERSION} with {jobs} job(s)")
    run(
        ["make", f"O={build_dir}", f"-j{jobs}", "bzImage", "modules"],
        cwd=source_dir,
        env=env,
        low_priority=True,
    )

    release = run(
        ["make", f"O={build_dir}", "-s", "kernelrelease"],
        cwd=source_dir,
        env=env,
        capture=True,
    ).stdout.strip()
    if not release:
        raise BuildError("Unable to determine kernel release string")

    modules_root = stage_dir / "rootfs"
    modules_root.mkdir(parents=True, exist_ok=True)
    run(
        [
            "make",
            f"O={build_dir}",
            f"INSTALL_MOD_PATH={modules_root}",
            "modules_install",
        ],
        cwd=source_dir,
        env=env,
        low_priority=True,
    )

    headers_root = stage_dir / "headers"
    headers_root.mkdir(parents=True, exist_ok=True)
    run(
        [
            "make",
            f"O={build_dir}",
            f"INSTALL_HDR_PATH={headers_root}",
            "headers_install",
        ],
        cwd=source_dir,
        env=env,
        low_priority=True,
    )

    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifacts = {
        build_dir / "arch" / "x86" / "boot" / "bzImage": artifact_dir / f"vmlinuz-{release}.efi",
        build_dir / "System.map": artifact_dir / f"System.map-{release}",
        build_dir / ".config": artifact_dir / f"config-{release}",
        build_dir / "vmlinux": artifact_dir / f"vmlinux-{release}",
    }
    for source, destination in artifacts.items():
        if not source.exists():
            raise BuildError(f"Expected kernel artifact is missing: {source}")
        shutil.copy2(source, destination)

    modules_archive = artifact_dir / f"modules-{release}.tar.zst"
    run(
        [
            "tar",
            "--zstd",
            "-cpf",
            str(modules_archive),
            "-C",
            str(modules_root),
            "lib",
        ],
        low_priority=True,
    )
    return release


def build_toybox(toybox_dir: Path, jobs: int, clean: bool) -> Path:
    log("Building static Toybox")
    if clean:
        run(["make", "distclean"], cwd=toybox_dir, check=False)
    run(["make", "defconfig"], cwd=toybox_dir)

    env = os.environ.copy()
    if shutil.which("musl-gcc"):
        env["CC"] = "musl-gcc"
    env["LDFLAGS"] = "--static"
    run(
        ["make", f"-j{jobs}", "toybox"],
        cwd=toybox_dir,
        env=env,
        low_priority=True,
    )
    binary = toybox_dir / "toybox"
    if not binary.exists():
        raise BuildError("Toybox build completed without producing the toybox binary")

    file_result = run(["file", str(binary)], capture=True, check=False)
    if "statically linked" not in file_result.stdout:
        raise BuildError(
            "Toybox did not link statically. Install a static libc toolchain (musl-gcc is preferred) "
            "and rerun with --clean."
        )
    return binary


def init_script(default_root: str | None, btrfs_subvol: str | None) -> str:
    default_root_shell = shlex.quote(default_root or "")
    default_subvol_shell = shlex.quote(btrfs_subvol or "")
    return f"""#!/bin/sh
# Toybox rescue/early-userspace init for Linux {KERNEL_VERSION}.

PATH=/bin:/sbin:/usr/bin:/usr/sbin
export PATH

mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /dev/pts /run /newroot
mount -t devpts devpts /dev/pts
mount -t tmpfs tmpfs /run

ROOT_SPEC={default_root_shell}
ROOTFLAGS=""
ROOTFSTYPE="btrfs"
INIT_PATH="/sbin/init"

for word in $(cat /proc/cmdline); do
    case "$word" in
        root=*) ROOT_SPEC="${{word#root=}}" ;;
        rootflags=*) ROOTFLAGS="${{word#rootflags=}}" ;;
        rootfstype=*) ROOTFSTYPE="${{word#rootfstype=}}" ;;
        init=*) INIT_PATH="${{word#init=}}" ;;
        rd.shell|rd.shell=1) ROOT_SPEC="" ;;
    esac
done

resolve_root() {{
    case "$1" in
        UUID=*) blkid -U "${{1#UUID=}}" ;;
        LABEL=*) blkid -L "${{1#LABEL=}}" ;;
        PARTUUID=*)
            value="${{1#PARTUUID=}}"
            blkid -t "PARTUUID=$value" -o device | head -n 1
            ;;
        /dev/*) printf '%s\\n' "$1" ;;
        *) printf '%s\\n' "$1" ;;
    esac
}}

if [ -n "$ROOT_SPEC" ]; then
    ROOTDEV="$(resolve_root "$ROOT_SPEC")"
    attempt=0
    while [ ! -b "$ROOTDEV" ] && [ "$attempt" -lt 20 ]; do
        sleep 1
        ROOTDEV="$(resolve_root "$ROOT_SPEC")"
        attempt=$((attempt + 1))
    done

    if [ -n "$ROOTDEV" ] && [ -b "$ROOTDEV" ]; then
        if [ -z "$ROOTFLAGS" ] && [ -n {default_subvol_shell} ]; then
            ROOTFLAGS="subvol={default_subvol_shell}"
        fi
        if [ -n "$ROOTFLAGS" ]; then
            mount -t "$ROOTFSTYPE" -o "$ROOTFLAGS" "$ROOTDEV" /newroot
        else
            mount -t "$ROOTFSTYPE" "$ROOTDEV" /newroot
        fi
        if [ $? -eq 0 ] && [ -x "/newroot$INIT_PATH" ]; then
            mount --move /proc /newroot/proc
            mount --move /sys /newroot/sys
            mount --move /dev /newroot/dev
            mount --move /run /newroot/run
            exec switch_root /newroot "$INIT_PATH"
        fi
    fi
fi

echo
echo "Hardened Linux {KERNEL_VERSION} Toybox rescue shell"
echo "Root was not mounted or systemd was not found."
echo "Kernel command line example: root=UUID=<uuid> rootfstype=btrfs rootflags=subvol=@ rw"
echo
exec sh
"""


def build_initramfs(
    toybox_binary: Path,
    stage_dir: Path,
    artifact_dir: Path,
    release: str,
    default_root: str | None,
    btrfs_subvol: str | None,
) -> Path:
    log("Creating static Toybox rescue/initramfs")
    root = stage_dir / "initramfs"
    if root.exists():
        shutil.rmtree(root)
    for rel in [
        "bin",
        "sbin",
        "etc",
        "proc",
        "sys",
        "dev",
        "dev/pts",
        "run",
        "newroot",
        "usr/bin",
        "usr/sbin",
    ]:
        (root / rel).mkdir(parents=True, exist_ok=True)

    target = root / "bin" / "toybox"
    shutil.copy2(toybox_binary, target)
    target.chmod(0o755)

    # Install all available applet symlinks into /bin.
    run([str(target), "--install", "-s", str(root / "bin")])
    if not (root / "bin" / "switch_root").exists():
        raise BuildError("Built Toybox does not provide switch_root; cannot create the boot initramfs")
    if not (root / "bin" / "blkid").exists():
        raise BuildError("Built Toybox does not provide blkid; UUID/LABEL root resolution would fail")

    init = root / "init"
    init.write_text(init_script(default_root, btrfs_subvol), encoding="utf-8")
    init.chmod(0o755)

    (root / "etc" / "os-release").write_text(
        'NAME="Corbett Hardened Rescue"\n'
        f'VERSION="{KERNEL_VERSION}"\n'
        'ID=corbett-hardened-rescue\n',
        encoding="utf-8",
    )

    output = artifact_dir / f"initramfs-{release}.img.zst"
    shell_cmd = (
        "find . -print0 | "
        "cpio --null --create --format=newc --owner=0:0 | "
        f"zstd -T1 -19 -o {shlex.quote(str(output))}"
    )
    run(["bash", "-o", "pipefail", "-c", shell_cmd], cwd=root, low_priority=True)
    return output


def meson_option_names(systemd_dir: Path) -> set[str]:
    options_file = systemd_dir / "meson_options.txt"
    if not options_file.exists():
        return set()
    text = options_file.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r"option\(\s*['\"]([^'\"]+)['\"]", text))


def build_systemd_boot(systemd_dir: Path, build_root: Path, clean: bool) -> Path:
    log("Attempting a source build of systemd-boot")
    build_dir = build_root / "systemd-boot-build"
    if clean and build_dir.exists():
        shutil.rmtree(build_dir)

    options = meson_option_names(systemd_dir)
    desired = {
        "bootloader": "true",
        "ukify": "true",
        "tests": "false",
        "install-tests": "false",
        "man": "disabled",
        "html": "disabled",
        "mode": "release",
    }
    setup = ["meson", "setup", str(build_dir), str(systemd_dir)]
    if build_dir.exists() and (build_dir / "build.ninja").exists():
        setup = ["meson", "setup", "--reconfigure", str(build_dir), str(systemd_dir)]
    for name, value in desired.items():
        if name in options:
            setup.append(f"-D{name}={value}")
    run(setup)
    run(["ninja", "-C", str(build_dir), "systemd-boot"], low_priority=True)

    candidates = list(build_dir.rglob("systemd-bootx64.efi"))
    if not candidates:
        raise BuildError(
            "systemd-boot target completed but systemd-bootx64.efi was not found in the build tree"
        )
    return candidates[0]


def find_installed_systemd_boot() -> Path | None:
    candidates = [
        Path("/usr/lib/systemd/boot/efi/systemd-bootx64.efi"),
        Path("/usr/lib/systemd/boot/efi/systemd-bootx64.efi.signed"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def stage_esp(
    artifact_dir: Path,
    release: str,
    initramfs: Path,
    root_spec: str | None,
    btrfs_subvol: str | None,
    systemd_boot_binary: Path | None,
) -> Path:
    log("Staging systemd-boot EFI System Partition tree")
    esp = artifact_dir / "esp-tree"
    if esp.exists():
        shutil.rmtree(esp)
    (esp / "EFI" / "Linux").mkdir(parents=True, exist_ok=True)
    (esp / "EFI" / "systemd").mkdir(parents=True, exist_ok=True)
    (esp / "EFI" / "BOOT").mkdir(parents=True, exist_ok=True)
    (esp / "loader" / "entries").mkdir(parents=True, exist_ok=True)

    kernel_name = f"vmlinuz-{release}.efi"
    initrd_name = f"initramfs-{release}.img.zst"
    shutil.copy2(artifact_dir / kernel_name, esp / "EFI" / "Linux" / kernel_name)
    shutil.copy2(initramfs, esp / "EFI" / "Linux" / initrd_name)

    if systemd_boot_binary:
        shutil.copy2(systemd_boot_binary, esp / "EFI" / "systemd" / "systemd-bootx64.efi")
        shutil.copy2(systemd_boot_binary, esp / "EFI" / "BOOT" / "BOOTX64.EFI")
    else:
        (esp / "SYSTEMD_BOOT_BINARY_NOT_STAGED.txt").write_text(
            "No systemd-bootx64.efi was found or built. Install systemd-boot or rerun with "
            "--build-systemd-boot.\n",
            encoding="utf-8",
        )

    (esp / "loader" / "loader.conf").write_text(
        "default corbett-hardened.conf\ntimeout 5\nconsole-mode max\neditor no\n",
        encoding="utf-8",
    )

    options: list[str] = []
    if root_spec:
        options += [f"root={root_spec}", "rootfstype=btrfs"]
        if btrfs_subvol:
            options.append(f"rootflags=subvol={btrfs_subvol}")
        options.append("rw")
    else:
        options.append("rd.shell=1")
    options += [
        "lsm=landlock,lockdown,yama,integrity,selinux,bpf",
        "selinux=1",
        "enforcing=1",
        "audit=1",
        "slab_nomerge",
        "page_alloc.shuffle=1",
        "init_on_alloc=1",
        "init_on_free=1",
        "pti=on",
        "vsyscall=none",
        "randomize_kstack_offset=on",
        "module.sig_enforce=1",
    ]
    entry = (
        f"title Corbett Hardened Linux {KERNEL_VERSION}\n"
        f"version {release}\n"
        f"linux /EFI/Linux/{kernel_name}\n"
        f"initrd /EFI/Linux/{initrd_name}\n"
        f"options {' '.join(options)}\n"
    )
    (esp / "loader" / "entries" / "corbett-hardened.conf").write_text(entry, encoding="utf-8")
    return esp


def write_manifest(
    artifact_dir: Path,
    linux_commit: str,
    toybox_dir: Path,
    systemd_dir: Path,
    release: str,
    args: argparse.Namespace,
) -> None:
    def rev(path: Path) -> str | None:
        try:
            return git_output(path, "rev-parse", "HEAD")
        except BuildError:
            return None

    manifest = {
        "kernel_version": KERNEL_VERSION,
        "kernel_release": release,
        "linux_commit": linux_commit,
        "toybox_commit": rev(toybox_dir),
        "systemd_commit": rev(systemd_dir),
        "compiler": args.compiler,
        "jobs": args.jobs,
        "root_spec": args.root_spec,
        "btrfs_subvol": args.btrfs_subvol,
        "source_dir": str(args.source_dir),
        "build_dir": str(args.build_dir),
        "stage_dir": str(args.stage_dir),
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    (artifact_dir / "build-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a hardened Linux 7.1.2 x86_64 UEFI/systemd/Btrfs kernel and Toybox initramfs."
    )
    parser.add_argument("--linux-repo", type=Path, default=DEFAULT_LINUX_REPO)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--build-dir", type=Path, default=DEFAULT_BUILD_DIR)
    parser.add_argument("--stage-dir", type=Path, default=DEFAULT_STAGE_DIR)
    parser.add_argument("--userspace-root", type=Path, default=DEFAULT_USERSPACE_ROOT)
    parser.add_argument("--artifact-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument(
        "--base-config",
        type=Path,
        help="Hardware-tested kernel .config to harden. Without this, x86_64_defconfig is used.",
    )
    parser.add_argument("--jobs", type=int, default=2, help="Parallel build jobs (default: 2)")
    parser.add_argument("--compiler", choices=["gcc", "clang"], default="gcc")
    parser.add_argument(
        "--root-spec",
        help="Target root for initramfs/boot entry, e.g. UUID=... or /dev/nvme0n1p2",
    )
    parser.add_argument(
        "--btrfs-subvol",
        default="@",
        help="Btrfs root subvolume (default: @; pass empty string for none)",
    )
    parser.add_argument("--toybox-ref", help="Optional Toybox tag/commit/branch to pin")
    parser.add_argument("--systemd-ref", help="Optional systemd tag/commit/branch to pin")
    parser.add_argument(
        "--build-systemd-boot",
        action="store_true",
        help="Build systemd-boot from the cloned systemd source instead of using the installed EFI binary",
    )
    parser.add_argument("--install-deps", action="store_true", help="Install Arch build dependencies with pacman")
    parser.add_argument("--print-deps", action="store_true", help="Print Arch dependency package names and exit")
    parser.add_argument("--clean", action="store_true", help="Delete build/stage/artifact directories before building")
    parser.add_argument(
        "--reset-source",
        action="store_true",
        help="Hard-reset an existing linux-7.1.2 worktree to v7.1.2 (destructive to that worktree only)",
    )
    parser.add_argument("--skip-kernel", action="store_true")
    parser.add_argument("--skip-toybox", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.print_deps:
        print(" ".join(ARCH_BUILD_DEPENDENCIES))
        return 0
    if args.jobs < 1:
        raise BuildError("--jobs must be at least 1")
    if os.geteuid() == 0:
        raise BuildError("Run the build as user corbett, not as root. The script invokes sudo only for --install-deps.")

    args.linux_repo = args.linux_repo.expanduser().resolve()
    args.source_dir = args.source_dir.expanduser().resolve()
    args.build_dir = args.build_dir.expanduser().resolve()
    args.stage_dir = args.stage_dir.expanduser().resolve()
    args.userspace_root = args.userspace_root.expanduser().resolve()
    args.artifact_dir = args.artifact_dir.expanduser().resolve()
    if args.base_config:
        args.base_config = args.base_config.expanduser().resolve()

    if args.install_deps:
        install_arch_dependencies()

    require_tools(
        [
            "bash",
            "cpio",
            "file",
            "git",
            "make",
            "tar",
            "zstd",
            "meson",
            "ninja",
        ]
    )

    if args.clean:
        for path in [args.build_dir, args.stage_dir, args.artifact_dir]:
            if path.exists():
                log(f"Removing {path}")
                shutil.rmtree(path)

    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    args.userspace_root.mkdir(parents=True, exist_ok=True)

    log("Preparing Linux, Toybox, and systemd source repositories")
    linux_commit = ensure_linux_source(args.linux_repo, args.source_dir, args.reset_source)

    toybox_dir = args.userspace_root / "toybox"
    systemd_dir = args.userspace_root / "systemd"
    ensure_git_repo(toybox_dir, TOYBOX_REPO_URL, args.toybox_ref)
    ensure_git_repo(systemd_dir, SYSTEMD_REPO_URL, args.systemd_ref)

    release = ""
    if not args.skip_kernel:
        seed_kernel_config(args.source_dir, args.build_dir, args.base_config, args.compiler)
        values = apply_kernel_config(args.source_dir, args.build_dir, args.compiler)
        missing_required, _ = write_config_report(values, args.artifact_dir, args.compiler)
        if missing_required:
            raise BuildError(
                "Required kernel settings were not accepted: "
                + ", ".join(missing_required)
                + f"\nInspect {args.artifact_dir / 'kernel-config-report.json'}"
            )
        release = build_kernel(
            args.source_dir,
            args.build_dir,
            args.stage_dir,
            args.artifact_dir,
            args.jobs,
            args.compiler,
        )
    else:
        candidates = sorted(args.artifact_dir.glob("vmlinuz-*.efi"))
        if not candidates:
            raise BuildError("--skip-kernel was used but no vmlinuz-*.efi exists in the artifact directory")
        release = candidates[-1].name.removeprefix("vmlinuz-").removesuffix(".efi")

    if args.skip_toybox:
        toybox_binary = toybox_dir / "toybox"
        if not toybox_binary.exists():
            raise BuildError("--skip-toybox was used but no Toybox binary exists")
    else:
        toybox_binary = build_toybox(toybox_dir, args.jobs, args.clean)

    initramfs = build_initramfs(
        toybox_binary,
        args.stage_dir,
        args.artifact_dir,
        release,
        args.root_spec,
        args.btrfs_subvol or None,
    )

    systemd_boot_binary: Path | None
    if args.build_systemd_boot:
        systemd_boot_binary = build_systemd_boot(systemd_dir, args.stage_dir, args.clean)
    else:
        systemd_boot_binary = find_installed_systemd_boot()

    esp = stage_esp(
        args.artifact_dir,
        release,
        initramfs,
        args.root_spec,
        args.btrfs_subvol or None,
        systemd_boot_binary,
    )
    write_manifest(
        args.artifact_dir,
        linux_commit,
        toybox_dir,
        systemd_dir,
        release,
        args,
    )

    log("Build completed")
    print(f"Kernel release: {release}")
    print(f"Artifacts:      {args.artifact_dir}")
    print(f"ESP staging:    {esp}")
    print(f"Config report:  {args.artifact_dir / 'kernel-config-report.json'}")
    print()
    print("Before booting with enforcing SELinux, install SELinux userspace and a policy,")
    print("label the Btrfs root filesystem, and verify /sbin/init points to systemd.")
    if not args.root_spec:
        print("No --root-spec was supplied, so the generated loader entry boots the Toybox rescue shell.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)
    except BuildError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
