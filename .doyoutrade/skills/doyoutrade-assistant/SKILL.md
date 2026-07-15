---
name: doyoutrade-assistant
description: Validate AND triage DoYouTrade assistant chat flows via `doyoutrade-cli assistant ...` after a programming agent starts the API server. Use when checking whether an agent conversation, assistant tool call, model invocation, trace, or copied-session diagnostic export works end-to-end without opening the browser — and when debugging a session by session_id (`assistant session list/get/events` to find it, read its status/error, and inspect its structured events).
category: tool
style: process
---

# DoYouTrade Assistant Chat Validation

Use this skill when you need to verify the real assistant chat path after a code change.

The CLI is a thin HTTP client. It does **not** start the server. The programming agent starts the API server in a separate shell first:

```bash
uv run doyoutrade
```

For repo-local programming-agent validation, run the CLI through `uv run`. Bare `doyoutrade-cli` is also fine when the console script is already on `PATH`.

The CLI resolves the API base URL in this order: `DOYOUTRADE_API_URL` -> `DOYOUTRADE_API_BASE_URL` -> `api.base_url` -> server host/port fallback. For raw `curl`, set `API_BASE` to the same resolved base URL first.

Find a valid active agent id with the CLI (or `curl` the API directly):

```bash
uv run doyoutrade-cli assistant agent list                 # add --include-inactive to see all
# or, raw:
: "${API_BASE:?set API_BASE to the resolved DoYouTrade API base URL, for example http://127.0.0.1:8000}"
curl "$API_BASE/assistant/agents?include_inactive=false"
```

Then validate via `uv run doyoutrade-cli assistant run`:

```bash
uv run doyoutrade-cli assistant run \
  --agent-id <active-agent-id> \
  --message "Validate the changed flow" \
  --output /tmp/doyoutrade-chat-export.md
```

The command creates a session, sends one message, exports diagnostics, writes markdown to `--output`, and emits one JSON envelope to stdout.

## Commands

### Create a Session

```bash
uv run doyoutrade-cli assistant session create --agent-id <active-agent-id> --title "validation"
```

Returns the assistant session payload under `.data`.

### Inspect Sessions (by session_id — triage)

```bash
# Discover a session_id (newest first); filter by source web|channel or channel id
uv run doyoutrade-cli assistant session list --limit 20
uv run doyoutrade-cli assistant session list --source channel
uv run doyoutrade-cli assistant session list --channel-id <id>

# One session's metadata: status, agent_id, title, created/updated timestamps
uv run doyoutrade-cli assistant session get <asst-session-id>

# A session's structured events (tool calls, errors, attempts) for triage
uv run doyoutrade-cli assistant session events <asst-session-id> --limit 100
uv run doyoutrade-cli assistant session events <asst-session-id> --after-id <event_id>
```

Use these when the user hands you a `session_id` and asks "what happened in
this session / why did it fail" — start with `session get` for status +
error, `session events` for the step-by-step, then `assistant export` for the
full transcript + spans + model invocations. A model invocation's `trace_id`
from the export pivots to `doyoutrade-cli debug get-trace-view <trace_id>` /
`debug model-invocations --trace-id <trace_id>` (the `doyoutrade-debug` skill).

### Send a Message

```bash
uv run doyoutrade-cli assistant chat --session-id <asst-session-id> --message "..."
uv run doyoutrade-cli assistant chat --session-id <asst-session-id> --message-file /tmp/prompt.txt
```

Returns the send-message payload, including user/assistant messages and `trace_id` when available.

### Export Diagnostics

```bash
uv run doyoutrade-cli assistant export --session-id <asst-session-id> --output /tmp/export.md
uv run doyoutrade-cli assistant export --session-id <asst-session-id> --format json
```

The export includes:

- session and agent metadata
- effective system prompt
- user and assistant messages
- thinking blocks
- tool calls, inputs, and result previews
- assistant events
- run ids and trace ids
- assistant spans
- model invocation request/response payloads

### One-Shot Run

```bash
uv run doyoutrade-cli assistant run \
  --agent-id <active-agent-id> \
  --message-file /tmp/prompt.txt \
  --output /tmp/doyoutrade-chat-export.md
```

Use this for programming-agent verification loops.

## Error Codes

| error_code | Meaning | Repair |
| --- | --- | --- |
| `api_unavailable` | The API server is not reachable | Start `uv run doyoutrade`; CLI base URL precedence is `DOYOUTRADE_API_URL` -> `DOYOUTRADE_API_BASE_URL` -> `api.base_url` -> server host/port fallback |
| `api_timeout` | API request timed out | Check server health, retry once, or use a closer/reachable API base URL |
| `api_transport_error` | HTTP transport failed before a valid response | Check the API base URL, network path, TLS/proxy settings, and server process |
| `server_unavailable` | API returned HTTP 503 | Wait for startup/recovery, then retry |
| `server_error` | API returned HTTP 5xx | Inspect server logs and the exported diagnostics if available |
| `validation_error` | API rejected the request shape or values | Fix CLI arguments, ids, or payload fields and retry |
| `assistant_session_not_found` | Session id does not exist | Discover a valid id with `doyoutrade-cli assistant session list`, or create a new session |
| `missing_message` | Neither `--message` nor `--message-file` was passed | Pass exactly one message source |
| `conflicting_message_args` | Both message sources were passed | Use only one |
| `message_file_read_failed` | CLI could not read the prompt file | Check the file path and permissions |
| `assistant_session_create_missing_id` | API session create response had no `session_id` | Treat as server/API contract bug |
| `export_write_failed` | CLI could not write the diagnostic export file | Check the output path and permissions |

## Validation Checklist

- [ ] Server was started separately with `uv run doyoutrade`.
- [ ] CLI output is a single JSON envelope.
- [ ] Export file contains `# Assistant Session Export`.
- [ ] Export contains `attempt_id`, `run_id`, and `trace_id` when a model call happened.
- [ ] Export contains tool input/output if the assistant called tools.
- [ ] Export contains assistant spans for the relevant trace when tracing is enabled.
- [ ] Export contains model invocation request/response payloads when tracing/model recording is enabled.
