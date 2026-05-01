from __future__ import annotations
from contextvars import ContextVar

current_org_id: ContextVar[str | None] = ContextVar("current_org_id", default=None)

current_user_id: ContextVar[str | None] = ContextVar("current_user_id", default=None)

current_user_role: ContextVar[str | None] = ContextVar("current_user_role", default=None)

current_is_service: ContextVar[bool] = ContextVar("current_is_service", default=False)
