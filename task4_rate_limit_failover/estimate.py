"""Estimate, before sending a chat request, how many tokens it can use.

The limiter has to decide before the provider answers, so it charges an upper-bound guess
first and corrects it to the provider's reported usage afterwards (see limiter.py).

The guess is the prompt size plus the most the model is allowed to write:

* Prompt: UTF-8 bytes of the messages and tools, divided by 4. English averages about four
  characters per token, and counting bytes instead of characters keeps the guess on the safe
  side for other scripts (a CJK character is three bytes and usually about one token).
* Completion: ``max_completion_tokens``, else ``max_tokens``, else a configured default.
"""

from __future__ import annotations

import json
import math
from typing import Any, Final

BYTES_PER_TOKEN: Final = 4


class InvalidRequest(ValueError):
    """The request body is missing something the estimate needs. The message is safe to show."""


def estimate_tokens(body: dict[str, Any], default_max_tokens: int) -> int:
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise InvalidRequest("messages must be a non-empty array.")
    tools = body.get("tools")
    if tools is not None and not isinstance(tools, list):
        raise InvalidRequest("tools must be an array.")
    prompt = json.dumps(messages, ensure_ascii=False) + json.dumps(tools or [], ensure_ascii=False)
    prompt_tokens = math.ceil(len(prompt.encode()) / BYTES_PER_TOKEN)

    cap = body.get("max_completion_tokens", body.get("max_tokens"))
    if cap is None:
        completion_tokens = default_max_tokens
    elif isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise InvalidRequest("max_tokens must be a positive integer.")
    else:
        completion_tokens = cap

    choices = body.get("n", 1)
    if choices is None:
        choices = 1
    if isinstance(choices, bool) or not isinstance(choices, int) or not 1 <= choices <= 128:
        raise InvalidRequest("n must be an integer from 1 to 128.")
    # Each extra choice is another full answer the provider bills for.
    return prompt_tokens + completion_tokens * choices
