from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.job import Job
from app.models.job_series import SeriesAttachment
from app.routers.auth import require_user
from app.routers.jobs import MEDIA_SIGNATURES, UPLOAD_DIR
from app.services.public_media import verify_public_media

router = APIRouter(prefix="/media", tags=["media"])


@router.get("/public/{expires}/{signature}/{filename}")
async def get_public_media(expires: int, signature: str, filename: str):
    """Signed links that let Instagram download a post's photo or video for an hour."""
    if not filename or Path(filename).name != filename or not verify_public_media(filename, expires, signature):
        raise HTTPException(status_code=404, detail="Media not found")
    path = (UPLOAD_DIR / filename).resolve()
    if path.parent != UPLOAD_DIR.resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail="Media not found")
    media_type = MEDIA_SIGNATURES.get(path.suffix.lower(), ("application/octet-stream", None))[0]
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})


@router.get("/{filename}")
async def get_media(filename: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = require_user(request)
    if not filename or Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="Media not found")

    path = (UPLOAD_DIR / filename).resolve()
    if path.parent != UPLOAD_DIR.resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail="Media not found")

    result = await db.execute(
        select(Job.id).where(Job.user_id == int(user["sub"]), Job.image_path == str(path)).limit(1)
    )
    if result.scalar_one_or_none() is None:
        asset = await db.scalar(select(SeriesAttachment.id).where(
            SeriesAttachment.user_id == int(user['sub']), SeriesAttachment.path == str(path),
        ).limit(1))
        if asset is None:
            raise HTTPException(status_code=404, detail="Media not found")

    media_type = MEDIA_SIGNATURES.get(path.suffix.lower(), ("application/octet-stream", None))[0]
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "private, max-age=300"})
