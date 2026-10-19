#!/usr/bin/env bash
set -euo pipefail

# Stop the gateway first. Use its HERMES_HOME and Python environment.
cd "$(dirname "$0")/.."
python -c 'import runpy; from hermes_constants import get_hermes_home; runpy.run_path("migration.py")["migrate"](get_hermes_home())'
hermes plugins remove telegram-business-voice-transcriber
hermes plugins install neoromantic/telesec --enable
