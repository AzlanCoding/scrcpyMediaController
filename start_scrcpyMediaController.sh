#!/usr/bin/env bash
BASEDIR=$(dirname "$(readlink -f "$0")")
exec uv run --project "$BASEDIR" python "$BASEDIR/main.py" "$@"
