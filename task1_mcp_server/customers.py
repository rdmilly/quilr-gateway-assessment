"""An in-memory stand-in for the customer database.

The data is fake (``example.com`` addresses). Balances are ``Decimal`` so money
arithmetic is exact: ``0.1 + 0.2`` is ``0.3`` here, not ``0.30000000000000004``.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Customer:
    customer_id: str
    name: str
    email: str
    tier: str


class RefundRejected(Exception):
    """A business rule said no. The tool reports this as a tool error, not a protocol error."""


_SEED: tuple[tuple[Customer, Decimal], ...] = (
    (Customer("CUST-00042", "Ada Lovelace", "ada@example.com", "gold"), Decimal("250.00")),
    (Customer("CUST-10001", "Grace Hopper", "grace@example.com", "silver"), Decimal("120.00")),
    (Customer("CUST-12345", "Alan Turing", "alan@example.com", "standard"), Decimal("75.50")),
)


class CustomerStore:
    """Customers plus each one's remaining refundable balance.

    A refund checks and reduces the balance under one lock, so two refunds that
    arrive together cannot both spend the same money.
    """

    def __init__(self) -> None:
        self._customers = {c.customer_id: c for c, _ in _SEED}
        self._balances = {c.customer_id: balance for c, balance in _SEED}
        self._lock = threading.Lock()

    def get(self, customer_id: str) -> Customer | None:
        return self._customers.get(customer_id)

    def balance(self, customer_id: str) -> Decimal:
        with self._lock:
            return self._balances[customer_id]

    def refund(self, customer_id: str, amount: Decimal) -> tuple[str, Decimal]:
        """Reduce the balance by ``amount``; return ``(refund_id, remaining balance)``."""
        with self._lock:
            balance = self._balances[customer_id]
            if amount > balance:
                raise RefundRejected(
                    f"Refund of {amount} exceeds the refundable balance of {balance} "
                    f"for {customer_id}."
                )
            self._balances[customer_id] = balance - amount
            return f"rf_{uuid.uuid4().hex}", balance - amount
