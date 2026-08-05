#!/usr/bin/env python3
"""CI helper: extract INSTALLER_SCRIPT from a build script for syntax
checking. Kept as a standalone file rather than embedded in the CI YAML,
since inline multi-line Python inside a YAML block scalar is fragile
(indentation requirements conflict between YAML and Python)."""
import sys
import importlib.util

path = sys.argv[1]
spec = importlib.util.spec_from_file_location("m", path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

if hasattr(mod, "INSTALLER_SCRIPT"):
    with open("/tmp/installer_extracted.sh", "w") as f:
        f.write(mod.INSTALLER_SCRIPT)
    print("Extracted OK")
else:
    print("No INSTALLER_SCRIPT found, skipping")
