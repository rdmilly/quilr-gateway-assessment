# QuilrAI assessment: MCP server, MCP gateway and LLM gateway

Work in progress for the QuilrAI Solution Architect take-home, due November 9, 2026.

Four parts, each runnable on its own:

| Task | What it is | Folder | Status |
| --- | --- | --- | --- |
| 1 | MCP server with strict input validation over stdio | [`task1_mcp_server/`](task1_mcp_server/README.md) | Done |
| 2 | MCP security gateway: bearer token roles, admin tool blocking | [`task2_mcp_gateway/`](task2_mcp_gateway/README.md) | Done |
| 3 | LLM gateway with streaming PII redaction | [`task3_pii_guardrail/`](task3_pii_guardrail/README.md) | Done |
| 4 | Token rate limiter and model failover router | `task4_rate_limit_failover/` | Not started |

Targets MCP specification 2026-07-28, and still accepts 2025-11-25 clients.

Design record: [decisions](docs/DECISIONS.md), [threat model](docs/THREAT-MODEL.md), [build log](docs/BUILD-LOG.md).

## Run the checks

Needs Python 3.12 to 3.14 and [uv](https://docs.astral.sh/uv/). Python 3.15 is excluded for now because pytest and mypy don't run on it yet (November 2026).

```bash
uv sync
./check.sh
```

Run the Task 1 server: `uv run python -m task1_mcp_server`. Run Task 2: see [its README](task2_mcp_gateway/README.md). Task 3: [its README](task3_pii_guardrail/README.md).
