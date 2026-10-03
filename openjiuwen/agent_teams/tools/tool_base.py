# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Base abstractions shared by all team tools."""

from abc import ABC
from functools import wraps
from typing import Any, AsyncIterator

from openjiuwen.core.foundation.tool.base import Tool, ToolCard


class TeamTool(Tool, ABC):
    """Base class for team tools.

    Subclasses override ``render_for_llm`` to control the text the model reads
    for their result; the structured ``ToolOutput`` stays with program
    consumers (events, logs). Without an override the core default rendering
    of ``Tool.render_for_llm`` applies.
    """

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        original = cls.__dict__.get("invoke")
        if original is None:
            return

        @wraps(original)
        async def logged_invoke(self, inputs, **invoke_kwargs):
            # Installed before Tool construction captures the original bound
            # method. Factory configuration never replaces the final boundary.
            if not getattr(self, "_team_invoke_logging", False):
                return await original(self, inputs, **invoke_kwargs)
            from openjiuwen.core.common.logging import team_logger

            name = self.card.name
            team_logger.debug(f"[{name}] invoke start, inputs={inputs}")
            result = await original(self, inputs, **invoke_kwargs)
            team_logger.debug(f"[{name}] invoke end, output={result}")
            return result

        cls.invoke = logged_invoke

    async def stream(self, inputs: dict[str, Any], **kwargs) -> AsyncIterator[Any]:
        raise NotImplementedError("TeamTool does not support streaming")
