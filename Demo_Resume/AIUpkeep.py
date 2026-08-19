#!/usr/bin/env python3
"""
AIUpkeep — job application tracker upkeep.

Layout: AIUpkeep.py lives in the project root; Applications.xls and every
resume file it points to live together in a "Resumes" folder right next
to it:

    AIUpKeep/
        AIUpkeep.py
        Resumes/
            Applications.xls
            <resume files>

Two ways to run it:

  `python3 AIUpkeep.py add`  — interactively add a new application. Prompts
     for Company, Job ID, and Link (the things you'd otherwise have to type
     into a spreadsheet by hand), plus the resume file name. Date Applied
     is filled in automatically from today's date — you never type it.

  `python3 AIUpkeep.py`  — the three-stage maintenance run:

    1. INDEX AUDIT — every row in Applications.xls is checked against the
       actual filesystem. Any resume the index points to that doesn't
       exist gets flagged, and any resume sitting in the Resumes folder
       that no row points to gets flagged too.

    2. STATUS CHECK + CLEANUP — for every row still in the index, the job
       posting's Link is requested. A 404 means the posting is gone.
       Before anything is deleted, you're prompted to confirm each closed
       posting (Company + Job ID) so you can double-check your email/notes
       before the resume and row disappear. Any other HTTP response is
       treated as still open. This check is company-agnostic — it works
       the same way regardless of which ATS or careers site the Link
       points to.

       For applications still open, if it's been two weeks (or more) since
       Date Applied, you're nudged with a one-line reminder to go check
       your email for a response — a job can sit "open" on the careers
       page long after a company has quietly moved on.

    3. SUMMARY — a one-line summary is printed, derived straight from the
       number of rows left in Applications.xls, and optionally written as
       JSON via --json-summary, for automation tools to consume.

Note: the "index audit" step is a plain filesystem consistency check, not
retrieval-augmented generation — that naming was a leftover from an
earlier draft. For an actual RAG pipeline (embedding resume bullets and
retrieving the ones most relevant to a job posting), see resume_rag.py.
"""

import argparse
import csv
import json
import sys
from datetime import datetime, date, timezone
from pathlib import Path

import requests

USER_AGENT = "Mozilla/5.0"  # some ATS platforms block the default requests UA
REQUEST_TIMEOUT = 10
DATE_FORMAT = "%Y-%m-%d"
STALE_REMINDER_DAYS = 14  # nudge to check email once an open application is this old
SCRIPT_VERSION = "2.1"  # bump this on every change, so a stale local copy is obvious at a glance

# Applications.xls lives inside the Resumes folder, alongside the resume
# files it tracks. "Job ID" makes each row easy to find again in your
# email (most ATS confirmation emails include it). "Date Applied" is set
# automatically when a row is added via `add` — never typed by hand.
APP_FIELDS = ["Company", "Job ID", "Link", "File Address", "Date Applied"]


def resolve_resume_path(addr: str, resume_dir: Path) -> Path:
    """
    Resolve a File Address from Applications.xls. Absolute paths and ~ are
    used as-is; relative paths (e.g. a bare filename like 'foo.pdf') are
    resolved against the Resumes folder — where Applications.xls itself
    lives — not the current working directory, so this works the same
    whether you run the script in place or point --resume-dir at it from
    somewhere else.
    """
    path = Path(addr).expanduser()
    if not path.is_absolute():
        path = resume_dir / path
    return path


# ---------------------------------------------------------------------------
# Applications file I/O  (plain CSV, just wearing an .xls extension)
# ---------------------------------------------------------------------------

def load_applications(apps_path: Path) -> list[dict]:
    if not apps_path.exists():
        return []

    with open(apps_path, newline="") as fh:
        reader = csv.DictReader(fh, skipinitialspace=True)
        rows = []
        for raw in reader:
            row = {k.strip(): (v.strip() if v else "") for k, v in raw.items() if k}
            # backfill any column added after this row was written (e.g. an
            # older Applications.xls without "Date Applied" yet)
            for field in APP_FIELDS:
                row.setdefault(field, "")
            rows.append(row)
    return rows


def save_applications(apps_path: Path, rows: list[dict]):
    apps_path.parent.mkdir(parents=True, exist_ok=True)
    with open(apps_path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(APP_FIELDS)
        for row in rows:
            writer.writerow([row.get(f, "") for f in APP_FIELDS])


# ---------------------------------------------------------------------------
# `add` — interactively log a new application
# ---------------------------------------------------------------------------

def prompt_new_application() -> dict:
    """
    Prompts for exactly the information you'd otherwise have to remember to
    type into the spreadsheet yourself: Company, Job ID, and Link. File
    Address is also asked for so the script can later find/delete the
    matching resume. Date Applied is never asked — it's stamped
    automatically from today's date.
    """
    print("  Add a new application (leave Job ID blank if the posting doesn't have one).")
    company = input("  Company: ").strip()
    job_id = input("  Job ID: ").strip()
    link = input("  Job posting Link: ").strip()
    file_address = input("  Resume file name (in Resumes/), or a full path: ").strip()
    return {
        "Company": company,
        "Job ID": job_id,
        "Link": link,
        "File Address": file_address,
        "Date Applied": date.today().strftime(DATE_FORMAT),
    }


def run_add(apps_path: Path):
    rows = load_applications(apps_path)
    added = 0

    while True:
        row = prompt_new_application()
        if not row["Company"] and not row["Link"]:
            print("  [skip] no company or link entered — not adding an empty row.\n")
        else:
            rows.append(row)
            added += 1
            print(f"  -> logged: {row['Company']} (Job ID: {row['Job ID'] or '—'}), "
                  f"applied {row['Date Applied']}\n")

        again = input("  Add another? [y/N]: ").strip().lower()
        if again not in ("y", "yes"):
            break

    if added:
        save_applications(apps_path, rows)
        print(f"\n{added} application(s) added. Applications.xls now has {len(rows)} row(s).")
    else:
        print("\nNothing added.")


# ---------------------------------------------------------------------------
# Stage 1 — index audit: Applications.xls vs. filesystem
# ---------------------------------------------------------------------------

def index_audit(rows: list[dict], resume_dir: Path, apps_path: Path):
    print("=== Stage 1: index audit (Applications.xls vs. filesystem) ===")

    indexed_paths = set()
    missing = []
    for row in rows:
        addr = row.get("File Address", "")
        if not addr:
            continue
        path = resolve_resume_path(addr, resume_dir)
        indexed_paths.add(str(path.resolve()) if path.exists() else str(path))
        if not path.exists():
            missing.append(row)

    if missing:
        print(f"  [!] {len(missing)} row(s) point to a resume that no longer exists:")
        for row in missing:
            print(f"      - {row.get('Company', '?')} (Job ID: {row.get('Job ID', '?')}): {row.get('File Address', '?')}")
    else:
        print("  All indexed resumes are present on disk.")

    orphans = []
    if resume_dir.is_dir():
        apps_path_resolved = apps_path.resolve()
        for f in resume_dir.iterdir():
            if not f.is_file():
                continue
            if f.resolve() == apps_path_resolved:
                continue  # Applications.xls itself lives alongside the resumes
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


def confirm_removal(row: dict) -> bool:
    """
    Interactive checkpoint before anything is deleted. Gives you a chance
    to check your email for that Job ID before the row/resume disappear —
    a 404 means the posting is gone, but it doesn't tell you whether you
    got rejected, ghosted, or something else, so this is your moment to
    go verify before AIUpkeep clears it out.
    """
    company = row.get("Company", "?")
    job_id = row.get("Job ID", "") or "(none on file)"
    link = row.get("Link", "")
    prompt = (
        f"      [confirm] Posting looks CLOSED — {company} (Job ID: {job_id})\n"
        f"                {link}\n"
        f"                Check your email for this one, then confirm: "
        f"delete resume + remove row? [y/N]: "
    )
    answer = input(prompt).strip().lower()
    return answer in ("y", "yes")


def days_since_applied(row: dict, today: date):
    date_str = row.get("Date Applied", "")
    if not date_str:
        return None
    try:
        applied = datetime.strptime(date_str, DATE_FORMAT).date()
    except ValueError:
        return None
    return (today - applied).days


def maybe_remind_stale(row: dict, today: date):
    """
    For an application that's still open: if it's been two weeks or more
    since Date Applied, nudge the user to go check their email. This is
    purely informational — an Enter keypress dismisses it, nothing about
    the row changes.
    """
    days = days_since_applied(row, today)
    if days is None or days < STALE_REMINDER_DAYS:
        return

    company = row.get("Company", "?")
    job_id = row.get("Job ID", "") or "(none on file)"
    applied_on = row.get("Date Applied", "?")
    input(
        f"      [reminder] It's been {days} days since you applied to {company} "
        f"(Job ID: {job_id}) on {applied_on}. Worth checking your email for a "
        f"response. Press Enter to continue: "
    )


# ---------------------------------------------------------------------------
# Stage 2 continued — apply cleanup
# ---------------------------------------------------------------------------

def check_and_clean(rows: list[dict], dry_run: bool, assume_yes: bool, resume_dir: Path) -> tuple[list[dict], int]:
    print("=== Stage 2: checking posting status ===")
    kept = []
    removed = 0
    today = date.today()
    # --yes and --dry-run both imply "don't block on interactive input"
    interactive = not dry_run and not assume_yes

    for row in rows:
        company = row.get("Company", "?")
        job_id = row.get("Job ID", "")
        link = row.get("Link", "")
        addr = row.get("File Address", "")
        label = f"{company} (Job ID: {job_id})" if job_id else company
        print(f"  {label}: {link}")

        if not link:
            print("      [warn] no link on this row — keeping, can't verify status")
            kept.append(row)
            continue

        status = check_job_status(link)

        if status == "closed":
            if dry_run:
                print("      -> CLOSED. Would prompt for confirmation (dry run — nothing removed).")
                kept.append(row)
                continue

            if not assume_yes:
                if not confirm_removal(row):
                    print("      -> kept — not confirmed, will check again next run.")
                    kept.append(row)
                    continue

            print("      -> CLOSED and confirmed. Removing row" + (" and deleting resume" if addr else ""))
            if addr:
                path = resolve_resume_path(addr, resume_dir)
                if path.exists():
                    path.unlink()
                    print(f"      -> deleted: {path}")
                else:
                    print(f"      -> resume already missing, nothing to delete: {path}")
            removed += 1
            # row dropped, not appended to kept
        else:
            print("      -> still open. Keeping.")
            if interactive:
                maybe_remind_stale(row, today)
            kept.append(row)

    print()
    return kept, removed


def write_json_summary(json_path: Path, *, checked: int, active: int, removed: int,
                        missing: list[dict], orphans: list, dry_run: bool):
    """
    Writes a machine-readable summary alongside the human-readable console output —
    this is what an automation tool (n8n, cron + curl, etc.) reads instead of
    scraping stdout.
    """
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "checked": checked,
        "active": active,
        "closed_removed": removed,
        "missing_resumes": [row.get("File Address", "") for row in missing],
        "orphan_resumes": [str(f) for f in orphans],
    }
    json_path.write_text(json.dumps(payload, indent=2))
    print(f"  JSON summary written: {json_path}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Audit, verify, and clean up the job application tracker")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "add"],
                     help="'run' (default): audit + status check + cleanup. "
                          "'add': interactively log a new application.")
    ap.add_argument("--aiupkeep-dir", default=None,
                     help="Folder containing AIUpkeep.py, i.e. the project root "
                          "(default: the folder this script itself is in, "
                          "regardless of your current working directory)")
    ap.add_argument("--resume-dir", default=None,
                     help="Folder containing both Applications.xls and the resume files "
                          "(default: 'Resumes' inside --aiupkeep-dir)")
    ap.add_argument("--dry-run", action="store_true",
                     help="Report everything without deleting files, writing changes, or prompting")
    ap.add_argument("--yes", "-y", action="store_true",
                     help="Skip interactive confirmations/reminders and auto-remove closed postings "
                          "(use for unattended/automated runs; not recommended otherwise)")
    ap.add_argument("--json-summary", default=None,
                     help="Path to also write a machine-readable JSON summary "
                          "(for automation tools like n8n, cron scripts, etc.)")
    args = ap.parse_args()

    aiupkeep_dir = (Path(args.aiupkeep_dir).expanduser().resolve() if args.aiupkeep_dir
                     else Path(__file__).resolve().parent)
    resume_dir = (Path(args.resume_dir).expanduser().resolve() if args.resume_dir
                  else (aiupkeep_dir / "Resumes").resolve())
    apps_path = resume_dir / "Applications.xls"

    if args.command == "add":
        print(f"AIUpkeep v{SCRIPT_VERSION}")
        print(f"Applications file: {apps_path}\n")
        run_add(apps_path)
        return

    print(f"AIUpkeep v{SCRIPT_VERSION}")
    print(f"AIUpKeep dir:      {aiupkeep_dir}")
    print(f"Resume dir:        {resume_dir}")
    print(f"Applications file: {apps_path}")
    print(f"Mode:              {'DRY RUN (nothing will be deleted, written, or prompted)' if args.dry_run else ('LIVE, auto-confirm' if args.yes else 'LIVE, interactive confirmation')}\n")

    if not apps_path.exists():
        print(f"[error] Applications file not found: {apps_path}")
        print(f"        Run 'python3 AIUpkeep.py add' to create it and log your first application.")
        sys.exit(1)

    rows = load_applications(apps_path)
    checked = len(rows)

    # Stage 1
    missing, orphans = index_audit(rows, resume_dir, apps_path)

    # Stage 2
    kept_rows, removed_count = check_and_clean(rows, args.dry_run, args.yes, resume_dir)

    if not args.dry_run:
        save_applications(apps_path, kept_rows)
    else:
        print(f"  (dry run — would remove {removed_count} row(s), "
              f"leaving {len(kept_rows)}; Applications.xls left untouched)\n")

    # Stage 3 — summary, straight from the row count (no separate count file)
    final_count = len(kept_rows)
    print("=== Stage 3: summary ===")
    print(f"  Applications currently tracked as active: {final_count}")
    if removed_count:
        print(f"  ({removed_count} closed application(s) cleaned up this run)")

    if args.json_summary:
        write_json_summary(
            Path(args.json_summary).expanduser().resolve(),
            checked=checked,
            active=final_count,
            removed=removed_count,
            missing=missing,
            orphans=orphans,
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    main()
