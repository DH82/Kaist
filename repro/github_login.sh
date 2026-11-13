#!/usr/bin/env bash
# Read PAT privately from the terminal; never store it in this repository or URL.
set -euo pipefail
command -v gh >/dev/null || { echo 'GitHub CLI(gh)를 먼저 설치하세요.' >&2; exit 1; }
[[ -t 0 ]] || { echo '이 스크립트는 로컬 터미널에서 실행하세요.' >&2; exit 1; }
# Disable tracing even if the caller enabled it.
set +x
trap 'unset KAIST_GITHUB_TOKEN' EXIT
IFS= read -r -s -p 'GitHub PAT (입력 내용 숨김): ' KAIST_GITHUB_TOKEN
printf '\n'
[[ -n "$KAIST_GITHUB_TOKEN" ]] || { echo '토큰이 비어 있습니다.' >&2; exit 1; }
printf '%s' "$KAIST_GITHUB_TOKEN" | gh auth login --hostname github.com --git-protocol https --with-token
unset KAIST_GITHUB_TOKEN
gh auth setup-git --hostname github.com
gh repo view DH82/Kaist --json nameWithOwner,url
echo 'GitHub 인증 완료. 저장소 코드에는 토큰을 기록하지 않았습니다.'
