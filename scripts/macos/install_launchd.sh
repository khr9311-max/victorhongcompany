#!/bin/bash
# launchd(LaunchAgent) 설치. 사용법:
#   scripts/macos/install_launchd.sh [--mode internal_paper|offline_demo|broker_sandbox|live] [--caffeinate]
# --caffeinate: 서비스 프로세스가 실행되는 동안에만 유휴 잠자기를 막는다(/usr/bin/caffeinate -i, 전원 연결 시 -s 추가).
#               시스템 설정(pmset)을 바꾸지 않으며 프로세스가 끝나면 효과도 사라진다. 뚜껑을 닫으면 잠자기는 막지 못할 수 있다.
source "$(dirname "$0")/common.sh"
need_venv
if [ "$(uname -s)" != "Darwin" ]; then echo "macOS 전용입니다." >&2; exit 1; fi

MODE="internal_paper"
CAFF=0
while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE="$2"; shift 2;;
    --caffeinate) CAFF=1; shift;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 1;;
  esac
done
LABEL="com.victorhong.aifund"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/var"

if [ "$CAFF" = "1" ]; then
  PROGRAM="    <string>/usr/bin/caffeinate</string>
    <string>-is</string>
    <string>/bin/bash</string>
    <string>$ROOT/scripts/macos/run.sh</string>"
else
  PROGRAM="    <string>/bin/bash</string>
    <string>$ROOT/scripts/macos/run.sh</string>"
fi

"$PY" - "$ROOT/deploy/launchd/$LABEL.plist.template" "$DEST" "$ROOT" "$MODE" "$PROGRAM" <<'EOF'
import sys
src, dest, root, mode, program = sys.argv[1:6]
t = open(src, encoding="utf-8").read()
t = t.replace("__PROGRAM__", program).replace("__ROOT__", root).replace("__MODE__", mode)
open(dest, "w", encoding="utf-8").write(t)
EOF
chmod 644 "$DEST"
plutil -lint "$DEST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
launchctl enable "gui/$(id -u)/$LABEL"
sleep 3
launchctl print "gui/$(id -u)/$LABEL" | grep -E "state|pid" || true
echo "설치 완료: $DEST (모드 $MODE). 로그: $ROOT/var/$MODE/logs/aifund.log, $ROOT/var/launchd.*.log"
echo "재시작: launchctl kickstart -k gui/$(id -u)/$LABEL   제거: scripts/macos/uninstall_launchd.sh"
