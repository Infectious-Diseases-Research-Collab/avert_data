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
- **Unreadable zips** are remembered and listed in the end-of-run summary. A
  corrupt upload is never marked processed — a good copy may yet arrive under
  the same name — so without this it would be reopened and re-reported on every
  run; three sat unread for a fortnight that way. The record is keyed on the
  file's size and modification time, so a replacement is tried again and merged
  automatically. Each zip is a full snapshot, so if the device has uploaded a
  readable one since, nothing is lost and the file can be deleted.

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

### Optional: email on failure or warnings (Windows)

`run_full_pipeline.ps1` logs every run to `logs/pipeline_<timestamp>.log`
regardless. To also get an email (useful when it's on a schedule via Task
Scheduler and nobody's watching the console), copy `smtp.json.example` to
`smtp.json` and fill in real SMTP credentials. `smtp.json` is gitignored —
never commit it. If it's absent, everything is still logged, just not emailed.

Two kinds of email are sent:

- **FAILED** — a step errored and the run stopped. Nothing was uploaded. The
  email carries the full contents of that run's log, not just its path, so
  the failure can usually be diagnosed from the email alone.
- **WITH WARNINGS** — everything uploaded, but something needs a person: a
  subject ID still shared after corrections, or a correction that no longer
  matches the record it was written against. `upload_to_supabase.py` signals
  this with exit code 3, which the wrapper treats as success. This email
  lists the specific warning lines rather than the whole log.

## Correcting a duplicated subject ID

A device derives its subject-ID counter from `MAX()` over its own table. If the
database is lost — uninstalling the app does this — the counter restarts and
begins reissuing IDs already given out. The records stay distinct (each has its
own barcode and `uniqueid`); only the subject ID collides.

The device keeps sending the original value forever, so corrections are applied
**on the way to Supabase**, not to the merged CSVs. `enrollee.csv` stays a
faithful record of what was collected.

```bash
python make_corrections.py --dry-run          # what would be proposed
python make_corrections.py --by <you> --reason "<what happened>"
```

That appends to `corrections/subjid_corrections.csv`, which **is** committed —
it is the only record of the decision, and losing it would silently restore the
duplicates. Rows already in the file are never rewritten, so a later incident
cannot disturb an earlier one. A review copy carrying names and dates is
written beside the merged data and stays out of git.

Review the proposed rows, commit the file, and the next upload applies them.

Two things make it safe to keep applying on every run:

- **Keyed on `uniqueid`**, which the collision doesn't affect and which never
  changes — not on the subject ID being corrected.
- **`old_subjid` is a guard.** A correction only fires when the record still
  holds the value it was written against; otherwise it is reported and skipped.

If any subject ID is still shared once the file has been applied — a device
reissued an ID that nobody has reviewed yet — the records go up **as
collected**. They are distinct apart from that one field, and nothing the
dashboard computes is keyed on the subject ID, so uploading them costs nothing;
refusing to would hold back every country's data until someone was at their
desk. The run reports the shared IDs, finishes with exit code 3 instead of 0
(see below), and the `duplicate_subjid` data-quality check keeps them in front
of you in the dashboard until a correction is committed.

Each applied correction is written to the audit trail alongside the field
changes the devices themselves sent, so a record's full history is in one
place. The audit row's timestamp is the correction date rather than the run
time, so re-uploading doesn't accumulate duplicates.

Corrected IDs keep their country, device and facility codes and take the
increment from the 9000 block — `21050050001` becomes `21050059001` — so a
corrected ID is recognisable and cannot collide with a future one.

## Correcting a data query

Some queries can't be fixed on a tablet any more. Since the eligibility window
changed, older records won't save, and re-opening one can wipe it. The site
sends the verified correction instead, and it is applied centrally. The same
rules as subject-ID corrections apply: the tablets keep sending the original
value, and the correction is reapplied **on the way to Supabase** on every
upload, so a corrected value is never overwritten by a later sync.

**Field corrections** are hand-written CSV batches in `corrections/field/`, one
file per batch the site sends, named `corrections_YYYY_MM_DD.csv`. The date is
the audit timestamp. Columns:

```
uniqueid,country,subjid,barcode,field,old_value,new_value,reason,source_document,verified_by,verified_at,corrected_by
```

- **One row per field.** Use the variable name exactly as it is in the data,
  and coded values (`consent2`, `0` → `1`), not question text.
- **`subjid` and `barcode` are the values currently in the data.** That holds
  even when the barcode is the one being fixed; they are cross-checks.
- **A barcode fix is two rows,** `barcode` and `barcode2`. The tablet asks for
  it twice.
- **`old_value` is the guard.** A row fires only while the record still holds
  it. A record that already holds `new_value` was fixed at source, which is
  fine. Anything else is reported and skipped.
- **A committed batch is never edited.** A later fix to the same field goes in
  a later batch, and its `old_value` is the earlier batch's `new_value`.

```bash
python check_corrections.py      # until it says "All clean."
```

The checker lists every problem by file and line: an unknown uniqueid, a
subjid/barcode that doesn't match, an `old_value` that isn't what the record
holds, a missing `barcode2` row, or a new barcode that isn't deployed or is
already in use. For a blank uniqueid it prints the record the subjid and
barcode point to, so you can paste it in. Commit the batch once it is clean,
and the next upload applies it.

**Wiped records.** Re-opening an older record on a tablet recalculated it as
age-ineligible and cleared the rest of the form. The cleared values are all in
the audit trail, so `corrections/record_restores.csv` names only the record and
the `lastmod` of the version that wiped it. Each upload rebuilds the record as
it was just before, field by field, before any field corrections apply.

```bash
python check_corrections.py --find-wipes   # candidates and their wiped_lastmod
```

A restore applies only while the record is still the wiped version. If the
tablet saves it again, the restore is reported and skipped.

Every applied correction and restored field is written to the audit trail
with its source file, and a dashboard query drops off the open list once the
corrected value is uploaded.

## Supabase schema changes happen in avert_dashboard, not here

This repo only ever *writes rows* into Supabase (`upload_to_supabase.py`, via
UPSERT). The schema itself — tables, RLS, `refresh_quality_issues()`,
`sync_duplicate_barcode_issues()` — is owned entirely by
[avert_dashboard](https://github.com/Infectious-Diseases-Research-Collab/avert_dashboard),
tracked with Supabase CLI migrations (`supabase/migrations/`, applied via
`supabase db push`) and documented in that repo's `Instructions.md`. If a
change here (like the two duplicate-barcode functions this script calls)
needs a matching database change, make it there — a one-off SQL file kept in
*this* repo is exactly how `sync_duplicate_barcode_issues` drifted out of
sync with avert_dashboard's own schema before.

## Data stays local

`data/` (raw zips and merged CSVs) is gitignored — this repo holds only the
pipeline code, never study data.

`corrections/` is the exception, and deliberately so: losing it would quietly
bring the errors back. It holds identifiers (`uniqueid`, `barcode`, subject
IDs) and the corrected values themselves, which may include dates of birth or
clinical values. It never holds participant or caregiver names: a restore
rebuilds those from the local audit trail rather than storing them.
