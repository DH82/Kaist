#!/usr/bin/env bash
# GitHub 업데이트 타이머(systemd --user kaist-update.timer)를 설치합니다. sudo 없이 현재 사용자로 동작합니다.
#   repro/install_updater.sh            # 3분마다 확인 (관리자 대시보드의 '업데이트' 요청이 있을 때만 반영)
#   repro/install_updater.sh --auto     # GitHub에 새 커밋이 올라오면 요청 없이 자동 반영
#   repro/install_updater.sh --remove   # 타이머 제거
# 로그: journalctl --user -u kaist-update -e   ·   상태: python3 repro/update_stack.py --status
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
AUTO=0
case "${1:-}" in
  --auto) AUTO=1 ;;
  --remove)
    systemctl --user disable --now kaist-update.timer 2>/dev/null || true
    rm -f "$UNIT_DIR/kaist-update.service" "$UNIT_DIR/kaist-update.timer"
    systemctl --user daemon-reload
    echo '업데이트 타이머를 제거했습니다.'; exit 0 ;;
  '') ;;
  *) echo "사용법: $0 [--auto|--remove]" >&2; exit 2 ;;
esac
command -v git >/dev/null || { echo 'git이 필요합니다.' >&2; exit 1; }
git -C "$ROOT" remote get-url origin >/dev/null || { echo 'origin 원격 저장소가 없습니다.' >&2; exit 1; }
[[ -f "$ROOT/.env" ]] || { echo '.env가 없습니다. 먼저 ./start.sh로 설치하세요.' >&2; exit 1; }
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/kaist-update.service" <<EOF
[Unit]
Description=KAIST stack update from GitHub ($ROOT)
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$ROOT
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=KAIST_AUTO_UPDATE=$AUTO
ExecStart=/usr/bin/python3 $ROOT/repro/update_stack.py
# 이미지 빌드는 오래 걸릴 수 있습니다.
TimeoutStartSec=6h
Nice=5
EOF
cat > "$UNIT_DIR/kaist-update.timer" <<'EOF'
[Unit]
Description=Check GitHub for KAIST stack updates every 3 minutes

[Timer]
OnBootSec=2min
OnUnitInactiveSec=3min
Persistent=false

[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now kaist-update.timer
# 로그아웃해도 타이머가 돌도록 linger가 필요합니다(관리자 권한이 필요할 수 있음).
if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != yes ]]; then
  echo "주의: 로그아웃 후에도 동작하려면 'sudo loginctl enable-linger $USER'를 한 번 실행하세요."
fi
echo "업데이트 타이머 설치 완료 (자동 반영: $([[ $AUTO == 1 ]] && echo 켜짐 || echo '꺼짐 · 대시보드 요청 시에만'))."
echo "  상태: python3 $ROOT/repro/update_stack.py --status   로그: journalctl --user -u kaist-update -e"
