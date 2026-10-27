"""Мини-сайт для чтения бекапа в прямом эфире. Работает в том же процессе, что и бот (aiohttp уже есть у aiogram).

GET /                      страница
GET /api/topics            топики со счётчиками
GET /api/messages          лента (?thread=&before=&after=&user=&q=&limit=)
GET /api/messages/{pk}     одно сообщение
GET /api/messages/{pk}/versions   история правок
GET /api/edits             лента правок
GET /api/users | /api/reports | /api/violations
GET /api/media/{id}        файл вложения
GET /api/stream            SSE: новые сообщения, правки, удаления, скачанные файлы
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import web
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..config import Settings
from . import queries
from .events import bus

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
SETTINGS_KEY = web.AppKey("settings", Settings)
SESSIONS_KEY = web.AppKey("sessionmaker", async_sessionmaker)


def _json(data: Any) -> web.Response:
    return web.json_response(data, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))


def _int(request: web.Request, name: str) -> int | None:
    raw = request.query.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        raise web.HTTPBadRequest(text=f"{name} must be int")


@web.middleware
async def basic_auth(request: web.Request, handler):
    settings = request.app[SETTINGS_KEY]
    password = settings.web_password.get_secret_value() if settings.web_password else ""
    if not password:
        return await handler(request)
    header = request.headers.get("Authorization", "")
    if header.startswith("Basic "):
        try:
            user, _, given = base64.b64decode(header[6:]).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError):
            user, given = "", ""
        if hmac.compare_digest(user, settings.web_user) and hmac.compare_digest(given, password):
            return await handler(request)
    raise web.HTTPUnauthorized(headers={"WWW-Authenticate": 'Basic realm="beta_bot"'})


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def topics(request: web.Request) -> web.Response:
    async with request.app[SESSIONS_KEY]() as session:
        return _json(await queries.list_topics(session, request.app[SETTINGS_KEY]))


async def messages(request: web.Request) -> web.Response:
    async with request.app[SESSIONS_KEY]() as session:
        data = await queries.list_messages(
            session,
            request.app[SETTINGS_KEY],
            thread=_int(request, "thread"),
            before=_int(request, "before"),
            after=_int(request, "after"),
            user_id=_int(request, "user"),
            q=request.query.get("q") or None,
            limit=min(_int(request, "limit") or 100, 500),
        )
    return _json(data)


async def message(request: web.Request) -> web.Response:
    async with request.app[SESSIONS_KEY]() as session:
        data = await queries.get_message(session, request.app[SETTINGS_KEY], pk=int(request.match_info["pk"]))
    if data is None:
        raise web.HTTPNotFound()
    return _json(data)


async def versions(request: web.Request) -> web.Response:
    async with request.app[SESSIONS_KEY]() as session:
        return _json(await queries.message_versions(session, int(request.match_info["pk"])))


async def edits(request: web.Request) -> web.Response:
    async with request.app[SESSIONS_KEY]() as session:
        return _json(await queries.list_edits(session, request.app[SETTINGS_KEY]))


async def users(request: web.Request) -> web.Response:
    async with request.app[SESSIONS_KEY]() as session:
        return _json(await queries.list_users(session, request.app[SETTINGS_KEY]))


async def reports(request: web.Request) -> web.Response:
    async with request.app[SESSIONS_KEY]() as session:
        return _json(await queries.list_reports(session))


async def violations(request: web.Request) -> web.Response:
    async with request.app[SESSIONS_KEY]() as session:
        return _json(await queries.list_violations(session))


async def media(request: web.Request) -> web.StreamResponse:
    settings = request.app[SETTINGS_KEY]
    async with request.app[SESSIONS_KEY]() as session:
        found = await queries.attachment_path(session, int(request.match_info["att_id"]))
    if found is None:
        raise web.HTTPNotFound()
    path_str, mime, name = found
    path = Path(path_str).resolve()
    # Отдаём только из папки медиа, даже если в БД вдруг окажется чужой путь.
    if not path.is_relative_to(settings.media_dir.resolve()) or not path.is_file():
        raise web.HTTPNotFound()
    headers = {"Cache-Control": "private, max-age=86400"}
    if mime:
        headers["Content-Type"] = mime
    if name and not (mime or "").startswith(("image/", "video/", "audio/")):
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return web.FileResponse(path, headers=headers)


async def stream(request: web.Request) -> web.StreamResponse:
    """Server-Sent Events. Сообщения/правки отдаём уже сериализованными, чтобы странице не делать лишний запрос."""
    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )
    await response.prepare(request)
    settings = request.app[SETTINGS_KEY]
    queue = bus.subscribe()
    try:
        await response.write(b": connected\n\n")
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
            except asyncio.TimeoutError:
                await response.write(b": ping\n\n")
                continue
            payload: dict[str, Any] = dict(event)
            if event["type"] in ("message", "edit", "attachment"):
                async with request.app[SESSIONS_KEY]() as session:
                    if event["type"] == "attachment":
                        msg = await queries.get_message(session, settings, pk=event["message_pk"])
                    else:
                        msg = await queries.get_message(
                            session, settings, chat_id=event["chat_id"], message_id=event["message_id"]
                        )
                    if event["type"] == "edit" and event.get("version_id"):
                        found = await queries.list_edits(session, settings, version_id=event["version_id"], limit=1)
                        payload["version"] = found[0] if found else None
                if msg is None:
                    continue
                payload["message"] = msg
            data = json.dumps(payload, ensure_ascii=False, default=str)
            await response.write(f"event: {event['type']}\ndata: {data}\n\n".encode())
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        bus.unsubscribe(queue)
    return response


def build_app(settings: Settings, sessionmaker: async_sessionmaker) -> web.Application:
    app = web.Application(middlewares=[basic_auth])
    app[SETTINGS_KEY] = settings
    app[SESSIONS_KEY] = sessionmaker
    app.router.add_get("/", index)
    app.router.add_get("/api/topics", topics)
    app.router.add_get("/api/messages", messages)
    app.router.add_get("/api/messages/{pk:\\d+}", message)
    app.router.add_get("/api/messages/{pk:\\d+}/versions", versions)
    app.router.add_get("/api/edits", edits)
    app.router.add_get("/api/users", users)
    app.router.add_get("/api/reports", reports)
    app.router.add_get("/api/violations", violations)
    app.router.add_get("/api/media/{att_id:\\d+}", media)
    app.router.add_get("/api/stream", stream)
    return app


async def start_web(settings: Settings, sessionmaker: async_sessionmaker) -> web.AppRunner:
    runner = web.AppRunner(build_app(settings, sessionmaker), shutdown_timeout=2)
    await runner.setup()
    await web.TCPSite(runner, settings.web_host, settings.web_port).start()
    log.info("web viewer on http://%s:%s", settings.web_host, settings.web_port)
    return runner
