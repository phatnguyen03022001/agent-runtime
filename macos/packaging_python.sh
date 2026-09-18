#!/usr/bin/env bash
# Canonical packaging interpreter resolver for candidate construction.

PACKAGING_PYTHON_REQUIRED="CPython 3.13.x/cp313 on macOS arm64"

packaging_python_identity() {
  local python_bin="$1"
  "$python_bin" -c 'import platform, sys, sysconfig; print("\t".join((sys.implementation.name, platform.python_version(), str(sys.implementation.cache_tag or ""), str(sysconfig.get_config_var("SOABI") or ""), sys.platform, platform.machine(), sys.executable)))'
}

validate_packaging_python() {
  local python_bin="$1"
  local prefix="${2:-PACKAGING ERROR}"
  local identity implementation version cache_tag soabi system machine executable

  if [[ "$python_bin" != /* || ! -x "$python_bin" ]]; then
    echo "$prefix: unsupported packaging interpreter: required $PACKAGING_PYTHON_REQUIRED; executable must be an absolute executable path: $python_bin" >&2
    return 2
  fi
  if ! identity="$(packaging_python_identity "$python_bin" 2>/dev/null)"; then
    echo "$prefix: unsupported packaging interpreter: required $PACKAGING_PYTHON_REQUIRED; failed to inspect $python_bin" >&2
    return 2
  fi
  IFS=$'\t' read -r implementation version cache_tag soabi system machine executable <<< "$identity"
  if [[ "$implementation" != "cpython" || "$version" != 3.13.* || "$cache_tag" != "cpython-313" || "$soabi" != cpython-313-* || "$system" != "darwin" || "$machine" != "arm64" ]]; then
    echo "$prefix: unsupported packaging interpreter: required $PACKAGING_PYTHON_REQUIRED; got implementation=$implementation version=$version cache_tag=$cache_tag soabi=$soabi platform=$system machine=$machine executable=$executable" >&2
    return 2
  fi
}

packaging_python_base_prefix() {
  local python_bin="$1"
  "$python_bin" -c 'import os, sys; print(os.path.realpath(sys.base_prefix))'
}

packaging_python_realpath() {
  local python_bin="$1"
  local path="$2"
  "$python_bin" -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$path"
}

packaging_python_linkage_dependencies() {
  local copied_python="$1"
  /usr/bin/otool -L "$copied_python" | /usr/bin/awk 'NR > 1 {print $1}'
}

materialize_packaging_python_runtime() {
  local python_bin="$1"
  local package_venv="$2"
  local repo_root="$3"
  local prefix="${4:-PACKAGING ERROR}"
  local copied_python="$package_venv/bin/python"
  local base_prefix dependencies
  local package_venv_real package_lib package_lib_real checkout_venv_real=""
  local dependency library source source_real base_lib_real target target_real

  [[ -x "$copied_python" ]] \
    || { echo "$prefix: copied packaging interpreter is missing or not executable: $copied_python" >&2; return 2; }

  if ! dependencies="$(packaging_python_linkage_dependencies "$copied_python" 2>/dev/null)"; then
    echo "$prefix: failed to inspect copied packaging interpreter Mach-O linkage: $copied_python" >&2
    return 2
  fi
  if ! base_prefix="$(packaging_python_base_prefix "$python_bin" 2>/dev/null)" || [[ -z "$base_prefix" ]]; then
    echo "$prefix: failed to resolve canonical packaging interpreter base prefix" >&2
    return 2
  fi

  package_venv_real="$(packaging_python_realpath "$python_bin" "$package_venv")" \
    || { echo "$prefix: failed to resolve package venv path" >&2; return 2; }
  package_lib="$package_venv/lib"
  mkdir -p "$package_lib"
  package_lib_real="$(packaging_python_realpath "$python_bin" "$package_lib")" \
    || { echo "$prefix: failed to resolve package venv lib path" >&2; return 2; }
  [[ "$package_lib_real" == "$package_venv_real/lib" ]] \
    || { echo "$prefix: unsafe package venv lib containment" >&2; return 2; }

  if [[ -e "$repo_root/.venv" ]]; then
    checkout_venv_real="$(packaging_python_realpath "$python_bin" "$repo_root/.venv")" \
      || { echo "$prefix: failed to resolve checkout .venv path" >&2; return 2; }
  fi

  while IFS= read -r dependency; do
    [[ -n "$dependency" ]] || continue

    if [[ "$dependency" == @executable_path/../lib/* ]]; then
      library="${dependency#@executable_path/../lib/}"
      if [[ -z "$library" || "$library" == */* || "$library" == "." || "$library" == ".." || ! "$library" =~ ^[A-Za-z0-9._+-]+$ ]]; then
        echo "$prefix: unsafe relative runtime library in copied interpreter linkage: $dependency" >&2
        return 2
      fi

      source="$base_prefix/lib/$library"
      [[ -f "$source" ]] \
        || { echo "$prefix: required runtime library is missing from canonical packaging interpreter: $source" >&2; return 2; }
      source_real="$(packaging_python_realpath "$python_bin" "$source")" \
        || { echo "$prefix: failed to resolve required runtime library: $source" >&2; return 2; }
      base_lib_real="$(packaging_python_realpath "$python_bin" "$base_prefix/lib")" \
        || { echo "$prefix: failed to resolve canonical packaging interpreter lib directory" >&2; return 2; }
      [[ -f "$source_real" ]] \
        || { echo "$prefix: required runtime library is not a regular file: $source_real" >&2; return 2; }
      case "$source_real" in
        "$base_lib_real"/*) ;;
        *)
          echo "$prefix: required runtime library escapes canonical packaging interpreter lib: $source_real" >&2
          return 2
          ;;
      esac
      if [[ -n "$checkout_venv_real" ]]; then
        case "$source_real" in
          "$checkout_venv_real"|"$checkout_venv_real"/*)
            echo "$prefix: required runtime library must not resolve from checkout .venv: $source_real" >&2
            return 2
            ;;
        esac
      fi

      target="$package_lib/$library"
      if [[ -L "$target" ]]; then
        echo "$prefix: package runtime library target must not be a symlink: $target" >&2
        return 2
      fi
      if [[ -e "$target" ]]; then
        [[ -f "$target" ]] \
          || { echo "$prefix: package runtime library target is not a regular file: $target" >&2; return 2; }
        /usr/bin/cmp -s "$source_real" "$target" \
          || { echo "$prefix: existing package runtime library bytes differ from canonical source: $target" >&2; return 2; }
        continue
      fi

      /bin/cp "$source_real" "$target"
      [[ -f "$target" && ! -L "$target" ]] \
        || { echo "$prefix: copied runtime library is not a package-owned regular file: $target" >&2; return 2; }
      target_real="$(packaging_python_realpath "$python_bin" "$target")" \
        || { echo "$prefix: failed to resolve copied runtime library: $target" >&2; return 2; }
      case "$target_real" in
        "$package_lib_real"/*) ;;
        *)
          echo "$prefix: copied runtime library escaped package venv lib: $target_real" >&2
          return 2
          ;;
      esac
      /usr/bin/cmp -s "$source_real" "$target" \
        || { echo "$prefix: copied runtime library bytes differ from canonical source: $target" >&2; return 2; }
    elif [[ "$dependency" == @* ]]; then
      echo "$prefix: unsupported relative linkage in copied packaging interpreter: $dependency" >&2
      return 2
    fi
  done <<< "$dependencies"
}


resolve_packaging_python() {
  local prefix="${1:-PACKAGING ERROR}"
  local python_bin="${AGENT_RUNTIME_PACKAGING_PYTHON:-}"

  if [[ -n "$python_bin" && "$python_bin" != /* ]]; then
    echo "$prefix: AGENT_RUNTIME_PACKAGING_PYTHON must be an absolute path" >&2
    return 2
  fi
  if [[ -z "$python_bin" ]]; then
    python_bin="$(command -v python3.13 || true)"
  fi
  if [[ -z "$python_bin" ]]; then
    echo "$prefix: canonical packaging interpreter not found: required $PACKAGING_PYTHON_REQUIRED; provide an absolute AGENT_RUNTIME_PACKAGING_PYTHON or put python3.13 on PATH" >&2
    return 2
  fi
  validate_packaging_python "$python_bin" "$prefix" || return $?
  printf '%s\n' "$python_bin"
}
