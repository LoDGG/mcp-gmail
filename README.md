# Gmail agent prototype

The agent uses a provider-neutral LLM contract and an MCP client. `GMAIL_BACKEND=fake` (the default) selects the in-memory fixture server with four tools. `GMAIL_BACKEND=real` selects a separate read-only Gmail MCP server exposing only `search_emails`, `get_email`, and `get_thread`. The agent loop is shared and still enforces four iterations and three tool calls. Real email content is untrusted model input; only application code decides which advertised tools may run. Development logs contain tool names and redacted arguments, never complete message bodies.

## Local development

```bash
uv sync
uv run pytest -q
uv run python -m fake_gmail_mcp.server
```

The fake server uses MCP stdio and has no credentials. `search_emails` performs a case-insensitive fixture substring search; real Gmail search uses Gmail query syntax and returns at most 20 messages per call. The real backend returns inline plain-text message parts and does not download attachments.

A later Gemini smoke run with the fake backend uses `GEMINI_API_KEY` and optionally `GEMINI_MODEL` from the environment:

```bash
uv run python -m gmail_agent.smoke 'Find the September invoice and label it TO_REVIEW'
```

## Manual Gmail OAuth setup

1. In [Google Cloud Console](https://console.cloud.google.com/), create or select a project and [enable the Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com).
2. Configure the [OAuth consent screen](https://console.cloud.google.com/auth/overview) for personal development. Choose External if using a personal Gmail account, leave the app in Testing, add your Gmail address as a test user, and add only `https://www.googleapis.com/auth/gmail.readonly`.
3. Create an OAuth client of type **Desktop app** in [Clients](https://console.cloud.google.com/auth/clients). Download its JSON credentials file to `.secrets/client_secret.json` in this repository. The `.secrets/` directory is ignored by Git. Restrict local file access to your account.
4. Run `uv run python -m real_gmail_mcp.oauth` locally. The command opens a browser for your sign-in and consent, then saves the token to `.secrets/gmail_token.json` with owner-only file permissions. It performs no Gmail API request. If needed, set `GMAIL_CLIENT_SECRET_FILE` and `GMAIL_TOKEN_FILE` to alternate local paths before running it.
5. After authorization, explicitly set `GMAIL_BACKEND=real` to use the read-only server. For example, with `GEMINI_API_KEY` already in your environment, run `GMAIL_BACKEND=real uv run python -m gmail_agent.smoke 'Find recent messages from example.com'`. Do not request labels or other mutations from real Gmail.

The sole OAuth scope is [`gmail.readonly`](https://developers.google.com/workspace/gmail/api/auth/scopes), required because Gmail's narrower `gmail.metadata` scope cannot search using `q` or read message bodies. The token may refresh during later use, but backend selection never opens the browser. Never commit the downloaded credentials or token.
