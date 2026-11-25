"""A mini-site for reading the backup live. Runs in the same process as the bot (aiogram already has aiohttp).

GET /                      the page
GET /api/topics            topics with counters
GET /api/messages          feed (?thread=&before=&after=&user=&q=&limit=)
GET /api/messages/{pk}     a single message
GET /api/messages/{pk}/versions   edit history
GET /api/edits             edit feed
GET /api/users | /api/reports | /api/violations
GET /api/media/{id}        attachment file
GET /api/stream            SSE: new messages, edits, deletions, downloaded files
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

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiohttp import web
from sqlalchemy.ext.asyncio import async_sessionmaker

from .. import texts
from ..config import CONFIG, Settings
from ..services import members as members_svc
from ..services import reports as reports_svc
from ..services.llm import Summarizer
from ..services.notify import announce
from ..services.prefs import Prefs
from ..utils.ids import normalize_public_id
from . import queries
from .events import bus

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
SETTINGS_KEY = web.AppKey("settings", Settings)
SESSIONS_KEY = web.AppKey("sessionmaker", async_sessionmaker)
# A live bot is needed for control actions (status changes, member moderation).
BOT_KEY: web.AppKey = web.AppKey("bot")
SUMMARIZER_KEY: web.AppKey = web.AppKey("summarizer")
PREFS_KEY: web.AppKey = web.AppKey("prefs")
BOTNAME_KEY: web.AppKey = web.AppKey("bot_username")


def _json(data: Any) -> web.Response:
    return web.json_response(data, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))


def _int(request: web.Request, name: str) -> int | None:
    raw = request.query.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        raise web.HTTPBadRequest(text=texts.WEB_ERR_MUST_BE_INT.format(name=name))


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
    raise web.HTTPUnauthorized(headers={"WWW-Authenticate": f'Basic realm="{texts.WEB_AUTH_REALM}"'})


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
            limit=min(_int(request, "limit") or CONFIG["web"]["feed_limit_default"], CONFIG["web"]["feed_limit_max"]),
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
        return _json(
            await queries.list_reports(session, request.app[SETTINGS_KEY], request.app.get(BOTNAME_KEY))
        )


async def drafts(request: web.Request) -> web.Response:
    async with request.app[SESSIONS_KEY]() as session:
        return _json(await queries.list_drafts(session))


async def meta(request: web.Request) -> web.Response:
    settings = request.app[SETTINGS_KEY]
    return _json(
        {
            "bot_username": request.app.get(BOTNAME_KEY),
            "can_manage": request.app.get(BOT_KEY) is not None,
            "member_actions": sorted(members_svc.ACTIONS),
            "statuses": {k: v for k, v in texts.STATUS_LABELS.items()},
        }
    )


async def set_report_status(request: web.Request) -> web.Response:
    bot: Bot | None = request.app.get(BOT_KEY)
    summarizer: Summarizer | None = request.app.get(SUMMARIZER_KEY)
    prefs: Prefs | None = request.app.get(PREFS_KEY)
    settings = request.app[SETTINGS_KEY]
    if bot is None or summarizer is None:
        raise web.HTTPServiceUnavailable(text=texts.WEB_ERR_BOT_UNAVAILABLE)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise web.HTTPBadRequest(text=texts.WEB_ERR_BAD_JSON)
    status = str(body.get("status") or "")
    if status not in texts.STATUS_LABELS:
        raise web.HTTPBadRequest(text=texts.WEB_ERR_UNKNOWN_STATUS)
    comment = (body.get("comment") or "").strip() or None
    rid = normalize_public_id(request.match_info["rid"])
    async with request.app[SESSIONS_KEY]() as session:
        report = await reports_svc.get_report(session, rid) if rid else None
        if report is None:
            raise web.HTTPNotFound()
        await reports_svc.apply_status(bot, session, report, status, comment, settings, summarizer)
        await session.commit()
        public_id = report.public_id
    if prefs is not None:
        await announce(
            bot, settings, prefs, "status_change",
            texts.STATUS_CHANGE_ANNOUNCE_WEB.format(rid=public_id, status=reports_svc.status_label(status)),
        )
    bus.publish({"type": "report", "id": public_id, "kind": "status"})
    return _json({"ok": True, "id": public_id, "status": status})


async def member_action(request: web.Request) -> web.Response:
    bot: Bot | None = request.app.get(BOT_KEY)
    settings = request.app[SETTINGS_KEY]
    if bot is None:
        raise web.HTTPServiceUnavailable(text=texts.WEB_ERR_BOT_UNAVAILABLE)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise web.HTTPBadRequest(text=texts.WEB_ERR_BAD_JSON)
    action = str(body.get("action") or "")
    if action not in members_svc.ACTIONS:
        raise web.HTTPBadRequest(text=texts.WEB_ERR_UNKNOWN_ACTION)
    minutes = body.get("minutes")
    minutes = int(minutes) if minutes not in (None, "") else None
    uid = int(request.match_info["uid"])
    try:
        label = await members_svc.apply_action(bot, settings.chat_id, uid, action, minutes)
    except TelegramAPIError as exc:
        raise web.HTTPBadGateway(text=exc.message)
    bus.publish({"type": "member", "user_id": uid, "action": action})
    return _json({"ok": True, "user_id": uid, "action": action, "result": label})


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
    # Serve only from the media folder, even if the DB somehow holds a foreign path.
    if not path.is_relative_to(settings.media_dir.resolve()) or not path.is_file():
        raise web.HTTPNotFound()
    headers = {"Cache-Control": "private, max-age=86400"}
    if mime:
        headers["Content-Type"] = mime
    if name and not (mime or "").startswith(("image/", "video/", "audio/")):
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return web.FileResponse(path, headers=headers)


async def stream(request: web.Request) -> web.StreamResponse:
    """Server-Sent Events. Messages/edits are sent already serialized, so the page needs no extra request."""
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


def build_app(
    settings: Settings,
    sessionmaker: async_sessionmaker,
    *,
    bot: Bot | None = None,
    summarizer: Summarizer | None = None,
    prefs: Prefs | None = None,
    bot_username: str | None = None,
) -> web.Application:
    app = web.Application(middlewares=[basic_auth])
    app[SETTINGS_KEY] = settings
    app[SESSIONS_KEY] = sessionmaker
    app[BOT_KEY] = bot
    app[SUMMARIZER_KEY] = summarizer
    app[PREFS_KEY] = prefs
    app[BOTNAME_KEY] = bot_username
    app.router.add_get("/", index)
    app.router.add_get("/api/meta", meta)
    app.router.add_get("/api/topics", topics)
    app.router.add_get("/api/messages", messages)
    app.router.add_get("/api/messages/{pk:\\d+}", message)
    app.router.add_get("/api/messages/{pk:\\d+}/versions", versions)
    app.router.add_get("/api/edits", edits)
    app.router.add_get("/api/users", users)
    app.router.add_get("/api/reports", reports)
    app.router.add_post("/api/reports/{rid}/status", set_report_status)
    app.router.add_get("/api/drafts", drafts)
    app.router.add_post("/api/members/{uid:\\d+}/action", member_action)
    app.router.add_get("/api/violations", violations)
    app.router.add_get("/api/media/{att_id:\\d+}", media)
    app.router.add_get("/api/stream", stream)
    return app


async def start_web(
    settings: Settings,
    sessionmaker: async_sessionmaker,
    *,
    bot: Bot | None = None,
    summarizer: Summarizer | None = None,
    prefs: Prefs | None = None,
    bot_username: str | None = None,
) -> web.AppRunner:
    app = build_app(
        settings, sessionmaker, bot=bot, summarizer=summarizer, prefs=prefs, bot_username=bot_username
    )
    runner = web.AppRunner(app, shutdown_timeout=2)
    await runner.setup()
    await web.TCPSite(runner, settings.web_host, settings.web_port).start()
    log.info(texts.LOG["web"]["started"], settings.web_host, settings.web_port)
    return runner
