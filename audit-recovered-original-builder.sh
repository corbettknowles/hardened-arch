#!/usr/bin/env bash
set -euo pipefail

BUILDER="$HOME/build_hardened_arch_iso_recovered_pre_damage.py"

echo "===== RECOVERED BUILDER ====="
[[ -f "$BUILDER" ]] || {
    echo "FAIL: Missing recovered builder: $BUILDER"
    exit 1
}

stat -c '%y | %s bytes | %n' "$BUILDER"

echo
echo "===== PYTHON SYNTAX ====="
python3 -m py_compile "$BUILDER"
echo "PYTHON SYNTAX: PASS"

echo
echo "===== COMMAND-LINE OPTIONS ====="
grep -nE 'ArgumentParser|add_argument|--repo-url|--force|--output|--rootfs|--stage' \
    "$BUILDER" || true

echo
echo "===== SOURCE / STAGE / OUTPUT PATHS ====="
grep -nEi \
    'hardened-rootfs-verified|xorg-source-stage|xfce-source-stage|kde|plasma|rootfs|stage|output_iso|repo_url|sourceforge|limine' \
    "$BUILDER" |
    head -n 320 || true

echo
echo "===== DISPLAY MANAGER / SESSION LOGIC ====="
grep -nEi \
    'sddm|gdm|display-manager|startplasma|plasmashell|kwin|startxfce4|xfce4-session|xsessions|wayland-sessions' \
    "$BUILDER" |
    head -n 260 || true

echo
echo "===== COPY / MERGE OPERATIONS ====="
grep -nE \
    'copytree|copy2|copyfile|shutil\.copy|rsync|cp -a|overlay|merge|rmtree|unlink|remove\(' \
    "$BUILDER" |
    head -n 260 || true

echo
echo "===== MAIN BUILD FLOW ====="
grep -nE '^def |^[[:space:]]+def |if __name__|main\(' "$BUILDER" |
    tail -n 120 || true

echo
echo "READ-ONLY AUDIT COMPLETE"
