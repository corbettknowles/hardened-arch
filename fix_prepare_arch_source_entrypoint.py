#!/usr/bin/env python3
from pathlib import Path
import os, pwd, shutil, sys

script = Path('/home/corbett/prepare_arch_sources.py')
root = Path('/home/corbett/arch-rebuild')

if os.geteuid() != 0:
    raise SystemExit('Run with: sudo python3 /home/corbett/fix_prepare_arch_source_entrypoint.py')
if not script.is_file():
    raise SystemExit(f'Missing {script}')

text = script.read_text(encoding='utf-8')
original = text

old1 = '        "--network=host",\n        "-v", f"{HOST_ROOT / \'sources\'}:/work/sources:ro",'
new1 = '        "--network=host",\n        "--entrypoint", "/usr/bin/python",\n        "-v", f"{HOST_ROOT / \'sources\'}:/work/sources:ro",'

old2 = '        IMAGE,\n        "/usr/bin/python", "/work/tools/prepare_arch_sources.py", "--inside",'
new2 = '        IMAGE,\n        "/work/tools/prepare_arch_sources.py", "--inside",'

old3 = '         str(HOST_ROOT / "source-cache"), str(HOST_ROOT / "source-plan")],'
new3 = ('         str(HOST_ROOT / "source-cache"), str(HOST_ROOT / "source-plan"),\n'
        '         str(HOST_ROOT / "pkgbuilds"), str(HOST_ROOT / "logs")],')

for old, new, label in ((old1, new1, 'entrypoint insertion'), (old2, new2, 'python argv cleanup'), (old3, new3, 'ownership list')):
    if old in text:
        text = text.replace(old, new, 1)
    elif new not in text:
        raise SystemExit(f'Could not locate expected block for {label}; no changes written.')

backup = script.with_suffix('.py.before-entrypoint-fix')
if not backup.exists():
    shutil.copy2(script, backup)

script.write_text(text, encoding='utf-8', newline='\n')
os.chmod(script, 0o755)

user = pwd.getpwnam('corbett')
for name in ('source-cache', 'source-plan', 'pkgbuilds', 'logs'):
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    for current, dirs, files in os.walk(path):
        os.chown(current, user.pw_uid, user.pw_gid)
        for item in dirs + files:
            try:
                os.chown(Path(current) / item, user.pw_uid, user.pw_gid)
            except FileNotFoundError:
                pass

compile(text, str(script), 'exec')
print('Patched:', script)
print('Backup :', backup)
print('The source-prep run now bypasses the builder entrypoint and uses Python directly.')
