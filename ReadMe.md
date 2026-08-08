https://github.com/MRCH33R10/AIUpkeep.git

Local-first tooling for tracking job applications: checks whether tracked
postings are still live, keeps a resume index and count file in sync, and
uses a local Ollama model to retrieve relevant resume experience and to
write plain-English status summaries. No data leaves the machine — every
LLM call in this repo hits `localhost:11434`.

## Contents

| File | What it does |
|---|---|
| `AIUpkeep.py` | Audits `Index.xls` against the filesystem, checks each posting's `Link`, deletes the resume + row for anything that's 404'd, and rewrites `count.xls`. |
| `resume_rag.py` | Real RAG: embeds resume bullets locally via Ollama, retrieves the ones most relevant to a given job description. |
| `resume_bullets.txt` | Starter resume bullets to embed — edit to match your actual resume. |
| `n8n_workflow.json` | An n8n workflow that runs `AIUpkeep.py` on a schedule and posts an Ollama-written summary. |
| `Index.xls` / `count.xls` | Plain comma-separated text files (despite the `.xls` name) — the tracked-applications table and the active-application count. |

---

## AIUpkeep.py — application status checker

Checks status of logged applications; when a posting 404's, deletes the
resume file and its row, logs the number of applications remaining.

```
pip install requests

python3 AIUpkeep.py                 # audit, check, and clean up for real
python3 AIUpkeep.py --dry-run       # same checks, nothing deleted or written
python3 AIUpkeep.py --json-summary /tmp/summary.json   # also write a JSON summary (for automation)
```

**Layout assumed** (override with `--aiupkeep-dir` / `--resume-dir`):

```
AIUpKeep/
    AIUpkeep.py
    Index.xls
    count.xls
Resumes/
    <resume files>
```

**Status check:** company-agnostic — a 404 on the `Link` means closed,
anything else (200, a resolved redirect, a network hiccup) is treated as
still open, so it never deletes on a guess.

Accessed via templated, unique company web addresses and a subsequent ID —
e.g. Micron's posting search: `https://careers.micron.com/careers?start=0&pid=X&sort_by=hot`
where `X` is an 8-digit posting id.

The "index audit" step (Stage 1) is a plain filesystem consistency check —
not retrieval-augmented generation, despite an earlier draft calling it
that. Actual RAG lives in `resume_rag.py`, below.

---

## resume_rag.py — real RAG over your resume

Embeds resume bullets with a local Ollama embedding model, then retrieves
the bullets most relevant to a given job description by cosine similarity.
This is the actual RAG pattern: embed a corpus once, embed each query,
retrieve by vector similarity.

```
ollama pull nomic-embed-text
ollama serve                      # if it isn't already running
pip install requests

# 1. Build the index from resume_bullets.txt (one bullet per line)
python3 resume_rag.py build --bullets resume_bullets.txt --index resume_index.json

# 2. Retrieve the bullets most relevant to a posting
python3 resume_rag.py query --index resume_index.json --file job_posting.txt --top-k 5
python3 resume_rag.py query --index resume_index.json --text "paste a job description here"
```

Rebuild the index (`build`) whenever `resume_bullets.txt` changes. Querying
only embeds the one job description — it doesn't re-embed the whole resume
each time.

---

## n8n_workflow.json — scheduled automation

Orchestrates `AIUpkeep.py` as a local LLM workflow in n8n:

```
Weekly Schedule
      │
      ▼
Run AIUpkeep  (executes AIUpkeep.py, outputs its JSON summary)
      │
      ▼
Parse Summary JSON
      │
      ▼
Summarize with Ollama  (POST http://localhost:11434/api/generate)
      │
      ▼
Extract Summary Text
      │
      ▼
Log Summary  (appends to n8n_summary_log.md)
```

**To run it:**

1. Install n8n locally: `npx n8n` (or `npm install -g n8n && n8n`), then open the UI it prints (usually `http://localhost:5678`).
2. In n8n: **Workflows → Import from File** → select `n8n_workflow.json`.
3. Open the **Run AIUpkeep** and **Log Summary** nodes and update the hardcoded paths to match your machine.
4. Make sure Ollama is running (`ollama serve`) with a text model pulled (`ollama pull llama3.2`).
5. Toggle the workflow **Active**, or click **Execute Workflow** to run it once manually.

Swap the final **Log Summary** node for a Slack notification by replacing
it with an HTTP Request node posting to a Slack Incoming Webhook URL:
`POST` body `{"text": "={{$json.summary_text}}"}`.

The sticky note inside the workflow (visible when you open it in n8n)
repeats these setup notes.
