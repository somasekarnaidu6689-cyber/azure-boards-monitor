# EOD Task Monitor for Azure DevOps

Automated end-of-day pipeline that fetches active sprint Task work items from Azure DevOps,
runs a multi-agent analysis pipeline using Groq, and sends an HTML email report to team leads.

## Directory structure

```
eod-task-monitor/
├── .env                        # secrets and config (never commit)
├── .gitignore
├── requirements.txt
├── config.py                   # central config loaded from .env
├── main.py                     # entry point
├── fetcher/
│   ├── azure_client.py         # Azure DevOps REST API calls
│   └── board_fetcher.py        # assembles full task payload
├── agents/
│   ├── comment_analyst.py      # Agent 2: quality eval + copy-paste detection
│   ├── risk_scorer.py          # Agent 3: weighted risk score (0-100)
│   ├── nudge_writer.py         # Agent 4: personalized nudge via Groq
│   └── pipeline.py             # orchestrates agents over all tasks
├── mailer/
│   ├── template.html           # full report HTML template (sent to EMAIL_TO)
│   ├── task_template.html       # single-task reminder template (sent to assignee)
│   └── sender.py               # renders templates + sends via SMTP
└── utils/
    └── logger.py               # structured logging setup
```

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env .env.local              # fill in your values
```

## Configuration (.env)

| Variable | Description |
|---|---|
| AZURE_DEVOPS_ORG | Your Azure DevOps organization name |
| AZURE_DEVOPS_PROJECT | Project name |
| AZURE_DEVOPS_PAT | Personal Access Token (needs Work Items: Read) |
| AZURE_DEVOPS_TEAM | Team name (used for sprint iteration lookup) |
| GROQ_API_KEY | Groq API key |
| GROQ_MODEL | Model name (default: llama-3.3-70b-versatile) |
| SMTP_HOST | SMTP server hostname |
| SMTP_PORT | SMTP port (default: 587) |
| SMTP_USER | SMTP login username |
| SMTP_PASSWORD | SMTP login password or app password |
| EMAIL_FROM | Sender address |
| EMAIL_TO | Comma-separated list of recipient addresses |
| RUN_HOUR | Hour (UTC) to run in scheduler mode (default: 18) |
| RUN_MINUTE | Minute to run in scheduler mode (default: 0) |
| COMMENT_LOOKBACK_DAYS | How many days of comments to fetch (default: 3) |
| COPY_PASTE_THRESHOLD | Cosine similarity threshold for copy-paste (default: 0.92) |
| RISK_FLAG_THRESHOLD | Risk score above which a task is flagged (default: 60) |

## PAT permissions required

In Azure DevOps, your PAT needs:
- Work Items: Read

## Running

```bash
# Run once immediately
python main.py

# Run on a daily schedule at RUN_HOUR:RUN_MINUTE UTC
python main.py --schedule
```

## Scope (from use case document)

- Task work items only (Bugs, Epics, and Features are excluded). Tasks are discovered as CHILD items of User Story (or other PARENT_WORK_ITEM_TYPES) work items in the active sprint, linked via System.LinkTypes.Hierarchy-Forward.
- Active sprint only. Tasks from closed or future sprints are not included.
- Comments from the last 3 days are fetched per task.
- EOD comment check: flags any task with no comment added on the current day.
- Copy-paste detection: TF-IDF vectorisation + cosine similarity > 0.92 against the past few days' comments (lightweight, no torch/transformers dependency).
- Risk scoring uses six weighted signals (days since state change 25%, remaining hours unchanged 20%, comment quality 20%, copy-paste 15%, blocker detected 10%, days remaining in sprint 10%).
- Risk labels: 0-30 Healthy, 31-60 Watch, 61-80 At Risk, 81-100 Critical.
- A task is flagged if its risk score >= 60, a blocker is detected, no comment was added today, or comment quality < 5.

## How tasks are discovered (two-step lookup)

1. **Find parent items in the sprint** — a WIQL flat query selects work items of type `PARENT_WORK_ITEM_TYPES` (default: `["User Story"]`) whose `System.IterationPath` matches the active sprint.
2. **Find child tasks per parent** — for each parent ID, a `oneHop` WIQL query over `WorkItemLinks` with `System.LinkTypes.Hierarchy-Forward` returns the linked child work item IDs.
3. **Filter by type** — once full details are fetched for all child IDs, only items whose `System.WorkItemType` is in `ALLOWED_WORK_ITEM_TYPES` (default: `["Task"]`) proceed to comment analysis and risk scoring.

If your sprint structure differs (e.g. Tasks linked directly to Features, or a different parent type), update `PARENT_WORK_ITEM_TYPES` and `ALLOWED_WORK_ITEM_TYPES` in `config.py` accordingly.

## Notes on real Azure DevOps responses

- The active iteration may have `attributes.startDate` / `attributes.finishDate` set to `null` if sprint dates aren't configured in Project Settings > Iterations. In that case `days_remaining` defaults to 0 and the "days remaining in sprint" risk signal (10% weight) is skipped rather than treated as maximum urgency. Configure sprint dates for full accuracy.
- Comment text comes back from Azure as HTML (e.g. `<div>testing phase</div>`). `azure_client.py` strips tags and decodes entities before passing text to Groq or the TF-IDF vectoriser.
- If your board's primary work item type isn't literally "Task" (some templates use "User Story" for day-to-day work), update `ALLOWED_WORK_ITEM_TYPES` in `config.py`. If the pipeline logs "No work items of type ['Task'] found", this is the cause.

## Email delivery

Two kinds of emails are sent each run:

1. **Full report** — `send_report()` renders `template.html` (summary grid + all flagged/healthy tasks) and sends it to every address in `EMAIL_TO`.
2. **Individual task reminders** — `send_individual_task_emails()` renders `task_template.html` (one task only) and sends it directly to that task's assignee, using the email from Azure DevOps' `System.AssignedTo.uniqueName` field. A task is skipped if it has no assignee, or if the assignee's email is already in `EMAIL_TO` (to avoid sending the same person two emails).

## Risk label override for missing comments

A task with no EOD comment today always gets at least a "Watch" label, even if its weighted risk score would otherwise round to "Healthy" (0-30). This prevents a task with zero visibility into its status from appearing healthy in the report. The numeric `risk_score` itself is unchanged; only the displayed `risk_label` is adjusted.

## EOD comment quality guide

The AI evaluates each comment on three things: what was done, what is left, and any risks or blockers. A score of 7 or above (out of 10) is considered good enough to stop sending individual reminder emails for tasks in `EMAIL_QUALITY_GATE_STATE`.

### Example comment that passes the threshold

> Completed the API integration for the payment gateway module — handled success and failure response codes, added retry logic for timeout errors. Remaining work is writing unit tests for the edge cases (estimated 2–3 hours). No blockers currently. Will push the PR tomorrow morning.

This scores high because it covers every signal the evaluator checks: a specific description of what was done, a concrete remaining task with a time estimate, an explicit blocker status, and a clear next step with a timeline.

### Why other comments fail

| Comment | Score | Reason |
|---|---|---|
| "done" | 0–1 | Matches vague phrase list, no substance |
| "in progress" | 1–2 | No specifics on what or how much |
| "working on it, almost done" | 2–3 | No what, no how, no when |
| "Fixed the bug" | 3–5 | No context on which bug, what is left |
| "Completed most of the work, will finish tomorrow" | 5–6 | No specifics on what was completed or what remains |

The threshold is set at 7 and not 10 deliberately — the developer does not need to write an essay, they just need to give the team enough information to not require a follow-up question the next morning.

The initial version of the pipeline took approximately 2 minutes to process 4 tasks. After profiling and targeted fixes, the same run completes in 59 seconds — a 55.30% reduction in execution time (123.73% speed gain). The bottlenecks and what was done about each:

### Problem 1: sequential comment fetching

The original code fetched comments one task at a time inside a `for` loop. Each HTTP call to the Azure DevOps comments API takes 500ms–1.5s, so N tasks meant N × that wait time back to back.

Fix: `get_comments_for_all_tasks()` in `azure_client.py` uses `ThreadPoolExecutor` with up to 20 workers to fire all comment requests concurrently. All tasks' comments are fetched in roughly the time it takes to fetch one.

```
Before: 4 tasks × ~1.5s = 6s sequential
After:  4 tasks in parallel ≈ 1.5s total
```

### Problem 2: sequential oneHop child ID queries

For each parent User Story, a separate WIQL `oneHop` query was fired one after another to find child Task IDs.

Fix: `get_task_ids_in_sprint()` now submits all parent queries to a `ThreadPoolExecutor` simultaneously, collecting results as they complete.

### Problem 3: new TCP connection per API call

Every `requests.get()` / `requests.post()` call was creating a fresh HTTP connection, paying the TCP + TLS handshake cost (~50–100ms) every single time.

Fix: a single `requests.Session` is created once and reused for the entire run. The session keeps connections alive and reuses them across all Azure DevOps calls.

### Problem 4: one Databricks MERGE per row, across 6 tables

The original storage layer had 6 tables (4 dimension + 2 fact) and executed one `MERGE` SQL statement per task per table — 30+ round trips to Databricks just for 4 tasks. Each round trip adds network latency and warehouse startup overhead.

Fix: collapsed to a single flat table `TaskDailySnapshot` (one row per task per day, all fields in one place). The write is now two SQL statements regardless of task count: one `DELETE` to remove today's rows (idempotency), then one `executemany` INSERT that sends all rows in a single batch.

```
Before: 6 tables × 4 tasks × ~5s per MERGE = ~120s
After:  DELETE + executemany = 2 round trips ≈ 5–8s total
```

### Summary

| Optimisation | Technique | Where |
|---|---|---|
| Parallel comment fetch | `ThreadPoolExecutor`, 20 workers | `azure_client.py` |
| Parallel child ID fetch | `ThreadPoolExecutor`, per parent | `azure_client.py` |
| HTTP connection reuse | `requests.Session` shared across all calls | `azure_client.py` |
| Bulk Databricks write | Single `executemany` INSERT, 1 flat table | `storage/writer.py` |

Measured result on 4 tasks: **~130s → 59s (55% faster)**. The gains compound as task count grows — comment fetching and child ID queries scale O(1) with parallelism instead of O(N).