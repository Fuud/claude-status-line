#!/usr/bin/env bash
# wrapper: exec status_line.py alongside the script
# using `cd ... && pwd` to resolve to an absolute path that python3 understands.
# `|| exit 0` is a hard safety net: if python3 is missing or the script
# fails, the status-line hook MUST NOT propagate a non-zero exit to the
# parent Claude Code session (which would surface as a visible error to
# the user).
exec python3 "$(cd "$(dirname "$0")" && pwd)/status_line.py" || exit 0
