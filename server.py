"""HTTP control for the PC power button, bound to the Tailscale address."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from pc_power import (
    NotConfigured,
    PcAlreadyOn,
    PressCoolingDown,
    PressInProgress,
    cooldown_remaining,
    log_event,
    pc_host,
    pc_is_up,
    press_with_retries,
    restart_sequence,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOG = logging.getLogger("pc_power.http")

# After a press, check every few seconds whether the PC answers ping.
WATCH_SECONDS = int(os.environ.get("WATCH_SECONDS", "180"))
WATCH_INTERVAL_SECONDS = 5

PAGE_PATH = Path(__file__).with_name("page.html")

app = FastAPI(title="PC power", docs_url=None, redoc_url=None, openapi_url=None)

_job: dict | None = None
_watch_task: asyncio.Task | None = None


def _unauthorized() -> JSONResponse:
    return JSONResponse(status_code=401, content={"error": "unauthorized"})


def _authorized(authorization: str | None) -> bool:
    expected = os.environ.get("API_TOKEN", "")
    if not expected or not authorization or not authorization.startswith("Bearer "):
        return False
    got = authorization.removeprefix("Bearer ").strip()
    return hmac.compare_digest(got, expected)


async def _json_body(request: Request) -> dict:
    if "application/json" not in request.headers.get("content-type", ""):
        return {}
    raw = await request.body()
    if not raw:
        return {}
    payload = json.loads(raw)
    return payload if isinstance(payload, dict) else {}


async def _force_from_body(request: Request, force: bool) -> bool:
    if force:
        return True
    return (await _json_body(request)).get("force") is True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _watch(job: dict, host: str) -> None:
    """Record whether the PC answers ping within WATCH_SECONDS of the press."""
    started = time.monotonic()
    job["phase"] = "waiting"
    try:
        while time.monotonic() - started < WATCH_SECONDS:
            if await asyncio.to_thread(pc_is_up, host):
                job["phase"] = "up"
                job["up_after_s"] = int(time.monotonic() - started)
                job["finished_at"] = _now_iso()
                log_event(
                    f"host={host} outcome=up after={job['up_after_s']}s via={job['kind']}"
                )
                return
            job["waited_s"] = int(time.monotonic() - started)
            await asyncio.sleep(WATCH_INTERVAL_SECONDS)
        job["phase"] = "no_response"
        job["finished_at"] = _now_iso()
        log_event(f"host={host} outcome=no_response after={WATCH_SECONDS}s via={job['kind']}")
    except asyncio.CancelledError:
        job["phase"] = "cancelled"
        raise


def _start_watch(kind: str, host: str) -> dict:
    global _job, _watch_task
    if _watch_task and not _watch_task.done():
        _watch_task.cancel()
    _job = {
        "kind": kind,
        "phase": "waiting",
        "pressed_at": _now_iso(),
        "watch_seconds": WATCH_SECONDS,
        "waited_s": 0,
    }
    _watch_task = asyncio.create_task(_watch(_job, host))
    return _job


def _record_failure(kind: str, error: str) -> None:
    global _job
    _job = {"kind": kind, "phase": "failed", "error": error, "finished_at": _now_iso()}


def _text_reply(resp: JSONResponse) -> PlainTextResponse:
    """Short plain-text form of a reply, for Apple Shortcuts (?format=text)."""
    data = json.loads(resp.body)
    if "pc_up" in data and "job" in data and "pressed" not in data:
        text = "on" if data["pc_up"] else "off"
    elif data.get("pressed"):
        text = "Pressed. Waiting for the PC to start."
    elif data.get("seconds_left") is not None:
        text = f"Wait {data['seconds_left']} s. The button was pressed recently."
    elif data.get("pc_up") is True:
        text = "PC is already on."
    else:
        text = data.get("reason") or data.get("error") or "Failed."
    return PlainTextResponse(text, status_code=resp.status_code)


def _maybe_text(resp: JSONResponse, fmt: str | None):
    return _text_reply(resp) if fmt == "text" else resp


@app.get("/", response_class=HTMLResponse)
async def page() -> HTMLResponse:
    # The page holds no secret. The API calls it makes need the bearer token.
    return HTMLResponse(PAGE_PATH.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@app.get("/status")
async def status(
    authorization: str | None = Header(default=None),
    format: str | None = Query(default=None),
):
    return _maybe_text(await _status(authorization), format)


async def _status(authorization: str | None) -> JSONResponse:
    if not _authorized(authorization):
        return _unauthorized()
    try:
        host = pc_host()
    except NotConfigured as exc:
        return JSONResponse(status_code=500, content={"error": str(exc)})
    up = await asyncio.to_thread(pc_is_up, host)
    return JSONResponse(
        content={
            "pc_up": up,
            "host": host,
            "cooldown_s": cooldown_remaining(),
            "job": dict(_job) if _job else None,
        }
    )


@app.post("/press")
async def press(
    request: Request,
    force: bool = Query(default=False),
    authorization: str | None = Header(default=None),
    format: str | None = Query(default=None),
):
    return _maybe_text(await _press(request, force, authorization), format)


async def _press(request: Request, force: bool, authorization: str | None) -> JSONResponse:
    if not _authorized(authorization):
        return _unauthorized()
    try:
        force = await _force_from_body(request, force)
        result = await press_with_retries(force=force)
    except PcAlreadyOn as exc:
        return JSONResponse(
            status_code=409,
            content={
                "pressed": False,
                "pc_up": True,
                "host": exc.host,
                "reason": "PC is reachable. Refusing to press. Pass force=true to override.",
            },
        )
    except PressCoolingDown as exc:
        return JSONResponse(
            status_code=409,
            content={
                "pressed": False,
                "reason": "A press just succeeded. Waiting before another press.",
                "seconds_left": exc.seconds_left,
            },
        )
    except PressInProgress:
        return JSONResponse(
            status_code=409,
            content={"pressed": False, "reason": "A press is already running."},
        )
    except NotConfigured as exc:
        return JSONResponse(status_code=500, content={"pressed": False, "error": str(exc)})
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})

    if not result.get("pressed"):
        _record_failure("wake", result.get("error") or "press failed")
        return JSONResponse(status_code=502, content=result)
    result["job"] = dict(_start_watch("wake", result["host"]))
    return JSONResponse(content=result)


@app.post("/restart")
async def restart(
    request: Request,
    authorization: str | None = Header(default=None),
    confirm: bool = Query(default=False),
    format: str | None = Query(default=None),
):
    return _maybe_text(await _restart(request, authorization, confirm), format)


async def _restart(request: Request, authorization: str | None, confirm: bool) -> JSONResponse:
    """Force the PC off with a long hold, then boot it. Loses unsaved work."""
    if not _authorized(authorization):
        return _unauthorized()
    try:
        body = await _json_body(request)
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})
    if not confirm and body.get("confirm") is not True:
        return JSONResponse(
            status_code=400,
            content={
                "pressed": False,
                "reason": 'Restart cuts power to the PC. Send {"confirm": true} or ?confirm=true to do it.',
            },
        )
    try:
        result = await restart_sequence()
    except PressInProgress:
        return JSONResponse(
            status_code=409,
            content={"pressed": False, "reason": "A press is already running."},
        )
    except NotConfigured as exc:
        return JSONResponse(status_code=500, content={"pressed": False, "error": str(exc)})

    if not result.get("pressed"):
        _record_failure("restart", result.get("error") or "restart failed")
        return JSONResponse(status_code=502, content=result)
    result["job"] = dict(_start_watch("restart", result["host"]))
    return JSONResponse(content=result)
