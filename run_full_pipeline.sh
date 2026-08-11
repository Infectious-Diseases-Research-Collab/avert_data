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

if [ -f venv/bin/activate ]; then
  source venv/bin/activate
elif [ -f venv/Scripts/activate ]; then
  source venv/Scripts/activate
else
  echo "Could not find venv/bin/activate or venv/Scripts/activate." >&2
  exit 1
fi

echo "=== 1/3: downloading new data ==="
python download_data.py

echo "=== 2/3: merging into CSVs ==="
python process_data.py

echo "=== 3/3: uploading to Supabase ==="
# Exit 3 means everything uploaded but the run raised warnings, which are
# already on screen here. Only a real failure should stop us short of "Done."
set +e
python upload_to_supabase.py
upload_status=$?
set -e
if [ "$upload_status" -ne 0 ] && [ "$upload_status" -ne 3 ]; then
  exit "$upload_status"
fi

echo "Done."
