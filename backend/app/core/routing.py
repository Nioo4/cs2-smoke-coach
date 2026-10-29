"""Enforce declared JSON Content-Type before parsing or executing a request."""

from fastapi import Request
from fastapi.routing import APIRoute

from app.core.errors import AppError


class JsonBodyRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()
        if self.body_field is None or self.body_field.field_info.media_type != "application/json":
            return original

        async def checked(request: Request):
            media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                raise AppError(415, "UNSUPPORTED_CONTENT_TYPE", "请求内容类型不受支持。")
            return await original(request)

        return checked
