"""Edge cases for the argument models. These run without any MCP machinery."""

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from task1_mcp_server.schemas import GetCustomerRecordInput, TriggerRefundInput

GOOD_REASON = "Item arrived damaged in transit"


def refund(**overrides: object) -> TriggerRefundInput:
    args: dict[str, object] = {
        "customer_id": "CUST-00042",
        "amount": 49.99,
        "reason": GOOD_REASON,
    }
    args.update(overrides)
    return TriggerRefundInput.model_validate(args)


def error_types(exc: pytest.ExceptionInfo[ValidationError]) -> set[str]:
    return {e["type"] for e in exc.value.errors()}


# --- customer_id -----------------------------------------------------------


@pytest.mark.parametrize("customer_id", ["CUST-00000", "CUST-00042", "CUST-12345", "CUST-99999"])
def test_valid_customer_ids(customer_id: str) -> None:
    assert GetCustomerRecordInput.model_validate({"customer_id": customer_id}).customer_id == (
        customer_id
    )


def test_leading_zeros_are_kept() -> None:
    parsed = GetCustomerRecordInput.model_validate({"customer_id": "CUST-00042"})
    assert parsed.customer_id == "CUST-00042"
    assert isinstance(parsed.customer_id, str)


@pytest.mark.parametrize(
    "customer_id",
    [
        "",
        "CUST-",
        "CUST-1234",  # too short
        "CUST-123456",  # too long
        "cust-12345",  # wrong case
        "CUST_12345",  # wrong separator
        "CUST-1234a",
        " CUST-12345",  # leading space
        "CUST-12345 ",  # trailing space
        "CUST-12345\n",  # trailing newline: Python's re "$" would accept this
        "CUST-\uff11\uff12\uff13\uff14\uff15",  # full-width digits
        "CUST-\u0661\u0662\u0663\u0664\u0665",  # Arabic-Indic digits
        "CUST-12345CUST-12345",
        "XCUST-12345",
    ],
)
def test_malformed_customer_ids_are_rejected(customer_id: str) -> None:
    with pytest.raises(ValidationError) as exc:
        GetCustomerRecordInput.model_validate({"customer_id": customer_id})
    assert error_types(exc) == {"string_pattern_mismatch"}


def test_lone_surrogate_in_customer_id_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        GetCustomerRecordInput.model_validate({"customer_id": "CUST-0004\ud800"})
    assert error_types(exc) == {"string_unicode"}


@pytest.mark.parametrize("customer_id", [12345, None, ["CUST-12345"], {"id": "CUST-12345"}])
def test_customer_id_must_be_a_string(customer_id: object) -> None:
    with pytest.raises(ValidationError) as exc:
        GetCustomerRecordInput.model_validate({"customer_id": customer_id})
    assert error_types(exc) == {"string_type"}


@given(st.text(max_size=20))
def test_customer_id_accepted_exactly_when_it_matches(customer_id: str) -> None:
    expected = re.fullmatch(r"CUST-[0-9]{5}", customer_id) is not None
    try:
        GetCustomerRecordInput.model_validate({"customer_id": customer_id})
        accepted = True
    except ValidationError:
        accepted = False
    assert accepted is expected


@given(st.from_regex(r"\ACUST-[0-9]{5}\Z"))
def test_every_well_formed_id_is_accepted(customer_id: str) -> None:
    assert GetCustomerRecordInput.model_validate({"customer_id": customer_id})


# --- amount ----------------------------------------------------------------


@pytest.mark.parametrize("amount", [0.01, 1, 10, 49.99, 100.5, 1000.0, 99999.99])
def test_valid_amounts(amount: float) -> None:
    assert refund(amount=amount).amount == amount


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (0, "greater_than"),
        (0.0, "greater_than"),
        (-0.0, "greater_than"),
        (-1, "greater_than"),
        (-49.99, "greater_than"),
        (float("nan"), "finite_number"),
        (float("inf"), "finite_number"),
        (float("-inf"), "finite_number"),
        (0.001, "value_error"),  # sub-cent
        (10.555, "value_error"),
        (1e-7, "value_error"),
        ("10.00", "float_type"),  # no string coercion
        ("ten", "float_type"),
        (True, "float_type"),  # bool is not a number here
        (None, "float_type"),
        ([10], "float_type"),
    ],
)
def test_invalid_amounts(amount: object, expected: str) -> None:
    with pytest.raises(ValidationError) as exc:
        refund(amount=amount)
    assert expected in error_types(exc)


def test_amount_too_large_for_a_float_is_rejected() -> None:
    with pytest.raises(ValidationError):
        refund(amount=10**400)


@given(st.integers(min_value=1, max_value=10**12))
def test_any_whole_number_of_cents_is_accepted(cents: int) -> None:
    assert refund(amount=cents / 100).amount == cents / 100


# --- reason ----------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "0123456789",  # exactly 10
        GOOD_REASON,
        "  padded but still long enough  ",
        "Remboursement: colis endommagé",  # accented text is fine
        "退款原因：商品在运输中损坏了",  # so is any script  # noqa: RUF001
        "x" * 500,
    ],
)
def test_valid_reasons(reason: str) -> None:
    assert refund(reason=reason).reason == reason


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("", "string_too_short"),
        ("too short", "string_too_short"),  # 9 characters
        (" " * 12, "value_error"),  # long enough only because of padding
        ("   short   ", "value_error"),
        ("x" * 501, "string_too_long"),
        ("valid text\x00here", "value_error"),  # NUL
        ("valid text\x1b[31m", "value_error"),  # terminal escape
        ("abcdefghij​", "value_error"),  # zero-width space
        ("​" * 12, "value_error"),
        ("refund for ‮evil", "value_error"),  # right-to-left override
        ("lone \ud800 surrogate", "string_unicode"),  # Pydantic refuses these itself
        ("private use ", "value_error"),
        (1234567890, "string_type"),
        (None, "string_type"),
    ],
)
def test_invalid_reasons(reason: object, expected: str) -> None:
    with pytest.raises(ValidationError) as exc:
        refund(reason=reason)
    assert expected in error_types(exc)


# --- the object as a whole -------------------------------------------------


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        refund(approved_by="manager")
    assert error_types(exc) == {"extra_forbidden"}


def test_missing_fields_are_all_reported() -> None:
    with pytest.raises(ValidationError) as exc:
        TriggerRefundInput.model_validate({})
    assert {tuple(e["loc"]) for e in exc.value.errors()} == {
        ("customer_id",),
        ("amount",),
        ("reason",),
    }


def test_advertised_schema_matches_the_rules() -> None:
    schema = TriggerRefundInput.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"customer_id", "amount", "reason"}
    props = schema["properties"]
    assert props["customer_id"]["pattern"] == "^CUST-[0-9]{5}$"
    assert props["amount"]["exclusiveMinimum"] == 0
    assert props["reason"]["minLength"] == 10
    assert props["reason"]["maxLength"] == 500
