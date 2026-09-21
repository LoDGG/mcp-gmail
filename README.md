# Fake Gmail MCP server

This phase provides a deterministic, in-memory email server using the [official Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk). It contains no Gmail connection, credentials, model integration, or agent loop.

## Run locally

```bash
uv sync --extra test
uv run --extra test pytest -q
uv run python -m fake_gmail_mcp.server
```

The last command starts an MCP server over stdio. A client launches this process and discovers `search_emails`, `get_email`, `get_thread`, and `apply_label` through MCP `tools/list`, then invokes a tool through `tools/call` with the arguments in its advertised schema. The SDK handles transport and protocol messages.

`search_emails(query="")` returns metadata for all messages. A nonempty query is a case-insensitive substring matched against sender, subject, and body. Gmail query operators are not supported. `get_thread` groups messages by their fixture `thread_id` and preserves fixture order. Unknown IDs and invalid labels return tool errors.

The fixture store lives in `src/fake_gmail_mcp/store.py`; the MCP adapter and development logging live in `server.py`. Each server can receive its own `FakeEmailStore`, and `store.reset()` restores the original fixtures. `apply_label` affects only that store instance. Exposing the tool does not authorize a future agent to call it; that decision belongs in a separate agent policy layer.
