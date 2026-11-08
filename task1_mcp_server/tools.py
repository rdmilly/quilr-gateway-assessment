"""The two tools, and the one function that dispatches a ``tools/call`` to them.

Every call goes through the same steps, in this order:

1. Unknown tool name: JSON-RPC ``-32602``.
2. Arguments fail the tool's Pydantic model: JSON-RPC ``-32602`` with one entry
   per bad field in ``error.data``. No tool code runs.
3. The tool runs. A business outcome the caller should see and act on (no such
   customer, refund larger than the balance) is a normal result with
   ``isError: true``, which is what MCP specifies for errors inside a tool.
4. Anything unexpected: JSON-RPC ``-32603 Internal error`` with a fixed message.
   The details go to the stderr log, never to the caller.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from mcp import MCPError, types
from mcp.types import INTERNAL_ERROR, INVALID_PARAMS
from pydantic import BaseModel, ValidationError

from shared.logs import log_event

from .customers import CustomerStore, RefundRejected
from .schemas import (
    CustomerRecord,
    GetCustomerRecordInput,
    RefundReceipt,
    TriggerRefundInput,
)

logger = logging.getLogger(__name__)

MAX_REPORTED_ERRORS = 20
"""Cap on field errors in one response, so a call with 10,000 unknown keys gets a short answer."""

MAX_FIELD_NAME_CHARS = 100

type Handler = Callable[[Any, CustomerStore], types.CallToolResult]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    title: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    annotations: types.ToolAnnotations
    handler: Handler

    def to_tool(self) -> types.Tool:
        return types.Tool(
            name=self.name,
            title=self.title,
            description=self.description,
            input_schema=self.input_model.model_json_schema(),
            output_schema=self.output_model.model_json_schema(),
            annotations=self.annotations,
        )


def _ok(model: BaseModel) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=model.model_dump_json())],
        structured_content=model.model_dump(mode="json"),
    )


def _tool_error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)],
        is_error=True,
    )


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01")))


def _get_customer_record(
    args: GetCustomerRecordInput, store: CustomerStore
) -> types.CallToolResult:
    customer = store.get(args.customer_id)
    if customer is None:
        return _tool_error(f"No customer with ID {args.customer_id}.")
    return _ok(
        CustomerRecord(
            customer_id=customer.customer_id,
            name=customer.name,
            email=customer.email,
            tier=customer.tier,
            refundable_balance=_money(store.balance(customer.customer_id)),
        )
    )


def _trigger_refund(args: TriggerRefundInput, store: CustomerStore) -> types.CallToolResult:
    if store.get(args.customer_id) is None:
        return _tool_error(f"No customer with ID {args.customer_id}.")
    amount = Decimal(str(args.amount))
    try:
        refund = store.refund(args.customer_id, amount, args.reason, args.idempotency_key)
    except RefundRejected as exc:
        return _tool_error(str(exc))
    return _ok(
        RefundReceipt(
            refund_id=refund.refund_id,
            customer_id=args.customer_id,
            amount=_money(amount),
            # "already_processed" tells a retrying agent nothing new happened.
            status="already_processed" if refund.replayed else "processed",
            remaining_refundable_balance=_money(refund.remaining),
        )
    )


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="get_customer_record",
        title="Get customer record",
        description="Look up a customer's profile and remaining refundable balance by customer ID.",
        input_model=GetCustomerRecordInput,
        output_model=CustomerRecord,
        annotations=types.ToolAnnotations(read_only_hint=True, open_world_hint=False),
        handler=_get_customer_record,
    ),
    ToolSpec(
        name="trigger_refund",
        title="Trigger refund",
        description=(
            "Issue a refund to a customer. Moves money and cannot be undone, so confirm "
            "the amount and reason with the user first. Pass an idempotency_key and reuse "
            "it on retries so a retry never refunds twice."
        ),
        input_model=TriggerRefundInput,
        output_model=RefundReceipt,
        annotations=types.ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=False,
        ),
        handler=_trigger_refund,
    ),
)

TOOLS_BY_NAME: Mapping[str, ToolSpec] = {spec.name: spec for spec in TOOLS}


def validation_error_data(tool: str, exc: ValidationError) -> dict[str, Any]:
    """Field-level detail for ``error.data``. Never includes the rejected values."""
    errors = exc.errors(include_input=False, include_url=False, include_context=False)
    reported = [
        {
            "field": (".".join(str(part) for part in err["loc"]) or "(arguments)")[
                :MAX_FIELD_NAME_CHARS
            ],
            "type": err["type"],
            "message": err["msg"],
        }
        for err in errors[:MAX_REPORTED_ERRORS]
    ]
    data: dict[str, Any] = {"tool": tool, "errors": reported}
    if len(errors) > MAX_REPORTED_ERRORS:
        data["truncated"] = True
    return data


def call_tool(
    name: str,
    arguments: Mapping[str, Any] | None,
    store: CustomerStore,
    *,
    request_id: str | int | None = None,
) -> types.CallToolResult:
    """Validate and run one tool call. Raises ``MCPError`` for protocol-level failures."""
    spec = TOOLS_BY_NAME.get(name)
    if spec is None:
        log_event(logger, logging.WARNING, "unknown tool", request_id=request_id)
        raise MCPError(INVALID_PARAMS, "Unknown tool", {"available_tools": sorted(TOOLS_BY_NAME)})

    try:
        args = spec.input_model.model_validate(dict(arguments or {}))
    except ValidationError as exc:
        data = validation_error_data(name, exc)
        log_event(
            logger,
            logging.INFO,
            "invalid params",
            tool=name,
            request_id=request_id,
            fields=[e["field"] for e in data["errors"]],
        )
        raise MCPError(INVALID_PARAMS, "Invalid params", data) from None

    try:
        result = spec.handler(args, store)
    except Exception:  # last line of defence: details are logged, never returned
        logger.exception("tool %s failed", name, extra={"fields": {"request_id": request_id}})
        raise MCPError(INTERNAL_ERROR, "Internal error") from None

    log_event(
        logger,
        logging.INFO,
        "tool call",
        tool=name,
        request_id=request_id,
        outcome="tool_error" if result.is_error else "ok",
    )
    return result
