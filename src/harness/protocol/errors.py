"""Consistent application errors, without a custom JSON-RPC envelope."""

from typing import Any


class HarnessError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_rpc(self) -> dict[str, Any]:
        return {
            "code": -32000,
            "message": self.message,
            "data": {"code": self.code, "details": self.details},
        }

    def to_http(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}
