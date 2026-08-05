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
    + ".before-targeted-source-retry-"
    + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
)
backup.write_text(text, encoding="utf-8")

new_fetch = r'''def fetch_arch_recipe_sources(
    closure: dict[str, Any],
    env: dict[str, str],
) -> dict[str, Any]:
    log("\n" + "=" * 78)
    log("FETCHING AND CHECKSUM-VERIFYING ARCH RECIPE SOURCES")
    log("=" * 78)

    DISTFILES.mkdir(parents=True, exist_ok=True)
    result_path = PLAN / "arch-source-fetch-results.json"
    fallback_path = PLAN / "source-fallbacks.json"

    recipes = closure["recipes"]
    current_bases = set(recipes)

    prior: dict[str, Any] = {}
    if result_path.is_file():
        try:
            prior = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prior = {}

    successes = set(prior.get("successes", [])) & current_bases
    failures: dict[str, str] = {
        key: value
        for key, value in prior.get("failures", {}).items()
        if key in current_bases and key not in successes
    }

    fallback_record: dict[str, Any] = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "policy": (
            "Fallbacks preserve the recipe's exact version/tag/commit and retain "
            "makepkg checksum verification. No checksum is regenerated or bypassed."
        ),
        "packages": {},
    }
    if fallback_path.is_file():
        try:
            old_fallbacks = json.loads(
                fallback_path.read_text(encoding="utf-8")
            )
            if isinstance(old_fallbacks.get("packages"), dict):
                fallback_record["packages"].update(old_fallbacks["packages"])
        except (OSError, json.JSONDecodeError):
            pass

    verify_env = dict(env)
    verify_env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
        }
    )

    cache_names: dict[str, tuple[str, ...]] = {
        "glibc": ("glibc",),
        "gmp": ("gmp-6.3.0.tar.lz",),
        "gpgmepp": ("gpgmepp",),
        "gumbo-parser": (
            "0.13.2.tar.gz",
            "gumbo-parser-0.13.2.tar.gz",
        ),
        "libassuan": ("libassuan",),
        "libasyncns": ("libasyncns",),
        "libcanberra": ("libcanberra",),
        "unzip": ("unzip-zipbomb-switch.patch",),
    }

    # Each fallback changes only the transport/mirror. Existing #tag=, #commit=,
    # and archive filenames remain intact, and makepkg still enforces the recipe
    # checksum or exact VCS reference.
    fallback_sets: dict[
        str,
        list[tuple[str, list[tuple[str, str]]]],
    ] = {
        "glibc": [
            (
                "sourceware mirror on GitHub",
                [
                    (
                        "https://sourceware.org/git/glibc.git",
                        "https://github.com/bminor/glibc.git",
                    ),
                ],
            ),
        ],
        "gmp": [
            (
                "GNU FTP mirror",
                [
                    (
                        "https://gmplib.org/download/gmp",
                        "https://ftp.gnu.org/gnu/gmp",
                    ),
                    (
                        "http://gmplib.org/download/gmp",
                        "https://ftp.gnu.org/gnu/gmp",
                    ),
                ],
            ),
            (
                "GNU automatic mirror",
                [
                    (
                        "https://gmplib.org/download/gmp",
                        "https://ftpmirror.gnu.org/gmp",
                    ),
                    (
                        "http://gmplib.org/download/gmp",
                        "https://ftpmirror.gnu.org/gmp",
                    ),
                ],
            ),
        ],
        "gpgmepp": [
            (
                "GnuPG GitHub read-only mirror",
                [
                    (
                        "https://dev.gnupg.org/source/gpgmepp.git",
                        "https://github.com/gpg/gpgmepp.git",
                    ),
                    (
                        "https://dev.gnupg.org/source/gpgmepp",
                        "https://github.com/gpg/gpgmepp.git",
                    ),
                ],
            ),
        ],
        "libassuan": [
            (
                "GnuPG GitHub read-only mirror",
                [
                    (
                        "https://dev.gnupg.org/source/libassuan.git",
                        "https://github.com/gpg/libassuan.git",
                    ),
                    (
                        "https://dev.gnupg.org/source/libassuan",
                        "https://github.com/gpg/libassuan.git",
                    ),
                ],
            ),
        ],
        "libasyncns": [
            (
                "SailfishOS upstream-history mirror",
                [
                    (
                        "https://git.0pointer.net/clone/libasyncns.git",
                        "https://github.com/sailfishos/libasyncns.git",
                    ),
                ],
            ),
            (
                "Deepin upstream-history mirror",
                [
                    (
                        "https://git.0pointer.net/clone/libasyncns.git",
                        "https://github.com/deepin-community/libasyncns.git",
                    ),
                ],
            ),
        ],
        "libcanberra": [
            (
                "Distrotech upstream-history mirror",
                [
                    (
                        "https://git.0pointer.net/clone/libcanberra.git",
                        "https://github.com/Distrotech/libcanberra.git",
                    ),
                ],
            ),
        ],
        "gumbo-parser": [
            (
                "Void source archive mirror",
                [
                    (
                        "https://codeberg.org/gumbo-parser/gumbo-parser//archive/0.13.2.tar.gz",
                        "https://sources.voidlinux.org/gumbo-parser-0.13.2/0.13.2.tar.gz",
                    ),
                    (
                        "https://codeberg.org/gumbo-parser/gumbo-parser/archive/0.13.2.tar.gz",
                        "https://sources.voidlinux.org/gumbo-parser-0.13.2/0.13.2.tar.gz",
                    ),
                    (
                        "$url/archive/$pkgver.tar.gz",
                        "https://sources.voidlinux.org/gumbo-parser-0.13.2/0.13.2.tar.gz",
                    ),
                    (
                        "${url}/archive/${pkgver}.tar.gz",
                        "https://sources.voidlinux.org/gumbo-parser-0.13.2/0.13.2.tar.gz",
                    ),
                ],
            ),
        ],
    }

    def remove_path(path: Path) -> None:
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        except FileNotFoundError:
            pass

    def purge_known_cache(pkgbase: str) -> None:
        for name in cache_names.get(pkgbase, (pkgbase,)):
            remove_path(DISTFILES / name)
            remove_path(DISTFILES / (name + ".part"))
            remove_path(DISTFILES / (name + ".tmp"))

    def verify(directory: Path) -> subprocess.CompletedProcess[str]:
        return run(
            ["makepkg", "--verifysource", "--skippgpcheck"],
            cwd=directory,
            env=verify_env,
            check=False,
            timeout=3600,
        )

    def save_progress() -> None:
        record = {
            "successes": sorted(successes),
            "failures": failures,
            "distfile_count": sum(
                1 for path in DISTFILES.rglob("*") if path.is_file()
            ),
            "resumed": True,
            "fallback_manifest": str(fallback_path),
        }
        write_json(result_path, record)
        fallback_record["generated_at"] = dt.datetime.now(
            dt.timezone.utc
        ).isoformat()
        write_json(fallback_path, fallback_record)

    pending = sorted(current_bases - successes)
    log(
        f"Resuming source verification: {len(successes)} recipes already passed; "
        f"{len(pending)} recipe(s) require verification."
    )

    for pkgbase in pending:
        directory = Path(recipes[pkgbase]["directory"])
        pkgbuild = directory / "PKGBUILD"
        if not pkgbuild.is_file():
            failures[pkgbase] = f"Missing PKGBUILD: {pkgbuild}"
            save_progress()
            continue

        original_text = pkgbuild.read_text(
            encoding="utf-8",
            errors="replace",
        )
        backup_pkgbuild = directory / "PKGBUILD.before-source-fallback"
        if not backup_pkgbuild.exists():
            backup_pkgbuild.write_text(original_text, encoding="utf-8")

        purge_known_cache(pkgbase)
        result = verify(directory)
        if result.returncode == 0:
            successes.add(pkgbase)
            failures.pop(pkgbase, None)
            save_progress()
            continue

        final_output = result.stdout[-12000:]
        passed = False

        # The unzip failure was a checksum mismatch. Refresh the official recipe
        # once and retry the exact published checksum; never run updpkgsums here.
        if pkgbase == "unzip" and (directory / ".git").is_dir():
            log("Refreshing the official unzip recipe before one clean retry.")
            refresh = run(
                ["git", "fetch", "--prune", "origin", "main"],
                cwd=directory,
                env=verify_env,
                check=False,
                timeout=300,
            )
            if refresh.returncode == 0:
                reset = run(
                    ["git", "reset", "--hard", "origin/main"],
                    cwd=directory,
                    env=verify_env,
                    check=False,
                    timeout=60,
                )
                if reset.returncode == 0:
                    purge_known_cache(pkgbase)
                    result = verify(directory)
                    final_output = result.stdout[-12000:]
                    if result.returncode == 0:
                        successes.add(pkgbase)
                        failures.pop(pkgbase, None)
                        fallback_record["packages"][pkgbase] = {
                            "method": "refreshed official Arch recipe",
                            "recipe_directory": str(directory),
                            "verified": True,
                        }
                        passed = True

        if not passed:
            for label, replacements in fallback_sets.get(pkgbase, []):
                candidate_text = original_text
                changed = False
                for old, new in replacements:
                    if old in candidate_text:
                        candidate_text = candidate_text.replace(old, new)
                        changed = True

                if not changed:
                    continue

                log(f"Trying {pkgbase} with {label}.")
                pkgbuild.write_text(candidate_text, encoding="utf-8")
                purge_known_cache(pkgbase)
                result = verify(directory)
                final_output = result.stdout[-12000:]

                if result.returncode == 0:
                    successes.add(pkgbase)
                    failures.pop(pkgbase, None)
                    fallback_record["packages"][pkgbase] = {
                        "method": label,
                        "recipe_directory": str(directory),
                        "original_pkgbuild_backup": str(backup_pkgbuild),
                        "modified_pkgbuild_sha256": sha256_file(pkgbuild),
                        "verified": True,
                    }
                    passed = True
                    break

        if not passed:
            # Do not leave a mirror rewrite behind when it did not verify.
            pkgbuild.write_text(original_text, encoding="utf-8")
            failures[pkgbase] = final_output

        save_progress()

    if failures:
        names = ", ".join(sorted(failures)[:25])
        extra = (
            ""
            if len(failures) <= 25
            else f" and {len(failures) - 25} more"
        )
        raise PrepareError(
            f"Source verification/download still failed for "
            f"{len(failures)} recipe(s): {names}{extra}. "
            "See arch-source-fetch-results.json and source-fallbacks.json."
        )

    record = {
        "successes": sorted(successes),
        "failures": {},
        "distfile_count": sum(
            1 for path in DISTFILES.rglob("*") if path.is_file()
        ),
        "resumed": True,
        "fallback_manifest": str(fallback_path),
    }
    write_json(result_path, record)
    write_json(fallback_path, fallback_record)
    log(
        f"All {len(successes)} Arch recipe source sets are downloaded "
        "and checksum/reference verified."
    )
    return record


'''

start = text.find("def fetch_arch_recipe_sources(")
end = text.find("def release_module_map(", start)
if start == -1 or end == -1:
    raise SystemExit(
        "Could not locate fetch_arch_recipe_sources function boundary."
    )
text = text[:start] + new_fetch + text[end:]

# Ensure the container gets a small init/reaper so the hundreds of completed Git
# children do not accumulate as zombies on future long source operations.
if '"podman", "run", "--init",' not in text:
    text = text.replace(
        '"podman", "run",',
        '"podman", "run", "--init",',
        1,
    )

# Record the fallback manifest in the final source lock when the existing
# create_source_lock layout matches.
needle = '''        "arch_source_fetch": fetch_results,
'''
replacement = '''        "arch_source_fetch": fetch_results,
        "source_fallbacks": (
            json.loads((PLAN / "source-fallbacks.json").read_text(encoding="utf-8"))
            if (PLAN / "source-fallbacks.json").is_file()
            else {}
        ),
'''
if needle in text:
    text = text.replace(needle, replacement, 1)

compile(text, str(TARGET), "exec")
TARGET.write_text(text, encoding="utf-8")
TARGET.chmod(0o755)

print(f"Patched: {TARGET}")
print(f"Backup:  {backup}")
print()
print("The next run will:")
print("  - reuse every recipe already verified successfully")
print("  - retry only the failed recipe set")
print("  - use checksum-preserving mirrors only when the original host fails")
print("  - never regenerate or bypass a failed checksum")
print("  - add Podman's --init child-process reaper")
print("  - write source-fallbacks.json into the source-plan directory")
