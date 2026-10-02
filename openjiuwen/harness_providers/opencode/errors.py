# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Nonsecret failure codes at the HTTP/process boundary."""

from openjiuwen.harness_protocol import HarnessError, TurnError

# Native messages may contain request bodies or credentials. Only fixed names
# belong in durable diagnostics; unknown values must not become log strings.
_NATIVE_ERRORS = frozenset({"UnknownError", "MessageAbortedError", "ProviderAuthError", "APIError"})
_ERROR_SOURCES = frozenset({"session.error", "message.updated"})


class OpenCodeError(HarnessError):
    def __init__(self, reason: str, *, category: str = "sdk_error", status: int | None = None,
                 native_name: str | None = None, source: str | None = None):
        self.reason = reason
        self.category = category
        self.status = status
        self.native_name = (native_name if isinstance(native_name, str) and native_name in _NATIVE_ERRORS
                            else "unrecognized" if native_name is not None else None)
        self.source = source if isinstance(source, str) and source in _ERROR_SOURCES else None
        super().__init__(f"OpenCode {reason}")

    def turn_error(self):
        diagnostic = {"reason": self.reason, "http_status": self.status}
        if self.native_name is not None:
            diagnostic["native_error_name"] = self.native_name
        if self.source is not None:
            diagnostic["error_source"] = self.source
        # Existing host history retains message/code. Include only the same
        # fixed diagnostic labels so that this survives that projection too.
        labels = [value for value in (self.native_name, self.source) if value is not None]
        message = "OpenCode execution failed" + (f" ({', '.join(labels)})" if labels else "")
        return TurnError(
            message=message,
            code=self.reason,
            category=self.category,
            retryable=self.category in {"network_timeout", "rate_limited", "server_unavailable"},
            provider_data={"opencode": diagnostic},
        )


def http_error(status, *, native_name=None, source=None):
    category = {401: "auth_required", 403: "auth_required", 429: "rate_limited"}.get(
        status, "server_unavailable" if status >= 500 else "sdk_error"
    )
    return OpenCodeError("http_error", category=category, status=status, native_name=native_name, source=source)
