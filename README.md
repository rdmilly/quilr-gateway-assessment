# QuilrAI assessment: MCP server, MCP gateway and LLM gateway

Work in progress for the QuilrAI Solution Architect take-home, due November 9, 2026.

Four parts, each runnable on its own:

| Task | What it is | Folder | Status |
| --- | --- | --- | --- |
| 1 | MCP server with strict input validation over stdio | `task1_mcp_server/` | Not started |
| 2 | MCP security gateway: bearer token roles, admin tool blocking | `task2_mcp_gateway/` | Not started |
| 3 | LLM gateway with streaming PII redaction | `task3_pii_guardrail/` | Not started |
| 4 | Token rate limiter and model failover router | `task4_rate_limit_failover/` | Not started |

Targets MCP specification 2026-07-28, and still accepts 2025-11-25 clients.

## Run the checks

Needs Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
./check.sh
```
