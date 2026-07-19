#!/usr/bin/env python3
"""Extract enrollee / vaccination_status data from downloaded zip snapshots.

Each zip file contains a full SQLite database snapshot uploaded by a field
device. The same record (identified by `uniqueid`) therefore appears in many
zips over time. For each of data/burkina and data/uganda this script builds:

  enrollee.csv            - most recent version of every enrollee record
  vaccination_status.csv  - most recent version of every vaccination_status record
  audittrail.csv          - one row per field that changed between versions
                            of the same uniqueid

"Most recent" is decided by the record's `lastmod` timestamp (falling back to
the zip upload time from the filename).

Processing is incremental: a state file (.merge_state.json) remembers which
zips have already been merged, so each run only reads new zip files. Delete
the state file (or the csv files) to force a full rebuild.
"""

import csv
import json
import os
import re
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
COUNTRIES = ("burkina", "uganda")
TABLES = ("enrollee", "vaccination_status")
STATE_FILE = ".merge_state.json"

AUDIT_IGNORED_FIELDS = {"lastmod", "stoptime"}

AUDIT_COLUMNS = [
    "table", "uniqueid", "subjid", "barcode", "fieldname",
    "old_value", "new_value",
    "old_startdate", "new_startdate",
    "old_lastmod", "new_lastmod",
    "old_sourcefile", "new_sourcefile",
    "audit_recorded_at",
]

ZIP_TIME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{2})_(\d{2})\.zip$")


def zip_sort_key(path):
    """Chronological sort key: upload timestamp from the filename, else mtime."""
    m = ZIP_TIME_RE.search(path.name)
    if m:
        return f"{m.group(1)}T{m.group(2)}:{m.group(3)}"
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat()


def normalize(value):
    """Make sqlite values comparable with values read back from csv."""
    if value is None:
        return ""
    return str(value)


def read_tables_from_zip(zip_path):
    """Return {table: [record dicts]} for the sqlite database inside a zip."""
    tables = {}
    with zipfile.ZipFile(zip_path) as zf:
        sqlite_members = [n for n in zf.namelist() if n.endswith(".sqlite")]
        if not sqlite_members:
            print(f"  WARNING: no .sqlite file inside {zip_path.name}, skipping")
            return {t: [] for t in TABLES}
        with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
            tmp.write(zf.read(sqlite_members[0]))
            tmp_path = tmp.name
        try:
            con = sqlite3.connect(tmp_path)
            try:
                cur = con.cursor()
                for table in TABLES:
                    try:
                        cur.execute(f"SELECT * FROM {table}")
                    except sqlite3.OperationalError:
                        print(f"  WARNING: table {table} missing in {zip_path.name}")
                        tables[table] = []
                        continue
                    columns = [d[0] for d in cur.description]
                    tables[table] = [
                        {c: normalize(v) for c, v in zip(columns, row)}
                        for row in cur.fetchall()
                    ]
            finally:
                con.close()
        finally:
            os.unlink(tmp_path)
    return tables


def load_csv(path):
    """Load an existing merged csv as (columns, {uniqueid: record})."""
    if not path.exists():
        return [], {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        columns = list(reader.fieldnames or [])
        records = {row["uniqueid"]: dict(row) for row in reader}
    return columns, records


def write_csv(path, columns, records):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, restval="")
        writer.writeheader()
        for rec in records.values():
            writer.writerow(rec)


def is_older(row, existing):
    """True if `row` is an older version than `existing` (by lastmod).

    Zips are merged in chronological upload order, so on a lastmod tie the
    incoming row is from the same-or-later upload and wins.
    """
    return (row.get("lastmod") or "") < (existing.get("lastmod") or "")


def merge_zip(table, records, columns, sources, zip_name, new_rows,
              audit_writer):
    """Merge one zip's rows for one table into the accumulated records."""
    changes = 0
    for row in new_rows:
        uid = row.get("uniqueid", "")
        if not uid:
            continue
        for col in row:
            if col not in columns:
                columns.append(col)
        existing = records.get(uid)
        if existing is None:
            records[uid] = row
            sources[uid] = zip_name
            continue
        if is_older(row, existing):
            continue  # an older version of a record we already have
        # Only compare fields present in both versions. A field missing from
        # `existing` means it didn't exist in that snapshot's schema yet -
        # that's a schema addition, not a data edit, so it's not audited.
        diffs = [
            field for field in row
            if field in existing and row[field] != existing[field]
        ] + [
            field for field in existing
            if field not in row and existing[field] != ""
        ]
        # lastmod/stoptime are bookkeeping timestamps that churn on every
        # edit; the audit rows already carry old/new lastmod columns.
        for field in (f for f in diffs if f not in AUDIT_IGNORED_FIELDS):
            audit_writer.writerow({
                "table": table,
                "uniqueid": uid,
                "subjid": row.get("subjid", existing.get("subjid", "")),
                "barcode": row.get("barcode", existing.get("barcode", "")),
                "fieldname": field,
                "old_value": existing.get(field, ""),
                "new_value": row.get(field, ""),
                "old_startdate": existing.get("startdate", ""),
                "new_startdate": row.get("startdate", ""),
                "old_lastmod": existing.get("lastmod", ""),
                "new_lastmod": row.get("lastmod", ""),
                "old_sourcefile": sources.get(uid, ""),
                "new_sourcefile": zip_name,
                "audit_recorded_at": datetime.now().isoformat(timespec="seconds"),
            })
            changes += 1
        # Always adopt the newer (or equal) version so any new columns it
        # introduces are captured, even when nothing else changed.
        records[uid] = row
        sources[uid] = zip_name
    return changes


def process_country(country):
    folder = DATA_DIR / country
    if not folder.is_dir():
        print(f"[{country}] folder {folder} does not exist, skipping")
        return

    state_path = folder / STATE_FILE
    csv_paths = {t: folder / f"{t}.csv" for t in TABLES}
    audit_path = folder / "audittrail.csv"

    # Incremental state: which zips are already merged, and which zip each
    # record version came from. Missing state or csvs -> full rebuild.
    full_rebuild = not state_path.exists() or not all(
        p.exists() for p in csv_paths.values()
    )
    if full_rebuild:
        state = {"processed_files": [], "sources": {t: {} for t in TABLES}}
        tables = {t: ([], {}) for t in TABLES}  # (columns, records)
        audit_path.unlink(missing_ok=True)
    else:
        with open(state_path) as f:
            state = json.load(f)
        tables = {t: load_csv(csv_paths[t]) for t in TABLES}

    processed = set(state["processed_files"])
    all_zips = sorted(folder.glob("*.zip"), key=zip_sort_key)
    new_zips = [z for z in all_zips if z.name not in processed]
    print(f"[{country}] {len(all_zips)} zip files, {len(new_zips)} new"
          + (" (full rebuild)" if full_rebuild else ""))
    if not new_zips:
        return

    audit_exists = audit_path.exists()
    with open(audit_path, "a", newline="", encoding="utf-8") as audit_file:
        audit_writer = csv.DictWriter(audit_file, fieldnames=AUDIT_COLUMNS)
        if not audit_exists:
            audit_writer.writeheader()

        for zip_path in new_zips:
            try:
                zip_tables = read_tables_from_zip(zip_path)
            except (zipfile.BadZipFile, sqlite3.DatabaseError) as e:
                print(f"  ERROR reading {zip_path.name}: {e} - skipping")
                continue
            for table in TABLES:
                columns, records = tables[table]
                changes = merge_zip(
                    table, records, columns, state["sources"][table],
                    zip_path.name, zip_tables[table], audit_writer,
                )
                if changes:
                    print(f"  {zip_path.name}: {changes} field change(s) "
                          f"in {table} written to audit trail")
            processed.add(zip_path.name)

    for table in TABLES:
        columns, records = tables[table]
        write_csv(csv_paths[table], columns, records)
        print(f"[{country}] {csv_paths[table].name}: {len(records)} record(s)")

    state["processed_files"] = sorted(processed)
    with open(state_path, "w") as f:
        json.dump(state, f, indent=1)


def main():
    for country in COUNTRIES:
        process_country(country)
    return 0


if __name__ == "__main__":
    sys.exit(main())
