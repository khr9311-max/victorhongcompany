#!/bin/bash
# 임의 CLI 명령 실행: scripts/macos/aifund.sh status | stop | backup | doctor | live status ...
source "$(dirname "$0")/common.sh"
need_venv
exec "$PY" -m aifund "$@"
