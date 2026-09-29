#!/bin/bash
source "$(dirname "$0")/common.sh"
need_venv
exec "$PY" -m aifund doctor "$@"
