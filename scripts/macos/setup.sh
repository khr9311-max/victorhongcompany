#!/bin/bash
# 설치: Python 3.12 가상환경 + 해시 고정 의존성 + 초기 설정(.env·관리자 토큰·DB·자체검증).
# 시스템 설정(잠자기·보안)은 바꾸지 않는다.
source "$(dirname "$0")/common.sh"

if [ "$(uname -s)" != "Darwin" ]; then echo "macOS 전용 스크립트입니다." >&2; exit 1; fi
if [ "$(uname -m)" != "arm64" ]; then echo "경고: Apple Silicon(arm64)이 아닙니다. Rosetta 환경이면 네이티브 터미널에서 실행하세요." >&2; fi

PY312=""
for c in python3.12 /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12; do
  if command -v "$c" >/dev/null 2>&1; then PY312="$(command -v "$c")"; break; fi
done
if [ -z "$PY312" ]; then
  cat >&2 <<'EOF'
Python 3.12가 필요합니다. 다음 중 하나로 설치한 뒤 다시 실행하세요(이 스크립트는 시스템 소프트웨어를 자동 설치하지 않습니다):
  brew install python@3.12
  또는 https://www.python.org/downloads/macos/ 의 3.12 설치 프로그램
EOF
  exit 1
fi
echo "Python: $PY312 ($("$PY312" -c 'import platform;print(platform.machine())'))"

if [ ! -x "$PY" ]; then "$PY312" -m venv "$VENV"; fi
"$PY" -m pip install --upgrade pip >/dev/null
"$PY" -m pip install --require-hashes -r "$ROOT/requirements.lock"
if [ "${1:-}" = "--dev" ]; then "$PY" -m pip install --require-hashes -r "$ROOT/requirements-dev.lock"; fi

"$PY" -m aifund setup
echo
echo "다음: scripts/macos/doctor.sh → scripts/macos/run.sh (포그라운드) 또는 scripts/macos/install_launchd.sh (상시 실행)"
