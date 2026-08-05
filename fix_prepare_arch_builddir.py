#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
from pathlib import Path

TARGET = Path("/home/corbett/prepare_arch_sources.py")

if not TARGET.is_file():
    raise SystemExit(f"Missing {TARGET}")

text = TARGET.read_text(encoding="utf-8")
backup = TARGET.with_name(
    TARGET.name
    + ".before-builddir-mount-fix-"
    + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
)
backup.write_text(text, encoding="utf-8")

changes = 0

old_dirs = '''    for relative in ("source-cache", "source-plan", "pkgbuilds", "logs"):
        (HOST_ROOT / relative).mkdir(parents=True, exist_ok=True)
'''
new_dirs = '''    for relative in ("source-cache", "source-plan", "pkgbuilds", "logs", "build"):
        (HOST_ROOT / relative).mkdir(parents=True, exist_ok=True)
'''
if old_dirs in text:
    text = text.replace(old_dirs, new_dirs, 1)
    changes += 1

old_chown = '''    subprocess.run(
        ["sudo", "chown", "-R", "corbett:corbett",
         str(HOST_ROOT / "source-cache"), str(HOST_ROOT / "source-plan")],
        check=True,
    )
'''
new_chown = '''    subprocess.run(
        [
            "sudo", "chown", "-R", "corbett:corbett",
            str(HOST_ROOT / "source-cache"),
            str(HOST_ROOT / "source-plan"),
            str(HOST_ROOT / "build"),
            str(HOST_ROOT / "logs"),
            str(HOST_ROOT / "pkgbuilds"),
        ],
        check=True,
    )
'''
if old_chown in text:
    text = text.replace(old_chown, new_chown, 1)
    changes += 1

mount_anchor = '''        "-v", f"{HOST_ROOT / 'source-plan'}:/work/source-plan:rw",
        "-v", f"{HOST_ROOT / 'logs'}:/work/logs:rw",
'''
mount_replacement = '''        "-v", f"{HOST_ROOT / 'source-plan'}:/work/source-plan:rw",
        "-v", f"{HOST_ROOT / 'build'}:/work/build:rw",
        "-v", f"{HOST_ROOT / 'logs'}:/work/logs:rw",
'''
if mount_anchor in text:
    text = text.replace(mount_anchor, mount_replacement, 1)
    changes += 1

silent_failure = '''    if result.returncode != 0:
        return
'''
verbose_failure = '''    if result.returncode != 0:
        log(f"makepkg --printsrcinfo failed for {directory}")
        if result.stdout:
            for line in result.stdout.rstrip().splitlines()[-40:]:
                log("  " + line)
        return
'''

index_start = text.find("def index_recipe_directory(")
if index_start != -1:
    failure_at = text.find(silent_failure, index_start)
    closure_at = text.find("def initial_recipe_index(", index_start)
    if failure_at != -1 and (closure_at == -1 or failure_at < closure_at):
        text = text[:failure_at] + text[failure_at:].replace(
            silent_failure, verbose_failure, 1
        )
        changes += 1

if changes < 3:
    raise SystemExit(
        f"Only {changes} expected edits matched. "
        "The script layout differs; no patched file was installed."
    )

compile(text, str(TARGET), "exec")
TARGET.write_text(text, encoding="utf-8")
TARGET.chmod(0o755)

print(f"Patched: {TARGET}")
print(f"Backup:  {backup}")
print(f"Applied edits: {changes}")
print("The source preparer now mounts a writable /work/build directory.")
print("Existing successful recipe clones will be reused on the next run.")
