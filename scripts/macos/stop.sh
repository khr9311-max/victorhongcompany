#!/bin/bash
# 정상 종료: 신규 주문 중지 → 진행 중 작업 대기 → 상태 기록.
# launchd로 실행 중이면 정상 종료(exit 0) 후 재시작되지 않는다(KeepAlive: 비정상 종료 시에만 재시작).
source "$(dirname "$0")/common.sh"
need_venv
exec "$PY" -m aifund stop "$@"
