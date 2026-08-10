#!/usr/bin/env python3
"""Propose subject-ID corrections for records that share a subject ID.

A device derives its subject-ID counter from MAX() over its own table. If the
database is lost -- uninstalling the app does this -- the counter restarts and
begins handing out IDs that were already used. The records are still distinct
(each has its own barcode and uniqueid); only the subject ID collides.

This proposes a correction for each collision and appends it to

    corrections/subjid_corrections.csv

which `upload_to_supabase.py` applies on every run. Rows already in that file
are never rewritten: their reason, date and author are the record of a decision
someone made, and a later incident must not disturb them.

Nothing is applied here. Review the generated rows -- a review copy carrying
names and dates is written next to the merged data -- then commit the file.

Usage:
  python make_corrections.py                 # propose, write, and report
  python make_corrections.py --dry-run       # report only
"""

import argparse
import csv
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CORRECTIONS_DIR = BASE_DIR / "corrections"
CORRECTIONS_FILE = CORRECTIONS_DIR / "subjid_corrections.csv"
REVIEW_FILE = "subjid_corrections_review.csv"

COUNTRY_CODE = {"burkina": "BF", "uganda": "UG"}

COLUMNS = [
    "uniqueid", "country", "barcode", "old_subjid", "new_subjid",
    "reason", "corrected_on", "corrected_by",
]
# Names and dates make the mapping checkable, and must not reach git.
REVIEW_COLUMNS = COLUMNS + ["startdate", "deviceid", "participantsname"]

# Corrected IDs keep their country, device and facility codes and take the
# increment from a block the natural counter will not reach for years, so a
# corrected ID is recognisable and cannot collide with a future one.
CORRECTED_BLOCK = 9


def load_corrections():
    if not CORRECTIONS_FILE.exists():
        return []
    with open(CORRECTIONS_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def remap(subjid):
    """Move the increment into the corrected block, keeping every other part."""
    prefix, increment = subjid[:-4], int(subjid[-4:])
    if not 1 <= increment <= 999:
        raise ValueError(
            f"increment {increment:04d} in {subjid} leaves no room in the "
            f"{CORRECTED_BLOCK}000 block; choose the replacement by hand")
    return f"{prefix}{CORRECTED_BLOCK}{increment:03d}"


def propose_for_country(folder, code, already_corrected, reason, today, author):
    """Corrections needed for one country, skipping records already covered."""
    path = DATA_DIR / folder / "enrollee.csv"
    if not path.exists():
        print(f"[{folder}] no enrollee.csv, skipping")
        return [], []

    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    by_subjid = defaultdict(list)
    for row in rows:
        if row.get("subjid"):
            by_subjid[row["subjid"]].append(row)

    # Every subject ID that exists or is already spoken for by a correction.
    taken = set(by_subjid) | {c["new_subjid"] for c in already_corrected}
    covered = {c["uniqueid"] for c in already_corrected}

    collisions = {s: rs for s, rs in by_subjid.items() if len(rs) > 1}
    print(f"[{folder}] {len(rows)} records, {len(collisions)} duplicated subject id(s)")

    proposed, review, skipped = [], [], 0
    for subjid in sorted(collisions):
        group = sorted(collisions[subjid], key=lambda r: r.get("startdate", ""))

        # Everything but the most recent record is renumbered, so the device's
        # live counter carries on undisturbed.
        for record in group[:-1]:
            if record["uniqueid"] in covered:
                skipped += 1
                continue

            if len(group) > 2:
                print(f"  NOTE {subjid} is shared by {len(group)} records; "
                      f"renumbering all but the most recent")

            new_subjid = remap(subjid)
            while new_subjid in taken:
                print(f"  NOTE {new_subjid} already in use, trying the next")
                new_subjid = remap(f"{new_subjid[:-4]}{int(new_subjid[-4:]) + 1:04d}")
            taken.add(new_subjid)

            entry = {
                "uniqueid": record["uniqueid"],
                "country": code,
                "barcode": record.get("barcode", ""),
                "old_subjid": subjid,
                "new_subjid": new_subjid,
                "reason": reason,
                "corrected_on": today,
                "corrected_by": author,
            }
            proposed.append(entry)
            review.append({**entry,
                           "startdate": record.get("startdate", ""),
                           "deviceid": record.get("deviceid", ""),
                           "participantsname": record.get("participantsname", "")})

    if skipped:
        print(f"  {skipped} already covered by an existing correction")
    return proposed, review


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be added without writing")
    parser.add_argument("--reason", default="", help="why these ids collided")
    parser.add_argument("--by", default="", help="who authored the correction")
    args = parser.parse_args()

    existing = load_corrections()
    print(f"{len(existing)} correction(s) already recorded")

    today = date.today().isoformat()
    reason = args.reason or ("subject ids reissued after the collecting device "
                             "lost its database and its counter restarted")

    proposed, review = [], []
    for folder, code in COUNTRY_CODE.items():
        country_existing = [c for c in existing if c["country"] == code]
        p, r = propose_for_country(folder, code, country_existing,
                                   reason, today, args.by)
        proposed.extend(p)
        review.extend(r)

    if not proposed:
        print("\nNo new corrections needed.")
        return 0

    print(f"\n{len(proposed)} new correction(s):")
    for entry in proposed:
        print(f"  {entry['old_subjid']} -> {entry['new_subjid']}  "
              f"{entry['barcode']}  ({entry['country']})")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0

    CORRECTIONS_DIR.mkdir(exist_ok=True)
    with open(CORRECTIONS_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        # Existing rows first, byte for byte: they record earlier decisions.
        writer.writerows(existing)
        writer.writerows(proposed)
    print(f"\nappended to {CORRECTIONS_FILE} ({len(existing) + len(proposed)} total)")

    for folder in COUNTRY_CODE:
        rows = [r for r in review if r["country"] == COUNTRY_CODE[folder]]
        if rows and (DATA_DIR / folder).is_dir():
            path = DATA_DIR / folder / REVIEW_FILE
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=REVIEW_COLUMNS)
                writer.writeheader()
                writer.writerows(rows)
            print(f"wrote {path} for review (not committed — carries names)")

    print("\nReview the rows, then commit corrections/subjid_corrections.csv.")
    print("They are applied on the next upload; nothing has changed yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
