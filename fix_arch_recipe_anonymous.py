#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
from pathlib import Path

TARGET = Path('/home/corbett/prepare_arch_sources.py')

if not TARGET.is_file():
    raise SystemExit(f'Missing {TARGET}')

text = TARGET.read_text(encoding='utf-8')
backup = TARGET.with_name(
    TARGET.name + '.before-anonymous-arch-fix-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S')
)
backup.write_text(text, encoding='utf-8')

old_env = '''        "SRCDEST": str(DISTFILES),
        "BUILDDIR": str(WORK / "build"),
'''
new_env = '''        "SRCDEST": str(DISTFILES),
        "BUILDDIR": str(WORK / "build"),
        # Never allow Git to stop and ask for a GitLab username/password.
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
'''
if old_env not in text:
    raise SystemExit('Could not locate clean_environment() insertion point; no changes applied.')
text = text.replace(old_env, new_env, 1)

start = text.index('def clone_recipe_into_index(')
end = text.index('\n\ndef collect_arch_recipe_closure', start)

replacement = r'''def resolve_arch_pkgbase(package: str) -> str | None:
    """Resolve a binary package name to its official Arch pkgbase anonymously."""
    query = urllib.parse.urlencode({"name": package})
    url = f"https://archlinux.org/packages/search/json/?{query}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "hardened-arch-source-preparer/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
    except Exception as exc:
        log(f"Arch package lookup failed for {package}: {exc}")
        return None

    candidates = [
        item for item in payload.get("results", [])
        if item.get("pkgname") == package
        and item.get("repo") in {"Core", "Extra", "core", "extra"}
        and item.get("arch") in {"x86_64", "any"}
    ]
    if not candidates:
        return None

    rank = {"Core": 0, "core": 0, "Extra": 1, "extra": 1}
    candidates.sort(key=lambda item: (rank.get(item.get("repo"), 99), item.get("arch") != "x86_64"))
    return candidates[0].get("pkgbase") or candidates[0].get("pkgname")


def clone_recipe_into_index(
    package: str,
    env: dict[str, str],
    package_map: dict[str, Path],
    recipe_data: dict[str, dict[str, Any]],
) -> Path | None:
    if package in package_map:
        return package_map[package]

    RECIPES.mkdir(parents=True, exist_ok=True)
    pkgbase = resolve_arch_pkgbase(package)
    if not pkgbase:
        log(f"No stable Core/Extra pkgbase found for {package}")
        return None

    destination = RECIPES / pkgbase
    if not (destination / "PKGBUILD").is_file():
        if destination.exists():
            shutil.rmtree(destination)
        url = (
            "https://gitlab.archlinux.org/archlinux/packaging/packages/"
            f"{urllib.parse.quote(pkgbase, safe='')}.git"
        )
        clone_env = dict(env)
        clone_env["GIT_TERMINAL_PROMPT"] = "0"
        clone_env["GIT_ASKPASS"] = "/bin/false"
        result = run(
            [
                "git", "clone",
                "--filter=blob:none",
                "--single-branch",
                "--branch", "main",
                url,
                str(destination),
            ],
            cwd=RECIPES,
            env=clone_env,
            check=False,
            timeout=600,
        )
        if result.returncode != 0:
            log(f"Anonymous Arch recipe clone failed for {package} ({pkgbase})")
            return None

    index_recipe_directory(destination, env, package_map, recipe_data)
    return package_map.get(package) or package_map.get(pkgbase)
'''

text = text[:start] + replacement + text[end:]
TARGET.write_text(text, encoding='utf-8')
TARGET.chmod(0o755)
compile(text, str(TARGET), 'exec')

print(f'Patched: {TARGET}')
print(f'Backup:  {backup}')
print('Arch recipes will now resolve through archlinux.org and clone public HTTPS repositories directly.')
print('No Arch GitLab account or token is required.')
