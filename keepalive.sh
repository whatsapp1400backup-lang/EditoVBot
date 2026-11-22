#!/bin/bash
cd ~/workspace/edito-vbot || exit 1
if pgrep -f "\.venv/bin/python bot\.py" > /dev/null; then
  exit 0
fi
set -a; . ./.env; set +a
nohup ./run.sh > bot.log 2>&1 &
