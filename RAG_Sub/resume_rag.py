#!/usr/bin/env python3
"""
resume_rag.py — a small, real Retrieval-Augmented Generation pipeline over your
resume, using a local Ollama embedding model.

This is not the "RAG audit" naming that used to live in AIUpkeep.py (that was a
plain filesystem check with a borrowed name). This is the actual pattern:
embed a corpus once, embed each query, retrieve by vector similarity — fully
local, nothing leaves your machine.

Setup:
    ollama pull nomic-embed-text
    ollama serve                       # if it isn't already running
    pip install requests

Usage:
    # 1. Put one resume bullet per line in a text file, then build the index:
    python3 resume_rag.py build --bullets resume_bullets.txt --index resume_index.json

    # 2. Retrieve the bullets most relevant to a job posting:
    python3 resume_rag.py query --index resume_index.json --text "paste a job description here"
    python3 resume_rag.py query --index resume_index.json --file job_posting.txt --top-k 5

Rebuild the index (`build`) any time resume_bullets.txt changes. Query as
often as you like — querying only embeds the one job description, it doesn't
re-embed the resume.
"""

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Optional

import requests

OLLAMA_EMBED_URL = "http://localhost:11434/api/embeddings"
DEFAULT_MODEL = "nomic-embed-text"
REQUEST_TIMEOUT = 30


def embed(text: str, model: str = DEFAULT_MODEL) -> list[float]:
    """Get an embedding vector for a piece of text from a local Ollama server."""
    try:
        resp = requests.post(
            OLLAMA_EMBED_URL,
            json={"model": model, "prompt": text},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[error] couldn't reach Ollama at {OLLAMA_EMBED_URL}: {e}")
        print(f"        is Ollama running (`ollama serve`)? is the model pulled (`ollama pull {model}`)?")
        sys.exit(1)

    data = resp.json()
    if "embedding" not in data:
        print(f"[error] unexpected response from Ollama: {data}")
        sys.exit(1)
    return data["embedding"]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# build — embed the resume once, save the index
# ---------------------------------------------------------------------------

def build_index(bullets_path: Path, index_path: Path, model: str):
    if not bullets_path.exists():
        print(f"[error] bullets file not found: {bullets_path}")
        sys.exit(1)

    lines = [ln.strip() for ln in bullets_path.read_text().splitlines() if ln.strip()]
    if not lines:
        print(f"[error] no bullets found in {bullets_path} (one per line, blank lines ignored)")
        sys.exit(1)

    print(f"Embedding {len(lines)} bullet(s) with '{model}'...")
    entries = []
    for i, bullet in enumerate(lines, 1):
        vec = embed(bullet, model)
        entries.append({"text": bullet, "embedding": vec})
        preview = bullet if len(bullet) <= 70 else bullet[:67] + "..."
        print(f"  [{i}/{len(lines)}] {preview}")

    index_path.write_text(json.dumps({"model": model, "entries": entries}, indent=2))
    print(f"\nSaved index: {index_path}  ({len(entries)} bullets, model={model})")


# ---------------------------------------------------------------------------
# query — embed the job description, retrieve top-k by similarity
# ---------------------------------------------------------------------------

def load_index(index_path: Path) -> dict:
    if not index_path.exists():
        print(f"[error] index not found: {index_path}  (run the 'build' command first)")
        sys.exit(1)
    return json.loads(index_path.read_text())


def query_index(index_path: Path, query_text: str, top_k: int, model: Optional[str] = None):
    data = load_index(index_path)
    entries = data.get("entries", [])
    embed_model = model or data.get("model", DEFAULT_MODEL)

    if not entries:
        print("[error] index has no entries — rebuild it with the 'build' command")
        sys.exit(1)

    query_vec = embed(query_text, embed_model)

    scored = [(cosine_similarity(query_vec, e["embedding"]), e["text"]) for e in entries]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[:top_k]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Real RAG over resume bullets, via local Ollama embeddings")
    sub = ap.add_subparsers(dest="command", required=True)

    build_p = sub.add_parser("build", help="Embed resume bullets and save an index")
    build_p.add_argument("--bullets", required=True, help="Text file, one resume bullet per line")
    build_p.add_argument("--index", default="resume_index.json", help="Where to save the embedding index")
    build_p.add_argument("--model", default=DEFAULT_MODEL, help="Ollama embedding model")

    query_p = sub.add_parser("query", help="Retrieve the bullets most relevant to a job posting")
    query_p.add_argument("--index", default="resume_index.json", help="Embedding index to search")
    group = query_p.add_mutually_exclusive_group(required=True)
    group.add_argument("--text", help="Job description text, inline")
    group.add_argument("--file", help="Path to a text file containing the job description")
    query_p.add_argument("--top-k", type=int, default=5, help="Number of bullets to return")
    query_p.add_argument("--json", action="store_true", help="Print results as JSON instead of text")

    args = ap.parse_args()

    if args.command == "build":
        build_index(Path(args.bullets), Path(args.index), args.model)

    elif args.command == "query":
        query_text = args.text if args.text else Path(args.file).read_text()
        results = query_index(Path(args.index), query_text, args.top_k)

        if args.json:
            print(json.dumps([{"score": score, "text": text} for score, text in results], indent=2))
        else:
            print(f"Top {len(results)} matching bullet(s):\n")
            for score, text in results:
                print(f"  [{score:.3f}] {text}")


if __name__ == "__main__":
    main()
