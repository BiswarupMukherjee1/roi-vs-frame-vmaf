#!/usr/bin/env bash

#   ./run.sh                 all stages (prepare roi pilot encode svtroi metrics timing analyze figures)
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
mkdir -p logs store/cache
export MPLCONFIGDIR="$PWD/store/cache/matplotlib" XDG_CACHE_HOME="$PWD/store/cache"
ts=$(date +%Y%m%d_%H%M%S)
log="logs/run_${ts}.log"
setsid nohup python3 -m hia.pipeline "$@" > "$log" 2>&1 < /dev/null &
echo $! > logs/run.pid
ln -sf "run_${ts}.log" logs/run_latest.log
echo "started PID $(cat logs/run.pid) -> $log"
