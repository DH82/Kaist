#!/usr/bin/env bash
# Backward-compatible entry point; starts the complete stack.
set -euo pipefail
exec "$(dirname "$0")/start.sh" "$@"
