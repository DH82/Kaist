#!/usr/bin/env bash
# 이미지 안 실행 workspace(REV)를 원래 PC와 같은 모양으로 연결합니다.
#   REV/workspace/{sam,phystwin,hamba}-env -> DEP/<env>
#   REV/workspace/<weight 경로>            -> DEP/<weight 경로>  (실행 시 호스트 weight가 DEP 쪽에 마운트됨)
set -euo pipefail
DEP=${1:?DEP_ROOT}; WS=${2:?REV workspace}
LIST=$(dirname "$0")/weights.txt
[[ -f $LIST ]] || LIST=/opt/kaist/weights.txt
for env in sam phystwin hamba; do ln -sfn "$DEP/$env-env" "$WS/$env-env"; done
grep -vE '^\s*(#|$)' "$LIST" | while read -r rel mode; do
  [[ $mode == link ]] || continue
  mkdir -p "$(dirname "$WS/$rel")"
  rm -rf "${WS:?}/$rel"
  ln -s "$DEP/$rel" "$WS/$rel"
done
