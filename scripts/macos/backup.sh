#!/bin/bash
# DB 온라인 백업(서비스 실행 중에도 안전). 기본 14개 보관. 모드: --mode internal_paper|live ...
source "$(dirname "$0")/common.sh"
need_venv
exec "$PY" -m aifund backup "$@"
