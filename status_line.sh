#!/usr/bin/env bash
# wrapper: run status_line.py with the first Python interpreter that actually
# works. On Windows `python3` may resolve to the Microsoft Store stub
# (prints "Python was not found" and exits 49), so candidates are probed
# with `-c 'import sys'` before use.
# The script MUST NOT propagate a non-zero exit to the parent Claude Code
# session (which would surface as a visible error to the user), hence the
# unconditional `exit 0`.
# Under Cygwin, native Windows Python can't read /cygdrive/... paths, so the
# script path is converted with cygpath -w when available (Cygwin Python
# accepts Windows paths too, and on Linux/macOS cygpath doesn't exist).
SCRIPT="$(cd "$(dirname "$0")" && pwd)/status_line.py"
command -v cygpath >/dev/null 2>&1 && SCRIPT="$(cygpath -w "$SCRIPT")"
for PY in python3 python py; do
  if "$PY" -c 'import sys' >/dev/null 2>&1; then
    "$PY" "$SCRIPT"
    exit 0
  fi
done
exit 0
