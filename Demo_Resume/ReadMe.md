https://github.com/MRCH33R10/AIUpkeep.git

Local-first tooling for tracking job applications: checks whether tracked
postings are still live, keeps a single resume index in sync, and uses a
local Ollama model to retrieve relevant resume experience and to write
plain-English status summaries. No data leaves the machine — every LLM
call in this repo hits `localhost:11434`.

## Contents

| File | What it does |
|---|---|
| `AIUpkeep.py` | Adds new applications interactively (`add`), audits `Applications.xls` against the filesystem, checks each posting's `Link`, nudges you to check email on anything open 2+ weeks, and — after you confirm — deletes the resume + row for anything that's 404'd. |
| `resume_rag.py` | Real RAG: embeds resume bullets locally via Ollama, retrieves the ones most relevant to a given job description. |
| `resume_bullets.txt` | Starter resume bullets to embed — edit to match your actual resume. |
| `n8n_workflow.json` | An n8n workflow that runs `AIUpkeep.py` on a schedule and posts an Ollama-written summary. |
| `Resumes/Applications.xls` | A plain comma-separated text file (despite the `.xls` name), living inside `Resumes/` alongside the resume files it tracks. One row per tracked application: `Company`, `Job ID`, `Link`, `File Address`, `Date Applied`. The active-application count is just the row count, so there's no separate count file to keep in sync. |
| `Demo_Resumes/` | A worked example of the expected folder layout — a sample `Applications.xls` plus matching placeholder resume PDFs — so you can see how everything lines up before wiring in your own data. See [below](#demo_resumes--worked-example). |

---

## AIUpkeep.py — application status checker

```
pip install requests

python3 AIUpkeep.py add                                # interactively log a new application
python3 AIUpkeep.py                                     # audit, check, and clean up for real (asks before each deletion)
python3 AIUpkeep.py --dry-run                           # same checks, nothing deleted, written, or prompted
python3 AIUpkeep.py --yes                                # skip confirmations/reminders (for unattended/automated runs)
python3 AIUpkeep.py --json-summary /tmp/summary.json     # also write a JSON summary (for automation)
```

**Layout assumed** (override with `--aiupkeep-dir` / `--resume-dir`):

```
AIUpKeep/
    AIUpkeep.py
    Resumes/
        Applications.xls
        <resume files>
```

`Applications.xls` lives *inside* `Resumes/`, right alongside the resume
files it tracks — not in the same folder as `AIUpkeep.py`.

**Adding an application:** run `python3 AIUpkeep.py add`. It prompts for
`Company`, `Job ID`, and `Link` — the three things worth typing by hand —
plus the resume file name (just the filename if it's already in
`Resumes/`, or a full path otherwise). `Date Applied` is filled in for you
automatically from today's date; you never type it. Say `y` at "Add
another?" to log several in one sitting. If `Applications.xls` doesn't
exist yet, `add` creates it.

`Applications.xls` has five columns: `Company`, `Job ID`, `Link`,
`File Address`, and `Date Applied`. `Job ID` isn't used by the script —
it's there so you can find the posting again in your email (most ATS
confirmation emails reference it) without having to dig through the
`Link` URL. `File Address` can be a bare filename (resolved against the
`Resumes/` folder) or an absolute path. `Date Applied` (format
`YYYY-MM-DD`) is what powers the two-week reminder below — rows added by
hand should follow the same format, or just use `add` and never think
about it.

**Status check:** company-agnostic — a 404 on the `Link` means closed,
anything else (200, a resolved redirect, a network hiccup) is treated as
still open, so it never deletes on a guess. When a posting does come back
closed, AIUpkeep stops and asks you to confirm (showing the company, Job
ID, and link) before deleting anything, so you have a chance to check your
email first — a 404 tells you the listing is gone, not whether you got
rejected, ghosted, or something else. Say no and the row is left alone to
be checked again next run.

**Two-week reminder:** for anything still showing as open, if it's been
14+ days since `Date Applied`, you're stopped with a one-line nudge —
"It's been 23 days since you applied to X, worth checking your email" —
and a press-Enter-to-continue prompt. Nothing about the row changes; it's
just a reminder that an "open" posting on a careers page doesn't mean the
company hasn't already moved on without telling you.

Pass `--yes` to skip both the deletion confirmations and the reminder
prompts, and auto-remove closed postings — for unattended/scheduled runs
(like the n8n workflow below) where nobody's there to answer a prompt.
`--dry-run` also skips all prompts (nothing is written either way).

Accessed via templated, unique company web addresses and a subsequent ID —
e.g. Micron's posting search: `https://careers.micron.com/careers?start=0&pid=X&sort_by=hot`
where `X` is an 8-digit posting id.

The "index audit" step (Stage 1) is a plain filesystem consistency check —
not retrieval-augmented generation, despite an earlier draft calling it
that. Actual RAG lives in `resume_rag.py`, below.

### Demo_Resumes/ — worked example

```
Demo_Resumes/
    AIUpKeep/
        AIUpkeep.py
        Resumes/
            Applications.xls
            JordanEllis_NorthwindTraders_Resume.pdf
            JordanEllis_GlobexCorp_Resume.pdf
            JordanEllis_SolsticeDynamics_Resume.pdf
```

Fictional applicant, fictional companies, `example.com` links that
deliberately never resolve — so running the demo never actually deletes
anything, it just shows the plumbing. Two of the three sample rows have a
`Date Applied` more than two weeks back, so a live run will trigger the
check-your-email reminder for those. Try it:

```
cd Demo_Resumes/AIUpKeep
python3 AIUpkeep.py --dry-run     # see the audit + status check, no prompts
python3 AIUpkeep.py               # see the reminder prompts fire for real
python3 AIUpkeep.py add           # try logging a new sample application
```

To start using AIUpkeep for real, copy this layout, swap in your own
`Applications.xls` rows and resume files (or just run `add` to build it up
from scratch), and drop the `Demo_Resumes/` folder (or just leave it as a
reference).

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
