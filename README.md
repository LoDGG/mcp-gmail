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
