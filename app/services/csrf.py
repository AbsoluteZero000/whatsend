import secrets

from fastapi import HTTPException, Request


CSRF_COOKIE = "csrf_token"
CSRF_FIELD = "csrf_token"


def get_or_create_csrf_token(request: Request) -> tuple[str, bool]:
    token = request.cookies.get(CSRF_COOKIE)
    if token:
        return token, False
    return secrets.token_urlsafe(32), True


async def csrf_protect(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return

    expected = request.cookies.get(CSRF_COOKIE, "")
    supplied = request.headers.get("X-CSRF-Token", "")
    if not supplied:
        form = await request.form()
        supplied = str(form.get(CSRF_FIELD, ""))

    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")
