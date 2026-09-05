#!/usr/bin/env bash
#
# Auto-restart wrapper for arbitrage_bot.py.
#
# - Restarts the bot automatically if it exits for any reason (crash,
#   unhandled exception outside run_once's own try/except, connection
#   drop that kills the process, etc).
# - Every restart is logged with a timestamp and the exit code, both to
#   the console and to a persistent log file, so you have a record to
#   scroll back through instead of a wall of scrolled-off stdout.
# - Restarts are rate-limited: if the bot dies almost immediately (e.g.
#   a bad .env, a syntax error) more than a few times in a row, this
#   backs off instead of burning CPU/API calls in a tight crash loop.
# - Ctrl+C stops the WRAPPER (not just one iteration) — it won't
#   immediately relaunch the bot out from under you.
#
# Usage:
#   chmod +x run_bot.sh
#   ./run_bot.sh
#
# Logs land in ./logs/arb_YYYYMMDD.log (one file per day) and are also
# still printed to the console via `tee`.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"

MAX_FAST_RESTARTS=5      # how many quick-in-a-row restarts before backing off
FAST_RESTART_WINDOW=60   # a restart counts as "fast" if the bot ran less than this many seconds
BACKOFF_SECONDS=120      # how long to pause after hitting the fast-restart limit
NORMAL_RESTART_DELAY=5   # normal pause between restarts

fast_restart_count=0

# Let Ctrl+C kill the whole wrapper, not just get swallowed and restart the bot.
trap 'echo; echo "[wrapper] Caught interrupt — stopping wrapper (not restarting)."; exit 0' INT TERM

echo "[wrapper] Starting. Logs: $LOG_DIR/arb_\$(date +%Y%m%d).log"

while true; do
    LOG_FILE="$LOG_DIR/arb_$(date +%Y%m%d).log"
    START_TS=$(date +%s)
    START_HUMAN=$(date '+%Y-%m-%d %H:%M:%S')

    echo "[wrapper] $START_HUMAN — launching arbitrage_bot.py" | tee -a "$LOG_FILE"

    # Run the bot, teeing combined stdout+stderr to the log file AND the console.
    python3 arbitrage_bot.py 2>&1 | tee -a "$LOG_FILE"
    EXIT_CODE=${PIPESTATUS[0]}

    END_TS=$(date +%s)
    END_HUMAN=$(date '+%Y-%m-%d %H:%M:%S')
    RAN_SECONDS=$((END_TS - START_TS))

    echo "[wrapper] $END_HUMAN — arbitrage_bot.py exited (code=$EXIT_CODE) after ${RAN_SECONDS}s" | tee -a "$LOG_FILE"

    if [ "$RAN_SECONDS" -lt "$FAST_RESTART_WINDOW" ]; then
        fast_restart_count=$((fast_restart_count + 1))
    else
        fast_restart_count=0
    fi

    if [ "$fast_restart_count" -ge "$MAX_FAST_RESTARTS" ]; then
        echo "[wrapper] $fast_restart_count fast restarts in a row (each under ${FAST_RESTART_WINDOW}s) — " \
             "something is likely wrong (bad .env, syntax error, unreachable LCD, etc). " \
             "Backing off for ${BACKOFF_SECONDS}s instead of crash-looping. Check the log above." \
             | tee -a "$LOG_FILE"
        sleep "$BACKOFF_SECONDS"
        fast_restart_count=0
    else
        echo "[wrapper] Restarting in ${NORMAL_RESTART_DELAY}s... (Ctrl+C now to stop the wrapper instead)" \
             | tee -a "$LOG_FILE"
        sleep "$NORMAL_RESTART_DELAY"
    fi
done