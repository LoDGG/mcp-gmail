# Gmail agent prototype

The agent uses a provider-neutral LLM contract and an MCP client. `GMAIL_BACKEND=fake` (the default) selects the in-memory fixture server with four tools. `GMAIL_BACKEND=real` selects the real Gmail MCP server. The real server exposes `search_emails`, `get_email`, and `get_thread` by default. It exposes `apply_label` only with the separate, explicit `GMAIL_LABEL_WRITES=1` opt-in; `GMAIL_LABEL_WRITES=0` is the default. The agent loop is shared and still enforces four iterations and three tool calls. Real email content is untrusted model input; only application code decides which advertised tools may run. Development logs contain tool names and redacted arguments, never complete message bodies or OAuth tokens.

The real `apply_label` tool accepts an exact existing user-label name, resolves its ID through Gmail, and sends only that ID in `addLabelIds` for one message. It cannot create or remove labels or apply system labels. No other mutation tool is exposed.

## Local development

```bash
uv sync
uv run pytest -q
uv run python -m fake_gmail_mcp.server
```

The fake server uses MCP stdio and has no credentials. `search_emails` performs a case-insensitive fixture substring search; real Gmail search uses Gmail query syntax and returns at most 20 messages per call. The real backend returns inline plain-text message parts and does not download attachments.

A Gemini smoke run with the fake backend uses `GEMINI_API_KEY` and optionally `GEMINI_MODEL` from the environment:

```bash
uv run python -m gmail_agent.smoke 'Find the September invoice and label it TO_REVIEW'
```

## Manual Gmail OAuth setup and reauthorization

1. In [Google Cloud Console](https://console.cloud.google.com/), create or select a project and [enable the Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com).
2. Configure the [OAuth consent screen](https://console.cloud.google.com/auth/overview) for personal development. Choose External for a personal Gmail account, leave the app in Testing, add your Gmail address as a test user, and configure only `https://www.googleapis.com/auth/gmail.modify` for this application. Remove the old `gmail.readonly` scope from the app configuration if present.
3. Create a **Desktop app** OAuth client in [Clients](https://console.cloud.google.com/auth/clients). Download its JSON file to `.secrets/client_secret.json` in this repository. The `.secrets/` directory is ignored by Git. Restrict local file access to your account.
4. If you have a token from Task 1D, move `.secrets/gmail_token.json` to a backup inside `.secrets/`. That old `gmail.readonly` token cannot authorize this version. Run `uv run python -m real_gmail_mcp.oauth` locally to open browser consent for the new scope. The new token is saved at `.secrets/gmail_token.json` with owner-only file permissions. If using alternate local paths, set `GMAIL_CLIENT_SECRET_FILE` and `GMAIL_TOKEN_FILE` before running the command.
5. Set `GMAIL_BACKEND=real` only when you intend to use real Gmail. This leaves label writes disabled. For one later, deliberate label operation, choose one message ID and an existing non-system user-label name, then use the command below. It calls MCP once and makes no Gemini API call.

```bash
GMAIL_BACKEND=real GMAIL_LABEL_WRITES=1 uv run python -m real_gmail_mcp.label_smoke 'MESSAGE_ID' 'EXISTING_LABEL_NAME'
```

The sole OAuth scope is [`gmail.modify`](https://developers.google.com/workspace/gmail/api/auth/scopes). Gmail requires it for [`users.messages.modify`](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/modify); it also covers the required read operations. This scope grants broader API authority than the application exposes, so the explicit tool surface and write opt-in are the application security boundary. Never commit credentials or tokens.

## Batch classification dry run

The separate classifier searches through the configured MCP backend, sends only batch-local index, sender, subject, and available date/snippet to Gemini, then maps validated indices back to Gmail message IDs. It never fetches full bodies in its first pass or calls a Gmail mutation tool. The default batch size is 10; snippets are limited to 200 characters. An `UNCERTAIN` result stays unresolved. No automatic second pass runs.

```bash
uv run python -m gmail_agent.classify_cli --query invoice --batch-size 10
```

The CLI uses `GMAIL_BACKEND=fake` by default. Real Gmail requires an explicit query. For a later dry run of at most five recent inbox messages, with Gmail and Gemini credentials already set locally:

```bash
GMAIL_BACKEND=real GMAIL_LABEL_WRITES=0 uv run python -m gmail_agent.classify_cli --query 'in:inbox newer_than:7d' --max-emails 5 --batch-size 5
```

The classifier forces real label writes off even if `GMAIL_LABEL_WRITES=1` is present in its environment. The CLI prints one concise classification per message, then model name, batch email count, input/output/total token counts when supplied by Gemini, and the Gemini request count. A successful first attempt makes one Gemini request per nonempty batch. Only Gemini HTTP 429, 500, 502, 503, and 504 failures are retried, with waits of 20 and 60 seconds and at most three total attempts per batch. SDK retries are disabled for these requests. Both classification CLIs print actual request and retry counts, including on failure (for example, `Gemini requests=3 retries=2`). Authentication, invalid requests, invalid model output, application validation, and Gmail writes are not retried. Exhausted provider failures propagate as a failed run before any message labels or predictions are saved, leaving messages eligible for the next scheduled run.

## Local taxonomy learning

`config/taxonomy.json` defines taxonomy version `1.0.0`, eleven active primary categories, category guidance, a target of 8–12 categories, and a hard cap of 12. The classifier now also predicts `importance` (`low`, `normal`, or `high`); `needs_reply` and `deadline` remain separate attributes. This changes the classifier's structured output contract, so classify new batches with the current code before saving them.

Use `--save` to persist a dry-run batch to `.local/gmail_agent.db` (or set `GMAIL_AGENT_DB`, or pass `--db`). The SQLite history keeps message IDs, predictions, taxonomy versions, run timestamps, and human review decisions. It also saves available date, sender, and subject headers for offline review (bounded to 100, 320, and 500 characters respectively). It never saves snippets or bodies. Headers come from search results, not model output; legacy rows show unavailable metadata. A run key and a unique run/message pair prevent duplicate entries when the same run is saved twice. The classifier remains dry-run even if the environment enables Gmail label writes.

```bash
GMAIL_BACKEND=real GMAIL_LABEL_WRITES=0 uv run python -m gmail_agent.classify_cli --query 'in:inbox newer_than:7d' --max-emails 10 --batch-size 10 --save
uv run python -m gmail_agent.review_cli
uv run python -m gmail_agent.taxonomy_stats
```

The review CLI shows a compact local queue with message ID, available date/sender/subject, primary category, confidence, subtype hint, needs_reply, importance, and deadline. Accept records primary prediction attributes as ground truth; category correction preserves the original prediction and records a separate human category. Skip leaves the record unchanged and moves on for this session. Pending primary predictions are never ground truth. The stats command uses local SQLite data only and reports category distribution, UNCERTAIN and confidence distributions, correction and disagreement rates, confusion counts, and deterministic proposal candidates. Confidence is a model score, not measured accuracy. Candidate types are CREATE, MERGE, SPLIT, DEPRECATE, and REFINE_DEFINITION. Candidates do not modify the taxonomy; use `uv run python -m gmail_agent.taxonomy_stats --save-proposals` only if you want to keep candidate records for later review.

Classifications also require `subtype_hint`: a non-binding lowercase snake_case semantic hint of at most 40 characters, or null when no useful subtype is apparent. Hints cannot repeat the primary category; malformed hints are rejected. For example, NEWSLETTER may have `job_alert` and FINANCE may have `subscription_invoice`. Hints never select or create Gmail labels or affect confidence, Review, or Processed decisions.

History databases automatically receive a nullable `subtype_hint` column on opening; existing predictions and reviews are preserved with null hints. Original hints remain in `subtype_hint`. Separate subtype review status, corrected hint, and review timestamp columns preserve human decisions. Accepting or correcting a primary category alone does not approve the hint. The additive migration also adds nullable header fields; existing reviews remain intact and subtype decisions start pending.

Taxonomy stats include unverified `subtype_candidates`, grouped by primary category with counts of distinct messages and reviewed/unreviewed category evidence. The default minimum is three distinct messages; configure it with `uv run python -m gmail_agent.taxonomy_stats --subtype-min-messages 5`. Each message contributes its latest reviewed record, or its latest prediction if never reviewed. Human category corrections take precedence in grouping. Null hints and groups below the threshold are omitted. These exploratory candidates do not change taxonomy files, generate new primary-category proposals, or create Gmail labels.

### Fast local review

```bash
uv run python -m gmail_agent.review_cli --limit 30 --only-unreviewed
uv run python -m gmail_agent.review_cli --db .local/gmail_agent.db --limit 50
uv run python -m gmail_agent.taxonomy_stats --subtype-min-messages 3
```

The default queue includes pending primary reviews and remaining subtype reviews where a model hint exists. `--limit` caps items shown, including skips; `--all` also displays completed records without allowing saved decisions to be overwritten. Older records need no API lookup and show unavailable headers.

Use one command per item:

- `a` / `accept`: accept primary prediction attributes; leave subtype undecided.
- `c ADMIN` / `correct ADMIN`: correct the primary category. Bare `c` asks only for the category. Valid categories appear once at startup.
- `s` / `skip`: leave unchanged for a later session.
- `q` / `quit`: stop; completed decisions are already saved.
- `sa`: accept the model subtype; `sr`: reject it; `sc account_notice`: supply a corrected subtype. These can be used independently or appended to a primary action, such as `a sa` or `c ADMIN sc account_notice`.

Corrected hints must be lowercase snake_case, at most 40 characters, and must not repeat the effective primary category. Use rejection to discard a hint; acceptance/rejection requires an existing non-null hint. A combined primary/subtype action validates both decisions before saving either. Subtype-only review never creates primary category ground truth. EOF or Ctrl-C stops safely between decisions.

Stats report primary reviewed count, accepted-category rate and category-correction rate (denominator: primary-reviewed records), separate subtype decision counts, and model subtype disagreement counts/rate (rejected or corrected decisions divided by subtype-reviewed records). Non-category attribute corrections still count as category agreement. Human-approved subtype candidates require both category ground truth and an accepted/corrected subtype; they use the configured distinct-message threshold and the latest explicit subtype decision per message. These are separate from exploratory unverified model hints. Review and stats use local SQLite only, never call Gemini/Gmail, never modify Gmail labels, and never change taxonomy files.

## Safe automatic triage

`config/gmail_labels.json` fixes the triage labels: ADMIN→Admin, FINANCE→Billing, PURCHASE→Purchase, APPOINTMENT→Appointment, TRAVEL→Journeys, PROFESSIONAL→Professional, PERSONAL→Personal, SECURITY→Security, NEWSLETTER→Newsletter, NOTIFICATION→Notification, plus Review and Processed. Internal taxonomy names and saved history are unchanged. No `AI/` prefix is used. In `--apply` mode, triage lists existing Gmail user labels, creates only missing names from this static allowlist, and resolves their Gmail IDs before it asks Gemini to classify. If Gmail rejects a configured name, triage reports that exact name and stops before classification or message labeling. Model output never supplies a label name or controls creation. Repeated runs reuse existing labels; no labels are renamed or deleted. Dry-run mode creates nothing.

The default query is `in:inbox -label:"Processed" newer_than:14d`, with batch size 10 and a maximum of 10 emails. An empty result makes no Gemini request. A prediction at confidence 0.90 or higher gets its mapped category label; lower confidence or `UNCERTAIN` gets Review. Processed is added last, only after the first label succeeds. No other Gmail mutation is used. Valid predictions are saved to local history before message labels are applied, and remain unreviewed until a human accepts or corrects them.

Real label creation and application require both `GMAIL_LABEL_WRITES=1` and `--apply`. Without `--apply`, triage only classifies and saves results, even if label writes are enabled elsewhere. `classify_cli` remains dry-run only.

For a deliberate real five-email test:

```bash
GMAIL_BACKEND=real GMAIL_LABEL_WRITES=1 uv run python -m gmail_agent.triage_cli --apply --query 'in:inbox -label:"Processed" newer_than:14d' --max-emails 5 --batch-size 5
```

For a future scheduled ten-email run, use the same command with `--max-emails 10 --batch-size 10`. Triage prints emails found/classified, each chosen category and action, processed status, Gemini request count, and input/output/thinking/total token counts when available. It never prints full bodies.

## Unattended Linux VPS runtime

The VPS needs network access to Gmail and Gemini, Python 3.12+, `uv`, `bash`, `flock` (util-linux), and systemd. No GPU is needed. `scripts/run_triage.sh` runs the same ten-email apply workflow as above with `GMAIL_BACKEND=real` and `GMAIL_LABEL_WRITES=1`. It uses a non-blocking lock in `.local/triage.lock`; an overlapping run exits successfully before loading secrets or making API requests. Start/end timestamps and concise CLI output append to `.local/triage.log`. The log and `.local/` directory are private to the deployment user.

The VPS must have these private files in the repository:

- `.secrets/client_secret.json`
- `.secrets/gmail_token.json` (authorized locally first; the VPS does not run browser OAuth)
- `.secrets/gemini.env`, with a `GEMINI_API_KEY=...` assignment (single or double quotes are accepted)

`.secrets/` and `.local/` are Git ignored. Never put secret values in systemd units. On the VPS, run `chmod 700 .secrets` and `chmod 600 .secrets/*` after transfer. The wrapper sources this private shell file as the deployment user, so keep only trusted assignments in it. It clears inherited `GEMINI_API_KEY` before loading the file, removes `GOOGLE_API_KEY`, and never prints either key.

### Copy code and secrets from WSL

Replace `user@vps.example` and `/home/user/gmail-agent` with your VPS login and chosen absolute repository path. The path used for systemd should have no spaces. From the WSL repository root, after the local Gmail OAuth token has been created:

```bash
VPS='user@vps.example'
REMOTE_REPO='/home/user/gmail-agent'
git check-ignore .secrets/client_secret.json .secrets/gmail_token.json .secrets/gemini.env
ssh "$VPS" "mkdir -p '$REMOTE_REPO/.secrets' && chmod 700 '$REMOTE_REPO/.secrets'"
rsync -az --exclude='.git' --exclude='.venv' --exclude='.local' --exclude='.secrets' ./ "$VPS:$REMOTE_REPO/"
scp .secrets/client_secret.json .secrets/gmail_token.json .secrets/gemini.env "$VPS:$REMOTE_REPO/.secrets/"
ssh "$VPS" "chmod 700 '$REMOTE_REPO/.secrets' && chmod 600 '$REMOTE_REPO'/.secrets/*"
```

Alternatively, once the code is in a remote repository, clone it on the VPS in place of `rsync`; still transfer the three secret files separately over SSH. The `rsync` command includes local uncommitted code but excludes credentials, the local database, and the virtual environment.

### Prepare and verify on the VPS

SSH to the VPS and set the path to the same absolute directory:

```bash
REPO_DIR='/home/user/gmail-agent'
cd "$REPO_DIR"
# If uv is absent, install it following the official installer:
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv sync --locked
chmod 700 .secrets
chmod 600 .secrets/*
GMAIL_BACKEND=real GMAIL_LABEL_WRITES=0 uv run --env-file .secrets/gemini.env python -m gmail_agent.triage_cli --query 'in:inbox -label:"Processed" newer_than:14d' --max-emails 5 --batch-size 5
```

The last command is a manual dry run: it uses live APIs for at most five emails, saves predictions locally, and creates or applies no Gmail labels. Run it yourself only after confirming the copied token and key. The scheduled wrapper performs label writes and therefore requires separate activation below.

### Install the systemd timer on the VPS

Render the service template for the current deployment account and absolute repository path. The renderer rejects unsupported characters and unresolved placeholders:

```bash
cd "$REPO_DIR"
python3 scripts/render_systemd.py --output /tmp/gmail-agent-triage.service
sudo install -m 0644 /tmp/gmail-agent-triage.service /etc/systemd/system/gmail-agent-triage.service
sudo install -m 0644 deploy/systemd/gmail-agent-triage.timer /etc/systemd/system/gmail-agent-triage.timer
rm /tmp/gmail-agent-triage.service
sudo systemctl daemon-reload
sudo systemctl enable --now gmail-agent-triage.timer
```

The timer starts ten minutes after boot and then approximately every six hours after each service activation. It includes `Persistent=true`; the boot trigger provides the post-reboot run for this monotonic schedule.

Inspect status and logs, or deliberately trigger one service execution:

```bash
systemctl status gmail-agent-triage.timer
systemctl list-timers --all gmail-agent-triage.timer
tail -n 50 "$REPO_DIR/.local/triage.log"
sudo journalctl -u gmail-agent-triage.service -n 50 --no-pager
sudo systemctl start gmail-agent-triage.service
```

Stop and remove the timer and service units if needed:

```bash
sudo systemctl disable --now gmail-agent-triage.timer
sudo rm /etc/systemd/system/gmail-agent-triage.timer /etc/systemd/system/gmail-agent-triage.service
sudo systemctl daemon-reload
```
