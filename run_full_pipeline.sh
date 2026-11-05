#!/usr/bin/env bash
# Download new data, merge it into per-country CSVs, and upload to Supabase —
# in that order, stopping immediately if any step fails so a broken step
# never lets a later one run against stale/partial data.
#
# One-time setup: copy supabase.env.example to supabase.env and fill in your
# real Supabase URL and service-role key (from Supabase -> Project Settings
# -> API). supabase.env is gitignored, so it never gets committed.
#   chmod +x run_full_pipeline.sh
#   chmod 600 supabase.env   # holds a live secret key
#   ./run_full_pipeline.sh

set -euo pipefail
cd "$(dirname "$0")"

if [ -f supabase.env ]; then
  source supabase.env
elif [ -z "${SUPABASE_URL:-}" ] || [ -z "${SUPABASE_SERVICE_ROLE_KEY:-}" ]; then
  echo "Missing Supabase credentials." >&2
  echo "Copy supabase.env.example to supabase.env and fill in your real values," >&2
  echo "or export SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY yourself first." >&2
  exit 1
fi

# Use the environment's interpreter directly. Activation scripts embed an
# absolute path that can become stale when the project is moved.
if [ -x venv/bin/python ]; then
  pipeline_python="$PWD/venv/bin/python"
elif [ -x venv/Scripts/python.exe ]; then
  pipeline_python="$PWD/venv/Scripts/python.exe"
else
  echo "Could not find a working Python interpreter in venv." >&2
  echo "Recreate it with: python3 -m venv --clear venv" >&2
  echo "Then install dependencies: venv/bin/python -m pip install -r requirements.txt" >&2
  exit 1
fi

# Exit 3 means the step did what it could but raised warnings, which are
# already on screen here. Only a real failure should stop us short of "Done."
# See EXIT_WARNINGS in download_data.py / upload_to_supabase.py.
run_step() {
  echo "=== $1 ==="
  set +e
  "$pipeline_python" "$2"
  local code=$?
  set -e
  if [ "$code" -ne 0 ] && [ "$code" -ne 3 ]; then
    exit "$code"
  fi
}

run_step "1/3: downloading new data"  download_data.py
run_step "2/3: merging into CSVs"     process_data.py
run_step "3/3: uploading to Supabase" upload_to_supabase.py

echo "Done."
