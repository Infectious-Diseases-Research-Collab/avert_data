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

source venv/bin/activate

echo "=== 1/3: downloading new data ==="
python download_data.py

echo "=== 2/3: merging into CSVs ==="
python process_data.py

echo "=== 3/3: uploading to Supabase ==="
python upload_to_supabase.py

echo "Done."
