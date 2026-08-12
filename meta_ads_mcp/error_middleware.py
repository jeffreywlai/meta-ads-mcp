"""Convert application exceptions into stable MCP error envelopes."""

from __future__ import annotations

import json

from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware
from fastmcp.server.middleware.middleware import CallNext, MiddlewareContext

from meta_ads_mcp.errors import MetaApiError, RateLimitError


class StructuredMetaErrorMiddleware(Middleware):
    """Expose actionable Meta failures without leaking arbitrary response data."""

    @staticmethod
    def _meta_cause(exc: BaseException) -> MetaApiError | RateLimitError | None:
        """Find the typed upstream error after FastMCP wraps tool exceptions."""
        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, (RateLimitError, MetaApiError)):
                return current
            current = current.__cause__ or current.__context__
        return None

    @staticmethod
    def _structured_error(exc: MetaApiError | RateLimitError) -> ToolError:
        """Build one compact, allowlisted MCP error."""
        return ToolError(
            json.dumps({"error": exc.to_public_dict()}, separators=(",", ":"))
        )

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        try:
            return await call_next(context)
        except RateLimitError as exc:
            raise self._structured_error(exc) from exc
        except MetaApiError as exc:
            raise self._structured_error(exc) from exc
        except ToolError as exc:
            meta_cause = self._meta_cause(exc)
            if meta_cause is None:
                raise
            raise self._structured_error(meta_cause) from meta_cause
