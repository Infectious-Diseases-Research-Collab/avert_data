#!/usr/bin/env python3
"""Upsert AVERT enrollee / vaccination-status / audit-trail / blood-smear CSVs into Supabase.

Run this locally AFTER the download + process pipeline has produced the
per-country CSVs in data/burkina and data/uganda. It is idempotent: every
row is UPSERTed on its natural key, so re-running the same files changes
nothing. Empty CSVs (e.g. vaccination_status / audittrail before any data
exists) are a no-op, not an error.

Each table stores a small set of typed columns used by the dashboard plus a
`raw` JSONB column holding the full source row, so new survey fields don't
require a schema migration.

Environment (required):
  SUPABASE_URL                 https://<project-ref>.supabase.co
  SUPABASE_SERVICE_ROLE_KEY    service-role key (server secret; never ship to app)

These can be set via `export` in your shell, or by keeping a supabase.env
file (see supabase.env.example) next to this script -- it is read
automatically as a fallback when the environment variables aren't already set.

Usage:
  python upload_to_supabase.py                # load both countries
  python upload_to_supabase.py --country uganda
  python upload_to_supabase.py --no-quality   # skip the data-quality refresh

Dependencies:  pip install supabase
"""

import argparse
import csv
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


def _get_supabase_factory():
    try:
        from supabase import create_client
    except ImportError:
        sys.exit("Missing dependency: pip install supabase")
    return create_client


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
COUNTRY_FOLDERS = {"burkina": "BF", "uganda": "UG"}
BATCH = 500
ENV_FILE = BASE_DIR / "supabase.env"


def load_env_file(path):
    """Parse `export KEY="value"` lines from a shell-style env file and set
    any of them not already present in os.environ. Lets supabase.env work
    without the user having to `source` it first (e.g. on Windows)."""
    if not path.exists():
        return
    pattern = re.compile(r'^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$')
    for line in path.read_text(encoding="utf-8").splitlines():
        m = pattern.match(line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)

# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def _clean(v):
    if v is None:
        return None
    v = v.strip()
    return v if v != "" else None


def to_int(v):
    v = _clean(v)
    if v is None:
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def pad_mrc(v):
    """MRC codes are always 3 chars, zero-padded (e.g. "5" -> "005")."""
    v = _clean(v)
    if v is not None and v.isdigit() and len(v) < 3:
        return v.zfill(3)
    return v          # None, already 3+, or non-numeric -> leave untouched


def int_or_zero(v):
    """NA -> 0 (matches the Shiny recodes for vx_card / vx_doses_received)."""
    r = to_int(v)
    return 0 if r is None else r


def to_num(v):
    v = _clean(v)
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def to_date(v):
    v = _clean(v)
    if v is None:
        return None
    # Accept YYYY-MM-DD or full ISO datetime; keep only the date part.
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(v[: len(fmt) + 6], fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(v).date().isoformat()
    except ValueError:
        return None


def to_ts(v):
    v = _clean(v)
    if v is None:
        return None
    try:
        return datetime.fromisoformat(v).isoformat()
    except ValueError:
        return None


def week_start(iso_date):
    """Sunday-start week floor, matching lubridate floor_date(x, 'week')."""
    if iso_date is None:
        return None
    d = date.fromisoformat(iso_date)
    return (d - timedelta(days=(d.weekday() + 1) % 7)).isoformat()


def recode_sex(gender):
    g = to_int(gender)
    if g == 1:
        return 1   # male
    if g == 2:
        return 0   # female
    return None


def read_rows(path):
    """Yield dict rows from a CSV, or nothing if the file is empty/missing."""
    if not path.exists() or path.stat().st_size == 0:
        return
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            return
        for row in reader:
            # DictReader may return a trailing None key for ragged rows; drop it.
            row.pop(None, None)
            if any((v or "").strip() for v in row.values()):
                yield row


# --------------------------------------------------------------------------
# Row builders (typed columns + full raw jsonb)
# --------------------------------------------------------------------------

def build_enrollee(row, country):
    row = dict(row)
    row["mrc"] = pad_mrc(row.get("mrc"))   # fixes typed column AND raw copy
    startdate = to_date(row.get("startdate"))
    return {
        "uniqueid": _clean(row.get("uniqueid")),
        "country": country,
        "subjid": _clean(row.get("subjid")),
        "barcode": _clean(row.get("barcode")),
        "mrc": row["mrc"],
        "district": _clean(row.get("district")),
        "subcounty": _clean(row.get("subcounty")),
        "parish": _clean(row.get("parish")),
        "village": _clean(row.get("village")),
        "startdate": startdate,
        "enrollment_week": week_start(startdate),
        "dob": to_date(row.get("dob")),
        "agemonths_calculated": to_int(row.get("agemonths_calculated")),
        "age_eligible": to_int(row.get("age_eligible")),
        "mal_test_eligible": to_int(row.get("mal_test_eligible")),
        "consent_eligible": to_int(row.get("consent_eligible")),
        "gender": to_int(row.get("gender")),
        "sex": recode_sex(row.get("gender")),
        "diagnostic": to_int(row.get("diagnostic")),
        "result": to_int(row.get("result")),
        "vx_card": int_or_zero(row.get("vx_card")),
        "need_vac_cov": to_int(row.get("need_vac_cov")),
        "vx_any": to_int(row.get("vx_any")),
        "vx_doses_received": int_or_zero(row.get("vx_doses_received")),
        "vx_dose1_date": to_date(row.get("vx_dose1_date")),
        "vx_dose2_date": to_date(row.get("vx_dose2_date")),
        "vx_dose3_date": to_date(row.get("vx_dose3_date")),
        "vx_dose4_date": to_date(row.get("vx_dose4_date")),
        "lastmod": to_ts(row.get("lastmod")),
        "raw": {k: v for k, v in row.items() if k},
    }


def build_vaccination_status(row, country):
    return {
        "barcode": _clean(row.get("barcode")),
        "country": country,
        "startdate": to_date(row.get("startdate")),
        "vx_card": to_int(row.get("vx_card")),
        "vx_doses_received": to_int(row.get("vx_doses_received")),
        "vx_dose1_date": to_date(row.get("vx_dose1_date")),
        "vx_dose2_date": to_date(row.get("vx_dose2_date")),
        "vx_dose3_date": to_date(row.get("vx_dose3_date")),
        "vx_dose4_date": to_date(row.get("vx_dose4_date")),
        "vx_doses_miss": to_int(row.get("vx_doses_miss")),
        "vx_dose_off_sched": to_int(row.get("vx_dose_off_sched")),
        "lastmod": to_ts(row.get("lastmod")),
        "raw": {k: v for k, v in row.items() if k},
    }


def build_blood_smear(row, country):
    density = to_num(row.get("parasitedensity"))
    return {
        "barcode": _clean(row.get("barcode")),
        "country": country,
        "parasitedensity": density,
        "slidequality": to_int(row.get("slidequality")),
        "gametocytes": to_int(row.get("gametocytes")),
        "readingcomments": _clean(row.get("readingcomments")),
        "mic_positive": None if density is None else (1 if density > 0 else 0),
        "raw": {k: v for k, v in row.items() if k},
    }


AUDIT_FIELDS = [
    "table", "uniqueid", "barcode", "fieldname",
    "old_value", "new_value", "startdate",
    "old_lastmod", "new_lastmod", "old_sourcefile", "new_sourcefile",
    "audit_recorded_at",
]


def build_audit(row, country):
    rec = {"country": country}
    for f in AUDIT_FIELDS:
        rec[f] = _clean(row.get(f))
    return rec


# --------------------------------------------------------------------------
# Subject-ID corrections
# --------------------------------------------------------------------------
#
# A device derives its subject-ID counter from MAX() over its own table, so
# losing the database -- uninstalling the app does this -- restarts it and
# reissues ids already given out. The devices keep sending the original value
# forever, so the correction is applied here, on the way to Supabase, and the
# merged CSVs stay a faithful record of what was actually collected.
#
# Applying it during the merge instead would fight itself: the next run would
# read the corrected CSV as the existing version, see the original coming back
# from the zip, write an audit row and adopt the original again -- undoing and
# redoing the fix on every run.

CORRECTIONS_FILE = BASE_DIR / "corrections" / "subjid_corrections.csv"


def load_subjid_corrections():
    """{uniqueid: correction} from the committed corrections file."""
    if not CORRECTIONS_FILE.exists():
        return {}
    with open(CORRECTIONS_FILE, newline="", encoding="utf-8-sig") as f:
        corrections = {c["uniqueid"]: c for c in csv.DictReader(f)
                       if c.get("uniqueid")}
    if corrections:
        print(f"  {len(corrections)} subject-id correction(s) loaded")
    return corrections


def apply_subjid_corrections(rows, corrections, country):
    """Rewrite corrected subject ids, returning the rows and an audit entry each.

    A correction only fires when the record still holds the value it was
    written against. That guard is what makes the file safe to keep applying:
    if the underlying data ever changes, the correction is reported rather than
    silently overwriting something it was not meant to touch.
    """
    applied, mismatched = [], []
    for row in rows:
        correction = corrections.get(row.get("uniqueid", ""))
        if not correction or correction.get("country") != country:
            continue

        current = (row.get("subjid") or "").strip()
        if current != correction["old_subjid"]:
            mismatched.append((correction, current))
            continue

        row["subjid"] = correction["new_subjid"]
        applied.append({
            "table": "enrollee",
            "uniqueid": correction["uniqueid"],
            "barcode": row.get("barcode", ""),
            "fieldname": "subjid",
            "old_value": correction["old_subjid"],
            "new_value": correction["new_subjid"],
            "startdate": row.get("startdate", ""),
            "old_lastmod": row.get("lastmod", ""),
            # Fixed, not "now": new_lastmod is part of the audittrail's key, so
            # a moving value would insert a fresh row on every upload.
            "new_lastmod": correction["corrected_at"],
            "old_sourcefile": "",
            "new_sourcefile": "corrections/subjid_corrections.csv",
            "audit_recorded_at": correction["corrected_at"],
        })

    for correction, current in mismatched:
        print(f"  ! correction for {correction['uniqueid']} expected subjid "
              f"{correction['old_subjid']} but found "
              f"{current or '(none)'} — NOT applied")

    expected = {c["uniqueid"] for c in corrections.values()
                if c.get("country") == country}
    unseen = expected - {a["uniqueid"] for a in applied} - {
        c["uniqueid"] for c, _ in mismatched}
    if unseen:
        print(f"  ! {len(unseen)} correction(s) refer to records not in this "
              f"data: {', '.join(sorted(unseen))}")

    # A correction that recreates a collision would put two children back under
    # one id, which is the thing being fixed. Stop before anything is uploaded.
    counts = {}
    for row in rows:
        subjid = (row.get("subjid") or "").strip()
        if subjid:
            counts[subjid] = counts.get(subjid, 0) + 1
    still_duplicated = sorted(s for s, n in counts.items() if n > 1)
    if applied and still_duplicated:
        sys.exit(f"After corrections, {len(still_duplicated)} subject id(s) are "
                 f"still shared: {', '.join(still_duplicated[:10])}. "
                 f"Re-run make_corrections.py and review before uploading.")

    if applied:
        print(f"  {len(applied)} subject-id correction(s) applied")
    return rows, applied


# --------------------------------------------------------------------------
# Upsert driver
# --------------------------------------------------------------------------

def upsert(client, table, rows, on_conflict):
    rows = [r for r in rows if r.get(on_conflict.split(",")[0])]
    if not rows:
        return 0
    total = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i : i + BATCH]
        client.table(table).upsert(chunk, on_conflict=on_conflict).execute()
        total += len(chunk)
    return total


def load_country(client, folder, country, corrections):
    d = DATA_DIR / folder
    if not d.exists():
        print(f"  ! {folder}: data folder not found, skipping")
        return

    rows, corrected = apply_subjid_corrections(
        list(read_rows(d / "enrollee.csv")), corrections, country)
    enrollees = [build_enrollee(r, country) for r in rows]
    n_e = upsert(client, "enrollee", enrollees, "uniqueid")

    covers = [build_vaccination_status(r, country) for r in read_rows(d / "vaccination_status.csv")]
    n_v = upsert(client, "vaccination_status", covers, "barcode")

    # Corrections are recorded in the audit trail alongside the field changes
    # the devices themselves sent, so the history of a record is in one place.
    audits = [build_audit(r, country) for r in read_rows(d / "audittrail.csv")]
    audits += [build_audit(a, country) for a in corrected]
    n_a = upsert(client, "audittrail", audits, "uniqueid,fieldname,new_lastmod")

    smears = [build_blood_smear(r, country) for r in read_rows(d / "blood_smear.csv")]
    n_b = upsert(client, "blood_smear", smears, "barcode")

    print(f"  {country}: enrollee={n_e}  vaccination_status={n_v}  audittrail={n_a}  blood_smear={n_b}")


def main():
    ap = argparse.ArgumentParser(description="Upsert AVERT CSVs into Supabase.")
    ap.add_argument("--country", choices=list(COUNTRY_FOLDERS), help="load one country only")
    ap.add_argument("--no-quality", action="store_true", help="skip data-quality refresh")
    args = ap.parse_args()

    load_env_file(ENV_FILE)

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY environment variables.")

    client = _get_supabase_factory()(url, key)

    folders = {args.country: COUNTRY_FOLDERS[args.country]} if args.country else COUNTRY_FOLDERS
    print("Uploading to Supabase...")
    corrections = load_subjid_corrections()
    for folder, country in folders.items():
        load_country(client, folder, country, corrections)

    if not args.no_quality:
        print("Refreshing data-quality issues...")
        res = client.rpc("refresh_quality_issues").execute()
        print(f"  currently firing issues: {res.data}")

    # Record this run so the dashboard can show a "last data pull" time.
    # (updated_at columns default only on INSERT and don't advance on re-upsert,
    # so they can't serve as a run timestamp.) A logging failure must not fail
    # an otherwise-successful load.
    try:
        n_enrollee = (
            client.table("enrollee").select("uniqueid", count="exact").limit(1).execute().count
        )
        client.table("pipeline_runs").insert(
            {"n_enrollee": n_enrollee, "note": "upload_to_supabase"}
        ).execute()
        print(f"  recorded pipeline run (enrollee rows: {n_enrollee}).")
    except Exception as exc:  # noqa: BLE001
        print(f"  warning: could not record pipeline_runs entry: {exc}")

    print("Done.")


if __name__ == "__main__":
    main()
