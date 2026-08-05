#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

BASH_REPO = "https://git.savannah.gnu.org/git/bash.git"
DEFAULT_REPO_URL = (
    "https://sourceforge.net/projects/"
    "hardened-software-update/files/updates/latest.json/download"
)


class BuildError(RuntimeError):
    pass


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


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        raise BuildError(f"Required command not found: {name}")


def install_dependencies() -> None:
    run([
        "sudo", "pacman", "-S", "--needed", "--noconfirm",
        "git", "base-devel", "autoconf", "automake", "bison",
        "musl", "file", "cpio", "zstd",
    ])


def prepare_bash_source(home: Path) -> Path:
    source = home / "userspace-src/bash"
    source.parent.mkdir(parents=True, exist_ok=True)
    if not (source / ".git").is_dir():
        if source.exists():
            raise BuildError(f"{source} exists but is not a Git checkout")
        run(["git", "clone", BASH_REPO, source])
    else:
        run(["git", "-C", source, "fetch", "--tags", "--prune"])
    run(["git", "-C", source, "status", "--short", "--branch"])
    return source


def ensure_bash_configure(source: Path) -> None:
    if (source / "configure").is_file():
        return
    for candidate in (source / "support/mkclone", source / "autogen.sh"):
        if candidate.is_file():
            candidate.chmod(candidate.stat().st_mode | 0o111)
            run([candidate], cwd=source)
            if (source / "configure").is_file():
                return
    run(["autoreconf", "-fi"], cwd=source)
    if not (source / "configure").is_file():
        raise BuildError("GNU Bash configure script was not generated")


def build_static_bash(home: Path, jobs: int) -> Path:
    source = prepare_bash_source(home)
    ensure_bash_configure(source)
    build = home / "system-build/bash-static"
    if build.exists():
        shutil.rmtree(build)
    build.mkdir(parents=True)

    env = os.environ.copy()
    env.update({"CC": "musl-gcc", "CFLAGS": "-O2 -pipe", "LDFLAGS": "-static"})
    run([
        source / "configure",
        "--prefix=/usr",
        "--without-bash-malloc",
        "--disable-nls",
        "--disable-readline",
        "--disable-history",
    ], cwd=build, env=env)
    run(["make", f"-j{jobs}", "bash"], cwd=build, env=env)

    binary = build / "bash"
    if not binary.is_file():
        raise BuildError(f"Static Bash build did not produce {binary}")
    info = run(["file", binary], capture=True).stdout.strip()
    print(info)
    if "statically linked" not in info:
        raise BuildError("Bash is not statically linked")
    run([
        binary, "--noprofile", "--norc", "-c",
        'f(){ echo "Static Bash works"; }; n=1; n=$((n+1)); f; test "$n" -eq 2',
    ])
    revision = run(["git", "-C", source, "rev-parse", "HEAD"], capture=True).stdout.strip()
    print(f"GNU Bash revision: {revision}")
    return binary


def install_bash_in_initramfs(stage: Path, bash_binary: Path) -> None:
    if not stage.is_dir():
        raise BuildError(f"Initramfs staging tree not found: {stage}")
    bin_dir = stage / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "bash"
    shutil.copy2(bash_binary, target)
    target.chmod(0o755)
    sh_link = bin_dir / "sh"
    if sh_link.exists() or sh_link.is_symlink():
        sh_link.unlink()
    os.symlink("bash", sh_link)
    init = stage / "init"
    if not init.is_file():
        raise BuildError(f"Initramfs /init not found: {init}")
    print(f"Installed static Bash: {target}")
    print(f"Installed shell link:  {sh_link} -> bash")


def configure_installed_init_channel(stage: Path, channel: str) -> None:
    init = stage / "init"
    template = stage.parent / "init.debug-template"
    if not template.exists():
        shutil.copy2(init, template)

    if channel in {"alpha", "beta"}:
        shutil.copy2(template, init)
        init.chmod(0o755)
        print(f"Installed initramfs channel: {channel} (debug shell enabled)")
        return

    text = template.read_text(encoding="utf-8")
    text = re.sub(
        r'rd\.shell\|rd\.shell=1\)\s+ROOT_SPEC=""\s+;;',
        'rd.shell|rd.shell=1) : ;;',
        text,
    )
    text = re.sub(
        r'echo\n'
        r'echo "Hardened Linux ([^"]+) Toybox rescue shell"\n'
        r'echo "Root was not mounted or systemd was not found\."\n'
        r'echo "Kernel command line example: ([^"]+)"\n'
        r'echo\n'
        r'exec sh\s*$',
        r'echo\n'
        r'echo "Hardened Linux \1 boot failure"\n'
        r'echo "Pre-handoff rescue shell is disabled for stable releases."\n'
        r'echo "Rebooting in 5 seconds."\n'
        r'sleep 5\n'
        r'exec reboot -f\n',
        text,
        flags=re.MULTILINE,
    )
    if re.search(r'(^|\s)exec\s+sh($|\s)', text):
        raise BuildError("Stable installed initramfs still contains an exec sh path")
    init.write_text(text, encoding="utf-8")
    init.chmod(0o755)
    print("Installed initramfs channel: stable (debug shell disabled)")


def kernel_release(home: Path) -> str:
    source = home / "linux-7.1.2"
    build = home / "linux-7.1.2-build"
    cp = run(["make", "-s", "-C", source, f"O={build}", "kernelrelease"], capture=True)
    value = cp.stdout.strip()
    if not value:
        raise BuildError("Could not determine kernel release")
    return value


def repack_installed_initramfs(home: Path, stage: Path, kver: str) -> Path:
    artifacts = home / "linux-7.1.2-artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    output = artifacts / f"initramfs-{kver}.img.zst"
    shell_command = (
        "find . -print0 | "
        "cpio --null --create --format=newc --owner=0:0 | "
        f"zstd -T0 -19 -f -o {shlex.quote(str(output))}"
    )
    run(["bash", "-o", "pipefail", "-c", shell_command], cwd=stage)
    run(["zstd", "-t", output])
    runtime_copy = home / "runtime-package-root/boot/EFI/Linux" / output.name
    run(["sudo", "install", "-Dm0644", output, runtime_copy])
    print(f"Installed-system initramfs: {output}")
    print(f"Runtime copy:                {runtime_copy}")
    return output


def replace_once(text: str, old: str, new: str, description: str) -> str:
    if new in text:
        return text
    if old not in text:
        raise BuildError(f"Could not patch ISO builder: {description}")
    return text.replace(old, new, 1)


def patch_iso_builder(path: Path) -> None:
    if not path.is_file():
        raise BuildError(f"ISO builder not found: {path}")
    backup = path.with_name(path.name + ".before-release-channel")
    if not backup.exists():
        shutil.copy2(path, backup)
    text = path.read_text(encoding="utf-8")

    text = replace_once(text,
'''class BuildConfig:
    version: str
    repo_url: str
''',
'''class BuildConfig:
    version: str
    release_channel: str
    repo_url: str
''', "BuildConfig.release_channel")

    text = replace_once(text,
'''    parser.add_argument("--version", default="1.10-alpha")
    parser.add_argument(
        "--repo-url",
''',
'''    parser.add_argument("--version", default="1.10-alpha")
    parser.add_argument(
        "--release-channel",
        choices=("alpha", "beta", "stable"),
        default="alpha",
        help="alpha/beta keep the pre-handoff rescue shell; stable disables it",
    )
    parser.add_argument(
        "--repo-url",
''', "--release-channel argument")

    text = replace_once(text,
'''    cfg = BuildConfig(
        version=args.version,
        repo_url=args.repo_url,
''',
'''    cfg = BuildConfig(
        version=args.version,
        release_channel=args.release_channel,
        repo_url=args.repo_url,
''', "BuildConfig construction")

    text = replace_once(text,
'''        print(f"  Kernel release:{kver}")
        print(f"  Work:          {paths.work}")
''',
'''        print(f"  Kernel release:{kver}")
        print(f"  Channel:       {cfg.release_channel}")
        print(f"  Debug shell:   {'enabled' if cfg.release_channel != 'stable' else 'disabled'}")
        print(f"  Work:          {paths.work}")
''', "release-channel status output")

    text = replace_once(text,
'''    init_script = LIVE_INIT.replace("__ISO_LABEL__", cfg.volume_label)
    write_text(paths.initramfs_root / "init", init_script, 0o755)
''',
'''    init_script = LIVE_INIT.replace("__ISO_LABEL__", cfg.volume_label)
    if cfg.release_channel == "stable":
        init_script = init_script.replace(
            'rd.shell|rd.shell=1) exec sh ;;',
            'rd.shell|rd.shell=1) : ;;',
        )
        init_script = init_script.replace(
            'echo "Dropping to the Toybox rescue shell."\\n    exec sh',
            'echo "Pre-handoff rescue shell is disabled for stable releases."\\n'
            '    echo "Rebooting in 5 seconds."\\n'
            '    sleep 5\\n'
            '    exec reboot -f',
        )
        if "exec sh" in init_script:
            raise BuildError("Stable live initramfs still contains an exec sh path")
    write_text(paths.initramfs_root / "init", init_script, 0o755)
''', "stable live-initramfs lockout")

    text = replace_once(text,
'''        "kernel": kver,
        "volume_label": cfg.volume_label,
''',
'''        "kernel": kver,
        "release_channel": cfg.release_channel,
        "initramfs_debug_shell": cfg.release_channel != "stable",
        "volume_label": cfg.volume_label,
''', "ISO build-manifest security fields")

    text = replace_once(text,
'''        "channel": "stable",
        "version": cfg.version,
''',
'''        "channel": cfg.release_channel,
        "initramfs_debug_shell": cfg.release_channel != "stable",
        "version": cfg.version,
''', "SourceForge manifest channel")

    if '"reboot",' not in text:
        text = replace_once(text,
'''        "sleep",
        "switch_root",
''',
'''        "sleep",
        "reboot",
        "switch_root",
''', "Toybox reboot requirement")

    path.write_text(text, encoding="utf-8")
    run([sys.executable, "-m", "py_compile", path])
    print(f"Patched ISO builder: {path}")


def run_iso_builder(home: Path, args: argparse.Namespace) -> None:
    builder = home / "build_hardened_arch_iso.py"
    cmd: list[str | os.PathLike[str]] = [
        "sudo", builder,
        "--version", args.version,
        "--release-channel", args.release_channel,
        "--repo-url", args.repo_url,
        "--jobs", str(args.jobs),
        "--force", "--keep-work",
    ]
    if args.install_iso_tools:
        cmd.append("--install-tools")
    run(cmd)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build static Bash, repair initramfs, and build Hardened Arch ISO."
    )
    parser.add_argument("--release-channel", choices=("alpha", "beta", "stable"), default="alpha")
    parser.add_argument("--version", default="1.10-alpha")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--repo-url", default=DEFAULT_REPO_URL)
    parser.add_argument("--install-deps", action="store_true")
    parser.add_argument("--install-iso-tools", action="store_true")
    parser.add_argument("--skip-iso", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if os.geteuid() == 0:
        raise BuildError(
            "Run this wrapper as corbett, not with sudo. It invokes sudo only where needed."
        )
    if args.jobs < 1:
        raise BuildError("--jobs must be at least 1")
    home = Path.home().resolve()

    if args.install_deps:
        install_dependencies()
    for command in ("git", "musl-gcc", "make", "file", "cpio", "zstd", "sudo"):
        require_command(command)

    bash_binary = build_static_bash(home, args.jobs)
    stage = home / "linux-7.1.2-stage/initramfs"
    install_bash_in_initramfs(stage, bash_binary)
    configure_installed_init_channel(stage, args.release_channel)
    kver = kernel_release(home)
    repack_installed_initramfs(home, stage, kver)
    patch_iso_builder(home / "build_hardened_arch_iso.py")

    print("\nPreflight complete")
    print(f"  Kernel release: {kver}")
    print(f"  Release channel: {args.release_channel}")
    print("  Pre-handoff debug shell: " + ("enabled" if args.release_channel != "stable" else "disabled"))
    print("  Toybox scope: initramfs only; switch_root hands off to systemd\n")

    if not args.skip_iso:
        run_iso_builder(home, args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BuildError, subprocess.CalledProcessError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
