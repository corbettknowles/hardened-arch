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
    + ".before-runtime-closure-fix-"
    + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
)
backup.write_text(text, encoding="utf-8")

new_index = r'''def index_recipe_directory(
    directory: Path,
    env: dict[str, str],
    package_map: dict[str, Path],
    recipe_data: dict[str, dict[str, Any]],
) -> None:
    pkgbuild = directory / "PKGBUILD"
    if not pkgbuild.is_file():
        return

    srcinfo = directory / ".SRCINFO"
    parsed = None

    # Official Arch recipe repositories usually include .SRCINFO. Reuse it when it
    # is current so a resumed source-preparation run does not reevaluate hundreds
    # of PKGBUILDs unnecessarily.
    if srcinfo.is_file() and srcinfo.stat().st_mtime >= pkgbuild.stat().st_mtime:
        try:
            parsed = parse_srcinfo(srcinfo)
        except OSError:
            parsed = None

    if parsed is None:
        result = run(
            ["makepkg", "--printsrcinfo"],
            cwd=directory,
            env=env,
            check=False,
            timeout=180,
            show_output=False,
        )
        if result.returncode != 0:
            log(f"makepkg --printsrcinfo failed for {directory}")
            if result.stdout:
                for line in result.stdout.rstrip().splitlines()[-40:]:
                    log("  " + line)
            return
        srcinfo.write_text(result.stdout, encoding="utf-8")
        parsed = parse_srcinfo(srcinfo)

    pkgbase = parsed["pkgbase"] or directory.name
    recipe_data[pkgbase] = {**parsed, "directory": str(directory)}
    package_map[pkgbase] = directory
    for name in parsed["pkgnames"]:
        package_map[name] = directory
    for provided in parsed["provides"]:
        package_map.setdefault(provided, directory)


'''

new_closure = r'''def collect_arch_recipe_closure(env: dict[str, str]) -> dict[str, Any]:
    log("\n" + "=" * 78)
    log("COLLECTING TARGET RUNTIME RECIPE AND DEPENDENCY CLOSURE")
    log("=" * 78)
    log(
        "Policy: recurse target runtime depends only. Build-only makedepends are "
        "recorded for the clean Arch builder; checkdepends are recorded but do "
        "not expand the target source graph."
    )

    package_map, recipe_data = initial_recipe_index(env)
    queue = collections.deque(SEED_PACKAGES)
    queued: set[str] = set(SEED_PACKAGES)
    seen: set[str] = set()
    processed_bases: set[str] = set()
    missing_seed: list[str] = []
    unresolved_virtual: set[str] = set()
    builder_makedepends: set[str] = set()
    ignored_checkdepends: set[str] = set()
    max_runtime_packages = 800

    def write_progress(current: str | None = None) -> None:
        filtered = {
            base: recipe_data[base]
            for base in sorted(processed_bases)
            if base in recipe_data
        }
        write_json(
            PLAN / "dependency-closure-progress.json",
            {
                "current_package": current,
                "runtime_packages_seen": sorted(seen),
                "runtime_package_count": len(seen),
                "processed_package_bases": sorted(processed_bases),
                "processed_package_base_count": len(processed_bases),
                "queue_remaining": list(queue),
                "queue_remaining_count": len(queue),
                "builder_makedepends": sorted(builder_makedepends),
                "checkdepends_not_in_target_graph": sorted(ignored_checkdepends),
                "unresolved_virtual_or_provider_dependencies": sorted(unresolved_virtual),
                "recipes": filtered,
            },
        )

    while queue:
        package = normalize_dependency(queue.popleft())
        if not package or package in seen or package in VIRTUAL_OR_TOOL_DEPS:
            continue

        if len(seen) >= max_runtime_packages:
            write_progress(package)
            raise PrepareError(
                "Target runtime dependency closure exceeded safety limit of "
                f"{max_runtime_packages} packages. See dependency-closure-progress.json."
            )

        if not package_exists(package, env):
            unresolved_virtual.add(package)
            seen.add(package)
            continue

        directory = clone_recipe_into_index(package, env, package_map, recipe_data)
        if directory is None:
            if package in SEED_PACKAGES:
                missing_seed.append(package)
            else:
                unresolved_virtual.add(package)
            seen.add(package)
            continue

        seen.add(package)
        matching_base = None
        matching = None
        for pkgbase, data in recipe_data.items():
            if (
                package == pkgbase
                or package in data["pkgnames"]
                or package in data["provides"]
            ):
                matching_base = pkgbase
                matching = data
                break

        if matching is None or matching_base is None:
            if package in SEED_PACKAGES:
                missing_seed.append(package)
            continue

        if matching_base in processed_bases:
            continue
        processed_bases.add(matching_base)

        for dep in matching["depends"]:
            dep = normalize_dependency(dep)
            if (
                dep
                and dep not in seen
                and dep not in queued
                and dep not in VIRTUAL_OR_TOOL_DEPS
            ):
                queue.append(dep)
                queued.add(dep)

        for dep in matching["makedepends"]:
            dep = normalize_dependency(dep)
            if dep and dep not in VIRTUAL_OR_TOOL_DEPS:
                builder_makedepends.add(dep)

        for dep in matching["checkdepends"]:
            dep = normalize_dependency(dep)
            if dep and dep not in VIRTUAL_OR_TOOL_DEPS:
                ignored_checkdepends.add(dep)

        if len(processed_bases) % 25 == 0:
            write_progress(package)
            log(
                f"Runtime closure progress: {len(processed_bases)} package bases, "
                f"{len(queue)} queued."
            )

    required_missing = sorted(set(missing_seed))
    if required_missing:
        write_progress()
        raise PrepareError(
            "Required Arch recipes could not be cloned/indexed: "
            + ", ".join(required_missing)
        )

    filtered_recipes = {
        base: recipe_data[base]
        for base in sorted(processed_bases)
        if base in recipe_data
    }

    runtime_names = set(seen)
    builder_only = sorted(
        dep
        for dep in builder_makedepends
        if dep not in runtime_names and dep not in VIRTUAL_OR_TOOL_DEPS
    )
    test_only = sorted(
        dep
        for dep in ignored_checkdepends
        if dep not in runtime_names and dep not in VIRTUAL_OR_TOOL_DEPS
    )

    result = {
        "closure_policy": {
            "target_graph": "recursive depends from explicit seed packages",
            "makedepends": "recorded for clean Arch builder, not recursively source-built into target",
            "checkdepends": "recorded for later test phase, not part of target graph",
            "cached_unreachable_recipes": "excluded from source fetch and build order",
        },
        "seed_packages": SEED_PACKAGES,
        "packages_processed": sorted(seen),
        "package_count": len(seen),
        "processed_package_bases": sorted(processed_bases),
        "package_base_count": len(filtered_recipes),
        "recipes": filtered_recipes,
        "builder_makedepends": builder_only,
        "checkdepends_for_later_tests": test_only,
        "unresolved_virtual_or_provider_dependencies": sorted(unresolved_virtual),
    }
    write_json(PLAN / "arch-recipe-closure.json", result)
    write_progress()

    log(
        f"Collected {len(filtered_recipes)} reachable target package-base recipes "
        f"covering {len(seen)} runtime package names."
    )
    log(
        f"Recorded {len(builder_only)} build-only dependencies for the clean "
        "Arch builder and "
        f"{len(test_only)} test-only dependencies for the later validation phase."
    )
    if unresolved_virtual:
        log(
            f"Recorded {len(unresolved_virtual)} virtual/provider or non-official "
            "dependencies for explicit resolution by the build orchestrator."
        )
    return result


'''

def replace_function(source: str, name: str, replacement: str, next_name: str) -> str:
    start = source.find(f"def {name}(")
    end = source.find(f"def {next_name}(", start)
    if start == -1 or end == -1:
        raise SystemExit(f"Could not locate function boundary: {name} -> {next_name}")
    return source[:start] + replacement + source[end:]

text = replace_function(
    text,
    "index_recipe_directory",
    new_index,
    "initial_recipe_index",
)
text = replace_function(
    text,
    "collect_arch_recipe_closure",
    new_closure,
    "fetch_arch_recipe_sources",
)

compile(text, str(TARGET), "exec")
TARGET.write_text(text, encoding="utf-8")
TARGET.chmod(0o755)

print(f"Patched: {TARGET}")
print(f"Backup:  {backup}")
print()
print("Changes:")
print("  - Runtime dependencies recurse into the target source graph.")
print("  - Build-only makedepends are recorded for the Arch builder.")
print("  - Checkdepends are recorded for later tests, not recursively expanded.")
print("  - Old cached recipes outside the reachable target graph are excluded.")
print("  - Existing .SRCINFO files are reused to speed resumed runs.")
print("  - Runtime closure progress is checkpointed.")
