import hashlib
import hmac

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app.config import settings

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.get("/meta", response_class=PlainTextResponse)
async def verify_meta_webhook(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
):
    if not settings.meta_webhook_verify_token:
        raise HTTPException(status_code=503, detail="Meta webhook is not configured")
    if hub_mode != "subscribe" or not hmac.compare_digest(hub_verify_token, settings.meta_webhook_verify_token):
        raise HTTPException(status_code=403, detail="Webhook verification failed")
    return hub_challenge


@router.post("/meta")
async def receive_meta_webhook(request: Request):
    if not settings.meta_app_secret:
        raise HTTPException(status_code=503, detail="Meta webhook is not configured")
    body = await request.body()
    supplied = request.headers.get("X-Hub-Signature-256", "")
    expected = "sha256=" + hmac.new(settings.meta_app_secret.encode(), body, hashlib.sha256).hexdigest()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Invalid webhook signature")
    # Delivery events will be normalized into DeliveryAttempt rows once the
    # account-specific Cloud API spike confirms the exact event contract.
    return {"received": True}
