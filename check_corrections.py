#!/usr/bin/env python3
"""Check correction batches against the merged data, without uploading anything.

Run this on a new or edited batch in corrections/field/ until it is clean,
then commit it; the next upload applies it. It checks every batch and the
restores file against data/<country>/enrollee.csv as the dashboard will see it
(subject-id corrections and restores already applied), and lists every
problem by file and line:

  - missing/unknown columns, blank required values, a bad verified_at date
  - a uniqueid that isn't in the data, or a subjid/barcode/country that
    doesn't match that record (they must be the values currently in the
    data -- even a barcode that is the one being corrected)
  - a field that isn't a column in the data
  - an old_value that isn't what the record holds, or a record that already
    holds the new_value
  - a barcode fix without its barcode2 partner (or vice versa)
  - a new barcode that isn't on the deployed list, or that another record uses
  - the same field corrected twice in one batch, or a later batch whose
    old_value doesn't follow on from the earlier one

A row with a blank uniqueid is resolved from its subjid + barcode and the
uniqueid printed, so it can be pasted in.

Usage:
  python check_corrections.py                 # every batch + restores
  python check_corrections.py --offline       # skip the deployed-barcode check
  python check_corrections.py --find-wipes    # list records a tablet wiped

Exit code 0 when clean, 1 when there is anything to fix.
"""

import argparse
import contextlib
import csv
import io
import os
import sys

import query_corrections as qc
from upload_to_supabase import (COUNTRY_FOLDERS, DATA_DIR, ENV_FILE,
                                apply_subjid_corrections, load_env_file,
                                load_subjid_corrections, read_rows)


def fetch_deployed_barcodes():
    """{country: set(barcode)} from Supabase, or None when it can't be read."""
    load_env_file(ENV_FILE)
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        return None
    try:
        from supabase import create_client
        client = create_client(url, key)
        deployed, start = {}, 0
        while True:
            page = (client.table("deployed_barcodes").select("barcode,country")
                    .range(start, start + 999).execute().data)
            for r in page:
                deployed.setdefault(r["country"], set()).add(r["barcode"])
            if len(page) < 1000:
                return deployed
            start += 1000
    except Exception as exc:  # noqa: BLE001
        print(f"(could not read deployed_barcodes: {exc})")
        return None


def country_data(folder, country, subjid_corrections, restores):
    """Rows as the dashboard will see them before field corrections, the same
    rows before restores, and any restore problems."""
    with contextlib.redirect_stdout(io.StringIO()):
        rows, _, _ = apply_subjid_corrections(
            list(read_rows(DATA_DIR / folder / "enrollee.csv")),
            subjid_corrections, country)
    before = {r["uniqueid"]: dict(r) for r in rows}
    events = qc.load_wipe_events(DATA_DIR / folder / "audittrail.csv")
    rows, _, warnings, _ = qc.apply_record_restores(rows, restores, events, country)
    return rows, before, [f"record_restores.csv: {w}" for w in warnings]


def suggest_uniqueids(rows_by_country):
    """For rows with a blank uniqueid, the record their subjid + barcode
    point to."""
    out = []
    for path in qc.field_batch_files():
        with open(path, newline="", encoding="utf-8-sig") as f:
            for line, r in enumerate(csv.DictReader(f), start=2):
                if (r.get("uniqueid") or "").strip():
                    continue
                subjid, barcode = (r.get("subjid") or "").strip(), (r.get("barcode") or "").strip()
                rows = rows_by_country.get((r.get("country") or "").strip(), [])
                hits = [x for x in rows if subjid and x.get("subjid") == subjid
                        and (not barcode or x.get("barcode") == barcode)]
                where = f"{path.name}:{line}"
                if len(hits) == 1:
                    out.append(f"{where}: subjid {subjid} -> uniqueid {hits[0]['uniqueid']}")
                elif not hits:
                    by_subjid = [x for x in rows if subjid and x.get("subjid") == subjid]
                    hint = (f" (subjid matches, but its barcode is "
                            f"{by_subjid[0].get('barcode') or '(blank)'})"
                            if len(by_subjid) == 1 else "")
                    out.append(f"{where}: no record with subjid {subjid!r} and "
                               f"barcode {barcode!r}{hint}")
                else:
                    out.append(f"{where}: subjid {subjid} matches {len(hits)} records "
                               f"-- fill in uniqueid by hand")
    return out


def check_rows(corrections, country, rows, before, deployed):
    problems = []
    by_id = {r["uniqueid"]: r for r in rows}
    columns = set(rows[0]) if rows else set()
    working = {}  # (uniqueid, field) -> value after the corrections so far
    mine = [c for c in corrections if c["country"] == country]

    for c in mine:
        uid, field = c["uniqueid"], c["field"]
        row = by_id.get(uid)
        if row is None:
            problems.append(f"{c.where}: uniqueid {uid} is not in the {country} data")
            continue
        for key in ("subjid", "barcode"):
            have = row.get(key) or ""
            if c[key] != have:
                old = (before.get(uid) or {}).get(key) or ""
                note = (f" (that was its value before the restore; it is now "
                        f"{have or '(blank)'})" if c[key] == old and old != have else "")
                problems.append(f"{c.where}: {key} {c[key] or '(blank)'} doesn't match "
                                f"the record's {have or '(blank)'}{note}")
        if field not in columns:
            problems.append(f"{c.where}: {field} isn't a column in the data")
            continue

        current = working.get((uid, field), row.get(field) or "")
        if c["old_value"] == current:
            working[(uid, field)] = c["new_value"]
        elif current == c["new_value"]:
            problems.append(f"{c.where}: record already holds {field} = "
                            f"{current!r} -- nothing to correct, drop the row")
        else:
            problems.append(f"{c.where}: old_value {c['old_value']!r} but the "
                            f"record holds {field} = {current!r}")

        partner = qc.PAIRED_FIELDS.get(field)
        if partner and not any(o["uniqueid"] == uid and o["field"] == partner
                               and o["_file"] == c["_file"] for o in mine):
            problems.append(f"{c.where}: {field} is corrected but {partner} isn't "
                            f"-- they must change together")
        elif partner and field == "barcode":
            other = next(o for o in mine if o["uniqueid"] == uid
                         and o["field"] == partner and o["_file"] == c["_file"])
            if other["new_value"] != c["new_value"]:
                problems.append(f"{c.where}: new barcode {c['new_value']} and new "
                                f"barcode2 {other['new_value']} ({other.where}) differ")

    # Barcodes as they will be once everything applies.
    final = {uid: row.get("barcode") or "" for uid, row in by_id.items()}
    for (uid, field), value in working.items():
        if field == "barcode":
            final[uid] = value
    for c in mine:
        if c["field"] != "barcode" or c["uniqueid"] not in by_id:
            continue
        new = c["new_value"]
        others = [u for u, b in final.items() if b == new and u != c["uniqueid"]]
        if others:
            problems.append(f"{c.where}: new barcode {new} is also used by "
                            f"{', '.join(by_id[u].get('subjid') or u for u in others)}")
        if deployed is not None and new not in deployed.get(country, set()):
            problems.append(f"{c.where}: new barcode {new} is not on the deployed "
                            f"barcode list for {country}")
    return problems


def find_wipes(subjid_corrections, restores):
    """Records whose latest version was saved as age-ineligible with consent
    cleared -- what re-opening an older record on a tablet did on 8 Oct."""
    found = False
    for folder, country in COUNTRY_FOLDERS.items():
        rows, _, _ = country_data(folder, country, subjid_corrections, {})
        by_id = {r["uniqueid"]: r for r in rows}
        events = qc.load_wipe_events(DATA_DIR / folder / "audittrail.csv")
        for (uid, lastmod), cleared in sorted(events.items(), key=lambda e: e[0][1]):
            row = by_id.get(uid)
            if (row is None or cleared.get("age_eligible") != "1"
                    or row.get("age_eligible") != "0" or "consent" not in cleared
                    or row.get("consent")):
                continue
            found = True
            status = "restore listed" if uid in restores else "NOT in record_restores.csv"
            current = "" if row.get("lastmod") == lastmod else " (since edited again)"
            print(f"{country}  {uid}  subjid {row.get('subjid', '')}  barcode was "
                  f"{cleared.get('barcode') or '(blank)'}  wiped_lastmod {lastmod}"
                  f"{current}  {len(cleared)} fields  -- {status}")
    if not found:
        print("No wiped records found.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true",
                    help="skip the deployed-barcode check (no Supabase)")
    ap.add_argument("--find-wipes", action="store_true",
                    help="list records a tablet wiped, for record_restores.csv")
    args = ap.parse_args()

    subjid_corrections = load_subjid_corrections()
    restores = qc.load_restores()
    if args.find_wipes:
        find_wipes(subjid_corrections, restores)
        return 0

    corrections, problems = qc.load_field_corrections()
    deployed = None if args.offline else fetch_deployed_barcodes()
    if deployed is None:
        print("(deployed-barcode check skipped)")

    known = {c for c in COUNTRY_FOLDERS.values()}
    for c in corrections:
        if c["country"] not in known:
            problems.append(f"{c.where}: country {c['country']!r} must be one of "
                            f"{', '.join(sorted(known))}")
    for uid, r in restores.items():
        if r.get("country") not in known:
            problems.append(f"record_restores.csv: {uid} has country {r.get('country')!r}")

    rows_by_country = {}
    for folder, country in COUNTRY_FOLDERS.items():
        if not (DATA_DIR / folder / "enrollee.csv").exists():
            print(f"({folder}: no enrollee.csv, skipped)")
            continue
        rows, before, p = country_data(folder, country, subjid_corrections, restores)
        rows_by_country[country] = rows
        problems += p
        problems += check_rows(corrections, country, rows, before, deployed)

    suggestions = suggest_uniqueids(rows_by_country)
    n_files = len(qc.field_batch_files())
    print(f"{len(corrections)} field correction(s) in {n_files} batch file(s), "
          f"{len(restores)} restore(s)")
    if suggestions:
        print("\nBlank uniqueids:")
        for s in suggestions:
            print(f"  {s}")
    if problems:
        print(f"\n{len(problems)} problem(s):")
        for p in problems:
            print(f"  {p}")
        return 1
    if suggestions:
        return 1
    print("All clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
