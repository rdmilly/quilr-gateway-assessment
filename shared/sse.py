"""Read a server-sent event stream as it arrives, piece by piece (Tasks 3 and 4).

Network reads cut the stream anywhere: in the middle of a line, between the
``\\r`` and ``\\n`` of a line ending, or inside a multi-byte UTF-8 character.
This reader keeps the unfinished part until the rest arrives and hands back
each complete event's ``data`` once it is whole. Only ``data`` matters for
chat completions; comments (lines starting with ``:``) and other fields are
dropped.
"""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field

MAX_EVENT_CHARS = 1_048_576  # one event bigger than this is refused, not buffered forever


class EventTooLarge(Exception):
    pass


@dataclass
class SSEReader:
    _decoder: codecs.IncrementalDecoder = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")(errors="replace")
    )
    _line: str = ""
    _data: list[str] = field(default_factory=list)
    _size: int = 0

    def feed(self, chunk: bytes) -> list[str]:
        """The data of every event completed by this chunk, in order."""
        return self._take(self._decoder.decode(chunk))

    def finish(self) -> list[str]:
        """End of the stream: an event without its closing blank line still counts."""
        events = self._take(self._decoder.decode(b"", final=True) + "\n\n")
        return events

    def _take(self, text: str) -> list[str]:
        events: list[str] = []
        self._line += text
        while True:
            # A line ends at \n, \r\n or a lone \r. A trailing \r might be the first
            # half of \r\n, so wait for the next character before deciding.
            ends = [i for i in (self._line.find("\n"), self._line.find("\r")) if i != -1]
            cut = min(ends) if ends else -1
            if cut != -1 and self._line[cut] == "\r" and cut + 1 == len(self._line):
                cut = -1  # might be \r\n split across chunks
            if cut == -1:
                if len(self._line) > MAX_EVENT_CHARS:
                    raise EventTooLarge
                return events
            line = self._line[:cut]
            skip = 2 if self._line[cut] == "\r" and self._line[cut + 1 : cut + 2] == "\n" else 1
            self._line = self._line[cut + skip :]
            if line == "":
                if self._data:
                    events.append("\n".join(self._data))
                self._data, self._size = [], 0
            elif line.startswith("data:"):
                value = line[5:]
                value = value[1:] if value.startswith(" ") else value
                self._size += len(value)
                if self._size > MAX_EVENT_CHARS:
                    raise EventTooLarge
                self._data.append(value)
            # Comments (":...") and other fields (event:, id:, retry:) are not needed.
