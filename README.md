# EOD Task Monitor for Azure DevOps

Automated end-of-day pipeline that fetches active sprint Task work items from Azure DevOps,
runs a multi-agent analysis pipeline using Groq, and sends an HTML email report to team leads.

## Directory structure

```
eod-task-monitor/
├── .env                        # secrets and config (never commit)
├── .env.example                # template with every variable documented
├── .gitignore
├── .pre-commit-config.yaml     # detect-secrets hook
├── .secrets.baseline           # detect-secrets baseline
├── .github/workflows/ci.yml    # test + secret-scan on every push/PR
├── pytest.ini
├── requirements.txt
├── requirements-dev.txt        # adds pytest, pre-commit, etc.
├── config.py                   # central config loaded from .env / Key Vault
├── main.py                     # entry point
├── fetcher/
│   ├── azure_client.py         # Azure DevOps REST API calls (PAT or service principal, retried)
│   └── board_fetcher.py        # assembles full task payload
├── agents/
│   ├── comment_analyst.py      # Agent 2: quality eval + copy-paste detection (JSON-validated, retried)
│   ├── risk_scorer.py          # Agent 3: weighted risk score (0-100)
│   ├── nudge_writer.py         # Agent 4: personalized nudge via Groq (JSON-validated, retried)
│   └── pipeline.py             # orchestrates agents over all tasks, concurrently
├── mailer/
│   ├── template.html           # full report HTML template (sent to EMAIL_TO)
│   ├── task_template.html       # single-task reminder template (sent to assignee)
│   ├── sender.py                # renders templates + sends via SMTP/SendGrid/ACS
│   └── teams.py                 # Agent 5 (delivery): direct Teams webhook notification
├── storage/
│   ├── databricks_client.py     # connection + retried execute()
│   ├── schema.py                 # TaskDailySnapshot, RunMetrics, CommentHistory DDL
│   ├── writer.py                  # writes snapshot rows + delivery status
│   ├── metrics.py                 # RunMetrics dataclass + write_run_metrics()
│   └── comment_history.py         # persists/loads per-assignee comment history
├── utils/
│   ├── logger.py                # structured logging setup
│   ├── retry.py                  # shared exponential-backoff retry decorators
│   └── secrets.py                 # Key Vault-aware secret resolution
└── tests/
    ├── conftest.py
    ├── test_risk_scorer.py
    ├── test_comment_analyst.py
    └── test_pipeline.py
```

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env .env.local              # fill in your values
```

## Configuration (.env)

See `.env.example` for the full, grouped, inline-commented list. Key new
groups (full detail in "Gap-analysis fixes" below):

### Secrets & Key Vault

| Variable | Description |
|---|---|
| KEY_VAULT_URL | Optional. If set, secrets resolve from this Azure Key Vault first, falling back to `.env`. |

### Authentication

| Variable | Description |
|---|---|
| AZURE_AUTH_MODE | `pat` (default) or `service_principal` |
| AZURE_DEVOPS_SP_TENANT_ID / _CLIENT_ID / _CLIENT_SECRET | Used when AZURE_AUTH_MODE=service_principal |

### Azure DevOps

| Variable | Description |
|---|---|
| AZURE_DEVOPS_ORG | Your Azure DevOps organization name |
| AZURE_DEVOPS_PROJECT | Project name (URL-encoded, e.g. Task%20Monitor) |
| AZURE_DEVOPS_PAT | Personal Access Token (needs Work Items: Read). Used when AZURE_AUTH_MODE=pat |
| AZURE_DEVOPS_TEAM | Team name (URL-encoded, used for sprint iteration lookup) |
| TEAMS_CONFIG | Optional JSON list to process multiple teams in one run (see "Multi-team Support" below) |

### Groq

| Variable | Description |
|---|---|
| GROQ_API_KEY | Groq API key |
| GROQ_MODEL | Model name (default: llama-3.3-70b-versatile) |

### Email delivery

| Variable | Description |
|---|---|
| EMAIL_PROVIDER | `smtp` (default), `sendgrid`, or `acs` |
| SMTP_HOST / _PORT / _USER / _PASSWORD | Used when EMAIL_PROVIDER=smtp |
| SENDGRID_API_KEY | Used when EMAIL_PROVIDER=sendgrid |
| AZURE_COMMUNICATION_CONNECTION_STRING | Used when EMAIL_PROVIDER=acs |
| EMAIL_FROM | Sender address (must match SMTP_USER on most relays) |
| EMAIL_TO | Comma-separated list of addresses for the full report |
| EMAIL_NOTIFY_STATES | Comma-separated task states that trigger any email (e.g. Active) |
| EMAIL_QUALITY_GATE_STATE | Single state where email is only sent if comment quality is insufficient (e.g. Closed). Tasks in this state that have ever produced a valid good comment are permanently suppressed. |
| EMAIL_GOOD_QUALITY_THRESHOLD | Quality score (0-10) at or above which individual emails stop (default: 7). Applies only to EMAIL_QUALITY_GATE_STATE tasks. |

### Teams integration

| Variable | Description |
|---|---|
| TEAMS_WEBHOOK_URL | Optional Incoming Webhook URL. If unset, Teams delivery is skipped. |

### Databricks

| Variable | Description |
|---|---|
| DATABRICKS_SERVER_HOSTNAME | Databricks workspace hostname (e.g. dbc-xxxx.cloud.databricks.com) |
| DATABRICKS_HTTP_PATH | SQL warehouse HTTP path (/sql/1.0/warehouses/...) |
| DATABRICKS_ACCESS_TOKEN | Databricks personal access token |
| DATABRICKS_CATALOG | Catalog name (default: hive_metastore) |
| DATABRICKS_SCHEMA | Schema name (default: eod_task_monitor) |

### Scheduling

| Variable | Description |
|---|---|
| RUN_HOUR / RUN_MINUTE | Used only in `--schedule` (local/fallback) mode |
| SKIP_WEEKENDS | Skip Sat/Sun (default: true) |
| HOLIDAYS | Comma-separated ISO dates to also skip |
| HOLIDAY_COUNTRY | Optional country code (e.g. US) to skip public holidays via the `holidays` package |

### Business rules / scalability / observability

| Variable | Description |
|---|---|
| COMMENT_LOOKBACK_DAYS | How many days of comments to fetch per task (default: 3) |
| COPY_PASTE_THRESHOLD | Cosine similarity threshold for copy-paste detection (default: 0.92) |
| RISK_FLAG_THRESHOLD | Risk score above which a task is flagged (default: 60) |
| PERSIST_COMMENT_HISTORY | Widen copy-paste corpus with persisted history (default: true) |
| COMMENT_HISTORY_MAX_PER_ASSIGNEE | Cap on historical comments loaded per assignee (default: 60) |
| MAX_CONCURRENT_TASK_WORKERS | Concurrent Groq workers in the pipeline (default: 8) |
| TASK_COUNT_WARNING_THRESHOLD | Logs a warning above this many tasks in one run (default: 50) |
| RUN_DEADLINE_MINUTES | Used by dashboards to alert on a run that never finished (default: 30) |
| METRICS_ALERT_ON_FAILURE | Post a Teams alert if a run fails (default: true) |

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
---

## Gap-analysis fixes

The sections below document every change made in response to the EOD Task
Monitor gap analysis. Each maps to a severity-rated gap; code-level fixes
are described here, and the matching deployment steps are in the
"Deployment runbook" section at the end of this document.

### Secrets & Key Vault (High)

Secrets (`AZURE_DEVOPS_PAT`, `DATABRICKS_ACCESS_TOKEN`, `GROQ_API_KEY`,
`SMTP_PASSWORD`, `SENDGRID_API_KEY`, `AZURE_COMMUNICATION_CONNECTION_STRING`,
`TEAMS_WEBHOOK_URL`) now resolve through `utils/secrets.py`, which checks
Azure Key Vault first (when `KEY_VAULT_URL` is set) and falls back to
`.env` / process environment otherwise — fully backward compatible.

A `.pre-commit-config.yaml` + `detect-secrets` baseline (`.secrets.baseline`)
guard against committing real credentials by accident, and the same scan
runs in CI (`.github/workflows/ci.yml`).

### Authentication (High)

`AZURE_AUTH_MODE` selects between the legacy PAT (`pat`, default) and a
service principal / managed identity (`service_principal`), via
`azure-identity`'s `ClientSecretCredential`. `fetcher/azure_client.py`
caches and auto-refreshes the AAD token. `validate_auth()` runs as Step 0
of every pipeline run and fails fast with a clear message if the
credential is invalid, rather than discovering it deep into a run.

### Error Handling & Retry (High)

`utils/retry.py` provides a shared exponential-backoff-with-jitter policy
(4 attempts, 1–20s wait), applied to every external call: Azure DevOps
REST (`_get`/`_post`), Databricks connect/execute, Groq calls (both
agents), SMTP/SendGrid/ACS sends, and the Teams webhook. HTTP retries only
trigger on 429/5xx/connection/timeout — never on 4xx client errors like
401/404, which won't be fixed by retrying.

### Observability (High)

`storage/metrics.py`'s `RunMetrics` dataclass tracks tasks processed, Groq
calls made/failed, emails sent/failed, and Teams notifications per run,
written to the new `RunMetrics` Databricks table at the end of every run
(success or failure). `main.py` posts a Teams alert
(`mailer/teams.send_teams_alert`) immediately if a team's run fails, when
`METRICS_ALERT_ON_FAILURE=true`. Dashboards/alerting can also watch for
rows where `finished_at IS NULL` and `started_at` is older than
`RUN_DEADLINE_MINUTES` — a run that started but never finished.

### Scheduling & Deployment (High)

The in-process `schedule` loop in `main.py --schedule` is now explicitly
documented as **local/fallback mode only**. `main.py` (no flag) runs the
pipeline once and exits cleanly — designed to be invoked by an external
trigger (Azure Functions timer trigger or Databricks Workflow — see
"Deployment runbook" below). `_is_working_day()` now skips weekends
(`SKIP_WEEKENDS`), an explicit `HOLIDAYS` list, and optionally a country's
public holidays (`HOLIDAY_COUNTRY`, via the `holidays` package) — so the
report and nudges no longer fire on non-working days. `--force` bypasses
this for manual backfills/reruns.

### Agent Architecture / Concurrency (Med)

`agents/pipeline.py` now runs Agents 2+3 (comment analysis + risk scoring)
for all tasks needing analysis concurrently via `ThreadPoolExecutor`, up to
`MAX_CONCURRENT_TASK_WORKERS` (default 8) at a time, instead of strictly
sequentially. One task's failure is caught and substituted with a degraded
result without aborting the others. This was also where a real bug was
fixed: the old code called `fetch_latest_snapshot(gate_task_ids)` a second
time, unconditionally, immediately after the first conditional call —
silently discarding the real lookup and doubling a Databricks round trip
for no benefit. It's been removed.

### LLM Reliability (Med)

Both `agents/comment_analyst.py` and `agents/nudge_writer.py` now validate
every Groq response against an explicit JSON Schema (`jsonschema`). On a
schema-validation failure, exactly one correction attempt is made — the
malformed output is fed back to the model with an instruction to fix it —
before falling back to a clearly labeled degraded state
(`quality_label="skipped"` / template-based nudge), rather than silently
guessing a fixed score. Transient Groq API/network errors (rate limits,
5xx, timeouts) are retried separately via `utils.retry.retryable_any_exception`,
since retrying an already-malformed prompt verbatim wouldn't help.

### Data Quality / Copy-paste detection (Med)

`storage/comment_history.py` persists each day's comment text per assignee
to a new `CommentHistory` Databricks table, and loads up to
`COMMENT_HISTORY_MAX_PER_ASSIGNEE` (default 60) recent historical comments
per assignee at the start of each run. `detect_copy_paste()` now compares
today's comment against this wider corpus, not just the current
`COMMENT_LOOKBACK_DAYS` window — catching boilerplate repeated across
weeks, not just within the lookback. Set `PERSIST_COMMENT_HISTORY=false`
to disable and revert to lookback-window-only comparison. Failures degrade
to a no-op (lookback-window-only) rather than blocking the run.

### Email Delivery (Med)

`EMAIL_PROVIDER` selects the transport: `smtp` (unchanged default),
`sendgrid`, or `acs` (Azure Communication Services) — both alternatives
give delivery receipts/bounce handling that raw Gmail SMTP cannot.
`mailer/sender.py` now returns a delivery status (`sent`/`skipped`/`failed`)
from every send instead of raising on failure, so one recipient's bounce
never aborts the run. Per-task delivery status is persisted to
`TaskDailySnapshot.report_email_status` / `individual_email_status` /
`teams_notification_status` via `storage/writer.update_delivery_status()`.

### Teams Integration (Med)

`mailer/teams.py` replaces the previous Office Script approach with a
direct POST from Python to a Teams Incoming Webhook, using the Adaptive
Card format — making Teams delivery a first-class, retried, observable
pipeline step instead of an external dependency with no documented
trigger. If `TEAMS_WEBHOOK_URL` is unset, Teams delivery is simply skipped
(status `disabled`) and email-only delivery continues unaffected.

### Testing (Med)

`tests/` adds pytest coverage for the previously-untested core logic:
- `test_risk_scorer.py` — pure-math invariants (bounds, monotonicity, the
  "missing comment is never Healthy" override).
- `test_comment_analyst.py` — copy-paste detection, and the JSON
  schema-validation / retry-with-correction / degraded-state paths, all
  with a mocked Groq client (no real API calls).
- `test_pipeline.py` — state-skip logic, the quality-gate suppression
  path (which also regression-guards the duplicate-fetch bug fix above),
  and that one task's failure doesn't abort others.

Run with `pytest` (after `pip install -r requirements-dev.txt`).
`.github/workflows/ci.yml` runs the suite plus a `detect-secrets` scan on
every push/PR.

### Scope clarity — Agents 1 & 5 (Low)

For consistency with the original 5-agent design: **Agent 1** is the
fetcher layer (`fetcher/azure_client.py` + `board_fetcher.py` — sprint +
task + comment retrieval). **Agent 5** is the delivery layer
(`mailer/sender.py` + `mailer/teams.py` — email and Teams notification).
Agents 2–4 (comment analyst, risk scorer, nudge writer) were already named
consistently; this section exists so the numbering is documented in one
place rather than only implied by file layout.

### Scalability (Low)

`MAX_CONCURRENT_TASK_WORKERS` (default 8) caps Groq concurrency safely
under typical free-tier requests-per-minute limits — raise it only after
checking your Groq plan's rate limit. `TASK_COUNT_WARNING_THRESHOLD`
(default 50) logs a warning (not a hard stop) if a single run has more
tasks than the pipeline was originally benchmarked against, as a heads-up
to watch Groq rate limits and Databricks write latency as a team's sprint
grows.

### Multi-team Support (Low)

`Config.teams()` returns a list of teams to process in one run, built from
the optional `TEAMS_CONFIG` JSON env var (list of `{name, azure_org,
azure_project, azure_team}` objects, each overriding only what differs
from the top-level `AZURE_DEVOPS_*` vars). If `TEAMS_CONFIG` is unset, it
returns a single synthetic team built from the existing vars — fully
backward compatible. `main.py` loops over `Config.teams()`, running the
full pipeline (including its own `RunMetrics` row and Teams alert on
failure) independently per team, and `TaskDailySnapshot.azure_team` /
`RunMetrics.azure_team` track which team each row belongs to.

### Documentation (Low)

This README section, `.env.example` (every variable now documented with
inline comments grouped by concern), and the "Deployment runbook" below
are the documentation-gap fix.

---

## Deployment runbook

This section covers the infrastructure/deployment side of the gaps above —
steps that can't be fully completed by editing code in this repo, since
they involve creating Azure resources and choosing how this pipeline gets
invoked in your environment.

### 1. Key Vault setup (Security & Secrets)

1. Create a Key Vault: `az keyvault create --name <vault-name> --resource-group <rg> --location <region>`
2. For each secret, store it under the env var name lowercased with
   underscores replaced by hyphens, e.g.:
   ```bash
   az keyvault secret set --vault-name <vault-name> --name azure-devops-pat --value "<pat>"
   az keyvault secret set --vault-name <vault-name> --name databricks-access-token --value "<token>"
   az keyvault secret set --vault-name <vault-name> --name groq-api-key --value "<key>"
   az keyvault secret set --vault-name <vault-name> --name smtp-password --value "<app-password>"
   az keyvault secret set --vault-name <vault-name> --name teams-webhook-url --value "<webhook-url>"
   ```
3. Grant whatever identity runs the pipeline (see step 3 below) the
   **Key Vault Secrets User** RBAC role on the vault (or an access policy
   with get/list permissions, if your vault uses the legacy model).
4. Set `KEY_VAULT_URL=https://<vault-name>.vault.azure.net/` in the
   deployment environment (an Azure Function app setting / Databricks
   Workflow parameter — **not** committed to `.env`).
5. Set a calendar reminder (or use Key Vault's own expiry-on-secret +
   Azure Monitor alert) to rotate the PAT/tokens periodically — Key Vault
   does not auto-rotate secrets you set manually; rotation is still a
   process you own, this just centralizes where the values live.

### 2. Service principal for Azure DevOps (Authentication)

1. Register an app in Microsoft Entra ID: `az ad app create --display-name eod-task-monitor`
2. Create a client secret for it and note the tenant ID, client (application) ID, and secret value.
3. In Azure DevOps: Organization Settings → Users → add the service
   principal as a user, then add it to a group with **Work Items: Read**
   (Basic access or a custom security group is enough — no admin rights
   needed).
4. Set `AZURE_AUTH_MODE=service_principal` and put the tenant ID / client
   ID as plain env vars, and the client secret into Key Vault as
   `azure-devops-sp-client-secret` (step 1 above).
5. Run `python main.py --force` once and confirm the "Azure DevOps
   authentication validated successfully" log line appears.

### 3. Replacing the in-process scheduler (Scheduling & Deployment)

Pick one, depending on what you already run:

**Option A — Azure Functions (Timer Trigger)**
1. Create a Function App (Python, Linux, Consumption or Premium plan).
2. Add a Timer Trigger function whose `function.json` schedule is a cron
   expression for your desired run time, e.g. `0 0 18 * * 1-5` (6pm UTC,
   weekdays — you can still rely on `SKIP_WEEKENDS`/`HOLIDAYS` as a second
   layer of defense for holidays the cron expression doesn't know about).
3. The function body just calls `main.run_once()` (import `main` from
   this repo, packaged alongside the function).
4. Use a **system-assigned managed identity** on the Function App instead
   of a service principal secret where possible — grant it the same Key
   Vault Secrets User role and Azure DevOps access as step 2 above, and
   `DefaultAzureCredential` in `utils/secrets.py` will pick it up
   automatically with no code changes.
5. Set all env vars (`KEY_VAULT_URL`, `AZURE_AUTH_MODE`, etc.) as Function
   App Application Settings.

**Option B — Databricks Workflow**
1. Create a Job with a single Python task pointing at `main.py`.
2. Set the Job's schedule (cron) to your desired run time.
3. Since the job already runs inside Databricks, you can use a Databricks
   service principal with a SQL warehouse permission instead of a PAT for
   `DATABRICKS_ACCESS_TOKEN`, and store the rest of the secrets in an Azure
   Key Vault-backed Databricks secret scope.
4. Set environment variables via the Job's "Environment variables" field
   or a cluster-scoped init script.

**Option C — plain cron (VM / container)**
- Simplest: `0 18 * * 1-5 cd /path/to/repo && python main.py` in crontab.
  No code changes needed beyond what's already in this repo.

In all three options, stop running `python main.py --schedule` in
production — keep it only for local development.

### 4. Email provider switch (Email Delivery)

- **SendGrid**: create an account, verify your sending domain, generate an
  API key with Mail Send permission, set `EMAIL_PROVIDER=sendgrid` and
  store the key in Key Vault as `sendgrid-api-key`.
- **Azure Communication Services**: create an ACS resource + Email
  Communication Service, verify/connect your domain, copy the connection
  string, set `EMAIL_PROVIDER=acs` and store it as
  `azure-communication-connection-string`.
- Either way, no code changes are needed — `mailer/sender.py` already
  dispatches based on `EMAIL_PROVIDER`.

### 5. Teams webhook (Teams Integration)

1. In the target Teams channel: **...** → **Connectors** → **Incoming
   Webhook** (or **Workflows** → "Post to a channel when a webhook
   request is received", depending on your Teams tenant's UI version).
2. Name it (e.g. "EOD Task Monitor") and copy the generated URL.
3. Store it in Key Vault as `teams-webhook-url` (or set
   `TEAMS_WEBHOOK_URL` directly for local testing).
4. No further setup needed — `main.py` calls `send_teams_report()` after
   email delivery on every run if the URL is configured.

### 6. Multi-team rollout

If onboarding additional teams, set `TEAMS_CONFIG` as a JSON list (see
`.env.example`) rather than deploying a second copy of this pipeline. Each
team still gets its own `RunMetrics` row and Teams failure alert, so one
team's Azure DevOps outage doesn't mask another's success.

### 7. Verifying the deployment

After wiring up steps 1–5, run through this checklist once:
1. `python main.py --force` locally with production env vars (or a
   staging Key Vault) — confirms auth, Databricks schema creation, and at
   least one successful end-to-end run before relying on the scheduler.
2. Check the `RunMetrics` table for a `status='success'` row.
3. Trigger a deliberate failure (e.g. temporarily wrong `GROQ_API_KEY`) and
   confirm a Teams alert arrives, then revert.
4. Confirm the external trigger (Functions/Workflow/cron) fired on its own
   at the next scheduled time before decommissioning any manual runs.
