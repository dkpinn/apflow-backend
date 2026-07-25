from __future__ import annotations

import json
from collections.abc import Awaitable, Callable


class _RequestBodyTooLarge(Exception):
    pass


class RequestSizeLimitMiddleware:
    """Enforce route-specific body limits, including chunked requests."""

    def __init__(self, app, *, limits: dict[str, int]):
        self.app = app
        self.limits = limits

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        limit = self.limits.get(scope.get("path", ""))
        if limit is None:
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                await self._send_error(send, 400, "Invalid Content-Length header")
                return
            if declared_size < 0:
                await self._send_error(send, 400, "Invalid Content-Length header")
                return
            if declared_size > limit:
                await self._send_error(send, 413, "Webhook request body is too large")
                return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await self._send_error(send, 413, "Webhook request body is too large")

    @staticmethod
    async def _send_error(send: Callable[[dict], Awaitable[None]], status: int, detail: str):
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        })
        await send({"type": "http.response.body", "body": body})
