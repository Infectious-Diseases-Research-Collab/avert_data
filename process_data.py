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

# Uganda enrollment before this date is pre-go-live and never enters the
# merged CSVs. Applies to both enrollee and vaccination_status.
UGANDA_STARTDATE_CUTOFF = "2026-08-27"

# Three Burkina test records entered before go-live (device 999, dummy
# names/barcodes). Excluded by uniqueid so they're gone regardless of what
# startdate ends up on them.
EXCLUDED_UNIQUEIDS = {
    "2b2d496b-b431-475e-8d63-76541f451e3c",
    "e1fe81c8-242f-40c7-9d2d-3d29891f8c4d",
    "3fc7ec95-fd70-4397-b123-642e1c0b5d3d",
}

# Uganda training records entered on 2026-11-16 against the test survey. The
# phones were switched to the production survey, but it uses the same database
# file, so these rows ride along in every later upload from those phones.
# Excluded by uniqueid, not deviceid: devices 118 and 123 are also real field
# devices, and the real 118 phone recorded interviews on 2026-11-16 too.
# This is the complete set: 10 phones (devices 113, 118, 119, 120, 122-127),
# 20 enrollee and 8 vaccination_status rows, all with dummy barcodes.
UGANDA_TRAINING_UNIQUEIDS = {
    # enrollee
    "76522c59-69f1-4a5d-8d1c-8badab85f61a",  # 113 11130270001
    "b78ae197-5137-4613-b82d-c768cbbc8643",  # 113 11130270002
    "fce764b6-6cd1-4895-9837-875a8c850130",  # 118 11180270001
    "59e96d1c-3555-40fd-8dad-bd2b7d8f823b",  # 118 11180270002
    "c6bbcafc-a55e-4e07-b6d9-254e92cdbe46",  # 119 11190270001
    "bad55ba6-8187-4c70-959f-818c252c1d4c",  # 119 11190270002
    "e634ac6a-7e68-447c-9915-921f35997e0c",  # 120 11200270001
    "4b8d5e35-cd18-4da2-95f7-eca3194ed537",  # 120 11200270002
    "a3bcc068-3e6f-43f9-ba35-ab1a33bf770d",  # 122 11220270001
    "4b313972-aa9b-4394-b756-144be2460092",  # 122 11220270002
    "e6f4b076-a127-4fe9-af6a-ea4f7bdc8c14",  # 123 11230270001
    "4551a24a-652c-4bcb-b509-f169baa253ac",  # 123 11230270002
    "a7f54765-449a-470b-86a2-ac231615033d",  # 124 11240270001
    "14c6d728-0581-48db-94d9-a95aed133acd",  # 124 11240790001
    "109d2648-6388-45fa-a746-a41499d3fdce",  # 125 11250270001
    "7a70b9ba-5735-4592-b713-fdf2aded066a",  # 125 11250270002
    "21c76ccf-aab7-47eb-b75a-405130766d33",  # 126 11260270001
    "4add002c-005d-4932-819d-61fe2283c552",  # 126 11260270002
    "67994fa4-d154-42d2-b6bb-9dd07157e116",  # 127 11270270001
    "75934a79-9ae5-4ebc-8974-a8cc18c25aa4",  # 127 11270360001
    # vaccination_status
    "1917e0b6-a4f9-4b9a-8a37-85f3a0a3d35f",
    "1ec5445b-6362-40c6-9e12-61c917fdbe18",
    "24987fbf-0299-459c-b8a4-25cd436e57c6",
    "2c5a815c-24d3-47cc-a3a4-68f9baa2bcdf",
    "76e1c1d6-297e-4a26-9910-f07845e62589",
    "7c781eac-f84d-433d-99da-f2b74b34d90d",
    "b8c7bab4-d00c-48a4-9b9e-a266996a007f",
    "e705ad27-c86e-4b9d-940f-b4d90a16f83d",
}
EXCLUDED_UNIQUEIDS |= UGANDA_TRAINING_UNIQUEIDS

# Burkina enrollment before this date is pre-go-live and never enters the
# merged CSVs. Applies to both enrollee and vaccination_status.
BURKINA_STARTDATE_CUTOFF = "2026-07-14"

AUDIT_COLUMNS = [
    "table", "uniqueid", "barcode", "fieldname",
    "old_value", "new_value",
    # The interview date, for context. It is not a value that changes -- it was
    # identical on both sides of all 408 rows recorded before this was
    # collapsed from old_startdate/new_startdate into one column.
    "startdate",
    "old_lastmod", "new_lastmod",
    "old_sourcefile", "new_sourcefile",
    "audit_recorded_at",
]

ZIP_TIME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{2})_(\d{2})\.zip$")


def zip_fingerprint(path):
    """Enough to tell whether a file has been replaced since we last read it.

    Size and modification time, not a hash: these files are tens of megabytes
    and the question is only "is this the same file", which a re-download
    answers by changing both.
    """
    stat = path.stat()
    return f"{stat.st_size}:{int(stat.st_mtime)}"


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


def pad_mrc(v):
    """MRC codes are always 3 chars, zero-padded (e.g. "5" -> "005").

    Historic zips may store mrc as an integer, dropping leading zeros; pad
    here so the merged csv is consistent and no spurious audit rows fire.
    """
    return v.zfill(3) if v.isdigit() and len(v) < 3 else v


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
                    rows = [
                        {c: normalize(v) for c, v in zip(columns, row)}
                        for row in cur.fetchall()
                    ]
                    for rec in rows:
                        if "mrc" in rec:
                            rec["mrc"] = pad_mrc(rec["mrc"])
                    tables[table] = rows
            finally:
                con.close()
        finally:
            os.unlink(tmp_path)
    return tables


def audit_columns_changed(path):
    """True when an existing audit file was written with different columns.

    The file is appended to across runs, so a changed column list would append
    rows that no longer line up with the header. Rebuilding is the only honest
    answer: the audit trail is derived from the zips and can always be redone.
    """
    if not path.exists():
        return False
    with open(path, newline="", encoding="utf-8") as f:
        header = next(csv.reader(f), None)
    return header is not None and header != AUDIT_COLUMNS


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


def count_repeated_uniqueids(rows):
    """How many rows in one snapshot are extra copies of a record it already has.

    A device database should hold each interview once. More than one row with
    the same uniqueid means the app saved it twice — the cause of the "the
    server has 56 rows but the dashboard shows 45" question. The merge already
    collapses them; this is only so the difference is reported rather than
    discovered later.
    """
    seen, repeats = set(), 0
    for row in rows:
        uid = row.get("uniqueid", "")
        if not uid:
            continue
        if uid in seen:
            repeats += 1
        else:
            seen.add(uid)
    return repeats


def device_max_increments(rows):
    """Highest subject-ID increment per device in one snapshot.

    subjid is country + deviceid + mrc + increment, so the last four digits are
    the device's counter. The counter is derived from MAX() over the device's
    own table, so it only ever climbs — unless the database is lost, which is
    what uninstalling the app does. A snapshot whose maximum has fallen is that
    happening, and it is the point at which subject IDs start being reissued.
    """
    highest = {}
    for row in rows:
        device, subjid = row.get("deviceid", ""), row.get("subjid", "") or ""
        if not device or len(subjid) < 4 or not subjid[-4:].isdigit():
            continue
        increment = int(subjid[-4:])
        if increment > highest.get(device, -1):
            highest[device] = increment
    return highest


def check_counter_regression(country, zip_name, rows, device_max):
    """Warn when a device's counter has gone backwards, and update the record."""
    warnings = 0
    for device, highest in sorted(device_max_increments(rows).items()):
        previous = device_max.get(device)
        if previous is not None and highest < previous:
            print(f"  WARNING {zip_name}: device {device} highest subject ID "
                  f"dropped from {previous:04d} to {highest:04d}. The database "
                  f"on that device was probably lost (app uninstalled or "
                  f"storage cleared) and the counter has restarted, so subject "
                  f"IDs are being issued a second time.")
            warnings += 1
        device_max[device] = max(highest, previous or 0)
    return warnings


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
                "barcode": row.get("barcode", existing.get("barcode", "")),
                "fieldname": field,
                "old_value": existing.get(field, ""),
                "new_value": row.get(field, ""),
                "startdate": row.get("startdate", existing.get("startdate", "")),
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
    full_rebuild = (
        not state_path.exists()
        or not all(p.exists() for p in csv_paths.values())
        or audit_columns_changed(audit_path)
    )
    if full_rebuild:
        state = {"processed_files": [], "sources": {t: {} for t in TABLES}}
        tables = {t: ([], {}) for t in TABLES}  # (columns, records)
        audit_path.unlink(missing_ok=True)
    else:
        with open(state_path) as f:
            state = json.load(f)
        tables = {t: load_csv(csv_paths[t]) for t in TABLES}

    # Highest subject-ID increment seen per device, so a counter that restarts
    # is noticed on the next upload rather than weeks later. Absent from state
    # files written before this check existed; it fills in as zips are read.
    device_max = state.setdefault("device_max", {})

    # Zips that could not be opened. They are never marked processed -- a good
    # copy may yet arrive under the same name -- so without this they would be
    # reopened and re-reported on every run for as long as they sit in the
    # folder. Keyed by fingerprint, so a replacement is tried again.
    known_bad = state.setdefault("unreadable", {})

    processed = set(state["processed_files"])
    all_zips = sorted(folder.glob("*.zip"), key=zip_sort_key)
    new_zips = [z for z in all_zips if z.name not in processed]
    print(f"[{country}] {len(all_zips)} zip files, {len(new_zips)} new"
          + (" (full rebuild)" if full_rebuild else ""))
    if not new_zips:
        return

    audit_exists = audit_path.exists()
    unreadable = []
    still_bad = []
    repeated_rows = 0
    counter_warnings = 0

    with open(audit_path, "a", newline="", encoding="utf-8") as audit_file:
        audit_writer = csv.DictWriter(audit_file, fieldnames=AUDIT_COLUMNS)
        if not audit_exists:
            audit_writer.writeheader()

        for zip_path in new_zips:
            fingerprint = zip_fingerprint(zip_path)
            remembered = known_bad.get(zip_path.name)
            if remembered and remembered.get("fingerprint") == fingerprint:
                still_bad.append((zip_path.name, remembered.get("error", "")))
                continue

            try:
                zip_tables = read_tables_from_zip(zip_path)
            except (zipfile.BadZipFile, sqlite3.DatabaseError) as e:
                print(f"  ERROR reading {zip_path.name}: {e} - skipping")
                known_bad[zip_path.name] = {
                    "fingerprint": fingerprint,
                    "error": str(e),
                    "first_seen": datetime.now().isoformat(timespec="seconds"),
                }
                unreadable.append((zip_path.name, str(e)))
                continue

            # It opened, so any earlier failure was a bad copy since replaced.
            if remembered:
                print(f"  {zip_path.name}: reads correctly now — was previously "
                      f"unreadable, retrying it")
                known_bad.pop(zip_path.name, None)
            for table in TABLES:
                columns, records = tables[table]
                rows = zip_tables[table]
                rows = [r for r in rows if r.get("uniqueid") not in EXCLUDED_UNIQUEIDS]
                if country == "uganda":
                    # A missing startdate isn't "before" the cutoff -- it's
                    # unknown -- so only a row with a dated, pre-cutoff value
                    # is dropped here.
                    rows = [r for r in rows
                            if not r.get("startdate")
                            or r.get("startdate") >= UGANDA_STARTDATE_CUTOFF]
                elif country == "burkina":
                    rows = [r for r in rows
                            if not r.get("startdate")
                            or r.get("startdate") >= BURKINA_STARTDATE_CUTOFF]

                repeats = count_repeated_uniqueids(rows)
                if repeats:
                    repeated_rows += repeats
                    print(f"  {zip_path.name}: {table} holds {repeats} extra "
                          f"copy/copies of records it already has; merged to one each")

                if table == "enrollee":
                    counter_warnings += check_counter_regression(
                        country, zip_path.name, rows, device_max)

                changes = merge_zip(
                    table, records, columns, state["sources"][table],
                    zip_path.name, rows, audit_writer,
                )
                if changes:
                    print(f"  {zip_path.name}: {changes} field change(s) "
                          f"in {table} written to audit trail")
            processed.add(zip_path.name)

    for table in TABLES:
        columns, records = tables[table]
        write_csv(csv_paths[table], columns, records)
        print(f"[{country}] {csv_paths[table].name}: {len(records)} record(s)")

    report_by_facility(country, tables["enrollee"][1])

    if repeated_rows or counter_warnings or unreadable or still_bad:
        print(f"[{country}] summary: {repeated_rows} duplicate row(s) merged, "
              f"{counter_warnings} counter regression warning(s), "
              f"{len(unreadable) + len(still_bad)} unreadable zip(s) "
              f"({len(still_bad)} already known)")

    if unreadable or still_bad:
        # Each zip is a full snapshot, so a later upload from the same device
        # carries the same records -- but nobody can know that without being
        # told the file exists. Three sat unnoticed for a fortnight.
        print(f"[{country}] {len(unreadable) + len(still_bad)} zip(s) could not "
              f"be read. They are skipped without reopening until replaced or "
              f"deleted:")
        for name, error in unreadable:
            print(f"    {name}: {error}  (new)")
        for name, error in still_bad:
            print(f"    {name}: {error}")
        print(f"[{country}] Check whether the device has uploaded a readable "
              f"snapshot since. If it has, the data is not lost and the file "
              f"can be deleted; if it has not, ask for a fresh upload.")

    state["processed_files"] = sorted(processed)
    with open(state_path, "w") as f:
        json.dump(state, f, indent=1)


def report_by_facility(country, records):
    """Interviews per facility in the merged output.

    Printed so a count queried against a device or the server can be checked
    against what the dashboard will show, without anyone having to open a
    database to explain the difference.
    """
    if not records:
        return
    counts = {}
    for row in records.values():
        counts[row.get("mrc", "") or "(blank)"] = counts.get(row.get("mrc", "") or "(blank)", 0) + 1
    line = "  ".join(f"{mrc}={n}" for mrc, n in sorted(counts.items()))
    print(f"[{country}] interviews by facility: {line}")


def main():
    for country in COUNTRIES:
        process_country(country)
    return 0


if __name__ == "__main__":
    sys.exit(main())
