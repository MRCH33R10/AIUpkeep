#!/usr/bin/env python3
"""
AIUpkeep — job application tracker upkeep.

Three-stage run, in order:

  1. RAG AUDIT — every row in Index.xls is checked against the actual
     filesystem. Any resume the index points to that doesn't exist gets
     flagged, and any resume sitting in the Resumes folder that no row
     points to gets flagged too.

  2. STATUS CHECK + CLEANUP — for every row still in the index, the job
     posting's Link is requested. A 404 means the posting is gone, so its
     resume is deleted and its row removed from Index.xls. Any other
     response is treated as still open and the row is kept. This check is
     company-agnostic — it works the same way regardless of which ATS or
     careers site the Link points to.

  3. SUMMARY — count.xls is rewritten to match the number of rows left in
     Index.xls, and a one-line summary is printed.
"""

import argparse
import csv
import re
import sys
from pathlib import Path

import requests

USER_AGENT = "Mozilla/5.0"  # some ATS platforms block the default requests UA
REQUEST_TIMEOUT = 10

INDEX_FIELDS = ["Company", "Link", "File Address"]


# ---------------------------------------------------------------------------
# Index / count I/O  (plain CSV, just wearing an .xls extension)
# ---------------------------------------------------------------------------

def load_index(index_path: Path) -> list[dict]:
    if not index_path.exists():
        print(f"[error] Index file not found: {index_path}")
        sys.exit(1)

    with open(index_path, newline="") as fh:
        reader = csv.DictReader(fh, skipinitialspace=True)
        rows = []
        for raw in reader:
            row = {k.strip(): (v.strip() if v else "") for k, v in raw.items() if k}
            rows.append(row)
    return rows


def save_index(index_path: Path, rows: list[dict]):
    with open(index_path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(INDEX_FIELDS)
        for row in rows:
            writer.writerow([row.get(f, "") for f in INDEX_FIELDS])


def load_count(count_path: Path) -> int:
    if not count_path.exists():
        return 0
    with open(count_path) as fh:
        content = fh.read().strip()
    match = re.search(r"count\s*,\s*(-?\d+)", content, re.IGNORECASE)
    return int(match.group(1)) if match else 0


def save_count(count_path: Path, value: int):
    with open(count_path, "w") as fh:
        fh.write(f"count, {value}")


# ---------------------------------------------------------------------------
# Stage 1 — RAG audit: index vs. filesystem
# ---------------------------------------------------------------------------

def rag_audit(rows: list[dict], resume_dir: Path):
    print("=== Stage 1: RAG audit (index vs. filesystem) ===")

    indexed_paths = set()
    missing = []
    for row in rows:
        addr = row.get("File Address", "")
        if not addr:
            continue
        path = Path(addr).expanduser()
        indexed_paths.add(str(path.resolve()) if path.exists() else str(path))
        if not path.exists():
            missing.append(row)

    if missing:
        print(f"  [!] {len(missing)} row(s) point to a resume that no longer exists:")
        for row in missing:
            print(f"      - {row.get('Company', '?')}: {row.get('File Address', '?')}")
    else:
        print("  All indexed resumes are present on disk.")

    orphans = []
    if resume_dir.is_dir():
        for f in resume_dir.iterdir():
            if not f.is_file():
                continue
            if str(f.resolve()) not in indexed_paths:
                orphans.append(f)

    if orphans:
        print(f"  [!] {len(orphans)} resume(s) in {resume_dir} aren't referenced by any row:")
        for f in orphans:
            print(f"      - {f.name}")
    elif resume_dir.is_dir():
        print("  Every resume in the resume folder is accounted for.")
    else:
        print(f"  [!] Resume folder not found: {resume_dir}")

    print()
    return missing, orphans


# ---------------------------------------------------------------------------
# Stage 2 — job status checking
# ---------------------------------------------------------------------------

def check_job_status(link: str) -> str:
    """
    Company-agnostic status check: a 404 means the posting is gone (closed);
    any other response (200, redirects the requests library already followed,
    3xx that resolved, etc.) is treated as still active.

    A network error (timeout, DNS failure, connection refused) isn't a 404,
    so it's treated as 'open' too — a transient error shouldn't delete a
    resume for a job that might still be live.
    """
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(link, headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        print(f"      [warn] network error checking posting, treating as still open: {e}")
        return "open"

    return "closed" if resp.status_code == 404 else "open"


# ---------------------------------------------------------------------------
# Stage 2 continued — apply cleanup
# ---------------------------------------------------------------------------

def check_and_clean(rows: list[dict], dry_run: bool) -> list[dict]:
    print("=== Stage 2: checking posting status ===")
    kept = []
    removed = 0

    for row in rows:
        company = row.get("Company", "?")
        link = row.get("Link", "")
        addr = row.get("File Address", "")
        print(f"  {company}: {link}")

        if not link:
            print("      [warn] no link on this row — keeping, can't verify status")
            kept.append(row)
            continue

        status = check_job_status(link)

        if status == "closed":
            print("      -> CLOSED. Removing row" + (" and deleting resume" if addr else "") + (" (dry run)" if dry_run else ""))
            if addr:
                path = Path(addr).expanduser()
                if path.exists():
                    if not dry_run:
                        path.unlink()
                    print(f"      -> {'would delete' if dry_run else 'deleted'}: {path}")
                else:
                    print(f"      -> resume already missing, nothing to delete: {path}")
            removed += 1
            # row dropped, not appended to kept
        else:
            print("      -> still open. Keeping.")
            kept.append(row)

    print()
    return kept, removed


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Audit, verify, and clean up the job application index")
    ap.add_argument("--aiupkeep-dir", default=".",
                     help="Folder containing Index.xls and count.xls (default: current dir)")
    ap.add_argument("--resume-dir", default=None,
                     help="Folder containing resume files (default: '../Resumes' next to --aiupkeep-dir)")
    ap.add_argument("--dry-run", action="store_true",
                     help="Report everything without deleting files or writing changes")
    args = ap.parse_args()

    aiupkeep_dir = Path(args.aiupkeep_dir).expanduser().resolve()
    resume_dir = (Path(args.resume_dir).expanduser().resolve() if args.resume_dir
                  else (aiupkeep_dir.parent / "Resumes").resolve())
    index_path = aiupkeep_dir / "Index.xls"
    count_path = aiupkeep_dir / "count.xls"

    print(f"AIUpKeep dir: {aiupkeep_dir}")
    print(f"Resume dir:   {resume_dir}")
    print(f"Index file:   {index_path}")
    print(f"Count file:   {count_path}")
    print(f"Mode:         {'DRY RUN (nothing will be deleted or written)' if args.dry_run else 'LIVE'}\n")

    rows = load_index(index_path)

    # Stage 1
    rag_audit(rows, resume_dir)

    # Stage 2
    kept_rows, removed_count = check_and_clean(rows, args.dry_run)

    if not args.dry_run:
        save_index(index_path, kept_rows)
        save_count(count_path, len(kept_rows))
    else:
        print(f"  (dry run — would remove {removed_count} row(s), "
              f"leaving {len(kept_rows)}; index and count files left untouched)\n")

    # Stage 3 — summary, from the count value
    final_count = len(kept_rows) if not args.dry_run else load_count(count_path)
    print("=== Stage 3: summary ===")
    print(f"  Applications currently tracked as active: {final_count}")
    if removed_count:
        print(f"  ({removed_count} closed application(s) cleaned up this run)")


if __name__ == "__main__":
    main()
