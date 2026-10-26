# Connecting Claude Desktop and Codex to Jolt

Jolt's nightly sync runs in the learner's own model client: a scheduled task in
Claude Desktop or Codex connects to Jolt's MCP server just after midnight, calls
`jolt_sync` and follows the plan (see
[`NIGHTLY_SYNC_PROMPT.md`](./NIGHTLY_SYNC_PROMPT.md)). This page is the exact
setup for each client.

> **Deploy pending.** No real Claude Desktop or Codex could be run while this
> was written. What the server accepts is covered by tests (`tests/test_sync.py`,
> `tests/test_auth.py`) and by `scripts/smoke.py` against a live server; what
> each client sends is from its documentation. Every step marked
> **[confirm]** must be checked against the real client and the `dev` server
> after deploy (checklist at the end), and this page corrected if a client
> behaves differently.

## What you need

| | Where it comes from |
|---|---|
| **Server URL** | `GET /v1/me` → `mcp_server_url`, shown on the app's "Connect your model" screen. In `dev`: `https://<app FQDN>/mcp/sse`. |
| **MCP token** | `POST /v1/mcp-tokens {label}` → `token` (`jolt_live_…`), shown **once**. Make one per client and label it after the client (`"Claude Desktop"`, `"Codex"`): the label is how the app names the client on each sync run. |

## What the server accepts

The MCP endpoint is the legacy **HTTP+SSE** transport: the client opens the event
stream `GET /mcp/sse` and posts messages to the `/mcp/messages/?session_id=…`
address the stream announces. Tool calls run on the event stream's connection, so
**only the request that opens the stream needs the token**; the message posts do
not.

The token is read from, in order:

1. `Authorization: Bearer jolt_live_…` (a bare `Authorization: jolt_live_…` also
   works);
2. the query string, `GET /mcp/sse?token=jolt_live_…`, for clients whose
   connector settings take only a URL. On by default; `MCP_QUERY_TOKEN_ENABLED=false`
   turns it off.

A missing, revoked or wrong-kind token (an app token, `jolt_app_…`) gets `401`
with `WWW-Authenticate: Bearer` before the stream starts. Every tool call
re-checks the token, so revoking it in the app cuts off an open connection at its
next call.

**About the token in the URL.** It is needed because Claude's custom connectors
take a URL and, for auth, only OAuth (which Jolt does not offer: its tokens are
opaque, per user, and pasted by the user, LLD §5). The trade-off is accepted
because an MCP token only opens `/mcp` (never `/v1`), is one per client, and is
revoked in one tap. The server strips `token=` (and `session_id=`) values from
its access log and from the request URLs it sends to Application Insights. A
header client also gets its session bound to its token (messages with any other
credential are refused); a `?token=` session cannot be, and relies on its
random session id staying secret (docs/SECURITY.md). Treat
a connector URL with a token like a password, and revoke the token if the URL
leaks. Where a client can send a header (Codex, Claude Desktop's local config),
use the header.

**A header session needs the header on every message.** When the stream was
opened with `Authorization: Bearer …`, each `POST /mcp/messages/?session_id=…`
must carry the same header; a message without it gets 404 "Could not find
session". `mcp-remote --header` already sends it on every request. A client
written by hand must do the same.

## Claude Desktop

Two ways; use **A** unless you prefer the token not to sit in a URL.

### A. Custom connector (URL only)

1. Claude Desktop → **Settings → Connectors → Add custom connector**. **[confirm]**
   menu names.
2. **Name:** `Jolt`
3. **Remote MCP server URL:**
   ```
   https://<app FQDN>/mcp/sse?token=jolt_live_…
   ```
4. Leave the OAuth fields (Advanced settings) empty. Add.
5. In a new chat, enable the Jolt connector and ask: "Call jolt_sync and tell me
   what it returns." You should see a plan with `done: true` (nothing uploaded
   yet), and the app's Settings shows a sync run from that token. **[confirm]**

Custom connectors are reached from Anthropic's side, not from your computer, so
the scheduled task works whenever Claude runs it. **[confirm]** that a connector
URL keeps its query string when Claude opens the stream (the server answers `401
missing bearer token` if it does not), and that scheduled tasks can use custom
connectors.

### B. Local config with a header (`mcp-remote`)

Needs Node.js. Claude Desktop starts the `mcp-remote` bridge, which connects to
Jolt with the token in a header. Edit `claude_desktop_config.json`
(Settings → Developer → Edit Config; macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`, Windows:
`%APPDATA%\Claude\claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "jolt": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://<app FQDN>/mcp/sse",
        "--transport",
        "sse-only",
        "--header",
        "Authorization:${JOLT_AUTH}"
      ],
      "env": {
        "JOLT_AUTH": "Bearer jolt_live_…"
      }
    }
  }
}
```

Restart Claude Desktop; `jolt` appears in the tools menu. The header value is
passed through `env` because spaces inside `args` are mangled on some platforms.
This only runs while Claude Desktop is open on that computer. **[confirm]**

## Codex

Codex reads MCP servers from `~/.codex/config.toml` (CLI, IDE extension and app
share it). Jolt serves HTTP+SSE, and Codex's built-in remote (`url = …`) support
speaks the newer Streamable HTTP transport, so connect through the same
`mcp-remote` bridge (needs Node.js):

```toml
[mcp_servers.jolt]
command = "npx"
args = ["-y", "mcp-remote", "https://<app FQDN>/mcp/sse", "--transport", "sse-only", "--header", "Authorization:${JOLT_AUTH}"]
env = { JOLT_AUTH = "Bearer jolt_live_…" }
# npx may download mcp-remote on first start.
startup_timeout_sec = 60
```

Check with `codex mcp list` (shows `jolt`), then in a session: "Call jolt_sync
and tell me what it returns." **[confirm]** the field names
(`startup_timeout_sec`) against the installed Codex version.

When Jolt serves Streamable HTTP (follow-up, see below) the bridge goes away:

```toml
# Future, once /mcp speaks Streamable HTTP:
[mcp_servers.jolt]
url = "https://<app FQDN>/mcp"
bearer_token_env_var = "JOLT_MCP_TOKEN"   # export JOLT_MCP_TOKEN=jolt_live_…
```

## The nightly scheduled task

Create a scheduled task in the client, **daily at 00:15 local time**, with the
Jolt server enabled, and paste the prompt from the app ("Copy prompt", served by
`GET /v1/sync/prompt`, source [`NIGHTLY_SYNC_PROMPT.md`](./NIGHTLY_SYNC_PROMPT.md)).
The app's Today and Settings screens then show each night's run
(`GET /v1/sync/status`): client label, sources read, questions written, answers
graded, problems, and `late` after 30 hours without a run.

A run is tied to the MCP token, not to one connection: if the client reconnects
in the middle (a dropped stream, a restarted bridge), its next `jolt_sync`
continues the same run.

## How each client sends the token

| Client | Setup | Sends the token as | Status |
|---|---|---|---|
| Claude Desktop, custom connector | URL only | `?token=` on `GET /mcp/sse` | server side tested; client **deploy pending** |
| Claude Desktop, local config | `mcp-remote --header` | `Authorization: Bearer` on every request | server side tested; client **deploy pending** |
| Codex | `config.toml` + `mcp-remote --header` | `Authorization: Bearer` on every request | server side tested; client **deploy pending** |
| `scripts/smoke.py` (MCP Python SDK) | `sse_client(headers=…)`, and `?token=` | both | passing locally (`tests/test_smoke_local.py`) |

## To confirm after deploy

Against `dev`, with a fresh MCP token per client:

1. Claude Desktop, connector (A): add it, run `jolt_sync` in a chat, see the run
   in `GET /v1/sync/runs` with `client_label` = the token's label.
2. Claude Desktop, local config (B): same.
3. Codex: same.
4. For each: upload two note photos in the app, run the scheduled task once (or
   paste the prompt into a chat), and check the next morning: concepts and
   questions in the app, `GET /v1/sync/status` → `health: ok`, last run
   `finished` with the right counts.
5. Revoke each token in the app and check the client's next call fails.
6. Long syncs: check the event stream is not cut by the Container Apps ingress
   during a run of several minutes. If it is, the client must reconnect (the run
   survives, see above); if a client does not reconnect, that is the case for
   adding the Streamable HTTP transport.
7. Check the Container Apps console and system logs do not record the `?token=`
   query string (the app's own access log redacts it).

Correct this page with whatever the real clients need.

## Follow-ups

- **Streamable HTTP transport** at `/mcp`: lets Codex connect natively (`url` +
  `bearer_token_env_var`) and avoids long-lived streams. The `mcp` package is
  already new enough (1.30.0 since the BE-10 security review); what is left is
  mounting the transport and re-exporting the contract.
- **OAuth for connectors**: would let Claude's connector UI sign in without a token
  in the URL. Not planned for the MVP (LLD §5).
