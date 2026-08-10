# AVERT Data Pipeline

Pulls AVERT study data from the field servers and loads it into the
[avert_dashboard](https://github.com/Infectious-Diseases-Research-Collab/avert_dashboard)
Supabase database. Runs locally — no data or credentials ever leave your
machine except to Supabase itself.

## Pipeline

```
download_data.py  →  raw zip snapshots (data/burkina/*.zip, data/uganda/*.zip)
process_data.py   →  merged per-country CSVs (enrollee.csv, vaccination_status.csv, audittrail.csv)
upload_to_supabase.py → UPSERTs those CSVs into Supabase
```

### Checks `process_data.py` reports

- **Test records are held back.** A survey package built for testing carries
  `test` in its surveyId, and stamps it on every record it collects. Those
  records go to `quarantine_test_records.csv` instead of `enrollee.csv`, so
  they never reach the dashboard. Nothing is deleted.
- **Repeated rows in one snapshot.** A device database should hold each
  interview once; more than one row with the same `uniqueid` means the app
  saved it twice. The merge already keeps only the most recent, and this
  reports the difference — it is why a device or the server can show more rows
  than the dashboard.
- **Subject-ID counter regressions.** The counter comes from `MAX()` over the
  device's own table, so it only climbs — unless the database is lost, which
  uninstalling the app does. A snapshot whose highest increment has *fallen* is
  that happening, and it means subject IDs are being issued a second time. The
  high-water mark per device is kept in `.merge_state.json`, so each regression
  is reported once rather than on every run.
- **Interviews by facility**, printed after each merge, so a count taken from a
  device or the server can be compared with what the dashboard will show.

Each step is safe to re-run: `download_data.py` skips files it already has,
`process_data.py` only processes zips it hasn't merged yet, and
`upload_to_supabase.py` upserts (no duplicates).

## Setup

macOS/Linux:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Windows (PowerShell):

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create two local files (both gitignored — never commit them):

- `credentials.json` — SFTP/FTP login for the Burkina Faso and Uganda
  servers. See `download_data.py` for the expected shape.
- `supabase.env` — copy `supabase.env.example` and fill in your real
  `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` (Supabase → Project
  Settings → API).

On macOS/Linux, lock down the permissions on these secret files:

```bash
chmod 600 credentials.json supabase.env
```

(Windows has no direct equivalent — NTFS file permissions already
restrict access to your user account by default.)

## Usage

macOS/Linux:

```bash
./run_full_pipeline.sh
```

Windows (PowerShell):

```powershell
.\run_full_pipeline.ps1
```

Runs all three steps in order, stopping immediately if any step fails.

### Optional: email on failure (Windows)

`run_full_pipeline.ps1` logs every run to `logs/pipeline_<timestamp>.log`
regardless. To also get an email when a run fails (useful when it's on a
schedule via Task Scheduler and nobody's watching the console), copy
`smtp.json.example` to `smtp.json` and fill in real SMTP credentials.
`smtp.json` is gitignored — never commit it. If it's absent, failures are
still logged, just not emailed.

## Data stays local

`data/` (raw zips and merged CSVs) is gitignored — this repo holds only the
pipeline code, never study data.
