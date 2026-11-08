"""Task 2: an MCP security gateway.

An HTTP reverse proxy between an agent and a downstream MCP server. It reads the
caller's bearer token to find their role, parses each JSON-RPC message once and
strictly, forwards ``tools/list`` and everything else unchanged, and refuses
``tools/call`` on any tool named ``admin_...`` unless the caller is an admin,
answering ``-32001 Unauthorized Tool Call`` without contacting the server.
"""

__version__ = "1.0.0"
