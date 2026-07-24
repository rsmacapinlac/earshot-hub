"""API error type — carries the stable code + HTTP status the spec assigns.

Errors serialise to ``{ "error": { "code", "message" } }`` (rpi/specs/api.md).
Codes are stable; messages are not.
"""

from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}
