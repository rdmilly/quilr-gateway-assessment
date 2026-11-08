"""Input and output models for the two tools.

The models are the single source of truth: the JSON Schema each tool advertises
in ``tools/list`` is generated from them, and the same models validate every
``tools/call``. There is no second hand-written schema to drift out of sync.

Strict mode means no type coercion: ``"10.00"`` is not a number, ``true`` is not
a number, ``12345`` is not a customer ID. ``extra="forbid"`` rejects arguments
the tool does not declare, so a caller cannot smuggle in fields like
``approved_by`` that some later code path might trust.
"""

from __future__ import annotations

import math
import unicodedata
from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

CUSTOMER_ID_PATTERN = r"^CUST-[0-9]{5}$"
"""``CUST-`` then exactly five ASCII digits.

Pydantic runs this pattern with Rust's regex engine, where ``$`` means the true
end of the string (Python's ``re`` would also accept a trailing newline) and
``[0-9]`` matches only ASCII digits, so full-width or Arabic-Indic digits that
Python's ``\\d`` would accept are rejected. The ID stays a string end to end,
so leading zeros (``CUST-00042``) are never lost.
"""

REASON_MIN_CHARS = 10
REASON_MAX_CHARS = 500
MAX_DECIMAL_PLACES = 2

# Unicode categories that are invisible or not text: control (Cc), format such
# as zero-width spaces and bidi overrides (Cf), lone surrogates (Cs), private
# use (Co) and unassigned (Cn). Allowing them would let ten invisible
# characters pass a "ten character" rule, or hide text from a reviewer.
_DISALLOWED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


def _check_amount(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("amount must be a finite number")
    # str() gives the shortest decimal that round-trips the float, so 49.99 is
    # read as "49.99", not as the binary approximation 49.9899999...
    exponent = Decimal(str(value)).as_tuple().exponent
    if not isinstance(exponent, int) or -exponent > MAX_DECIMAL_PLACES:
        raise ValueError(f"amount can have at most {MAX_DECIMAL_PLACES} decimal places")
    return value


def _check_reason(value: str) -> str:
    for char in value:
        if unicodedata.category(char) in _DISALLOWED_CATEGORIES:
            raise ValueError("reason contains control or invisible characters")
    if len(value.strip()) < REASON_MIN_CHARS:
        raise ValueError(
            f"reason must have at least {REASON_MIN_CHARS} characters, not counting "
            "leading or trailing whitespace"
        )
    return value


CustomerId = Annotated[
    str,
    Field(
        pattern=CUSTOMER_ID_PATTERN,
        description="Customer ID: CUST- followed by exactly five digits, e.g. CUST-00042.",
        examples=["CUST-00042"],
    ),
]

Amount = Annotated[
    float,
    Field(
        gt=0,
        allow_inf_nan=False,
        description="Refund amount in US dollars: positive, at most two decimal places.",
        examples=[49.99],
    ),
    AfterValidator(_check_amount),
]

Reason = Annotated[
    str,
    Field(
        min_length=REASON_MIN_CHARS,
        max_length=REASON_MAX_CHARS,
        description=f"Why the refund is being issued, {REASON_MIN_CHARS} to "
        f"{REASON_MAX_CHARS} characters.",
        examples=["Item arrived damaged in transit"],
    ),
    AfterValidator(_check_reason),
]


class _Model(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class GetCustomerRecordInput(_Model):
    customer_id: CustomerId


class TriggerRefundInput(_Model):
    customer_id: CustomerId
    amount: Amount
    reason: Reason


class CustomerRecord(_Model):
    customer_id: str
    name: str
    email: str
    tier: str
    refundable_balance: str = Field(description="Remaining refundable amount, e.g. '120.00'.")


class RefundReceipt(_Model):
    refund_id: str
    customer_id: str
    amount: str = Field(description="Amount refunded, as an exact decimal string.")
    status: str
    remaining_refundable_balance: str
