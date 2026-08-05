#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${1:-$HOME/arch-rebuild}"
MAIN_CONTAINER="arch-rebuild-builder"
BOOTSTRAP_CONTAINER="arch-flit3-bootstrap"
IMAGE="${ARCH_BUILD_IMAGE:-docker.io/library/archlinux:latest}"

TESTPATH="$ROOT/source-cache/auto-recipes/python-testpath/PKGBUILD"
FLITDIR="$ROOT/source-cache/auto-recipes/python-flit-core"
FLITPKG="$FLITDIR/PKGBUILD"
REPO="$ROOT/build-output/repo"
STATE="$ROOT/build-state"
WORK="$ROOT/build-work"

export XDG_RUNTIME_DIR="/tmp/podman-runtime-$(id -u)"
export CONTAINERS_CGROUP_MANAGER="cgroupfs"

mkdir -p "$XDG_RUNTIME_DIR" "$REPO" "$STATE" "$WORK"
chmod 700 "$XDG_RUNTIME_DIR"

[[ -f "$TESTPATH" ]] || { echo "ERROR: missing $TESTPATH"; exit 1; }
[[ -f "$FLITPKG" ]] || { echo "ERROR: missing $FLITPKG"; exit 1; }

# makepkg/pacman cannot install a dependency target literally named
# "python-flit-core<4". Restore the Arch dependency name; the local repo
# will supply the compatible 3.x build.
sed -i "s/'python-flit-core<4'/'python-flit-core'/g" "$TESTPATH"
sed -i 's/"python-flit-core<4"/"python-flit-core"/g' "$TESTPATH"

FLIT_VERSION="$(
  sed -n 's/^pkgver=//p' "$FLITPKG" | head -n1 | tr -d "'\""
)"

case "$FLIT_VERSION" in
  3.*) ;;
  *)
    echo "ERROR: python-flit-core recipe is not pinned to 3.x:"
    grep -E '^(pkgver|pkgrel)=' "$FLITPKG" || true
    exit 1
    ;;
esac

echo "Pinned python-flit-core version: $FLIT_VERSION"
echo "Removing any stale local flit-core 4.x package..."
find "$REPO" -maxdepth 1 -type f \
  -name 'python-flit-core-4*.pkg.tar.*' -print -delete 2>/dev/null || true

podman --cgroup-manager=cgroupfs rm -f "$BOOTSTRAP_CONTAINER" \
  >/dev/null 2>&1 || true

HOST_UID="$(id -u)"
HOST_GID="$(id -g)"

echo "Building python-flit-core $FLIT_VERSION with checks disabled only for bootstrap..."

podman --cgroup-manager=cgroupfs run --name "$BOOTSTRAP_CONTAINER" --rm \
  --userns=keep-id \
  --user 0:0 \
  -e HOST_UID="$HOST_UID" \
  -e HOST_GID="$HOST_GID" \
  -e FLIT_VERSION="$FLIT_VERSION" \
  -v "$ROOT:/work" \
  "$IMAGE" \
  bash -Eeuo pipefail -c '
    pacman -Syu --noconfirm --needed base-devel sudo

    if ! getent group "$HOST_GID" >/dev/null; then
      groupadd -g "$HOST_GID" builder
    fi

    if ! getent passwd "$HOST_UID" >/dev/null; then
      useradd -m -u "$HOST_UID" -g "$HOST_GID" -s /bin/bash builder
    fi

    USER_NAME="$(getent passwd "$HOST_UID" | cut -d: -f1)"
    printf "%s ALL=(ALL) NOPASSWD: ALL\n" "$USER_NAME" \
      > "/etc/sudoers.d/$USER_NAME"
    chmod 0440 "/etc/sudoers.d/$USER_NAME"

    mkdir -p \
      /work/build-output/repo \
      /work/build-work/python-flit-core-bootstrap
    chown -R "$HOST_UID:$HOST_GID" \
      /work/build-output/repo \
      /work/build-work/python-flit-core-bootstrap

    runuser -u "$USER_NAME" -- env \
      HOME="/home/$USER_NAME" \
      SRCDEST=/work/source-cache/arch-distfiles \
      PKGDEST=/work/build-output/repo \
      BUILDDIR=/work/build-work/python-flit-core-bootstrap \
      PACKAGER="Local Arch Rebuild <builder@localhost.invalid>" \
      bash -Eeuo pipefail -c "
        cd /work/source-cache/auto-recipes/python-flit-core
        makepkg \
          --syncdeps \
          --noconfirm \
          --cleanbuild \
          --clean \
          --force \
          --nocheck \
          --skippgpcheck
      "

    shopt -s nullglob
    packages=(/work/build-output/repo/python-flit-core-3*.pkg.tar.zst)
    if ((${#packages[@]} == 0)); then
      echo "ERROR: no python-flit-core 3.x package was produced"
      exit 1
    fi

    repo-add /work/build-output/repo/arch-rebuild.db.tar.gz "${packages[@]}"
    printf "Built local package(s):\n"
    printf "  %s\n" "${packages[@]}"
  '

echo "Clearing failed testpath work and stale state..."
rm -rf \
  "$WORK/python-testpath" \
  "$WORK/python-flit-core"

sed -i \
  '/^python-testpath$/d;/^python-flit-core$/d' \
  "$STATE/completed.txt" 2>/dev/null || true

rm -f "$STATE/BUILD_COMPLETE.json"

echo "Removing dead main build container..."
podman --cgroup-manager=cgroupfs rm -f "$MAIN_CONTAINER" \
  >/dev/null 2>&1 || true

echo "Starting main build..."
"$ROOT/tools/arch_build.sh" resume \
  --root "$ROOT" \
  --jobs "${JOBS:-3}" \
  --cpus "${CPUS:-3}"

echo
echo "Local flit-core packages:"
find "$REPO" -maxdepth 1 -type f \
  -name 'python-flit-core-3*.pkg.tar.zst' -ls

echo
echo "Follow with:"
echo "  $ROOT/tools/arch_build.sh logs --follow"
