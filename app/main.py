from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import zoneinfo
import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from sqlalchemy import select, text

from app.database import async_session
from app.i18n import _ as _translate
from app.models.job import Job
from app.routers import about, api, api_keys, auth, dashboard, jobs, logs, media, tokens, webhooks
from app.routers.auth import RedirectRequired, get_current_user
from app.services.scheduler import load_all_jobs, scheduler as apscheduler
from app.services.csrf import get_or_create_csrf_token
from app.config import settings
from app.services.logging import configure_logging

configure_logging()

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


WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def cron_to_text(expr: str, schedule_tz: str = "UTC", display_tz: str | None = None) -> str:
    try:
        parts = expr.split()
        if len(parts) != 5:
            return expr
        minute, hour, dom, month, dow = parts
        time = f"{hour.zfill(2)}:{minute.zfill(2)}"

        display_tz = display_tz or schedule_tz
        if schedule_tz != display_tz and hour != "*":
            try:
                t = time.split(":")
                today = datetime.now(zoneinfo.ZoneInfo(schedule_tz)).strftime("%Y-%m-%d")
                scheduled_dt = datetime.strptime(f"{today} {t[0]}:{t[1]}", "%Y-%m-%d %H:%M").replace(tzinfo=zoneinfo.ZoneInfo(schedule_tz))
                local_dt = scheduled_dt.astimezone(zoneinfo.ZoneInfo(display_tz))
                time = local_dt.strftime("%H:%M")
            except Exception:
                pass

        if dow == "*" and dom == "*":
            return f"Daily at {time}"
        if dow == "0-4" and dom == "*":
            return f"Weekdays at {time}"
        if dow == "5,6" and dom == "*":
            return f"Weekends at {time}"
        if dom == "*" and "," in dow:
            names = [WEEKDAYS[int(d)] for d in dow.split(",")]
            return f"{', '.join(names)} at {time}"
        if dom == "*" and "-" not in dow:
            return f"Weekly on {WEEKDAYS[int(dow)]} at {time}"
        if dom != "*" and dow == "*":
            return f"Monthly on day {dom} at {time}"
        return expr
    except (ValueError, IndexError, AttributeError):
        return expr


_jinja_env.globals["cron_to_text"] = cron_to_text


def render(request: Request, template_name: str, **context) -> HTMLResponse:
    user = get_current_user(request)
    if user:
        context.setdefault("user_tz", user.get("tz", "UTC"))
        lang = user.get("lang", "en")
    else:
        context.setdefault("user_tz", "UTC")
        lang = "en"
    lang = request.cookies.get("lang", lang)
    context.setdefault("lang", lang)
    context.setdefault("api_keys_enabled", settings.api_keys_enabled)
    csrf_token, csrf_is_new = get_or_create_csrf_token(request)
    context.setdefault("csrf_token", csrf_token)
    csp_nonce = secrets.token_urlsafe(16)
    context.setdefault("csp_nonce", csp_nonce)

    def _(key: str) -> str:
        return _translate(key, lang)

    template = _jinja_env.get_template(template_name)
    html = template.render(request=request, _=_, **context)
    response = HTMLResponse(html)
    response.headers["Content-Security-Policy"] = (
        f"default-src 'self'; script-src 'self' 'nonce-{csp_nonce}' https://unpkg.com; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if csrf_is_new:
        response.set_cookie(
            key="csrf_token",
            value=csrf_token,
            max_age=86400,
            secure=settings.cookie_secure,
            httponly=False,
            samesite="strict",
        )
    return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with async_session() as db:
        result = await db.execute(select(Job).where(Job.image_path.isnot(None)))
        for job in result.scalars().all():
            if job.image_path and not Path(job.image_path).exists():
                job.image_path = None
        await db.commit()

    apscheduler.start()
    await load_all_jobs()
    yield
    apscheduler.shutdown()


app = FastAPI(title="whatsend", lifespan=lifespan)

app.state.render = render


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if settings.cookie_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


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
app.include_router(media.router)
app.include_router(webhooks.router)
if settings.api_keys_enabled:
    app.include_router(api_keys.router)
    app.include_router(api.router)


@app.get("/")
async def root():
    return RedirectResponse(url="/dashboard")


@app.get("/health/live", include_in_schema=False)
async def health_live():
    return {"status": "ok"}


@app.get("/health/ready", include_in_schema=False)
async def health_ready():
    try:
        async with async_session() as db:
            await db.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database is unavailable") from exc
    if not apscheduler.running:
        raise HTTPException(status_code=503, detail="Scheduler is not running")
    return {"status": "ready"}
