from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.database import create_tables
from app.routers import auth, dashboard, jobs, logs, tokens

template_dir = Path(__file__).resolve().parent / "templates"
_jinja_env = Environment(
    loader=FileSystemLoader(str(template_dir)),
    autoescape=select_autoescape(["html", "xml"]),
    cache_size=0,
)


def render(request: Request, template_name: str, **context) -> HTMLResponse:
    template = _jinja_env.get_template(template_name)
    html = template.render(request=request, **context)
    return HTMLResponse(html)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_tables()
    yield


app = FastAPI(title="WhatSend", lifespan=lifespan)

app.state.render = render

static_dir = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(tokens.router)
app.include_router(jobs.router)
app.include_router(logs.router)


@app.get("/")
async def root():
    return RedirectResponse(url="/dashboard")
