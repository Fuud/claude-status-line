#!/usr/bin/env bash
# wrapper: exec status_line.py alongside the script
# using `cd ... && pwd` to resolve to an absolute path that python3 understands
exec python3 "$(cd "$(dirname "$0")" && pwd)/status_line.py"
