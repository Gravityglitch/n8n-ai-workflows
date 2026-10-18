# n8n AI Workflows + Python Sidecar

Three importable n8n workflows and the FastAPI service they call, built the way I build them for
clients: the AI step lives in a small Python sidecar with structured output and a deterministic
fallback, every write to a CRM, mailbox or Slack is queued for **human approval** before it
happens, and the workflow JSON is linted in CI before anyone imports it.

| Workflow | Trigger | AI step | Writes (all approval-gated) |
|---|---|---|---|
| `workflows/lead_intake_ai_scoring.json` | Webhook (web form) | `POST /score` lead scoring | HubSpot contact + booking-slot email, or nurture email |
| `workflows/gmail_triage_digest.json` | Hourly schedule | `POST /triage` classify / summarise / action items | Slack digest, Gmail labels, urgent pings |
| `workflows/scrape_summarise_report.json` | Weekly schedule | `POST /summarise` per page | Markdown report emailed as attachment, Slack note |

Stack: n8n, FastAPI, Claude structured outputs (Anthropic SDK), Pydantic 2, SQLAlchemy 2 on
SQLite, structlog JSON logs, tenacity retries, uv, ruff, pytest, Docker Compose, GitHub Actions.

> **Fictional data notice.** Every lead, email, page, company and address in `data/`, the tests
> and the workflow examples is invented for this demo (reserved `.test` / `example.com` domains).
> Nothing here was taken from a real client or mailbox.

## Architecture

```
                 n8n (docker.n8n.io/n8nio/n8n)                         Python sidecar (FastAPI)
 ┌────────────────────────────────────────────────┐       ┌──────────────────────────────────────┐
 │ Webhook ─► Set ─► HTTP /score ─► IF score>=thr │──────►│ POST /score     Lead ─► LeadScore    │
 │                                  │        │    │       │   Idempotency-Key, X-API-Key         │
 │                             true │  false │    │       │                                      │
 │        HTTP /actions (crm) ◄─────┘        └──► HTTP /actions (nurture email)                  │
 │              │                                 │       │ POST /actions   ─► actions table     │
 │        Slack "approve?"                        │       │   (pending, resume_url stored)       │
 │              │                                 │       │                                      │
 │        Wait (resume: webhook) ◄────────────────┼───────│ POST /approve/{id}  ─► adapter write │
 │              │                                 │       │   CRM | Email | Slack  (DryRun/live) │
 │        IF approved ─► Gmail booking slots      │       │   then POST $execution.resumeUrl     │
 │                    └► Slack "rejected"         │       │ POST /reject/{id}   ─► resume only   │
 │                                                │       │ GET  /pending                        │
 │ Schedule ─► Gmail unread ─► Aggregate ─► HTTP /triage ─► Slack digest, Gmail addLabels        │
 │ Schedule ─► URLs ─► HTTP fetch ─► Aggregate ─► HTTP /summarise ─► ConvertToFile ─► Gmail      │
 └────────────────────────────────────────────────┘       └──────────────────────────────────────┘
                                                                        │
                                                           LLM adapter: ClaudeLLM | FixtureLLM
                                                           fallback:    sidecar/rules.py
```

Design rules that the code enforces, not the prompt:

- **Reads and writes are separate.** `/score`, `/triage`, `/summarise` never touch an external
  system. The only code that does is `ApprovalQueue._perform`, and it runs only after
  `POST /approve/{id}`.
- **Structured output, validated twice.** Claude is asked for a Pydantic schema
  (`LeadScore`, `EmailTriage`, `PageSummary`) through `messages.parse`; the API response is
  validated again on the way out.
- **Deterministic fallback.** If the model fails after `LLM_MAX_RETRIES` (exponential backoff with
  jitter on connection errors, timeouts, 429 and 5xx), `sidecar/rules.py` produces the same schema
  and the response says `"source": "rules"` so n8n can route differently if it wants.
- **Idempotent scoring.** `Idempotency-Key` on `/score` returns the stored response on replay
  (`Idempotent-Replay: true`) and 409s if the key is reused with a different body. The workflow
  derives the key from `email + date`, so a double-submitted form never double-scores.
- **Approval resumes the workflow.** `POST /actions` stores n8n's `$execution.resumeUrl`; after a
  decision the sidecar POSTs the action (status, result, error) back to the Wait node, so the
  workflow continues with the real CRM id and booking slots.
- **Adapters with DryRun defaults.** `Adapters.from_settings` picks `DryRunCRM/Chat/Email/Callback`
  unless `*_MODE=live` and a token is set; tests and the compose stack never make a real call.

## Run it

### Docker Compose (n8n + sidecar)

```bash
cp .env.example .env            # set SIDECAR_API_KEY (and N8N_ENCRYPTION_KEY)
make up                         # docker compose up --build -d
open http://localhost:5678      # n8n UI
curl -s localhost:8000/health   # {"status":"ok","llm":"fixture","auth_enabled":true}
```

The compose file passes `SIDECAR_URL` and `SIDECAR_API_KEY` into the n8n container and sets
`N8N_BLOCK_ENV_ACCESS_IN_NODE=false`, which is what lets the HTTP Request nodes use
`{{ $env.SIDECAR_URL }}` and `{{ $env.SIDECAR_API_KEY }}` instead of a pasted key.

### Import the workflows into n8n

1. n8n UI -> **Workflows** -> **Import from File** -> pick a file from `workflows/`
   (the folder is also mounted read-only at `/workflows` inside the n8n container).
2. Open each Slack / Gmail node and select your credentials (the exports reference
   `REPLACE_ME` placeholders by id/name only; no secrets are stored in the JSON).
3. For `gmail_triage_digest.json`, create the Gmail labels `Triage/Customer`, `Triage/Sales`,
   `Triage/Billing`, `Triage/Internal`, `Triage/Newsletter`, `Triage/Spam` or map the
   `label` field to your own ids in the "Apply Label" node.
4. Activate the workflow. For the lead workflow, POST a form to
   `http://localhost:5678/webhook/lead-intake`.

Or with the CLI inside the container:

```bash
docker compose exec n8n n8n import:workflow --input=/workflows/lead_intake_ai_scoring.json
```

### Local (no Docker)

```bash
uv sync
cp .env.example .env
make run                         # uvicorn sidecar.api:app --reload, fixture LLM, dry-run adapters
```

### Quick tour of the approval loop

```bash
H='-H content-type:application/json -H X-API-Key:change-me'
curl -s -X POST localhost:8000/score $H -H 'Idempotency-Key: priya:2026-10-18' \
  -d @- <<'JSON' | jq
{"name":"Priya Natarajan","email":"priya@northwind-analytics.test","company":"Northwind Analytics",
 "role":"VP Engineering","company_size":350,"budget_usd":25000,
 "message":"We need to integrate AI scoring into our n8n lead pipeline. Looking for a quote and timeline."}
JSON
# {"score":100,"tier":"hot","qualified":true,"threshold":70,"reasons":[...],"source":"llm",...}

curl -s -X POST localhost:8000/actions $H \
  -d '{"type":"create_crm_contact","payload":{"lead":{"name":"Priya Natarajan","email":"priya@northwind-analytics.test"}},"resume_url":"http://localhost:5678/webhook-waiting/123"}'
curl -s localhost:8000/pending $H
curl -s -X POST localhost:8000/approve/<id> $H -d '{"decided_by":"vikrant"}'
# -> status "approved", result {"contact_id":"dryrun-contact-0001","proposed_slots":[...]}, n8n resumed
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Public. Reports active LLM backend and whether auth is on. |
| POST | `/score` | Lead -> score/tier/reasons/next step. Honours `Idempotency-Key`. |
| POST | `/triage` | Emails -> category, priority, summary, action items, Gmail label, Slack digest markdown. |
| POST | `/summarise` | Pages (HTML or text) -> per-page summary + key points + compiled Markdown report. |
| POST | `/actions` | Queue a write (`create_crm_contact`, `send_email`, `post_slack`) with an optional n8n resume URL. |
| GET | `/pending` | Actions awaiting a decision. |
| GET | `/actions/{id}` | One action with its result or error. |
| POST | `/approve/{id}` | Execute through the adapter, record result, resume n8n. |
| POST | `/reject/{id}` | Record rejection, resume n8n, write nothing. |

All routes except `/health` require `X-API-Key`. Every log line is JSON and carries the
`X-Request-ID` (minted if absent, echoed on the response).

## Extending a client's workflow safely

Most "fix my n8n workflow" jobs are "add a step between A and B without breaking the rest".
`tools/n8n_patch.py` does exactly that on the export, so the change is reviewable in a diff:

```bash
uv run python -m tools.n8n_patch workflows/lead_intake_ai_scoring.json \
  --after "Normalise Lead" --before "Score Lead (sidecar)" \
  --node-file tests/fixtures/dedupe_node.json --out /tmp/patched.json
uv run python -m tools.lint_workflows /tmp/patched.json --strict
```

It refuses ambiguous names, missing edges and duplicate node names; keeps every other node and
connection byte-identical; places the new node at the midpoint of its neighbours; and
`--in-place` leaves a `.bak`. The process I follow on a real engagement: export, patch, lint,
diff, import into a staging n8n, run with pinned data, then import to production.

### Workflow linter

`tools/lint_workflows.py` runs in CI (`--strict`) and fails on: missing top-level keys, duplicate
or missing node names, connections to nodes that do not exist, unknown node packages, webhooks
without a path/`webhookId`, Wait nodes resumed by webhook without a `webhookId`, hard-coded
secrets (OpenAI/Anthropic/Slack/HubSpot/AWS/PEM patterns, literal `Bearer` tokens), credential
blocks that contain anything besides id/name, sensitive headers that are literals, and any call
to the sidecar whose URL is not built from `$env.SIDECAR_URL` or whose `X-API-Key` is not
`$env.SIDECAR_API_KEY`. `tests/fixtures/broken_workflow.json` trips every one of those rules.

## Configuration

See `.env.example`. The important ones:

| Variable | Default | Notes |
|---|---|---|
| `API_KEYS` | empty | Comma-separated; empty disables auth (development only, logged at startup). |
| `LLM_BACKEND` | `fixture` | `claude` needs `ANTHROPIC_API_KEY`; without it the server logs a warning and uses the fixture. |
| `SCORE_THRESHOLD` | `70` | Leads at or above take the CRM + booking branch. |
| `CRM_MODE` / `SLACK_MODE` / `EMAIL_MODE` | `dry_run` | `live` requires the matching token. |
| `N8N_CALLBACK_MODE` | `dry_run` (`live` in compose) | Whether approvals POST to the n8n resume URL. |
| `APP_ENV=production` | | Refuses to start without `API_KEYS` and the tokens for any live mode. |

## Development

```bash
make lint     # ruff check, ruff format --check, workflow lint
make test     # pytest with the 80% coverage gate (currently ~99%)
```

```
sidecar/            FastAPI service
  api.py            app factory, routes
  services.py       score / triage / summarise, idempotency, digest + report rendering
  llm.py            LLM protocol, ClaudeLLM (tenacity retries), FixtureLLM
  rules.py          deterministic fallback scoring / triage / summarising, HTML stripping
  approvals.py      approval queue; the only place writes happen
  adapters.py       CRM / Chat / Email / WorkflowCallback protocols, DryRun + live httpx impls
  db.py             SQLAlchemy 2 models (actions, idempotency_keys)
  config.py         pydantic-settings, production validation
  auth.py, observability.py
tools/
  lint_workflows.py workflow export linter (CLI + library)
  n8n_patch.py      insert-node-between patcher (CLI + library)
workflows/          three n8n exports
data/               fictional sample leads, emails, pages
tests/              80 tests; fixtures include a deliberately broken workflow
```

## Evaluation and limits

- **All numbers in this repo are offline.** The test suite and the sample runs use `FixtureLLM`,
  which routes through the same rule engine as the fallback path. No live Claude, HubSpot, Gmail
  or Slack call was made while building or testing this repo, and CI never makes one.
- The rule engine is a reasonable baseline, not a model: on the three sample leads it scores
  100 / 52 / 20 (hot / warm / cold) and classifies the five sample emails as customer / sales /
  billing / newsletter / spam. Real-world accuracy depends on the model and prompt, which is why
  `source` is reported on every result and why writes still need a human.
- `ClaudeLLM` is exercised in tests with a scripted SDK stand-in (retry on connection error,
  429, 5xx; no retry on 4xx; unparsed output raises). The exact prompts in `sidecar/llm.py` have
  not been tuned against a labelled set.
- The n8n exports target n8n 1.x node versions (`httpRequest` 4.2, `set` 3.4, `if` 2, `wait` 1.1,
  `slack` 2.2, `gmail` 2.1, `scheduleTrigger` 1.2, `aggregate` 1, `splitOut` 1,
  `convertToFile` 1.1). Older instances may prompt to upgrade nodes on import.
- Gmail label application uses label *ids*; the workflow passes the sidecar's label name and
  expects a one-to-one label set (see import step 3).
- The live adapters are intentionally thin (one endpoint each) and are tested against
  `httpx.MockTransport`, not against the real APIs.
- SQLite is fine for one sidecar replica. For more, point `DATABASE_URL` at Postgres; the
  SQLAlchemy models do not need to change.

## License

MIT - see `LICENSE`. Author: Vikrant Reddy.
