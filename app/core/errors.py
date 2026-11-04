"""Domain errors and FastAPI exception handlers."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class AgentForgeError(Exception):
    """Base class for expected application errors."""

    status_code = 400
    code = "agentforge_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"detail": self.message, "code": self.code}
        if self.details:
            payload["details"] = self.details
        return payload


class NotFoundError(AgentForgeError):
    status_code = 404
    code = "not_found"


class ConflictError(AgentForgeError):
    status_code = 409
    code = "conflict"


class PermissionDeniedError(AgentForgeError):
    status_code = 403
    code = "permission_denied"


class AuthenticationError(AgentForgeError):
    status_code = 401
    code = "authentication_error"


class WorkflowError(AgentForgeError):
    status_code = 422
    code = "workflow_error"


class EvaluationError(AgentForgeError):
    status_code = 422
    code = "evaluation_error"


class ToolExecutionError(AgentForgeError):
    status_code = 422
    code = "tool_error"


class LLMConfigurationError(AgentForgeError):
    status_code = 503
    code = "llm_configuration_error"


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AgentForgeError)
    async def _agentforge_handler(request: Request, exc: AgentForgeError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(ValueError)
    async def _value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc), "code": "bad_request"})
