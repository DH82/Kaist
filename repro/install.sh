#!/usr/bin/env bash
# Fresh-PC one-command installation (legacy positional interface).
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
[[ $# -ge 3 && $# -le 5 ]] || { echo 'Usage: repro/install.sh PC_ID NAS_GID LOCAL_PROJECT [WEIGHTS_ROOT] [NAS_MOUNT]' >&2; exit 2; }
args=(--pc-id "$1" --nas-gid "$2" --project "$3")
[[ $# -lt 4 ]] || args+=(--weights "$4")
[[ $# -lt 5 ]] || args+=(--nas "$5")
exec "$ROOT/start.sh" "${args[@]}"
