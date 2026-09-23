# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Finite, lossless output staging; never wait for a slow output consumer.

Spools are private, anonymous TemporaryFiles. Only bytes serialized by this
process are read back; no filenames or serialized input are accepted from users.
Limits measure serialized bytes, not interpreter allocator/RSS overhead.
"""

from __future__ import annotations

import asyncio
import io
import pickle
import tempfile
from collections import deque
from dataclasses import dataclass
from typing import Any


class OutputBudgetExceeded(RuntimeError):
    """Host output delivery failed; this is not a Provider terminal result."""

    code = "OUTPUT_BUDGET_EXCEEDED"


@dataclass(frozen=True, slots=True)
class OutputLimits:
    """Positive limits for serialized queue items and storage."""

    max_items: int = 8192
    memory_bytes: int = 4 * 1024 * 1024
    storage_bytes: int = 256 * 1024 * 1024
    item_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        values = (self.max_items, self.memory_bytes, self.storage_bytes, self.item_bytes)
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in values):
            raise ValueError("output limits must be positive")


class OutputBudget:
    """One shared accounting scope across queues, including ownership handoff."""

    def __init__(self, limits: OutputLimits | None = None) -> None:
        self.limits = limits or OutputLimits()
        self.items = 0
        self.memory_bytes = 0
        self.storage_bytes = 0


class _LimitedBytes(io.BytesIO):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit

    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > self.limit:
            raise OutputBudgetExceeded("single output item byte budget exhausted")
        return super().write(data)


class OutputBuffer:
    """Queue-compatible staging with finite memory and disk, no blocking puts.

    ``memory_items`` is a RAM threshold, not permission to grow without bound.
    Disk accounting includes consumed extents until the spool becomes empty.
    Failures wake readers after already accepted output has been drained.
    """

    def __init__(self, budget: OutputBudget | None = None, *, memory_items: int = 128) -> None:
        self.budget = budget or OutputBudget()
        self.maxsize = max(1, memory_items)
        self._entries: deque[bytes | tuple[int, int]] = deque()
        self._ready = asyncio.Event()
        self._file: Any = None
        self._disk_bytes = 0
        self._disk_items = 0
        self._error: BaseException | None = None
        self._finished = False

    def qsize(self) -> int:
        """Return the number of accepted items awaiting delivery."""
        return len(self._entries)

    def empty(self) -> bool:
        """Return whether all accepted items have been delivered."""
        return not self._entries

    async def put(self, item: Any) -> None:
        """Stage an item without waiting for a consumer."""
        self.put_nowait(item)

    def put_nowait(self, item: Any) -> None:
        """Stage one complete item or raise an explicit capacity error."""
        if self._error is not None:
            raise type(self._error)(str(self._error)) from None
        if self._finished:
            raise RuntimeError("output buffer is closed")
        budget = self.budget
        limits = budget.limits
        if budget.items >= limits.max_items:
            raise OutputBudgetExceeded("output item count budget exhausted")
        try:
            stream = _LimitedBytes(limits.item_bytes)
            pickle.dump(item, stream, protocol=pickle.HIGHEST_PROTOCOL)
            data = stream.getvalue()
        except OutputBudgetExceeded:
            raise
        except Exception as exc:
            raise OutputBudgetExceeded("output cannot be staged losslessly") from exc
        size = len(data)
        if size > limits.item_bytes:
            raise OutputBudgetExceeded("single output item byte budget exhausted")
        if len(self._entries) < self.maxsize and budget.memory_bytes + size <= limits.memory_bytes:
            self._entries.append(data)
            budget.memory_bytes += size
        else:
            if budget.storage_bytes + size > limits.storage_bytes:
                raise OutputBudgetExceeded("output spool byte budget exhausted")
            try:
                if self._file is None:
                    self._file = tempfile.TemporaryFile(prefix="harness-output-")
                self._file.seek(self._disk_bytes)
                written = self._file.write(data)
                if written != size:
                    raise OSError("short output spool write")
            except OSError as exc:
                raise OutputBudgetExceeded("output spool write failed") from exc
            self._entries.append((self._disk_bytes, size))
            self._disk_bytes += size
            self._disk_items += 1
            budget.storage_bytes += size
        budget.items += 1
        self._ready.set()

    def get_nowait(self) -> Any:
        """Read one accepted item and release its accounting."""
        if not self._entries:
            if self._error is not None:
                raise type(self._error)(str(self._error)) from None
            if self._finished:
                raise EOFError("output buffer finished")
            raise asyncio.QueueEmpty
        entry = self._entries.popleft()
        self.budget.items -= 1
        try:
            if isinstance(entry, bytes):
                self.budget.memory_bytes -= len(entry)
                data = entry
            else:
                offset, size = entry
                self._file.seek(offset)
                data = self._file.read(size)
                self._disk_items -= 1
                if len(data) != size:
                    raise OSError("short output spool read")
                if not self._disk_items:
                    self._release_file()
            return pickle.loads(data)  # private anonymous file; never external serialized data
        except Exception as exc:
            error = OutputBudgetExceeded("output spool read failed")
            self.fail(error)
            raise error from exc
        finally:
            if not self._entries and not self._finished and self._error is None:
                self._ready.clear()

    async def wait_ready(self) -> None:
        """Wait until an item, failure, or end of stream is available."""
        await self._ready.wait()

    async def get(self) -> Any:
        """Wait for and retrieve one accepted item."""
        while True:
            try:
                return self.get_nowait()
            except asyncio.QueueEmpty:
                await self._ready.wait()

    def fail(self, error: BaseException) -> None:
        """Wake readers with an error after accepted items drain."""
        self._error = error
        self._ready.set()

    def finish(self) -> None:
        """End admission while preserving accepted items for readers."""
        self._finished = True
        self._ready.set()

    def _release_file(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        self.budget.storage_bytes -= self._disk_bytes
        self._disk_bytes = 0
        self._disk_items = 0

    def close(self) -> None:
        """Release storage and accounting; safe to call repeatedly."""
        for entry in self._entries:
            self.budget.items -= 1
            if isinstance(entry, bytes):
                self.budget.memory_bytes -= len(entry)
        self._entries.clear()
        self._release_file()
        self.finish()


class OutputText:
    """Complete UTF-8 text with a finite total and a small in-memory prefix."""

    def __init__(
        self, *, memory_bytes: int = 256 * 1024, max_bytes: int = 64 * 1024 * 1024, budget: OutputBudget | None = None
    ) -> None:
        if min(memory_bytes, max_bytes) < 1:
            raise ValueError("text limits must be positive")
        # Lifetime spans multiple reads/appends; close() releases the owned spool.
        self._file = tempfile.SpooledTemporaryFile(  # pylint: disable=consider-using-with
            max_size=memory_bytes, mode="w+b"
        )
        self.budget = budget
        self._memory_bytes = memory_bytes
        self.max_bytes = max_bytes
        self.size = 0

    def __bool__(self) -> bool:
        return bool(self.size)

    def append(self, text: str) -> None:
        """Append complete UTF-8 text within the storage limits."""
        data = text.encode("utf-8")
        if self.budget is not None and self.budget.storage_bytes + len(data) > self.budget.limits.storage_bytes:
            raise OutputBudgetExceeded("projection text total byte budget exhausted")
        if self.size + len(data) > self.max_bytes:
            raise OutputBudgetExceeded("output text byte budget exhausted")
        try:
            if self.size + len(data) > self._memory_bytes:
                self._file.rollover()
            self._file.seek(self.size)
            if self._file.write(data) != len(data):
                raise OSError("short text spool write")
        except OSError as exc:
            raise OutputBudgetExceeded("output text spool write failed") from exc
        self.size += len(data)
        if self.budget is not None:
            self.budget.storage_bytes += len(data)

    def clear(self) -> None:
        """Discard text and release its shared budget."""
        self.replace("")

    def replace(self, text: str) -> None:
        """Replace text after validating the new size against limits."""
        new_size = len(text.encode("utf-8"))
        if (
            self.budget is not None
            and self.budget.storage_bytes - self.size + new_size > self.budget.limits.storage_bytes
        ):
            raise OutputBudgetExceeded("projection text total byte budget exhausted")
        if new_size > self.max_bytes:
            raise OutputBudgetExceeded("output text byte budget exhausted")
        self._file.seek(0)
        self._file.truncate()
        if self.budget is not None:
            self.budget.storage_bytes -= self.size
        self.size = 0
        self.append(text)

    def __iter__(self):
        yield self.read()

    def read(self) -> str:
        """Read the complete text or report a storage failure."""
        try:
            self._file.seek(0)
            data = self._file.read(self.size)
            if len(data) != self.size:
                raise OSError("short text spool read")
            return data.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise OutputBudgetExceeded("output text spool read failed") from exc

    def close(self) -> None:
        """Release storage and accounting; safe to call repeatedly."""
        self._file.close()
        if self.budget is not None:
            self.budget.storage_bytes -= self.size
        self.size = 0
