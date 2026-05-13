from fastapi import APIRouter, Request

from app.routers.auth import get_current_user

router = APIRouter(prefix="/about", tags=["about"])


@router.get("")
async def about_page(request: Request):
    return request.app.state.render(request, "about/index.html")
