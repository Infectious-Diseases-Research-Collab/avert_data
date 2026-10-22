"""Central corrections for data queries that can no longer be fixed on a tablet.

Two kinds, both committed under corrections/ and reapplied on every upload:

  corrections/field/corrections_YYYY_MM_DD*.csv
      Hand-written batches, one row per field: "this record's <field> should
      be <new_value>, not <old_value>". One file per batch the sites send;
      a committed batch is never edited, a later fix goes in a later batch.

  corrections/record_restores.csv
      Records a tablet wiped. Re-opening an older record after the eligibility
      window changed recalculated it as ineligible and cleared the rest of the
      form. The values it cleared are all in the audit trail, so a restore
      names only the wipe (uniqueid + the lastmod of the wiping version) and
      rebuilds the record from audittrail.csv -- the file itself holds no
      values.

Like subject-id corrections (see upload_to_supabase.py), these are applied on
the way to Supabase, not to the merged CSVs: the devices keep sending the
original values forever, and enrollee.csv stays a faithful record of what was
collected. Order on each run: subject ids, then restores, then field
corrections, so a field fix can apply on top of a restored record.

Every correction is guarded by the value it was written against. If the
underlying record changes, the correction is reported and skipped rather than
overwriting something it was not meant to touch.

`check_corrections.py` runs the same checks without uploading anything, so a
batch can be made clean before it is committed.
"""

import csv
import re
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
FIELD_DIR = BASE_DIR / "corrections" / "field"
RESTORES_FILE = BASE_DIR / "corrections" / "record_restores.csv"

FIELD_COLUMNS = [
    "uniqueid", "country", "subjid", "barcode", "field",
    "old_value", "new_value", "reason", "source_document",
    "verified_by", "verified_at", "corrected_by",
]
FIELD_REQUIRED = ["uniqueid", "country", "field", "new_value", "reason",
                  "source_document", "verified_by", "verified_at",
                  "corrected_by"]

RESTORE_COLUMNS = [
    "uniqueid", "country", "subjid", "wiped_lastmod", "reason",
    "verified_by", "verified_at", "corrected_at", "corrected_by",
]

# The batch date in the filename is what the audit rows carry as their
# timestamp: it has to be fixed, not "now", because new_lastmod is part of the
# audittrail's key and a moving value would insert fresh rows on every upload.
BATCH_NAME_RE = re.compile(r"^corrections_(\d{4})_(\d{2})_(\d{2})(?:_[A-Za-z0-9_-]+)?\.csv$")

# Fields that must change together. The tablet asks for the barcode twice and
# the barcode_reentry_mismatch check compares them, so fixing one alone would
# just trade one data-quality issue for another.
PAIRED_FIELDS = {"barcode": "barcode2", "barcode2": "barcode"}

# Fields that identify a record rather than describe it -- a correction to
# these goes through subjid_corrections.csv or isn't a correction at all.
NOT_CORRECTABLE = {"uniqueid", "subjid", "country", "lastmod"}


def _v(value):
    return (value or "").strip()


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

class Correction(dict):
    """A field-correction row plus where it came from, for messages."""

    @property
    def where(self):
        return f"{self['_file']}:{self['_line']}"


def batch_timestamp(filename):
    """corrections_2026_10_22.csv -> 2026-10-22T00:00:00, or None."""
    m = BATCH_NAME_RE.match(filename)
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3])).isoformat() + "T00:00:00"
    except ValueError:
        return None


def field_batch_files(directory=FIELD_DIR):
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.csv"), key=lambda p: p.name)


def read_field_batch(path):
    """(corrections, problems) for one batch file. Problems are structural --
    they make a row unusable, whatever the data says."""
    problems = []
    if batch_timestamp(path.name) is None:
        problems.append(f"{path.name}: name must be corrections_YYYY_MM_DD.csv "
                        f"(optionally with a _suffix) -- the date is the "
                        f"audit timestamp")
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = [h.strip() for h in (reader.fieldnames or [])]
        missing = [c for c in FIELD_COLUMNS if c not in header]
        unknown = [c for c in header if c and c not in FIELD_COLUMNS]
        if missing:
            problems.append(f"{path.name}: missing column(s): {', '.join(missing)}")
        if unknown:
            problems.append(f"{path.name}: unknown column(s): {', '.join(unknown)}")
        if missing:
            return [], problems
        rows = []
        for line, raw in enumerate(reader, start=2):
            raw.pop(None, None)
            row = Correction({k.strip(): _v(v) for k, v in raw.items() if k})
            if not any(row.values()):
                continue
            row["_file"], row["_line"] = path.name, line
            row["_stamp"] = batch_timestamp(path.name)
            if row.get("verified_at"):
                try:
                    date.fromisoformat(row["verified_at"])
                except ValueError:
                    problems.append(f"{row.where}: verified_at {row['verified_at']!r} "
                                    f"is not a YYYY-MM-DD date")
            blank = [c for c in FIELD_REQUIRED if not row.get(c)]
            if blank:
                problems.append(f"{row.where}: blank {', '.join(blank)}")
                continue
            if row["field"] in NOT_CORRECTABLE:
                problems.append(f"{row.where}: {row['field']} can't be corrected here")
                continue
            rows.append(row)
    return rows, problems


def load_field_corrections(directory=FIELD_DIR):
    """(corrections in filename order, structural problems)."""
    corrections, problems = [], []
    for path in field_batch_files(directory):
        rows, p = read_field_batch(path)
        problems += p
        # Without a date in its name a batch has no fixed audit timestamp, and
        # applying it would add fresh audit rows on every upload.
        if batch_timestamp(path.name) is not None:
            corrections += rows
    problems += chain_problems(corrections)
    return corrections, problems


def chain_problems(corrections):
    """A record's field may be corrected again in a later batch, but each step
    must start where the previous one ended, and a batch may not correct the
    same field twice."""
    problems, last = [], {}
    for c in corrections:
        key = (c["uniqueid"], c["field"])
        prev = last.get(key)
        if prev is not None:
            if prev["_file"] == c["_file"]:
                problems.append(f"{c.where}: {c['field']} for {c['uniqueid']} is "
                                f"already corrected at line {prev['_line']} of "
                                f"this batch")
            elif c["old_value"] != prev["new_value"]:
                problems.append(f"{c.where}: old_value {c['old_value']!r} doesn't "
                                f"follow on from {prev.where}, which set it to "
                                f"{prev['new_value']!r}")
        last[key] = c
    return problems


def load_restores(path=RESTORES_FILE):
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        return {r["uniqueid"].strip(): {k: _v(v) for k, v in r.items() if k}
                for r in csv.DictReader(f) if _v(r.get("uniqueid"))}


def load_wipe_events(audit_path):
    """{(uniqueid, new_lastmod): {field: old_value}} for enrollee audit rows,
    i.e. everything a given saved version changed and what it had been."""
    events = {}
    if not audit_path.exists():
        return events
    with open(audit_path, newline="", encoding="utf-8-sig") as f:
        for a in csv.DictReader(f):
            if a.get("table") != "enrollee":
                continue
            key = (a["uniqueid"], a["new_lastmod"])
            events.setdefault(key, {})[a["fieldname"]] = a.get("old_value") or ""
    return events


# --------------------------------------------------------------------------
# Applying
# --------------------------------------------------------------------------

def _audit(row, field, old, new, stamp, sourcefile, correction=None):
    entry = {
        "table": "enrollee",
        "uniqueid": row.get("uniqueid", ""),
        "barcode": row.get("barcode", ""),
        "fieldname": field,
        "old_value": old,
        "new_value": new,
        "startdate": row.get("startdate", ""),
        "old_lastmod": row.get("lastmod", ""),
        "new_lastmod": stamp,
        "old_sourcefile": "",
        "new_sourcefile": sourcefile,
        "audit_recorded_at": stamp,
    }
    if correction:
        entry["correction_reason"] = correction.get("reason", "")
        entry["correction_source_document"] = correction.get("source_document", "")
        entry["correction_verified_by"] = correction.get("verified_by", "")
    return entry


def apply_record_restores(rows, restores, wipe_events, country):
    """Rebuild wiped records from the audit trail. Returns (rows, audit
    entries, warnings, applied uniqueids)."""
    by_id = {r.get("uniqueid", ""): r for r in rows}
    audits, warnings, applied = [], [], []
    for uid, restore in restores.items():
        if restore.get("country") != country:
            continue
        row = by_id.get(uid)
        if row is None:
            warnings.append(f"restore for {uid} refers to a record not in this data")
            continue
        wiped = restore["wiped_lastmod"]
        if _v(row.get("lastmod")) != wiped:
            warnings.append(
                f"restore for {uid} ({row.get('subjid', '')}) expected the version "
                f"saved {wiped} but the record is now {row.get('lastmod') or '(none)'}"
                f" -- the tablet changed it again; NOT applied")
            continue
        cleared = wipe_events.get((uid, wiped))
        if not cleared:
            warnings.append(f"restore for {uid}: no audit-trail rows for the "
                            f"version saved {wiped} -- NOT applied")
            continue
        stamp = restore["corrected_at"]
        for field, old_value in sorted(cleared.items()):
            current = row.get(field, "")
            if current == old_value:
                continue
            row[field] = old_value
            audits.append(_audit(row, field, current, old_value, stamp,
                                 "corrections/record_restores.csv", restore))
        applied.append(uid)
    return rows, audits, warnings, applied


def apply_field_corrections(rows, corrections, country):
    """Apply each record-field's chain of corrections. Returns (rows, audit
    entries, warnings, counts).

    For one record's field the batches form a chain A->B, B->C, ... Whatever
    the record currently holds picks the starting point: a value the chain
    starts from is corrected through to the end; the chain's final value means
    it was already fixed at source, which is fine and only counted; anything
    else is something nobody verified, so it is reported and left alone.
    """
    by_id = {r.get("uniqueid", ""): r for r in rows}
    chains = {}
    for c in corrections:
        if c["country"] == country:
            chains.setdefault((c["uniqueid"], c["field"]), []).append(c)

    audits, warnings = [], []
    counts = {"applied": 0, "already": 0}
    for (uid, field), chain in chains.items():
        row = by_id.get(uid)
        if row is None:
            warnings.append(f"{chain[0].where}: {uid} is not in this data -- NOT applied")
            continue
        current = row.get(field, "") or ""
        start = next((i for i, c in enumerate(chain) if c["old_value"] == current), None)
        if start is None:
            if current == chain[-1]["new_value"]:
                counts["already"] += 1
                continue
            warnings.append(
                f"{chain[-1].where}: {row.get('subjid', '')} {field} expected "
                f"{chain[0]['old_value']!r} but found {current!r} -- NOT applied")
            continue
        for c in chain[start:]:
            audits.append(_audit(row, field, row.get(field, ""), c["new_value"],
                                 c["_stamp"], f"corrections/field/{c['_file']}", c))
            row[field] = c["new_value"]
            counts["applied"] += 1
    return rows, audits, warnings, counts
