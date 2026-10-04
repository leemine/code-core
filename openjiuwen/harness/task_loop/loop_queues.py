# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025.
# All rights reserved.
"""Dual-queue buffer for steer/follow_up messages.

Bridges EventHandler -> Executor/Loop by providing two
async-safe queues:
- steering: drained by the executor before each
  inner invoke.
- follow_up: drained by outer task loop after
  each iteration completes.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import List

from openjiuwen.core.controller.schema.execution_origin import (
    ORIGIN_UNSET,
    _unwrap_origin_inputs,
    capture_origin_input,
    consume_origin_input,
)


def _drain(queue, expected_origin):
    messages = []
    while not queue.empty():
        try:
            value = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        messages.append(consume_origin_input(value, expected_origin))
    return messages


@dataclass
class LoopQueues:
    """Buffer between EventHandler and Executor/Loop.

    Attributes:
        steering: Queue for steer messages, drained
            by the executor before each invoke.
        follow_up: Queue for follow-up messages,
            drained by the outer task loop.
    """

    steering: asyncio.Queue = field(
        default_factory=asyncio.Queue
    )
    follow_up: asyncio.Queue = field(
        default_factory=asyncio.Queue
    )

    def push_steer(self, msg: str, *, origin=ORIGIN_UNSET) -> None:
        """Push a steering message.

        Args:
            msg: Steering instruction text.
        """
        self.steering.put_nowait(capture_origin_input(msg, origin))

    def push_follow_up(self, msg: str, *, origin=ORIGIN_UNSET) -> None:
        """Push a follow-up message.

        Args:
            msg: Follow-up content text.
        """
        self.follow_up.put_nowait(capture_origin_input(msg, origin))

    def _discard_origin(self, origin):
        if origin is None:
            raise ValueError("exact input discard requires an original source")
        for queue in (self.steering, self.follow_up):
            # Synchronous event-loop operation on the existing queue. Balance
            # retained entries before acknowledging removed originals, so join()
            # cannot observe a transient zero while foreign inputs remain.
            retained = []
            removed = 0
            while not queue.empty():
                value = queue.get_nowait()
                source, _ = _unwrap_origin_inputs([value])
                removed += 1
                if source is not origin:
                    retained.append(value)
            for value in retained:
                queue.put_nowait(value)
            for _ in range(removed):
                queue.task_done()

    def has_follow_up(self) -> bool:
        """Return whether follow-up messages are pending."""
        return not self.follow_up.empty()

    def drain_steering(self, *, expected_origin=ORIGIN_UNSET) -> List[str]:
        """Drain all pending steering messages.

        Returns:
            List of steering message strings.
        """
        return _drain(self.steering, expected_origin)

    def drain_follow_up(self, *, expected_origin=ORIGIN_UNSET) -> List[str]:
        """Drain all pending follow-up messages.

        Returns:
            List of follow-up message strings.
        """
        return _drain(self.follow_up, expected_origin)

    def drain_sourced_follow_up(self):
        """Take one batch from the existing queue, preserving its actual source.

        A mixed batch is rejected as a whole; no later enqueued work is touched.
        This synchronous operation never consults the consumer's ambient scope.
        """
        values = []
        while not self.follow_up.empty():
            try:
                values.append(self.follow_up.get_nowait())
            except asyncio.QueueEmpty:
                break
        return _unwrap_origin_inputs(values)

    def clear_follow_up(self) -> None:
        """Discard queued values without treating them as executable input."""
        while not self.follow_up.empty():
            try:
                self.follow_up.get_nowait()
            except asyncio.QueueEmpty:
                break


__all__ = ["LoopQueues"]
