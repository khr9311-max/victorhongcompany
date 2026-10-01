#!/bin/bash
# 서비스 실행(포그라운드). launchd도 이 스크립트를 실행한다.
source "$(dirname "$0")/common.sh"
need_venv
# launchd 표준 출력 로그가 커지면 시작 시 한 번 순환(앱 로그는 앱이 자체 순환)
for f in "$ROOT/var/launchd.out.log" "$ROOT/var/launchd.err.log"; do
  if [ -f "$f" ] && [ "$(stat -f%z "$f")" -gt 20000000 ]; then mv -f "$f" "$f.1"; fi
done
exec "$PY" -m aifund run "$@"
