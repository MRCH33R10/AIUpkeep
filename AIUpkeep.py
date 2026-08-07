#!/usr/bin/env python3
"""
AI File Sorter — uses a local Ollama model to read files, generate a
summary, pick a category, then sorts the files into category folders
and writes a summary log (CSV + per-file .summary.txt).

Requirements:
    pip install ollama python-docx PyPDF2

    Ollama must be running locally with a model pulled, e.g.:
        ollama pull llama3.2
    (Use a bigger model like llama3.1:8b or mistral for better summaries.)

Usage:
    python3 ai_file_sorter.py --source ~/Downloads --dest ~/Sorted --model llama3.2
    python3 ai_file_sorter.py --source ~/Downloads --dest ~/Sorted --dry-run
"""

import argparse
import csv
import json
import shutil
import sys
from pathlib import Path

import ollama

# ---------- text extraction ----------

def extract_text(path: Path, max_chars: int = 6000) -> str:
    """Pull readable text out of a file. Returns '' if we can't read it."""
    suffix = path.suffix.lower()

    try:
        if suffix in {".txt", ".md", ".csv", ".json", ".py", ".js", ".html",
                       ".css", ".yaml", ".yml", ".log"}:
            return path.read_text(errors="ignore")[:max_chars]

        if suffix == ".pdf":
            from PyPDF2 import PdfReader
            reader = PdfReader(str(path))
            text = ""
            for page in reader.pages[:10]:  # cap pages for speed
                text += page.extract_text() or ""
                if len(text) >= max_chars:
                    break
            return text[:max_chars]

        if suffix == ".docx":
            import docx
            doc = docx.Document(str(path))
            text = "\n".join(p.text for p in doc.paragraphs)
            return text[:max_chars]

        # Images: send to a vision model separately (see describe_image)
        if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            return ""  # handled separately

    except Exception as e:
        print(f"  [warn] couldn't extract text from {path.name}: {e}")

    return ""


def describe_image(path: Path, vision_model: str) -> str:
    """Use a vision-capable Ollama model (e.g. llava) to describe an image."""
    try:
        resp = ollama.chat(
            model=vision_model,
            messages=[{
                "role": "user",
                "content": "Describe this image's content in 2-3 sentences.",
                "images": [str(path)],
            }],
        )
        return resp["message"]["content"]
    except Exception as e:
        print(f"  [warn] vision model failed on {path.name}: {e}")
        return ""


# ---------- LLM classification ----------

CATEGORIES = [
    "Invoices & Receipts", "Contracts & Legal", "Reports & Presentations",
    "Code & Scripts", "Images & Media", "Personal Notes",
    "Correspondence & Emails", "Reference & Manuals", "Other",
]

PROMPT_TEMPLATE = """You are a file organizing assistant. Given a file name and
its extracted content, respond with STRICT JSON only, no extra text, in this
exact shape:

{{"category": "<one of: {categories}>", "summary": "<one sentence, max 25 words>"}}

Filename: {filename}
Content:
\"\"\"
{content}
\"\"\"
"""

def classify(model: str, filename: str, content: str) -> dict:
    prompt = PROMPT_TEMPLATE.format(
        categories=", ".join(CATEGORIES),
        filename=filename,
        content=content[:4000] if content else "(no extractable text)",
    )
    resp = ollama.chat(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        options={"temperature": 0.2},
    )
    raw = resp["message"]["content"].strip()

    # Models sometimes wrap JSON in ```json fences — strip those.
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[-1] if raw.lower().startswith("json") else raw

    try:
        data = json.loads(raw)
        if data.get("category") not in CATEGORIES:
            data["category"] = "Other"
        return data
    except json.JSONDecodeError:
        return {"category": "Other", "summary": raw[:150] or "Could not summarize."}


# ---------- main sort routine ----------

def sort_files(source: Path, dest: Path, model: str, vision_model: str,
                dry_run: bool):
    dest.mkdir(parents=True, exist_ok=True)
    log_path = dest / "sort_log.csv"
    rows = []

    files = [f for f in source.rglob("*") if f.is_file()]
    print(f"Found {len(files)} files in {source}\n")

    for i, f in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {f.name}")

        if f.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            content = describe_image(f, vision_model)
        else:
            content = extract_text(f)

        result = classify(model, f.name, content)
        category = result.get("category", "Other")
        summary = result.get("summary", "")

        target_dir = dest / category
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f.name

        print(f"  -> {category}: {summary}")

        if not dry_run:
            shutil.move(str(f), str(target_path))
            (target_dir / f"{f.stem}.summary.txt").write_text(summary)

        rows.append({
            "original_path": str(f),
            "new_path": str(target_path),
            "category": category,
            "summary": summary,
        })

    with open(log_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["original_path", "new_path",
                                                  "category", "summary"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nDone. Log written to {log_path}")
    if dry_run:
        print("(dry run — no files were actually moved)")


def prompt_path(message: str, must_exist: bool = False) -> Path:
    """Ask the user for a folder path on the command line, retrying on bad input."""
    while True:
        raw = input(message).strip().strip('"').strip("'")
        if not raw:
            print("  Please enter a path.")
            continue
        path = Path(raw).expanduser().resolve()
        if must_exist and not path.is_dir():
            print(f"  That folder doesn't exist: {path}")
            continue
        return path


def main():
    ap = argparse.ArgumentParser(description="AI file sorter using Ollama")
    ap.add_argument("--source", help="Folder to sort (skips the prompt if given)")
    ap.add_argument("--dest", help="Where sorted folders go (skips the prompt if given)")
    ap.add_argument("--model", default="llama3.2", help="Ollama text model")
    ap.add_argument("--vision-model", default="llava",
                     help="Ollama vision model for images")
    ap.add_argument("--dry-run", action="store_true",
                     help="Preview categorization without moving files")
    args = ap.parse_args()

    # If --source/--dest weren't passed on the command line, ask for them
    # interactively so the script can just be run with `python3 ai_file_sorter.py`.
    if args.source:
        source = Path(args.source).expanduser().resolve()
        if not source.is_dir():
            print(f"Source folder not found: {source}")
            sys.exit(1)
    else:
        source = prompt_path("Folder to sort (source): ", must_exist=True)

    if args.dest:
        dest = Path(args.dest).expanduser().resolve()
    else:
        default_dest = source.parent / f"{source.name}_sorted"
        raw_dest = input(f"Destination folder [{default_dest}]: ").strip()
        dest = Path(raw_dest).expanduser().resolve() if raw_dest else default_dest

    dry_run = args.dry_run
    if not args.dry_run:
        answer = input("Do a dry run first (no files moved)? [Y/n]: ").strip().lower()
        dry_run = answer in ("", "y", "yes")

    print(f"\nSource: {source}")
    print(f"Dest:   {dest}")
    print(f"Mode:   {'DRY RUN' if dry_run else 'LIVE (files will be moved)'}\n")

    sort_files(source, dest, args.model, args.vision_model, dry_run)


if __name__ == "__main__":
    main()
