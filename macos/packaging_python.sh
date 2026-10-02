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

packaging_python_stdlib_path() {
  local python_bin="$1"
  "$python_bin" -c 'import os, sysconfig; print(os.path.realpath(sysconfig.get_path("stdlib")))'
}

packaging_python_library_identity() {
  local library="$1"
  /usr/bin/otool -D "$library" | /usr/bin/awk 'NR == 2 {print $1; exit}'
}

packaging_python_set_library_identity() {
  local library="$1"
  local identity="$2"
  /usr/bin/install_name_tool -id "$identity" "$library"
}

materialize_packaging_python_runtime() {
  local python_bin="$1"
  local package_venv="$2"
  local repo_root="$3"
  local prefix="${4:-PACKAGING ERROR}"
  local copied_python="$package_venv/bin/python"
  local base_prefix dependencies source_stdlib source_stdlib_real
  local package_venv_real package_lib package_lib_real checkout_venv_real=""
  local dependency library source source_real base_lib_real target target_real
  local current_identity expected_identity

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

  base_lib_real="$(packaging_python_realpath "$python_bin" "$base_prefix/lib")" \
    || { echo "$prefix: failed to resolve canonical packaging interpreter lib directory" >&2; return 2; }
  if ! source_stdlib="$(packaging_python_stdlib_path "$python_bin" 2>/dev/null)" || [[ -z "$source_stdlib" ]]; then
    echo "$prefix: failed to resolve canonical packaging interpreter standard library" >&2
    return 2
  fi
  source_stdlib_real="$(packaging_python_realpath "$python_bin" "$source_stdlib")" \
    || { echo "$prefix: failed to resolve canonical packaging interpreter standard library" >&2; return 2; }
  [[ "$source_stdlib_real" == "$base_lib_real/python3.13" && -d "$source_stdlib_real" ]] \
    || { echo "$prefix: unsupported canonical packaging interpreter standard-library layout: $source_stdlib_real" >&2; return 2; }

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
      else
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
      fi

      if [[ "$library" == "libpython3.13.dylib" ]]; then
        expected_identity="@executable_path/../lib/libpython3.13.dylib"
        current_identity="$(packaging_python_library_identity "$target" 2>/dev/null)" \
          || { echo "$prefix: failed to inspect copied libpython install identity" >&2; return 2; }
        if [[ "$current_identity" != "$expected_identity" ]]; then
          if [[ "$current_identity" != "$source" && "$current_identity" != "$source_real" ]]; then
            echo "$prefix: unsupported copied libpython install identity: $current_identity" >&2
            return 2
          fi
          packaging_python_set_library_identity "$target" "$expected_identity" >/dev/null 2>&1 \
            || { echo "$prefix: failed to normalize copied libpython install identity" >&2; return 2; }
        fi
        current_identity="$(packaging_python_library_identity "$target" 2>/dev/null)" \
          || { echo "$prefix: failed to re-inspect copied libpython install identity" >&2; return 2; }
        [[ "$current_identity" == "$expected_identity" ]] \
          || { echo "$prefix: copied libpython install identity did not normalize" >&2; return 2; }
      fi
    elif [[ "$dependency" == @* ]]; then
      echo "$prefix: unsupported relative linkage in copied packaging interpreter: $dependency" >&2
      return 2
    fi
  done <<< "$dependencies"

  "$python_bin" - "$source_stdlib_real" "$package_venv/lib/python3.13" "$base_prefix" "${HOME:-}" <<'PY'
import ast
import os
import pprint
import stat
import sys
from pathlib import Path

source = Path(sys.argv[1])
target = Path(sys.argv[2])
base_prefix = sys.argv[3]
operator_home = sys.argv[4]
prefix_token = "__AGENT_RUNTIME_SYS_BASE_PREFIX__"

target.mkdir(parents=True, exist_ok=True)
for root_raw, dirs, files in os.walk(source, topdown=True, followlinks=False):
    root = Path(root_raw)
    relative_root = root.relative_to(source)
    kept_dirs = []
    for name in sorted(dirs):
        child = root / name
        relative = child.relative_to(source)
        info = child.lstat()
        if name == "__pycache__" or (len(relative.parts) == 1 and name == "site-packages"):
            continue
        if stat.S_ISLNK(info.st_mode):
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise SystemExit("unsupported special directory in packaging standard library")
        destination = target / relative
        if destination.exists():
            if destination.is_symlink() or not destination.is_dir():
                raise SystemExit("unsafe standard-library destination directory")
        else:
            destination.mkdir(mode=stat.S_IMODE(info.st_mode) & 0o755)
        kept_dirs.append(name)
    dirs[:] = kept_dirs

    for name in sorted(files):
        source_file = root / name
        relative = source_file.relative_to(source)
        info = source_file.lstat()
        if name.endswith(".pyc") or "__pycache__" in relative.parts:
            continue
        if stat.S_ISLNK(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit("unsupported special file in packaging standard library")
        destination = target / relative
        if destination.exists() and (destination.is_symlink() or not destination.is_file()):
            raise SystemExit("unsafe standard-library destination file")
        data = source_file.read_bytes()
        if destination.exists():
            if destination.read_bytes() != data:
                raise SystemExit("standard-library destination collision")
        else:
            destination.write_bytes(data)
        destination.chmod(stat.S_IMODE(info.st_mode) & 0o755)

sysconfig_path = target / "_sysconfigdata__darwin_darwin.py"
if not sysconfig_path.is_file() or sysconfig_path.is_symlink():
    raise SystemExit("canonical sysconfig data is missing from packaged standard library")
tree = ast.parse(sysconfig_path.read_text(encoding="utf-8"), filename=str(sysconfig_path))
values = []
for node in tree.body:
    if isinstance(node, ast.Assign) and any(isinstance(item, ast.Name) and item.id == "build_time_vars" for item in node.targets):
        values.append(ast.literal_eval(node.value))
if len(values) != 1 or not isinstance(values[0], dict):
    raise SystemExit("canonical sysconfig data shape is unsupported")
build_time_vars = values[0]
sanitized = {}
for key, value in build_time_vars.items():
    if not isinstance(key, str):
        raise SystemExit("canonical sysconfig data key is unsupported")
    if isinstance(value, str):
        if prefix_token in value:
            raise SystemExit("canonical sysconfig data collides with runtime prefix token")
        value = value.replace(base_prefix, prefix_token)
        if operator_home and operator_home != "/" and operator_home in value:
            raise SystemExit("canonical sysconfig data retains operator HOME outside packaging prefix")
    sanitized[key] = value

rendered = (
    "import sys as _sys\n\n"
    f"_PREFIX_TOKEN = {prefix_token!r}\n"
    "_RAW_BUILD_TIME_VARS = "
    + pprint.pformat(sanitized, sort_dicts=True, width=120)
    + "\n"
    "build_time_vars = {\n"
    "    _key: (_value.replace(_PREFIX_TOKEN, _sys.base_prefix) if isinstance(_value, str) else _value)\n"
    "    for _key, _value in _RAW_BUILD_TIME_VARS.items()\n"
    "}\n"
)
sysconfig_path.write_text(rendered, encoding="utf-8")
payload = sysconfig_path.read_bytes()
for marker in (base_prefix.encode(), operator_home.encode() if operator_home and operator_home != "/" else b""):
    if marker and marker in payload:
        raise SystemExit("sanitized sysconfig data retains build-host identity")

lib_dynload = target / "lib-dynload"
if not lib_dynload.is_dir() or lib_dynload.is_symlink():
    raise SystemExit("packaged standard library is missing lib-dynload")
PY
  [[ "$?" == "0" ]] \
    || { echo "$prefix: failed to materialize relocatable CPython standard library" >&2; return 2; }
}

finalize_packaging_python_runtime() {
  local python_bin="$1"
  local package_venv="$2"
  local repo_root="$3"
  local prefix="${4:-PACKAGING ERROR}"
  local base_prefix package_venv_real

  package_venv_real="$(packaging_python_realpath "$python_bin" "$package_venv")" \
    || { echo "$prefix: failed to resolve package venv during finalization" >&2; return 2; }
  [[ "$package_venv_real" == "$package_venv" ]] \
    || { echo "$prefix: package venv must be a canonical non-symlink path during finalization" >&2; return 2; }
  if ! base_prefix="$(packaging_python_base_prefix "$python_bin" 2>/dev/null)" || [[ -z "$base_prefix" ]]; then
    echo "$prefix: failed to resolve packaging prefix during finalization" >&2
    return 2
  fi

  rm -f "$package_venv/pyvenv.cfg"
  [[ ! -e "$package_venv/pyvenv.cfg" ]] \
    || { echo "$prefix: final package runtime must not retain pyvenv.cfg" >&2; return 2; }

  "$python_bin" - "$package_venv" "$base_prefix" "${HOME:-}" <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1])
markers = [sys.argv[2].encode()]
if sys.argv[3] and sys.argv[3] != "/":
    markers.append(sys.argv[3].encode())

for path in root.rglob("*"):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise SystemExit("final package runtime contains a symlink")
    if stat.S_ISDIR(info.st_mode):
        if path.name == "__pycache__":
            raise SystemExit("final package runtime contains __pycache__")
        continue
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit("final package runtime contains a special file")
    if path.suffix == ".pyc":
        raise SystemExit("final package runtime contains bytecode cache")
    data = path.read_bytes()
    if any(marker and marker in data for marker in markers):
        raise SystemExit(f"final package runtime retains build-host identity: {path.relative_to(root)}")
PY
  [[ "$?" == "0" ]] \
    || { echo "$prefix: final package runtime contains build-host or unsafe material" >&2; return 2; }

  [[ ! -e "$repo_root/.venv" || "$package_venv_real" != "$(packaging_python_realpath "$python_bin" "$repo_root/.venv" 2>/dev/null || true)" ]] \
    || { echo "$prefix: final package runtime must not alias checkout .venv" >&2; return 2; }
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
