import asyncio
from tempfile import SpooledTemporaryFile

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile

from app.routers import jobs
from app.services.csrf import csrf_protect


class FakeRequest:
    def __init__(self, method="POST", cookies=None, headers=None, form=None):
        self.method = method
        self.cookies = cookies or {}
        self.headers = headers or {}
        self._form_value = form or {}

    async def form(self):
        return self._form_value


def make_upload(content: bytes, filename: str, content_type: str) -> UploadFile:
    file = SpooledTemporaryFile(max_size=1024 * 1024)
    file.write(content)
    file.seek(0)
    return UploadFile(file, filename=filename, headers=Headers({"content-type": content_type}))


def test_csrf_accepts_matching_form_token():
    request = FakeRequest(cookies={"csrf_token": "safe"}, form={"csrf_token": "safe"})
    asyncio.run(csrf_protect(request))


def test_csrf_rejects_missing_token():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(csrf_protect(FakeRequest()))
    assert exc.value.status_code == 403


def test_upload_validates_signature_and_uses_random_name(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "UPLOAD_DIR", tmp_path)
    upload = make_upload(b"\x89PNG\r\n\x1a\n" + b"payload", "message.png", "image/png")
    saved = asyncio.run(jobs.save_upload(upload))
    assert saved is not None
    assert saved.endswith(".png")
    assert "message.png" not in saved


def test_upload_rejects_spoofed_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "UPLOAD_DIR", tmp_path)
    upload = make_upload(b"not really a png", "message.png", "image/png")
    with pytest.raises(HTTPException, match="content"):
        asyncio.run(jobs.save_upload(upload))
    assert not list(tmp_path.iterdir())


def test_job_token_must_belong_to_user():
    class EmptyResult:
        def scalar_one_or_none(self):
            return None

    class CapturingDb:
        query = None

        async def execute(self, query):
            self.query = query
            return EmptyResult()

    db = CapturingDb()
    with pytest.raises(HTTPException, match="belongs to your account"):
        asyncio.run(jobs.require_owned_active_token(db, user_id=7, token_id=99))
    sql = str(db.query)
    assert "tokens.id" in sql
    assert "tokens.user_id" in sql
    assert "tokens.is_active" in sql
