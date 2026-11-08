"""Task 1: a customer-service MCP server over stdio.

Two tools, ``get_customer_record`` and ``trigger_refund``. Every argument is
checked by a strict Pydantic model before any tool code runs; a bad argument
becomes a JSON-RPC ``-32602 Invalid params`` error. Stdout carries only
JSON-RPC messages; logs go to stderr.
"""

__version__ = "1.0.0"
