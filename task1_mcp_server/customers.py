"""An in-memory stand-in for the customer database.

The data is fake (``example.com`` addresses). Balances are ``Decimal`` so money
arithmetic is exact: ``0.1 + 0.2`` is ``0.3`` here, not ``0.30000000000000004``.
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal

MAX_REMEMBERED_KEYS = 10_000
"""Idempotency keys kept in memory; the oldest are forgotten first, so memory stays bounded."""


@dataclass(frozen=True, slots=True)
class Customer:
    customer_id: str
    name: str
    email: str
    tier: str


class RefundRejected(Exception):
    """A business rule said no. The tool reports this as a tool error, not a protocol error."""


@dataclass(frozen=True, slots=True)
class Refund:
    refund_id: str
    remaining: Decimal
    replayed: bool = False  # True when this is the stored result of an earlier, identical call


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
        # idempotency key -> (what the first call asked for, what it returned)
        self._by_key: OrderedDict[str, tuple[tuple[str, Decimal, str], Refund]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, customer_id: str) -> Customer | None:
        return self._customers.get(customer_id)

    def balance(self, customer_id: str) -> Decimal:
        with self._lock:
            return self._balances[customer_id]

    def refund(
        self,
        customer_id: str,
        amount: Decimal,
        reason: str = "",
        idempotency_key: str | None = None,
    ) -> Refund:
        """Reduce the balance by ``amount``, at most once per idempotency key.

        The key check, the balance check and the update all happen under one
        lock, so two identical retries arriving together still pay out once.
        """
        request = (customer_id, amount, reason)
        with self._lock:
            if idempotency_key is not None and idempotency_key in self._by_key:
                first_request, first_result = self._by_key[idempotency_key]
                if first_request != request:
                    raise RefundRejected(
                        "This idempotency key was already used for a different refund."
                    )
                return Refund(first_result.refund_id, first_result.remaining, replayed=True)

            balance = self._balances[customer_id]
            if amount > balance:
                raise RefundRejected(
                    f"Refund of {amount} exceeds the refundable balance of {balance} "
                    f"for {customer_id}."
                )
            self._balances[customer_id] = balance - amount
            result = Refund(f"rf_{uuid.uuid4().hex}", balance - amount)
            if idempotency_key is not None:
                self._by_key[idempotency_key] = (request, result)
                if len(self._by_key) > MAX_REMEMBERED_KEYS:
                    self._by_key.popitem(last=False)
            return result
