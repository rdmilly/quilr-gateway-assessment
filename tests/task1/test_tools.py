"""The dispatcher and the business rules, called directly."""

import dataclasses
import logging
from decimal import Decimal
from typing import NoReturn

import pytest
from mcp import MCPError

from task1_mcp_server import tools
from task1_mcp_server.customers import CustomerStore
from task1_mcp_server.tools import TOOLS, call_tool

REASON = "Item arrived damaged in transit"


def swap_handler(monkeypatch: pytest.MonkeyPatch, name: str, handler: tools.Handler) -> None:
    spec = dataclasses.replace(tools.TOOLS_BY_NAME[name], handler=handler)
    monkeypatch.setitem(tools.TOOLS_BY_NAME, name, spec)


@pytest.fixture
def store() -> CustomerStore:
    return CustomerStore()


def test_lookup_returns_the_record(store: CustomerStore) -> None:
    result = call_tool("get_customer_record", {"customer_id": "CUST-00042"}, store)
    assert not result.is_error
    assert result.structured_content == {
        "customer_id": "CUST-00042",
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "tier": "gold",
        "refundable_balance": "250.00",
    }


def test_unknown_customer_is_a_tool_error_not_a_protocol_error(store: CustomerStore) -> None:
    result = call_tool("get_customer_record", {"customer_id": "CUST-99999"}, store)
    assert result.is_error
    assert "CUST-99999" in result.content[0].text  # type: ignore[union-attr]


def test_refund_reduces_the_balance_exactly(store: CustomerStore) -> None:
    for _ in range(3):
        result = call_tool(
            "trigger_refund",
            {"customer_id": "CUST-10001", "amount": 0.1, "reason": REASON},
            store,
        )
        assert not result.is_error
    assert store.balance("CUST-10001") == Decimal("119.70")  # not 119.69999999
    assert result.structured_content is not None
    assert result.structured_content["remaining_refundable_balance"] == "119.70"
    assert result.structured_content["amount"] == "0.10"
    assert result.structured_content["refund_id"].startswith("rf_")


def test_refund_over_the_balance_is_refused(store: CustomerStore) -> None:
    result = call_tool(
        "trigger_refund", {"customer_id": "CUST-12345", "amount": 75.51, "reason": REASON}, store
    )
    assert result.is_error
    assert store.balance("CUST-12345") == Decimal("75.50")


def test_refund_for_unknown_customer_is_refused(store: CustomerStore) -> None:
    result = call_tool(
        "trigger_refund", {"customer_id": "CUST-99999", "amount": 1, "reason": REASON}, store
    )
    assert result.is_error


def test_unknown_tool_is_invalid_params(store: CustomerStore) -> None:
    with pytest.raises(MCPError) as exc:
        call_tool("delete_everything", {}, store)
    assert exc.value.error.code == -32602
    assert exc.value.error.data == {"available_tools": ["get_customer_record", "trigger_refund"]}


def test_bad_arguments_never_reach_the_tool(
    store: CustomerStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def must_not_run(*_: object) -> NoReturn:
        raise AssertionError("handler ran with invalid arguments")

    swap_handler(monkeypatch, "trigger_refund", must_not_run)
    with pytest.raises(MCPError) as exc:
        call_tool("trigger_refund", {"customer_id": "CUST-00042", "amount": -5}, store)
    assert exc.value.error.code == -32602
    assert store.balance("CUST-00042") == Decimal("250.00")


def test_error_data_names_fields_but_never_echoes_values(store: CustomerStore) -> None:
    secret = "sk-live-do-not-echo-0123456789"
    with pytest.raises(MCPError) as exc:
        call_tool(
            "trigger_refund",
            {"customer_id": secret, "amount": "1e9", "reason": secret + "\x00", "note": secret},
            store,
        )
    data = exc.value.error.data
    assert data["tool"] == "trigger_refund"
    assert {e["field"] for e in data["errors"]} == {"customer_id", "amount", "reason", "note"}
    assert secret not in repr(exc.value.error)


def test_missing_arguments_object_is_treated_as_empty(store: CustomerStore) -> None:
    with pytest.raises(MCPError) as exc:
        call_tool("get_customer_record", None, store)
    assert exc.value.error.data["errors"] == [
        {"field": "customer_id", "type": "missing", "message": "Field required"}
    ]


def test_flood_of_unknown_fields_gets_a_short_answer(store: CustomerStore) -> None:
    flood = {f"k{i}": i for i in range(10_000)} | {"customer_id": "CUST-00042"}
    with pytest.raises(MCPError) as exc:
        call_tool("get_customer_record", flood, store)
    data = exc.value.error.data
    assert len(data["errors"]) == tools.MAX_REPORTED_ERRORS
    assert data["truncated"] is True


def test_long_field_names_are_cut_short(store: CustomerStore) -> None:
    with pytest.raises(MCPError) as exc:
        call_tool("get_customer_record", {"customer_id": "CUST-00042", "k" * 5000: 1}, store)
    assert len(exc.value.error.data["errors"][0]["field"]) == tools.MAX_FIELD_NAME_CHARS


def test_unexpected_failure_is_a_generic_internal_error(
    store: CustomerStore, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*_: object) -> NoReturn:
        raise RuntimeError("db password is hunter2")

    swap_handler(monkeypatch, "get_customer_record", broken)
    with caplog.at_level(logging.ERROR), pytest.raises(MCPError) as exc:
        call_tool("get_customer_record", {"customer_id": "CUST-00042"}, store)
    assert exc.value.error.code == -32603
    assert exc.value.error.message == "Internal error"
    assert "hunter2" not in repr(exc.value.error)
    assert "hunter2" in caplog.text  # the operator still gets the detail, on stderr


def test_tool_definitions() -> None:
    by_name = {spec.name: spec.to_tool() for spec in TOOLS}
    assert set(by_name) == {"get_customer_record", "trigger_refund"}
    refund = by_name["trigger_refund"]
    assert refund.annotations is not None
    assert refund.annotations.destructive_hint is True
    assert refund.annotations.idempotent_hint is False
    lookup = by_name["get_customer_record"]
    assert lookup.annotations is not None
    assert lookup.annotations.read_only_hint is True
    assert lookup.input_schema["required"] == ["customer_id"]
    assert lookup.output_schema is not None


def test_concurrent_refunds_cannot_overspend(store: CustomerStore) -> None:
    """Fifty threads each try to refund 10.00 from a 120.00 balance: exactly 12 succeed."""
    import threading

    results: list[bool] = []
    record = threading.Lock()
    start = threading.Barrier(50)

    def attempt() -> None:
        start.wait()
        result = call_tool(
            "trigger_refund", {"customer_id": "CUST-10001", "amount": 10, "reason": REASON}, store
        )
        with record:
            results.append(not result.is_error)

    threads = [threading.Thread(target=attempt) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 12
    assert store.balance("CUST-10001") == Decimal("0.00")
