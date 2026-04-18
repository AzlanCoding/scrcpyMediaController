#!/usr/bin/env bash
# Starts scrcpy (audio/no-window) and the MPRIS media controller together.
# Output from each is prefixed with [scrcpy] / [mediactl].
# Killing this script (Ctrl-C, SIGTERM) tears down both child processes.

BASEDIR=$(dirname "$(readlink -f "$0")")

# Print each line of stdin as "[tag] line"
prefix() {
    local tag="$1"
    while IFS= read -r line; do
        printf '[%s] %s\n' "$tag" "$line"
    done
}

cleanup() {
    printf '[scrcpy-ctl] shutting down...\n'
    kill "${SCRCPY_PID-}"    2>/dev/null || true
    kill "${MEDIACTL_PID-}"  2>/dev/null || true
    # wait for prefix subshells to drain remaining output
    wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Launch scrcpy; combined stdout+stderr is prefixed with [scrcpy]
scrcpy --no-window --no-video > >(prefix "scrcpy") 2>&1 &
SCRCPY_PID=$!

# Launch the media controller; combined stdout+stderr is prefixed with [mediactl]
uv run --project "$BASEDIR" python "$BASEDIR/main.py" "$@" > >(prefix "mediactl") 2>&1 &
MEDIACTL_PID=$!

# Block until either process exits; the EXIT trap then kills the other
wait -n "$SCRCPY_PID" "$MEDIACTL_PID"
