# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Strict construction values; no CLI discovery or optional imports here."""

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from openjiuwen.harness_providers.opencode.native_plugins import OpenCodeNativePluginConfig
from openjiuwen.harness_providers.skills import SkillSource, normalize_skills

CLI_VERSION = "1.18.18"
CLI_SHA256 = "bb71f45b564f9234a97f54d6252a4a41d2f4388ae4b078918f691824cc3b3e54"


def literal(value: str) -> str:
    if "{env:" in value or "{file:" in value or "\x00" in value:
        raise ValueError("OpenCode configuration interpolation is not allowed")
    return value


@dataclass(frozen=True, slots=True)
class OpenCodeModelConfig:
    model: str
    api_base: str
    api_key: str | None = field(default=None, repr=False)
    provider: str = "openjiuwen"

    def __post_init__(self):
        for name in ("model", "api_base", "provider"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"OpenCode model {name} must be a nonempty string")
            literal(value)
        if not self.provider.replace("_", "").replace("-", "").isalnum():
            raise ValueError("OpenCode model provider must be a simple identifier")
        url = urlsplit(self.api_base)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.fragment:
            raise ValueError("OpenCode model endpoint must be an HTTP(S) URL without userinfo or fragment")
        if self.api_key is not None:
            if not isinstance(self.api_key, str) or not self.api_key:
                raise ValueError("OpenCode model api_key must be a nonempty string or null")
            literal(self.api_key)

    @classmethod
    def from_mapping(cls, value):
        if value is None or isinstance(value, cls):
            return value
        if not isinstance(value, Mapping) or set(value) - {f.name for f in fields(cls)}:
            raise ValueError("invalid OpenCode model configuration fields")
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class OpenCodeHarnessConfig:
    cli_path: str | None = None
    runtime_root: str | None = None
    model: OpenCodeModelConfig | None = None
    full_access: bool = False
    skills: tuple[SkillSource, ...] = ()
    skill_conflict: str = "skip"
    native_plugins: tuple[OpenCodeNativePluginConfig, ...] | None = field(default=None, repr=False)
    startup_timeout_s: float = 30
    request_timeout_s: float = 15
    turn_timeout_s: float = 180
    shutdown_timeout_s: float = 10
    event_buffer_capacity: int = 1024
    transport_capacity: int = 256
    max_frame_bytes: int = 1024 * 1024
    max_response_bytes: int = 8 * 1024 * 1024
    max_turn_bytes: int = 8 * 1024 * 1024

    def __post_init__(self):
        object.__setattr__(self, "model", OpenCodeModelConfig.from_mapping(self.model))
        object.__setattr__(self, "skills", normalize_skills(self.skills, self.skill_conflict))
        plugins = self.native_plugins
        if plugins is not None:
            if not isinstance(plugins, (list, tuple)) or any(
                not isinstance(plugin, OpenCodeNativePluginConfig) for plugin in plugins
            ):
                raise TypeError(
                    "OpenCode native_plugins must be an array of OpenCodeNativePluginConfig values or null"
                )
            object.__setattr__(self, "native_plugins", tuple(plugins))
        for name in ("cli_path", "runtime_root"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not Path(value).is_absolute()):
                raise ValueError(f"OpenCode {name} must be an absolute path")
            if value is not None:
                literal(value)
        if not isinstance(self.full_access, bool):
            raise TypeError("OpenCode full_access must be a boolean")
        for name in ("startup_timeout_s", "request_timeout_s", "turn_timeout_s", "shutdown_timeout_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 3600:
                raise ValueError(f"OpenCode {name} must be a positive bounded timeout")
        for name in (
            "event_buffer_capacity",
            "transport_capacity",
            "max_frame_bytes",
            "max_response_bytes",
            "max_turn_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 64 * 1024 * 1024:
                raise ValueError(f"OpenCode {name} must be a positive bounded integer")

    @classmethod
    def from_mapping(cls, config):
        if not isinstance(config, Mapping) or set(config) - {f.name for f in fields(cls)}:
            raise ValueError("unknown OpenCode configuration fields")
        values = dict(config)
        if "native_plugins" in values and values["native_plugins"] is not None:
            raw_plugins = values["native_plugins"]
            if not isinstance(raw_plugins, (list, tuple)):
                raise TypeError("OpenCode native_plugins must be an array or null")
            values["native_plugins"] = tuple(
                plugin
                if isinstance(plugin, OpenCodeNativePluginConfig)
                else OpenCodeNativePluginConfig.from_mapping(plugin)
                for plugin in raw_plugins
            )
        return cls(**values)
