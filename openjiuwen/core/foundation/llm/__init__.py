# -*- coding: UTF-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

# Core classes
from openjiuwen.core.foundation.llm.model import Model, init_model

# Built-in implementations
from openjiuwen.core.foundation.llm.model_clients.anthropic_model_client import AnthropicModelClient
from openjiuwen.core.foundation.llm.model_clients.base_model_client import BaseModelClient
from openjiuwen.core.foundation.llm.model_clients.openai_model_client import OpenAIModelClient
from openjiuwen.core.foundation.llm.output_parsers.json_output_parser import JsonOutputParser
from openjiuwen.core.foundation.llm.output_parsers.markdown_output_parser import MarkdownOutputParser
from openjiuwen.core.foundation.llm.output_parsers.output_parser import BaseOutputParser
from openjiuwen.core.foundation.llm.reasoning import (
    get_provider_reasoning_rules,
    get_reasoning_capability,
    get_reasoning_capability_catalog,
)
from openjiuwen.core.foundation.llm.reasoning_profiles import ReasoningCapability
from openjiuwen.core.foundation.llm.request_authority import (
    ModelRequestAuthority,
    ModelRequestAuthorityFactory,
    ModelRequestDenied,
    ModelRequestTarget,
)

# Configuration
from openjiuwen.core.foundation.llm.schema.config import (
    KVCacheExtensionConfig,
    LLMApiMode,
    LLMAuthMode,
    LLMExtensionsConfig,
    ModelClientConfig,
    ModelRequestConfig,
    ProviderType,
    ReasoningConfig,
)

# Messages
from openjiuwen.core.foundation.llm.schema.message import (
    OPENJIUWEN_MESSAGE_ORIGIN_EXTERNAL_USER,
    OPENJIUWEN_MESSAGE_ORIGIN_HARNESS_INTERNAL,
    OPENJIUWEN_MESSAGE_ORIGIN_METADATA,
    OPENJIUWEN_MESSAGE_PROVENANCE_METADATA,
    OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA,
    AssistantMessage,
    BaseMessage,
    SystemMessage,
    ToolMessage,
    UsageMetadata,
    UserMessage,
)
from openjiuwen.core.foundation.llm.schema.message_chunk import (
    AssistantMessageChunk,
)
from openjiuwen.core.foundation.llm.schema.mode_info import BaseModelInfo, ModelConfig

# Tools
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall

# Public exports are literal so static tooling can verify every reexport.
__all__ = [
    # core classes
    "Model",
    "init_model",
    "BaseModelClient",
    "BaseOutputParser",
    "ModelRequestAuthority",
    "ModelRequestAuthorityFactory",
    "ModelRequestDenied",
    "ModelRequestTarget",
    # config classes
    "ModelRequestConfig",
    "ModelClientConfig",
    "ProviderType",
    "LLMApiMode",
    "LLMAuthMode",
    "LLMExtensionsConfig",
    "KVCacheExtensionConfig",
    "ReasoningConfig",
    "BaseModelInfo",
    "ModelConfig",
    # reasoning apis
    "ReasoningCapability",
    "get_provider_reasoning_rules",
    "get_reasoning_capability",
    "get_reasoning_capability_catalog",
    # message classes
    "BaseMessage",
    "AssistantMessage",
    "UserMessage",
    "SystemMessage",
    "ToolMessage",
    "UsageMetadata",
    # message metadata constants
    "OPENJIUWEN_MESSAGE_ORIGIN_EXTERNAL_USER",
    "OPENJIUWEN_MESSAGE_ORIGIN_HARNESS_INTERNAL",
    "OPENJIUWEN_MESSAGE_ORIGIN_METADATA",
    "OPENJIUWEN_MESSAGE_PROVENANCE_METADATA",
    "OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA",
    # message chunk classes
    "AssistantMessageChunk",
    # tool classes
    "ToolCall",
    # prebuilt model clients
    "AnthropicModelClient",
    "OpenAIModelClient",
    # prebuilt output parsers
    "JsonOutputParser",
    "MarkdownOutputParser",
]
