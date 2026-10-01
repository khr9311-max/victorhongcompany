#!/bin/bash
source "$(dirname "$0")/common.sh"
need_venv
"$PY" -m aifund status "$@"
if [ "$(uname -s)" = "Darwin" ]; then
  echo "--- launchd ---"
  launchctl print "gui/$(id -u)/com.victorhong.aifund" 2>/dev/null | grep -E "state|pid|last exit" || echo "launchd 미등록"
fi
