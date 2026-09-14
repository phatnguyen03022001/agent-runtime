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
