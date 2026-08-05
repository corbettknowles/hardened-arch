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
    + ".before-splitpkg-soname-fix-"
    + dt.datetime.now().strftime("%Y%m%d-%H%M%S")
)
backup.write_text(text, encoding="utf-8")

new_parse = r'''def parse_srcinfo(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {
        "pkgbase": None,
        "pkgnames": [],
        "base_depends": [],
        "base_makedepends": [],
        "base_checkdepends": [],
        "base_provides": [],
        "packages": {},
        # Compatibility/summary fields populated after parsing.
        "depends": [],
        "makedepends": [],
        "checkdepends": [],
        "provides": [],
    }

    current_package: str | None = None

    def dependency_key(key: str) -> str | None:
        for prefix in ("depends", "makedepends", "checkdepends", "provides"):
            if key == prefix or key.startswith(prefix + "_"):
                return prefix
        return None

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in raw:
            continue
        key, value = (part.strip() for part in raw.split("=", 1))

        if key == "pkgbase":
            data["pkgbase"] = value
            current_package = None
            continue

        if key == "pkgname":
            current_package = value
            data["pkgnames"].append(value)
            data["packages"].setdefault(
                value,
                {
                    "depends": [],
                    "makedepends": [],
                    "checkdepends": [],
                    "provides": [],
                },
            )
            continue

        dep_key = dependency_key(key)
        if dep_key is None:
            continue

        normalized = normalize_dependency(value)
        if not normalized:
            continue

        if current_package is None:
            data[f"base_{dep_key}"].append(normalized)
        else:
            data["packages"][current_package][dep_key].append(normalized)

    for package in data["pkgnames"]:
        data["packages"].setdefault(
            package,
            {
                "depends": [],
                "makedepends": [],
                "checkdepends": [],
                "provides": [],
            },
        )

    for key in (
        "pkgnames",
        "base_depends",
        "base_makedepends",
        "base_checkdepends",
        "base_provides",
    ):
        data[key] = sorted(set(data[key]))

    for package_data in data["packages"].values():
        for key in ("depends", "makedepends", "checkdepends", "provides"):
            package_data[key] = sorted(set(package_data[key]))

    # Summary fields remain available for older code, but closure selection below
    # uses only the chosen split-package output instead of every output in pkgbase.
    data["depends"] = sorted(
        set(data["base_depends"]).union(
            *(set(item["depends"]) for item in data["packages"].values())
        )
    )
    data["makedepends"] = sorted(
        set(data["base_makedepends"]).union(
            *(set(item["makedepends"]) for item in data["packages"].values())
        )
    )
    data["checkdepends"] = sorted(
        set(data["base_checkdepends"]).union(
            *(set(item["checkdepends"]) for item in data["packages"].values())
        )
    )
    data["provides"] = sorted(
        set(data["base_provides"]).union(
            *(set(item["provides"]) for item in data["packages"].values())
        )
    )
    return data


'''

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

    for provided in parsed["base_provides"]:
        package_map.setdefault(provided, directory)

    for package_data in parsed["packages"].values():
        for provided in package_data["provides"]:
            package_map.setdefault(provided, directory)


'''

new_closure = r'''def collect_arch_recipe_closure(env: dict[str, str]) -> dict[str, Any]:
    log("\n" + "=" * 78)
    log("COLLECTING TARGET RUNTIME RECIPE AND DEPENDENCY CLOSURE")
    log("=" * 78)
    log(
        "Policy: follow only the selected split-package output and its runtime "
        "dependencies. SONAME constraints are recorded, not treated as package names. "
        "Build-only and test-only dependencies do not recursively expand the target."
    )

    package_map, recipe_data = initial_recipe_index(env)
    queue = collections.deque(SEED_PACKAGES)
    queued: set[str] = set(SEED_PACKAGES)
    seen_requests: set[str] = set()
    processed_outputs: set[tuple[str, str]] = set()
    selected_outputs: dict[str, set[str]] = collections.defaultdict(set)
    selected_depends: dict[str, set[str]] = collections.defaultdict(set)
    selected_makedepends: dict[str, set[str]] = collections.defaultdict(set)
    selected_checkdepends: dict[str, set[str]] = collections.defaultdict(set)
    selected_sonames: dict[str, set[str]] = collections.defaultdict(set)
    missing_seed: list[str] = []
    unresolved_virtual: set[str] = set()
    builder_makedepends: set[str] = set()
    ignored_checkdepends: set[str] = set()

    # Count actual source package bases, not virtual names, SONAME constraints,
    # or split-package outputs.
    max_package_bases = 1200

    def is_soname_dependency(value: str) -> bool:
        return bool(re.search(r"\.so(?:\.[0-9]+)*$", value))

    def locate_output(
        requested: str,
    ) -> tuple[str | None, str | None, dict[str, Any] | None]:
        for pkgbase, data in recipe_data.items():
            if requested in data["pkgnames"]:
                return pkgbase, requested, data

            if requested == pkgbase:
                if requested in data["pkgnames"]:
                    return pkgbase, requested, data
                if len(data["pkgnames"]) == 1:
                    return pkgbase, data["pkgnames"][0], data

            if requested in data.get("base_provides", []):
                output = requested if requested in data["pkgnames"] else (
                    data["pkgnames"][0] if data["pkgnames"] else pkgbase
                )
                return pkgbase, output, data

            for output, output_data in data.get("packages", {}).items():
                if requested in output_data.get("provides", []):
                    return pkgbase, output, data
        return None, None, None

    def write_progress(current: str | None = None) -> None:
        filtered = {}
        for base in sorted(selected_outputs):
            if base not in recipe_data:
                continue
            item = dict(recipe_data[base])
            item["selected_outputs"] = sorted(selected_outputs[base])
            item["depends"] = sorted(selected_depends[base])
            item["makedepends"] = sorted(selected_makedepends[base])
            item["checkdepends"] = sorted(selected_checkdepends[base])
            item["soname_dependencies"] = sorted(selected_sonames[base])
            filtered[base] = item

        write_json(
            PLAN / "dependency-closure-progress.json",
            {
                "current_package": current,
                "requested_runtime_packages_seen": sorted(seen_requests),
                "requested_runtime_package_count": len(seen_requests),
                "selected_package_bases": sorted(selected_outputs),
                "selected_package_base_count": len(selected_outputs),
                "selected_split_outputs": {
                    base: sorted(outputs)
                    for base, outputs in sorted(selected_outputs.items())
                },
                "queue_remaining": list(queue),
                "queue_remaining_count": len(queue),
                "builder_makedepends": sorted(builder_makedepends),
                "checkdepends_not_in_target_graph": sorted(ignored_checkdepends),
                "soname_constraints": {
                    base: sorted(values)
                    for base, values in sorted(selected_sonames.items())
                    if values
                },
                "unresolved_virtual_or_provider_dependencies": sorted(unresolved_virtual),
                "recipes": filtered,
            },
        )

    while queue:
        requested = normalize_dependency(queue.popleft())
        if (
            not requested
            or requested in seen_requests
            or requested in VIRTUAL_OR_TOOL_DEPS
        ):
            continue

        if is_soname_dependency(requested):
            # A raw SONAME is an ABI constraint, not an Arch package name.
            unresolved_virtual.add(requested)
            continue

        if len(selected_outputs) >= max_package_bases:
            write_progress(requested)
            raise PrepareError(
                "Target runtime closure exceeded "
                f"{max_package_bases} actual package bases. "
                "See dependency-closure-progress.json."
            )

        if not package_exists(requested, env):
            unresolved_virtual.add(requested)
            seen_requests.add(requested)
            continue

        directory = clone_recipe_into_index(
            requested,
            env,
            package_map,
            recipe_data,
        )
        if directory is None:
            if requested in SEED_PACKAGES:
                missing_seed.append(requested)
            else:
                unresolved_virtual.add(requested)
            seen_requests.add(requested)
            continue

        pkgbase, output, data = locate_output(requested)
        seen_requests.add(requested)

        if pkgbase is None or output is None or data is None:
            if requested in SEED_PACKAGES:
                missing_seed.append(requested)
            else:
                unresolved_virtual.add(requested)
            continue

        output_key = (pkgbase, output)
        if output_key in processed_outputs:
            continue
        processed_outputs.add(output_key)
        selected_outputs[pkgbase].add(output)

        output_data = data.get("packages", {}).get(
            output,
            {
                "depends": [],
                "makedepends": [],
                "checkdepends": [],
                "provides": [],
            },
        )

        runtime_deps = sorted(
            set(data.get("base_depends", []))
            | set(output_data.get("depends", []))
        )
        build_deps = sorted(
            set(data.get("base_makedepends", []))
            | set(output_data.get("makedepends", []))
        )
        test_deps = sorted(
            set(data.get("base_checkdepends", []))
            | set(output_data.get("checkdepends", []))
        )

        for dep in runtime_deps:
            dep = normalize_dependency(dep)
            if not dep or dep in VIRTUAL_OR_TOOL_DEPS:
                continue
            if is_soname_dependency(dep):
                selected_sonames[pkgbase].add(dep)
                continue
            selected_depends[pkgbase].add(dep)
            if dep not in seen_requests and dep not in queued:
                queue.append(dep)
                queued.add(dep)

        for dep in build_deps:
            dep = normalize_dependency(dep)
            if not dep or dep in VIRTUAL_OR_TOOL_DEPS:
                continue
            if is_soname_dependency(dep):
                selected_sonames[pkgbase].add(dep)
                continue
            selected_makedepends[pkgbase].add(dep)
            builder_makedepends.add(dep)

        for dep in test_deps:
            dep = normalize_dependency(dep)
            if not dep or dep in VIRTUAL_OR_TOOL_DEPS:
                continue
            if is_soname_dependency(dep):
                selected_sonames[pkgbase].add(dep)
                continue
            selected_checkdepends[pkgbase].add(dep)
            ignored_checkdepends.add(dep)

        if len(processed_outputs) % 25 == 0:
            write_progress(requested)
            log(
                f"Runtime closure progress: {len(selected_outputs)} package bases, "
                f"{len(processed_outputs)} selected split outputs, "
                f"{len(queue)} queued."
            )

    required_missing = sorted(set(missing_seed))
    if required_missing:
        write_progress()
        raise PrepareError(
            "Required Arch recipes could not be cloned/indexed: "
            + ", ".join(required_missing)
        )

    filtered_recipes: dict[str, dict[str, Any]] = {}
    for base in sorted(selected_outputs):
        if base not in recipe_data:
            continue
        item = dict(recipe_data[base])
        item["selected_outputs"] = sorted(selected_outputs[base])
        item["depends"] = sorted(selected_depends[base])
        item["makedepends"] = sorted(selected_makedepends[base])
        item["checkdepends"] = sorted(selected_checkdepends[base])
        item["soname_dependencies"] = sorted(selected_sonames[base])
        filtered_recipes[base] = item

    runtime_names = set(seen_requests)
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
            "target_graph": (
                "recursive runtime depends for selected split-package outputs only"
            ),
            "soname_dependencies": (
                "recorded as ABI constraints and excluded from package-name recursion"
            ),
            "makedepends": (
                "recorded for clean Arch builder, not recursively source-built "
                "into target"
            ),
            "checkdepends": (
                "recorded for later tests, not part of target runtime graph"
            ),
            "cached_unreachable_recipes": (
                "excluded from source fetch and build order"
            ),
        },
        "seed_packages": SEED_PACKAGES,
        "packages_processed": sorted(seen_requests),
        "package_count": len(seen_requests),
        "processed_package_bases": sorted(filtered_recipes),
        "package_base_count": len(filtered_recipes),
        "selected_split_outputs": {
            base: sorted(outputs)
            for base, outputs in sorted(selected_outputs.items())
        },
        "recipes": filtered_recipes,
        "builder_makedepends": builder_only,
        "checkdepends_for_later_tests": test_only,
        "soname_constraints": {
            base: sorted(values)
            for base, values in sorted(selected_sonames.items())
            if values
        },
        "unresolved_virtual_or_provider_dependencies": sorted(unresolved_virtual),
    }
    write_json(PLAN / "arch-recipe-closure.json", result)
    write_progress()

    log(
        f"Collected {len(filtered_recipes)} reachable package-base recipes "
        f"for {len(processed_outputs)} selected split-package outputs."
    )
    log(
        f"Recorded {len(builder_only)} build-only dependencies, "
        f"{len(test_only)} test-only dependencies, and "
        f"{sum(len(v) for v in selected_sonames.values())} SONAME constraints."
    )
    return result


'''

new_topological = r'''def topological_order(
    closure: dict[str, Any],
) -> tuple[list[str], list[list[str]]]:
    recipes = closure["recipes"]
    package_to_base: dict[str, str] = {}

    for base, data in recipes.items():
        package_to_base[base] = base

        selected = set(data.get("selected_outputs", data.get("pkgnames", [])))
        for name in selected:
            package_to_base[name] = base

        for provided in data.get("base_provides", []):
            package_to_base.setdefault(provided, base)

        for output in selected:
            output_data = data.get("packages", {}).get(output, {})
            for provided in output_data.get("provides", []):
                package_to_base.setdefault(provided, base)

    nodes = set(recipes)
    dependencies: dict[str, set[str]] = {node: set() for node in nodes}
    reverse: dict[str, set[str]] = {node: set() for node in nodes}

    for base, data in recipes.items():
        # Runtime edges define the target order. A build dependency contributes an
        # edge only when that dependency is itself already a selected target source
        # node; disposable builder-only tools remain outside the target graph.
        for dep in data.get("depends", []) + data.get("makedepends", []):
            dep = normalize_dependency(dep)
            if re.search(r"\.so(?:\.[0-9]+)*$", dep):
                continue
            dep_base = package_to_base.get(dep)
            if dep_base and dep_base != base and dep_base in nodes:
                dependencies[base].add(dep_base)
                reverse[dep_base].add(base)

    indegree = {node: len(dependencies[node]) for node in nodes}
    ready = collections.deque(
        sorted(node for node, count in indegree.items() if count == 0)
    )
    order: list[str] = []

    while ready:
        node = ready.popleft()
        order.append(node)
        for dependent in sorted(reverse[node]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)

    remaining = sorted(node for node in nodes if node not in order)
    cycles = [remaining] if remaining else []
    order.extend(remaining)
    return order, cycles


'''

def replace_function(source: str, name: str, replacement: str, next_name: str) -> str:
    start = source.find(f"def {name}(")
    end = source.find(f"def {next_name}(", start)
    if start == -1 or end == -1:
        raise SystemExit(
            f"Could not locate function boundary: {name} -> {next_name}"
        )
    return source[:start] + replacement + source[end:]

text = replace_function(text, "parse_srcinfo", new_parse, "index_recipe_directory")
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
text = replace_function(
    text,
    "topological_order",
    new_topological,
    "tier_for",
)

compile(text, str(TARGET), "exec")
TARGET.write_text(text, encoding="utf-8")
TARGET.chmod(0o755)

print(f"Patched: {TARGET}")
print(f"Backup:  {backup}")
print()
print("Corrected causes of runaway closure:")
print("  - Split PKGBUILDs no longer contribute every sibling package's deps.")
print("  - SONAME constraints such as libavcodec.so are no longer queried as packages.")
print("  - Safety limit now counts actual package bases.")
print("  - Cached recipes outside the reachable target graph stay excluded.")
print("  - Existing clones and .SRCINFO files remain reusable.")
