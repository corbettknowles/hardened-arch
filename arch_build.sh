#!/usr/bin/env bash
set -Eeuo pipefail

CONTAINER_NAME="arch-rebuild-builder"
IMAGE="${ARCH_BUILD_IMAGE:-archlinux:latest}"
ROOT_DEFAULT="$HOME/arch-rebuild"

usage() {
  cat <<'EOF'
Usage:
  arch_build.sh start [--root PATH] [--jobs N] [--cpus N] [--follow]
  arch_build.sh resume [--root PATH] [--jobs N] [--cpus N] [--follow]
  arch_build.sh status [--root PATH]
  arch_build.sh logs [--follow] [--tail N]
  arch_build.sh stop

Environment switches:
  AUTO_FETCH_DEPS=1        Fetch missing official Arch PKGBUILDs and build them.
  ALLOW_BINARY_FALLBACK=1  Final fallback to Arch's signed binary repositories.
  VERIFY_PGP=0             Set to 1 for strict source-signature verification.
  ARCH_BUILD_IMAGE=...     Container image; default archlinux:latest.
EOF
}

find_engine() {
  if command -v docker >/dev/null 2>&1; then
    printf '%s\n' docker
  elif command -v podman >/dev/null 2>&1; then
    printf '%s\n' podman
  else
    echo "ERROR: docker or podman is required." >&2
    exit 1
  fi
}

host_main() {
  local command="${1:-}"
  [[ -n "$command" ]] || { usage; exit 1; }
  if [[ "$command" == "-h" || "$command" == "--help" ]]; then
    usage
    exit 0
  fi
  shift || true

  local root="$ROOT_DEFAULT" jobs="" cpus="" follow=0 tail=120
  while (($#)); do
    case "$1" in
      --root) root="$2"; shift 2 ;;
      --jobs) jobs="$2"; shift 2 ;;
      --cpus) cpus="$2"; shift 2 ;;
      --follow|-f) follow=1; shift ;;
      --tail) tail="$2"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    esac
  done

  root="$(readlink -f "$root")"
  local engine
  engine="$(find_engine)"

  case "$command" in
    start|resume)
      [[ -f "$root/source-plan/READY_FOR_BUILD.json" ]] || {
        echo "Missing $root/source-plan/READY_FOR_BUILD.json" >&2; exit 1;
      }
      [[ -f "$root/source-plan/build-order.txt" ]] || {
        echo "Missing $root/source-plan/build-order.txt" >&2; exit 1;
      }
      grep -q '"ready"[[:space:]]*:[[:space:]]*true' "$root/source-plan/READY_FOR_BUILD.json" || {
        echo "READY_FOR_BUILD.json does not contain ready=true" >&2; exit 1;
      }

      if "$engine" inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
        if [[ "$command" == start ]]; then
          echo "$CONTAINER_NAME already exists. Use resume or remove it first." >&2
          exit 1
        fi
        "$engine" rm -f "$CONTAINER_NAME" >/dev/null
      fi

      if [[ -z "$jobs" ]]; then
        local n
        n="$(nproc 2>/dev/null || echo 2)"
        (( n > 1 )) && n=$((n - 1))
        (( n > 4 )) && n=4
        (( n < 1 )) && n=1
        jobs="$n"
      fi
      [[ -n "$cpus" ]] || cpus="$jobs"

      local self
      self="$(readlink -f "$0")"
      "$engine" run -d \
        --name "$CONTAINER_NAME" \
        --init \
        --cpus "$cpus" \
        -v "$root:/work" \
        -v "$self:/runner.sh:ro" \
        -e BUILD_JOBS="$jobs" \
        -e HOST_UID="$(id -u)" \
        -e HOST_GID="$(id -g)" \
        -e AUTO_FETCH_DEPS="${AUTO_FETCH_DEPS:-1}" \
        -e ALLOW_BINARY_FALLBACK="${ALLOW_BINARY_FALLBACK:-1}" \
        -e VERIFY_PGP="${VERIFY_PGP:-0}" \
        "$IMAGE" /bin/bash -lc \
        'set -e; pacman -Syu --noconfirm --needed python base-devel devtools git gnupg sudo pacman-contrib; bash /runner.sh __inside' \
        >/dev/null

      echo "Started $CONTAINER_NAME"
      echo "Build root: $root"
      echo "Parallel jobs: $jobs; CPU limit: $cpus"
      echo "Follow: $self logs --follow"
      echo "Status: $self status --root '$root'"
      echo "Stop:   $self stop"
      (( follow )) && "$engine" logs -f "$CONTAINER_NAME"
      ;;

    status)
      if "$engine" inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
        "$engine" inspect -f 'container={{.State.Status}} running={{.State.Running}} exit={{.State.ExitCode}}' "$CONTAINER_NAME"
      else
        echo "container=not-created"
      fi
      if [[ -f "$root/build-state/status.env" ]]; then
        # shellcheck disable=SC1090
        source "$root/build-state/status.env"
        echo "progress=${COMPLETED_COUNT:-0}/${TOTAL_COUNT:-?}"
        echo "current=${CURRENT_PACKAGE:-none}"
        echo "last_result=${LAST_RESULT:-unknown}"
        echo "updated=${UPDATED_AT:-unknown}"
      else
        echo "build-state=not-initialized"
      fi
      [[ -f "$root/build-state/BUILD_COMPLETE.json" ]] && echo "complete=true"
      ;;

    logs)
      "$engine" inspect "$CONTAINER_NAME" >/dev/null 2>&1 || { echo "Container does not exist." >&2; exit 1; }
      if (( follow )); then
        "$engine" logs --tail "$tail" -f "$CONTAINER_NAME"
      else
        "$engine" logs --tail "$tail" "$CONTAINER_NAME"
      fi
      ;;

    stop)
      "$engine" stop -t 30 "$CONTAINER_NAME" || true
      ;;

    *) usage; exit 1 ;;
  esac
}

inside_setup() {
  local uid="${HOST_UID:-1000}" gid="${HOST_GID:-1000}"
  local group_name user_name

  mkdir -p /work/{build-output/repo,build-work,build-logs/makepkg,build-state}
  mkdir -p /work/source-cache/{auto-recipes,arch-distfiles}

  if ! getent group "$gid" >/dev/null 2>&1; then
    groupadd -g "$gid" builder
  fi
  group_name="$(getent group "$gid" | cut -d: -f1)"

  if ! getent passwd "$uid" >/dev/null 2>&1; then
    useradd -m -u "$uid" -g "$group_name" -s /bin/bash builder
  fi
  user_name="$(getent passwd "$uid" | cut -d: -f1)"

  printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$user_name" > "/etc/sudoers.d/$user_name"
  chmod 0440 "/etc/sudoers.d/$user_name"

  chown -R "$uid:$gid" \
    /work/build-output /work/build-work /work/build-logs /work/build-state \
    /work/source-cache/auto-recipes

  local repo_db=/work/build-output/repo/arch-rebuild.db.tar
  if [[ ! -f "$repo_db" ]]; then
    bsdtar -cf "$repo_db" --files-from /dev/null
  fi
  ln -sfn "$(basename "$repo_db")" /work/build-output/repo/arch-rebuild.db

  if ! grep -q '^\[arch-rebuild\]$' /etc/pacman.conf; then
    awk '
      BEGIN { inserted=0 }
      /^\[core\]$/ && !inserted {
        print "[arch-rebuild]"
        print "SigLevel = Optional TrustAll"
        print "Server = file:///work/build-output/repo"
        print ""
        inserted=1
      }
      { print }
    ' /etc/pacman.conf > /etc/pacman.conf.new
    mv /etc/pacman.conf.new /etc/pacman.conf
  fi
  pacman -Sy --noconfirm

  exec sudo -u "$user_name" -H env \
    BUILD_JOBS="${BUILD_JOBS:-3}" \
    AUTO_FETCH_DEPS="${AUTO_FETCH_DEPS:-1}" \
    ALLOW_BINARY_FALLBACK="${ALLOW_BINARY_FALLBACK:-1}" \
    VERIFY_PGP="${VERIFY_PGP:-0}" \
    bash /runner.sh __driver
}

inside_driver() {
  set -Eeuo pipefail
  shopt -s nullglob extglob

  local WORK=/work
  local RECIPES="$WORK/source-cache/arch-recipes"
  local AUTO_RECIPES="$WORK/source-cache/auto-recipes"
  local DISTFILES="$WORK/source-cache/arch-distfiles"
  local ORDER="$WORK/source-plan/build-order.txt"
  local REPO="$WORK/build-output/repo"
  local REPO_DB="$REPO/arch-rebuild.db.tar"
  local BUILDROOT="$WORK/build-work"
  local LOGROOT="$WORK/build-logs"
  local STATE="$WORK/build-state"
  local COMPLETED="$STATE/completed.txt"
  local INDEX="$STATE/recipe-index.tsv"
  local AUTO_MANIFEST="$STATE/auto-dependencies.tsv"
  local MASTER="$LOGROOT/overnight-build.log"
  local SESSION_LOCAL="$STATE/installed-local-session.txt"
  local JOBS="${BUILD_JOBS:-3}"
  local AUTO_FETCH="${AUTO_FETCH_DEPS:-1}"
  local BINARY_FALLBACK="${ALLOW_BINARY_FALLBACK:-1}"
  local VERIFY_PGP_MODE="${VERIFY_PGP:-0}"
  local -a PGP_ARGS=()
  [[ "$VERIFY_PGP_MODE" == 1 ]] || PGP_ARGS=(--skippgpcheck)

  mkdir -p "$REPO" "$BUILDROOT" "$LOGROOT/makepkg" "$STATE" "$AUTO_RECIPES"
  touch "$COMPLETED" "$AUTO_MANIFEST" "$SESSION_LOCAL"
  : > "$SESSION_LOCAL"

  exec > >(tee -a "$MASTER") 2>&1
  exec 9>"$STATE/build.lock"
  flock -n 9 || { echo "Another build driver already holds the lock."; exit 1; }

  export LC_ALL=C.UTF-8 LANG=C.UTF-8
  export SRCDEST="$DISTFILES"
  export PKGDEST="$REPO"
  export LOGDEST="$LOGROOT/makepkg"
  export MAKEFLAGS="-j$JOBS"
  export CMAKE_BUILD_PARALLEL_LEVEL="$JOBS"
  export NINJAFLAGS="-j$JOBS"
  export CARGO_BUILD_JOBS="$JOBS"
  export PACKAGER="${PACKAGER:-Local Arch Rebuild}"

  declare -A ACTIVE=()

  now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
  log() { printf '[%s] %s\n' "$(now)" "$*"; }
  dep_name() { printf '%s\n' "$1" | sed -E 's/[<>=].*$//'; }
  completed_count() { sort -u "$COMPLETED" | sed '/^$/d' | wc -l; }
  total_count() { grep -Ev '^[[:space:]]*(#|$)' "$ORDER" | awk '!seen[$0]++' | wc -l; }

  write_status() {
    local current="${1:-}" result="${2:-running}"
    cat > "$STATE/status.env.tmp" <<EOF
TOTAL_COUNT=$(total_count)
COMPLETED_COUNT=$(completed_count)
CURRENT_PACKAGE=$(printf '%q' "$current")
LAST_RESULT=$(printf '%q' "$result")
UPDATED_AT=$(printf '%q' "$(now)")
EOF
    mv "$STATE/status.env.tmp" "$STATE/status.env"
  }

  on_signal() {
    log "Stop signal received. State is saved; use resume to continue."
    write_status "${CURRENT_PACKAGE:-}" stopped
    exit 130
  }
  trap on_signal INT TERM

  recipe_srcinfo() {
    local dir="$1"
    if [[ -f "$dir/.SRCINFO" ]]; then
      cat "$dir/.SRCINFO"
    else
      (cd "$dir" && makepkg --printsrcinfo)
    fi
  }

  add_recipe_to_index() {
    local dir="$1" prepared="$2" info base name
    info="$(recipe_srcinfo "$dir")" || return 1
    base="$(awk -F ' = ' '$1 ~ /^[[:space:]]*pkgbase$/ {print $2; exit}' <<<"$info")"
    [[ -n "$base" ]] || base="$(basename "$dir")"
    printf '%s\t%s\t%s\t%s\n' "$base" "$base" "$dir" "$prepared" >> "$INDEX"
    while IFS= read -r name; do
      [[ -n "$name" ]] || continue
      name="$(dep_name "$name")"
      printf '%s\t%s\t%s\t%s\n' "$name" "$base" "$dir" "$prepared" >> "$INDEX"
    done < <(awk -F ' = ' '$1 ~ /^[[:space:]]*(pkgname|provides)$/ {print $2}' <<<"$info")
  }

  build_index() {
    : > "$INDEX"
    local dir count=0
    log "Indexing prepared recipes."
    for dir in "$RECIPES"/*; do
      [[ -f "$dir/PKGBUILD" ]] || continue
      add_recipe_to_index "$dir" prepared || log "WARNING: could not index $(basename "$dir")"
      count=$((count + 1))
      (( count % 50 == 0 )) && log "Indexed $count recipe directories."
    done
    for dir in "$AUTO_RECIPES"/*; do
      [[ -f "$dir/PKGBUILD" ]] || continue
      add_recipe_to_index "$dir" auto || true
    done
    awk -F '\t' '!seen[$1]++' "$INDEX" > "$INDEX.tmp" && mv "$INDEX.tmp" "$INDEX"
    log "Recipe index contains $(wc -l < "$INDEX") package/provide names."
  }

  lookup_recipe() {
    local name="$1"
    awk -F '\t' -v n="$name" '$1==n {print $2 "\t" $3; exit}' "$INDEX"
  }

  package_deps() {
    local dir="$1"
    recipe_srcinfo "$dir" | awk -F ' = ' '
      $1 ~ /^[[:space:]]*(depends|makedepends|checkdepends)(_[A-Za-z0-9_]+)?$/ {print $2}
    ' | awk '!seen[$0]++'
  }

  missing_deps() {
    local -a deps=("$@")
    ((${#deps[@]})) || return 0
    pacman -T "${deps[@]}" 2>/dev/null || true
  }

  repo_has_name() {
    pacman -Slq arch-rebuild 2>/dev/null | grep -Fxq "$1"
  }

  prefer_local_dep() {
    local expression="$1" name
    name="$(dep_name "$expression")"
    repo_has_name "$name" || return 0
    pacman -Q "$name" >/dev/null 2>&1 || return 0
    grep -Fxq "$name" "$SESSION_LOCAL" && return 0
    log "Replacing installed bootstrap copy of $name with source-built local package."
    sudo pacman -S --noconfirm --asdeps "arch-rebuild/$name"
    printf '%s\n' "$name" >> "$SESSION_LOCAL"
  }

  resolve_official_base() {
    local expression="$1" name target base
    name="$(dep_name "$expression")"
    if pacman -Si "$name" >/tmp/pacman-si.$$ 2>/dev/null; then
      target="$name"
    else
      target="$(pacman -Sddp --noconfirm --print-format '%n' "$expression" 2>/dev/null | head -n1 || true)"
      [[ -n "$target" ]] || { rm -f /tmp/pacman-si.$$; return 1; }
      pacman -Si "$target" >/tmp/pacman-si.$$ 2>/dev/null || { rm -f /tmp/pacman-si.$$; return 1; }
    fi
    base="$(awk -F ':' '$1 ~ /^Base[[:space:]]*$/ {gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2); print $2; exit}' /tmp/pacman-si.$$)"
    if [[ -z "$base" || "$base" == None ]]; then
      base="$(awk -F ':' '$1 ~ /^Name[[:space:]]*$/ {gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2); print $2; exit}' /tmp/pacman-si.$$)"
    fi
    rm -f /tmp/pacman-si.$$
    [[ -n "$base" ]] || return 1
    printf '%s\t%s\n' "$target" "$base"
  }

  fetch_official_recipe() {
    local expression="$1" parent="$2" resolved target base dir head
    resolved="$(resolve_official_base "$expression")" || return 1
    target="${resolved%%$'\t'*}"
    base="${resolved#*$'\t'}"
    dir="$AUTO_RECIPES/$base"
    if [[ ! -f "$dir/PKGBUILD" ]]; then
      log "Fetching official Arch recipe $base for missing $expression required by $parent." >&2
      (cd "$AUTO_RECIPES" && pkgctl repo clone --protocol https "$base") >&2 || return 1
    fi
    head="$(git -C "$dir" rev-parse HEAD 2>/dev/null || echo unknown)"
    printf '%s\t%s\t%s\t%s\t%s\n' "$(now)" "$expression" "$target" "$base" "$head" >> "$AUTO_MANIFEST"
    add_recipe_to_index "$dir" auto
    awk -F '\t' '!seen[$1]++' "$INDEX" > "$INDEX.tmp" && mv "$INDEX.tmp" "$INDEX"
    printf '%s\n' "$base"
  }

  declare CURRENT_PACKAGE=""

  build_package() {
    local requested="$1" reason="$2" record base dir
    record="$(lookup_recipe "$requested" || true)"
    if [[ -z "$record" ]]; then
      local fetched
      fetched="$(fetch_official_recipe "$requested" "$reason" || true)"
      [[ -n "$fetched" ]] || { log "No recipe found for $requested"; return 1; }
      record="$(lookup_recipe "$fetched")"
    fi
    base="${record%%$'\t'*}"
    dir="${record#*$'\t'}"

    if grep -Fxq "$base" "$COMPLETED"; then
      log "SKIP $base: already completed."
      return 0
    fi
    if [[ -n "${ACTIVE[$base]:-}" ]]; then
      log "Dependency cycle detected at $base"
      return 1
    fi

    ACTIVE[$base]=1
    CURRENT_PACKAGE="$base"
    write_status "$base" running
    log "=============================================================================="
    log "BUILD $base — $reason"
    log "=============================================================================="

    export BUILDDIR="$BUILDROOT/$base"
    mkdir -p "$BUILDDIR"

    log "Verifying checksums for $base."
    if ! (cd "$dir" && makepkg --verifysource "${PGP_ARGS[@]}" --noconfirm); then
      log "SOURCE VERIFICATION FAILED for $base. No checksum was changed or bypassed."
      unset 'ACTIVE[$base]'
      write_status "$base" source-verification-failed
      return 1
    fi

    local -a deps=()
    mapfile -t deps < <(package_deps "$dir")
    local dep miss dep_record dep_base

    for dep in "${deps[@]}"; do
      prefer_local_dep "$dep" || true
    done

    while IFS= read -r miss; do
      [[ -n "$miss" ]] || continue
      dep="$(dep_name "$miss")"
      dep_record="$(lookup_recipe "$dep" || true)"
      if [[ -n "$dep_record" ]]; then
        dep_base="${dep_record%%$'\t'*}"
        if [[ "$dep_base" != "$base" ]]; then
          log "Missing $miss maps to source recipe $dep_base."
          build_package "$dep_base" "dependency of $base" || return 1
          prefer_local_dep "$miss" || true
          if pacman -T "$miss" >/dev/null 2>&1; then
            continue
          fi
          if repo_has_name "$dep"; then
            sudo pacman -S --noconfirm --asdeps "arch-rebuild/$dep" || true
          fi
          pacman -T "$miss" >/dev/null 2>&1 && continue
        fi
      fi

      if [[ "$AUTO_FETCH" == 1 ]]; then
        dep_base="$(fetch_official_recipe "$miss" "$base" || true)"
        if [[ -n "$dep_base" && "$dep_base" != "$base" ]]; then
          build_package "$dep_base" "auto-fetched dependency of $base" || return 1
          if repo_has_name "$dep"; then
            sudo pacman -S --noconfirm --asdeps "arch-rebuild/$dep" || true
          fi
          pacman -T "$miss" >/dev/null 2>&1 && continue
        fi
      fi

      if [[ "$BINARY_FALLBACK" == 1 ]]; then
        log "Final dependency fallback: installing Arch-signed binary $miss for $base."
        sudo pacman -S --noconfirm --needed --asdeps "$miss" || return 1
        pacman -T "$miss" >/dev/null 2>&1 && continue
      fi

      log "UNRESOLVED dependency $miss for $base"
      unset 'ACTIVE[$base]'
      write_status "$base" unresolved-dependency
      return 1
    done < <(missing_deps "${deps[@]}")

    local build_log="$LOGROOT/${base}.driver.log"
    local rc=0
    set +e
    (cd "$dir" && makepkg --syncdeps --noconfirm --cleanbuild --clean --log "${PGP_ARGS[@]}") 2>&1 | tee -a "$build_log"
    rc=${PIPESTATUS[0]}
    set -e

    if (( rc != 0 )); then
      if (( rc == 8 )) || grep -qiE 'missing dependencies|could not resolve all dependencies' "$build_log"; then
        log "Dependency failure detected for $base; rescanning once."
        mapfile -t deps < <(package_deps "$dir")
        while IFS= read -r miss; do
          [[ -n "$miss" ]] || continue
          dep="$(dep_name "$miss")"
          dep_record="$(lookup_recipe "$dep" || true)"
          if [[ -n "$dep_record" ]]; then
            dep_base="${dep_record%%$'\t'*}"
            [[ "$dep_base" == "$base" ]] || build_package "$dep_base" "retry dependency of $base" || return 1
          elif [[ "$AUTO_FETCH" == 1 ]]; then
            dep_base="$(fetch_official_recipe "$miss" "$base" || true)"
            [[ -n "$dep_base" ]] && build_package "$dep_base" "retry auto-dependency of $base" || true
          fi
          if ! pacman -T "$miss" >/dev/null 2>&1 && [[ "$BINARY_FALLBACK" == 1 ]]; then
            sudo pacman -S --noconfirm --needed --asdeps "$miss" || return 1
          fi
        done < <(missing_deps "${deps[@]}")

        set +e
        (cd "$dir" && makepkg --syncdeps --noconfirm --cleanbuild --clean --log "${PGP_ARGS[@]}") 2>&1 | tee -a "$build_log"
        rc=${PIPESTATUS[0]}
        set -e
      fi
    fi

    if (( rc != 0 )); then
      log "REAL BUILD FAILURE for $base (exit $rc). Stopping at the first honest compiler/package error."
      unset 'ACTIVE[$base]'
      write_status "$base" build-failed
      return "$rc"
    fi

    local -a artifacts=()
    mapfile -t artifacts < <(cd "$dir" && makepkg --packagelist)
    ((${#artifacts[@]})) || { log "No artifacts listed for $base"; return 1; }
    for dep in "${artifacts[@]}"; do
      [[ -f "$dep" ]] || { log "Missing expected artifact: $dep"; return 1; }
    done

    repo-add "$REPO_DB" "${artifacts[@]}"
    ln -sfn "$(basename "$REPO_DB")" "$REPO/arch-rebuild.db"
    sudo pacman -Sy --noconfirm

    local artifact pkgname
    for artifact in "${artifacts[@]}"; do
      pkgname="$(pacman -Qp --quiet "$artifact" | head -n1 || true)"
      [[ -n "$pkgname" ]] || continue
      sha256sum "$artifact" >> "$STATE/package-sha256sums.txt"
      if pacman -Q "$pkgname" >/dev/null 2>&1; then
        log "Activating rebuilt bootstrap package $pkgname."
        sudo pacman -U --noconfirm --asdeps "$artifact" || true
        printf '%s\n' "$pkgname" >> "$SESSION_LOCAL"
      fi
    done

    printf '%s\n' "$base" >> "$COMPLETED"
    sort -u "$COMPLETED" -o "$COMPLETED"
    unset 'ACTIVE[$base]'
    write_status "$base" completed
    log "DONE $base — ${#artifacts[@]} artifact(s)."
  }

  build_index
  write_status "" starting
  log "Starting/resuming source build with $JOBS parallel jobs."
  log "Auto-fetch source dependencies: $AUTO_FETCH; signed binary fallback: $BINARY_FALLBACK; strict PGP: $VERIFY_PGP_MODE"

  local item position=0 total
  total="$(total_count)"
  while IFS= read -r item; do
    item="${item##+([[:space:]])}"
    item="${item%%+([[:space:]])}"
    [[ -n "$item" && "$item" != \#* ]] || continue
    position=$((position + 1))
    log "ORDER $position/$total: $item"
    if ! build_package "$item" "build-order position $position"; then
      log "BUILD STOPPED at $item. Resume after fixing the recorded error."
      write_status "$item" failed
      exit 1
    fi
  done < "$ORDER"

  cat > "$STATE/BUILD_COMPLETE.json" <<EOF
{
  "complete": true,
  "completed_at": "$(now)",
  "ordered_packages": $total,
  "completed_recipes": $(completed_count),
  "repository": "$REPO",
  "message": "All ordered Arch package recipes built successfully."
}
EOF
  write_status "" complete
  log "=============================================================================="
  log "ARCH SOURCE BUILD COMPLETE"
  log "=============================================================================="
}

case "${1:-}" in
  __inside) inside_setup ;;
  __driver) inside_driver ;;
  *) host_main "$@" ;;
esac
