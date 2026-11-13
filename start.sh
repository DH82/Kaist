#!/usr/bin/env bash
# Configure, build, validate and start the complete stack with one command.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 repro/start_stack.py "$@"
