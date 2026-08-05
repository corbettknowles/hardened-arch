#!/bin/bash
# =============================================================================
# Hardened Arch Linux V1.10 - KDE Plasma Stack Build Script (WSL2 x86_64)
# Builds KDE Frameworks 6.27.0 + Plasma 6.7.0 + GDM 50.0 + Dolphin + Pacman + Plymouth
# from source tarballs in /home/corbett, with git fallback for missing deps.
# x86-64 EFI kernel is built separately and linked later.
# =============================================================================

set -e  # Exit on error
set -u  # Treat unset vars as error

# --- Configuration ---
SRC_DIR="/home/corbett"
BUILD_DIR="$HOME/kde-build"
INSTALL_PREFIX="/usr"
JOBS=$(nproc)
LOG_DIR="$BUILD_DIR/logs"

mkdir -p "$BUILD_DIR" "$LOG_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[BUILD]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }

# --- Git dependency helper ---
# If tarball is missing, try to fetch source from git.
get_source_from_git() {
    local name="$1"
    local git_url="$2"
    local src="$BUILD_DIR/$name"

    if [ -d "$src" ]; then
        log "Git source for $name already present."
        return 0
    fi

    log "Tarball for $name missing, cloning from git: $git_url"
    git clone --depth=1 "$git_url" "$src" || fail "Failed to clone $git_url for $name"
}

# Map component name to git URL (extend as needed)
git_url_for() {
    local name="$1"
    case "$name" in
        extra-cmake-modules-6.27.0) echo "https://invent.kde.org/frameworks/extra-cmake-modules.git" ;;
        kwidgetsaddons-6.27.0)      echo "https://invent.kde.org/frameworks/kwidgetsaddons.git" ;;
        kde-frameworks-*)           echo "https://invent.kde.org/frameworks/${name%%-*}.git" ;;
        libplasma-6.7.0)            echo "https://invent.kde.org/plasma/libplasma.git" ;;
        plasma-workspace-6.7.0)     echo "https://invent.kde.org/plasma/plasma-workspace.git" ;;
        plasma-desktop-6.7.0)       echo "https://invent.kde.org/plasma/plasma-desktop.git" ;;
        gdm-50.0)                   echo "https://gitlab.gnome.org/GNOME/gdm.git" ;;
        dolphin-26.04.2)            echo "https://invent.kde.org/system/dolphin.git" ;;
        pacman-7.0.0)               echo "https://gitlab.archlinux.org/pacman/pacman.git" ;;
        plymouth)                   echo "https://gitlab.freedesktop.org/plymouth/plymouth.git" ;;
        *)                          echo "" ;;
    esac
}

# --- Helper: ensure source (tar or git) ---
ensure_source() {
    local tarball="$1"
    local name=$(basename "$tarball" .tar.xz)
    local src="$BUILD_DIR/$name"

    if [ -f "$tarball" ]; then
        log "Using tarball for $name: $tarball"
        mkdir -p "$src"
        tar -xf "$tarball" -C "$BUILD_DIR" || fail "Failed to extract $tarball"
    else
        warn "Tarball $tarball not found for $name."
        local url
        url=$(git_url_for "$name")
        if [ -n "$url" ]; then
            get_source_from_git "$name" "$url"
        else
            fail "No git URL known for $name and tarball missing."
        fi
    fi
}

# --- Helper: extract, configure, build, install (CMake) ---
build_cmake() {
    local tarball="$1"
    local name=$(basename "$tarball" .tar.xz)
    local extra_flags="${2:-}"

    log "Building $name..."
    local src="$BUILD_DIR/$name"
    local build="$BUILD_DIR/${name}-build"
    local logfile="$LOG_DIR/${name}.log"

    if [ -f "$LOG_DIR/${name}.done" ]; then
        log "$name already built, skipping."
        return 0
    fi

    # Ensure source (tar or git)
    ensure_source "$tarball"

    # Configure
    mkdir -p "$build"
    cmake -S "$src" -B "$build" \
        -DCMAKE_CXX_FLAGS="-Wno-error -Wno-error=unused-command-line-argument" 
		-DCMAKE_INSTALL_PREFIX="$INSTALL_PREFIX" \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_TESTING=OFF \
        -DBUILD_PYTHON=OFF \
        -DKF_ENABLE_PYTHON_BINDINGS=OFF \
		$extra_flags \
        >> "$logfile" 2>&1 || fail "CMake configure failed for $name — see $logfile"

    # Build
    cmake --build "$build" -j"$JOBS" >> "$logfile" 2>&1 || fail "Build failed for $name — see $logfile"

    # Install
    sudo cmake --install "$build" >> "$logfile" 2>&1 || fail "Install failed for $name — see $logfile"

    touch "$LOG_DIR/${name}.done"
    log "$name installed successfully."
}

# --- Helper: extract, configure, build, install (Meson) ---
build_meson() {
    local tarball="$1"
    local name=$(basename "$tarball" .tar.xz)
    local extra_flags="${2:-}"

    log "Building $name (meson)..."
    local src="$BUILD_DIR/$name"
    local build="$BUILD_DIR/${name}-build"
    local logfile="$LOG_DIR/${name}.log"

    if [ -f "$LOG_DIR/${name}.done" ]; then
        log "$name already built, skipping."
        return 0
    fi

    # Ensure source (tar or git)
    ensure_source "$tarball"

    meson setup "$build" "$src" \
        --prefix="$INSTALL_PREFIX" \
        --buildtype=release \
        $extra_flags \
        >> "$logfile" 2>&1 || fail "Meson setup failed for $name — see $logfile"

    ninja -C "$build" -j"$JOBS" >> "$logfile" 2>&1 || fail "Ninja build failed for $name — see $logfile"

    sudo ninja -C "$build" install >> "$logfile" 2>&1 || fail "Install failed for $name — see $logfile"

    touch "$LOG_DIR/${name}.done"
    log "$name installed successfully."
}

# =============================================================================
# STAGE 1: Extra CMake Modules (must be first)
# =============================================================================
log "=== STAGE 1: Extra CMake Modules ==="
build_cmake "$SRC_DIR/extra-cmake-modules-6.27.0.tar.xz"

# =============================================================================
# STAGE 2: KDE Frameworks 6.27.0 (dependency order)
# =============================================================================
log "=== STAGE 2: KDE Frameworks ==="

KF6_FLAGS="-DKF6_ENABLE_FINAL_PRODUCT=ON"

build_cmake "$SRC_DIR/kapidox-6.27.0.tar.xz"
build_cmake "$SRC_DIR/karchive-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kcodecs-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kconfig-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kwidgetsaddons-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kcompletion-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kcoreaddons-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kdbusaddons-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kguiaddons-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/ki18n-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kidletime-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kimageformats-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kitemmodels-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kitemviews-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kplotting-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kwindowsystem-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/solid-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/sonnet-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/threadweaver-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kauth-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kbookmarks-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kcmutils-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kcolorscheme-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kcrash-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kdoctools-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kfilemetadata-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kglobalaccel-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kiconthemes-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kio-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kjobwidgets-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/knotifications-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/knotifyconfig-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kpackage-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kparts-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kpeople-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kpty-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kquickcharts-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/krunner-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kservice-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kstatusnotifieritem-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/ksvg-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/ktexteditor-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/ktexttemplate-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/ktextwidgets-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kunitconversion-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kuserfeedback-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kwallet-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kxmlgui-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kirigami-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/knewstuff-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kdeclarative-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kded-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kdesu-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/frameworkintegration-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/attica-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/baloo-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/bluez-qt-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/breeze-icons-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kcalendarcore-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kcontacts-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kdav-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kdnssd-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kholidays-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/kmime-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/modemmanager-qt-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/networkmanager-qt-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/prison-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/purpose-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/qqc2-desktop-style-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/syndication-6.27.0.tar.xz" "$KF6_FLAGS"
build_cmake "$SRC_DIR/syntax-highlighting-6.27.0.tar.xz" "$KF6_FLAGS"

# =============================================================================
# STAGE 2.5: XORG STACK (REQUIRED BEFORE PLASMA/GDM)
# =============================================================================
log "=== STAGE 2.5: XORG STACK ==="

build_cmake "$SRC_DIR/xorgproto-*.tar.xz"
build_cmake "$SRC_DIR/libX11-*.tar.xz"
build_cmake "$SRC_DIR/libXext-*.tar.xz"
build_cmake "$SRC_DIR/libXrender-*.tar.xz"
build_cmake "$SRC_DIR/libXrandr-*.tar.xz"
build_cmake "$SRC_DIR/libXfixes-*.tar.xz"
build_cmake "$SRC_DIR/libXi-*.tar.xz"
build_cmake "$SRC_DIR/libXcursor-*.tar.xz"
build_cmake "$SRC_DIR/libXcomposite-*.tar.xz"
build_cmake "$SRC_DIR/libXdamage-*.tar.xz"
build_cmake "$SRC_DIR/libXinerama-*.tar.xz"
build_cmake "$SRC_DIR/libXau-*.tar.xz"
build_cmake "$SRC_DIR/libXdmcp-*.tar.xz"

# xorg-server is usually meson, NOT cmake:
build_meson "$SRC_DIR/xorg-server-*.tar.xz"

build_cmake "$SRC_DIR/xinit-*.tar.xz"
# =============================================================================
# STAGE 3: KDE Plasma 6.7.0
# =============================================================================
log "=== STAGE 3: KDE Plasma ==="

build_cmake "$SRC_DIR/libplasma-6.7.0.tar.xz"
build_cmake "$SRC_DIR/kdecoration-6.7.0.tar.xz"
build_cmake "$SRC_DIR/kscreenlocker-6.7.0.tar.xz"
build_cmake "$SRC_DIR/breeze-6.7.0.tar.xz"
build_cmake "$SRC_DIR/layer-shell-qt-6.7.0.tar.xz"
build_cmake "$SRC_DIR/kwayland-6.7.0.tar.xz"
build_cmake "$SRC_DIR/kwin-6.7.0.tar.xz"
build_cmake "$SRC_DIR/kwin-x11-6.7.0.tar.xz"
build_cmake "$SRC_DIR/plasma-activities-6.7.0.tar.xz"
build_cmake "$SRC_DIR/plasma-activities-stats-6.7.0.tar.xz"
build_cmake "$SRC_DIR/kglobalacceld-6.7.0.tar.xz"
build_cmake "$SRC_DIR/kscreen-6.7.0.tar.xz"
build_cmake "$SRC_DIR/libkscreen-6.7.0.tar.xz"
build_cmake "$SRC_DIR/libksysguard-6.7.0.tar.xz"
build_cmake "$SRC_DIR/plasma5support-6.7.0.tar.xz"
build_cmake "$SRC_DIR/plasma-workspace-6.7.0.tar.xz"
build_cmake "$SRC_DIR/plasma-desktop-6.7.0.tar.xz"
build_cmake "$SRC_DIR/plasma-integration-6.7.0.tar.xz"
build_cmake "$SRC_DIR/powerdevil-6.7.0.tar.xz"
build_cmake "$SRC_DIR/bluedevil-6.7.0.tar.xz"
build_cmake "$SRC_DIR/breeze-gtk-6.7.0.tar.xz"
build_cmake "$SRC_DIR/drkonqi-6.7.0.tar.xz"
build_cmake "$SRC_DIR/flatpak-kcm-6.7.0.tar.xz"
build_cmake "$SRC_DIR/kactivitymanagerd-6.7.0.tar.xz"
build_cmake "$SRC_DIR/kde-cli-tools-6.7.0.tar.xz"
build_cmake "$SRC_DIR/kde-gtk-config-6.7.0.tar.xz"
build_cmake "$SRC_DIR/kdeplasma-addons-6.7.0.tar.xz"
build_cmake "$SRC_DIR/kgamma-6.7.0.tar.xz"
build_cmake "$SRC_DIR/kinfocenter-6.7.0.tar.xz"
build_cmake "$SRC_DIR/kmenuedit-6.7.0.tar.xz"
build_cmake "$SRC_DIR/kpipewire-6.7.0.tar.xz"
build_cmake "$SRC_DIR/krdp-6.7.0.tar.xz"
build_cmake "$SRC_DIR/ksshaskpass-6.7.0.tar.xz"
build_cmake "$SRC_DIR/ksystemstats-6.7.0.tar.xz"
build_cmake "$SRC_DIR/kwayland-integration-6.7.0.tar.xz"
build_cmake "$SRC_DIR/kwallet-pam-6.7.0.tar.xz"
build_cmake "$SRC_DIR/kwrited-6.7.0.tar.xz"
build_cmake "$SRC_DIR/milou-6.7.0.tar.xz"
build_cmake "$SRC_DIR/ocean-sound-theme-6.7.0.tar.xz"
build_cmake "$SRC_DIR/oxygen-6.7.0.tar.xz"
build_cmake "$SRC_DIR/oxygen-icons-6.27.0.tar.xz"
build_cmake "$SRC_DIR/oxygen-sounds-6.7.0.tar.xz"
build_cmake "$SRC_DIR/plasma-browser-integration-6.7.0.tar.xz"
build_cmake "$SRC_DIR/plasma-disks-6.7.0.tar.xz"
build_cmake "$SRC_DIR/plasma-firewall-6.7.0.tar.xz"
build_cmake "$SRC_DIR/plasma-nm-6.7.0.tar.xz"
build_cmake "$SRC_DIR/plasma-pa-6.7.0.tar.xz"
build_cmake "$SRC_DIR/plasma-sdk-6.7.0.tar.xz"
build_cmake "$SRC_DIR/plasma-systemmonitor-6.7.0.tar.xz"
build_cmake "$SRC_DIR/plasma-thunderbolt-6.7.0.tar.xz"
build_cmake "$SRC_DIR/plasma-vault-6.7.0.tar.xz"
build_cmake "$SRC_DIR/plasma-welcome-6.7.0.tar.xz"
build_cmake "$SRC_DIR/plasma-workspace-wallpapers-6.7.0.tar.xz"
build_cmake "$SRC_DIR/polkit-kde-agent-1-6.7.0.tar.xz"
build_cmake "$SRC_DIR/print-manager-6.7.0.tar.xz"
build_cmake "$SRC_DIR/qqc2-breeze-style-6.7.0.tar.xz"
build_cmake "$SRC_DIR/sddm-kcm-6.7.0.tar.xz"
build_cmake "$SRC_DIR/systemsettings-6.7.0.tar.xz"
build_cmake "$SRC_DIR/wacomtablet-6.7.0.tar.xz"
build_cmake "$SRC_DIR/xdg-desktop-portal-kde-6.7.0.tar.xz"
build_cmake "$SRC_DIR/aurorae-6.7.0.tar.xz"
build_cmake "$SRC_DIR/discover-6.7.0.tar.xz"
build_cmake "$SRC_DIR/knighttime-6.7.0.tar.xz"
build_cmake "$SRC_DIR/krdp-6.7.0.tar.xz"
build_cmake "$SRC_DIR/plasma-login-manager-6.7.0.tar.xz"
build_cmake "$SRC_DIR/plasma-mobile-6.7.0.tar.xz"
build_cmake "$SRC_DIR/plasma-nano-6.7.0.tar.xz"
build_cmake "$SRC_DIR/plasma-setup-6.7.0.tar.xz"
build_cmake "$SRC_DIR/spacebar-6.7.0.tar.xz"
build_cmake "$SRC_DIR/union-6.7.0.tar.xz"
build_cmake "$SRC_DIR/breeze-grub-6.7.0.tar.xz"
build_cmake "$SRC_DIR/breeze-plymouth-6.7.0.tar.xz"
build_cmake "$SRC_DIR/plymouth-kcm-6.7.0.tar.xz"

# =============================================================================
# STAGE 4: GDM 50.0
# =============================================================================
log "=== STAGE 4: GDM 50.0 ==="
build_meson "$SRC_DIR/gdm-50.0.tar.xz" \
    "-Dplymouth=enabled -Dsystemd=enabled -Ddefault-pam-config=arch"

# =============================================================================
# STAGE 5: Dolphin 26.04.2
# =============================================================================
log "=== STAGE 5: Dolphin ==="
build_cmake "$SRC_DIR/dolphin-26.04.2.tar.xz"

# =============================================================================
# STAGE 6: Pacman 7.0.0
# =============================================================================
log "=== STAGE 6: Pacman ==="

PACMAN_TAR="$SRC_DIR/pacman-7.0.0.tar.xz"
PACMAN_NAME="pacman-7.0.0"
PACMAN_SRC="$BUILD_DIR/$PACMAN_NAME"
PACMAN_BUILD="$BUILD_DIR/${PACMAN_NAME}-build"
PACMAN_LOG="$LOG_DIR/${PACMAN_NAME}.log"

if [ ! -f "$LOG_DIR/${PACMAN_NAME}.done" ]; then
    log "Building Pacman..."
    ensure_source "$PACMAN_TAR"

    meson setup "$PACMAN_BUILD" "$PACMAN_SRC" \
        --prefix="$INSTALL_PREFIX" \
        --buildtype=release \
        -Ddoc=disabled \
        -Dscriptlet-shell=/usr/bin/bash \
        -Dldconfig=/usr/bin/ldconfig \
        >> "$PACMAN_LOG" 2>&1 || fail "Pacman meson setup failed — see $PACMAN_LOG"

    ninja -C "$PACMAN_BUILD" -j"$JOBS" >> "$PACMAN_LOG" 2>&1 || fail "Pacman build failed"
    sudo ninja -C "$PACMAN_BUILD" install >> "$PACMAN_LOG" 2>&1 || fail "Pacman install failed"
    touch "$LOG_DIR/${PACMAN_NAME}.done"
    log "Pacman installed."
fi

# =============================================================================
# STAGE 7: Plymouth (from git in buildroot dl cache or git fallback)
# =============================================================================
log "=== STAGE 7: Plymouth ==="

PLYMOUTH_SRC="$HOME/buildroot/dl/plymouth/git"
PLYMOUTH_BUILD="$BUILD_DIR/plymouth-build"
PLYMOUTH_LOG="$LOG_DIR/plymouth.log"

if [ ! -f "$LOG_DIR/plymouth.done" ]; then
    log "Building Plymouth from git source..."
    if [ ! -d "$PLYMOUTH_SRC" ]; then
        warn "Plymouth git source not found at $PLYMOUTH_SRC, cloning..."
        git clone --depth=1 "$(git_url_for plymouth)" "$PLYMOUTH_SRC" || fail "Failed to clone Plymouth git"
    fi

    mkdir -p "$PLYMOUTH_BUILD"

    meson setup "$PLYMOUTH_BUILD" "$PLYMOUTH_SRC" \
        --prefix="$INSTALL_PREFIX" \
        --buildtype=release \
        -Dlogo=/usr/share/plymouth/themes/hardened-arch/logo.png \
        -Ddefault-theme=hardened-arch \
        >> "$PLYMOUTH_LOG" 2>&1 || fail "Plymouth meson setup failed — see $PLYMOUTH_LOG"

    ninja -C "$PLYMOUTH_BUILD" -j"$JOBS" >> "$PLYMOUTH_LOG" 2>&1 || fail "Plymouth build failed"
    sudo ninja -C "$PLYMOUTH_BUILD" install >> "$PLYMOUTH_LOG" 2>&1 || fail "Plymouth install failed"
    touch "$LOG_DIR/plymouth.done"
    log "Plymouth installed."
fi

# =============================================================================
# DONE
# =============================================================================
log "=== ALL STAGES COMPLETE ==="
log "KDE Plasma 6.7.0 + Frameworks 6.27.0 + GDM 50.0 + Dolphin + Pacman + Plymouth"
log "All installed to $INSTALL_PREFIX"
log "Build logs: $LOG_DIR"
