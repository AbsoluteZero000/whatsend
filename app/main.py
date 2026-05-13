from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.database import create_tables
from app.routers import about, auth, dashboard, jobs, logs, tokens
from app.routers.auth import RedirectRequired, get_current_user
from app.services.scheduler import load_all_jobs, scheduler as apscheduler

template_dir = Path(__file__).resolve().parent / "templates"
_jinja_env = Environment(
    loader=FileSystemLoader(str(template_dir)),
    autoescape=select_autoescape(["html", "xml"]),
    cache_size=0,
)


def tz(dt: datetime | None, tz_name: str = "UTC") -> str:
    if dt is None:
        return "—"
    import zoneinfo
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=zoneinfo.ZoneInfo("UTC"))
        tz_obj = zoneinfo.ZoneInfo(tz_name)
        local = dt.astimezone(tz_obj)
        return local.strftime("%Y-%m-%d %H:%M")
    except (zoneinfo.ZoneInfoNotFoundError, OSError, ValueError):
        return dt.strftime("%Y-%m-%d %H:%M")


def parse_dt(value: str, fmt: str = "%Y-%m-%d %H:%M") -> datetime | None:
    try:
        return datetime.strptime(value, fmt)
    except (ValueError, TypeError):
        return None


_jinja_env.globals["tz"] = tz
_jinja_env.globals["parse_dt"] = parse_dt


def render(request: Request, template_name: str, **context) -> HTMLResponse:
    user = get_current_user(request)
    if user:
        context.setdefault("user_tz", user.get("tz", "UTC"))
    else:
        context.setdefault("user_tz", "UTC")
    template = _jinja_env.get_template(template_name)
    html = template.render(request=request, **context)
    return HTMLResponse(html)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_tables()
    apscheduler.start()
    await load_all_jobs()
    yield
    apscheduler.shutdown()


app = FastAPI(title="WhatSend", lifespan=lifespan)

app.state.render = render


@app.exception_handler(RedirectRequired)
async def redirect_handler(request: Request, exc: RedirectRequired):
    return RedirectResponse(url=exc.url, status_code=303)

static_dir = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(about.router)
app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(tokens.router)
app.include_router(jobs.router)
app.include_router(logs.router)


@app.get("/")
async def root():
    return RedirectResponse(url="/dashboard")
