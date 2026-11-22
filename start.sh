#!/bin/bash
cd ~/workspace/edito-vbot || exit 1
set -a; . ./.env; set +a
./run.sh > bot.log 2>&1 &
echo $! > bot.pid
