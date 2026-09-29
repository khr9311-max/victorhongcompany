#!/bin/bash
# launchd 제거. 서비스는 SIGTERM을 받아 정상 종료(신규 주문 중지·상태 기록)한다. 데이터(var/)는 지우지 않는다.
set -euo pipefail
LABEL="com.victorhong.aifund"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "서비스 중지(bootout)" || echo "등록되어 있지 않음"
if [ -f "$DEST" ]; then rm -f "$DEST"; echo "삭제: $DEST"; fi
