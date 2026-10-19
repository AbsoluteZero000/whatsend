"""MCP server for the Claude connector (Streamable HTTP transport, stateless, JSON responses).

Claude signs in with OAuth (routers/oauth.py) and calls the tools in services/assistant_tools.py.
"""
import json
import logging

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.routers.oauth import connector_enabled
from app.services import assistant_tools, oauth

router = APIRouter(tags=["mcp"], dependencies=[Depends(connector_enabled)])
logger = logging.getLogger(__name__)

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "echosend", "title": "EchoSend", "version": "1.0.0"}
INSTRUCTIONS = (
    "EchoSend schedules WhatsApp messages that are sent from the user's own WhatsApp. "
    "Start with get_account_overview: all dates and times are in the account timezone it returns. "
    "Use list_recipients to find group names before creating a message, and confirm the text, recipients and time "
    "with the user before calling create_message, because messages go to real people."
)


def tool_definitions() -> list[dict]:
    definitions = []
    for name, (_, model, title, description, annotations) in assistant_tools.TOOLS.items():
        schema = model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        definitions.append({"name": name, "title": title, "description": description, "inputSchema": schema,
                            "annotations": {"title": title, **annotations}})
    return definitions


def unauthorized(request: Request, error: str = "") -> Response:
    metadata_url = oauth.public_base_url(request) + "/.well-known/oauth-protected-resource/mcp"
    challenge = f'Bearer realm="EchoSend", resource_metadata="{metadata_url}", scope="{oauth.SCOPE}"'
    if error:
        challenge += f', error="{error}"'
    return JSONResponse({"error": error or "unauthorized", "error_description": "Connect EchoSend to Claude again."},
                        status_code=401, headers={"WWW-Authenticate": challenge})


def rpc_error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def tool_result(payload: dict, is_error: bool = False) -> dict:
    result = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=1)}], "isError": is_error}
    if not is_error:
        result["structuredContent"] = payload
    return result


async def call_tool(ctx: assistant_tools.Context, params: dict) -> dict:
    name = params.get("name")
    if name not in assistant_tools.TOOLS:
        raise LookupError(f"Unknown tool: {name}")
    function, model, *_ = assistant_tools.TOOLS[name]
    try:
        arguments = model.model_validate(params.get("arguments") or {})
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(part) for part in error['loc']) or 'arguments'}: {error['msg']}"
                             for error in exc.errors(include_url=False))
        return tool_result({"error": "Invalid arguments. " + problems}, is_error=True)
    try:
        return tool_result(await function(ctx, arguments))
    except assistant_tools.ToolError as exc:
        return tool_result({"error": str(exc)}, is_error=True)
    except Exception:
        logger.exception("connector tool failed", extra={"tool": name, "user_id": ctx.user.id})
        await ctx.db.rollback()
        return tool_result({"error": "Something went wrong in EchoSend. Nothing more was changed; try again shortly."}, is_error=True)


async def handle(ctx: assistant_tools.Context, message) -> dict | None:
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return rpc_error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid request")
    if "id" not in message:
        return None  # notifications, e.g. notifications/initialized, need no answer
    request_id, method = message["id"], message["method"]
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return rpc_error(request_id, -32602, "params must be an object")
    if method == "initialize":
        requested = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": requested if requested in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tool_definitions()}}
    if method == "tools/call":
        try:
            return {"jsonrpc": "2.0", "id": request_id, "result": await call_tool(ctx, params)}
        except LookupError as exc:
            return rpc_error(request_id, -32602, str(exc))
    return rpc_error(request_id, -32601, f"Method not found: {method}")


@router.post("/mcp", include_in_schema=False)
async def mcp(request: Request, db: AsyncSession = Depends(get_db)):
    authorization = request.headers.get("authorization") or ""
    if not authorization.lower().startswith("bearer "):
        return unauthorized(request)
    found = await oauth.user_for_access_token(db, authorization[7:].strip())
    if found is None:
        return unauthorized(request, "invalid_token")
    user, _grant = found
    try:
        body = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(rpc_error(None, -32700, "Parse error"), status_code=400)

    ctx = assistant_tools.Context(db, user, oauth.public_base_url(request))
    if isinstance(body, list):  # JSON-RPC batches (protocol 2025-03-26)
        if not body:
            return JSONResponse(rpc_error(None, -32600, "Empty batch"), status_code=400)
        replies = [reply for reply in [await handle(ctx, message) for message in body] if reply is not None]
        return JSONResponse(replies) if replies else Response(status_code=202)
    reply = await handle(ctx, body)
    return JSONResponse(reply) if reply is not None else Response(status_code=202)


@router.get("/mcp", include_in_schema=False)
@router.delete("/mcp", include_in_schema=False)
async def mcp_no_stream():
    # Stateless server: no server-sent event stream and no sessions to end.
    return Response(status_code=405, headers={"Allow": "POST"})
