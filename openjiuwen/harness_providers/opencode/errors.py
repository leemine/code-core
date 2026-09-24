# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Nonsecret failure codes at the HTTP/process boundary."""

from openjiuwen.harness_protocol import HarnessError, TurnError


class OpenCodeError(HarnessError):
    def __init__(self, reason: str, *, category: str = "sdk_error", status: int | None = None):
        self.reason = reason
        self.category = category
        self.status = status
        super().__init__(f"OpenCode {reason}")

    def turn_error(self):
        return TurnError(
            message="OpenCode execution failed",
            code=self.reason,
            category=self.category,
            retryable=self.category in {"network_timeout", "rate_limited", "server_unavailable"},
            provider_data={"opencode": {"reason": self.reason, "http_status": self.status}},
        )


def http_error(status):
    category = {401: "auth_required", 403: "auth_required", 429: "rate_limited"}.get(
        status, "server_unavailable" if status >= 500 else "sdk_error"
    )
    return OpenCodeError("http_error", category=category, status=status)
