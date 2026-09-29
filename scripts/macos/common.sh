#!/bin/bash
# 공통: 프로젝트 루트·가상환경 경로. 다른 스크립트가 source 한다.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
export PYTHONPATH="$ROOT/src"
export AIFUND_HOME="$ROOT"
export PYTHONUNBUFFERED=1
cd "$ROOT"

need_venv() {
  if [ ! -x "$PY" ]; then
    echo "가상환경이 없습니다. 먼저 scripts/macos/setup.sh 를 실행하세요." >&2
    exit 1
  fi
}
