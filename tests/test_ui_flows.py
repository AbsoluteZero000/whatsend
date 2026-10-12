import asyncio

import httpx
import pytest

from app.models.job import Job
from app.models.log import Log
from app.models.user import User
from app.routers import logs
from test_native_api import SESSION, exercise


def test_delivery_filters_pagination_and_user_isolation(tmp_path, monkeypatch):
    async def scenario(client, sessions, context):
        client._transport.app.include_router(logs.router)
        monkeypatch.setattr(logs, "require_user", lambda request: {"sub": "7"})
        async with sessions() as db:
            db.add(User(id=8, username="other", password_hash="hash"))
            for job_id, user_id, label in [(1, 7, "Weekly report"), (2, 8, "Private message")]:
                db.add(Job(id=job_id, user_id=user_id, token_id=3, label=label,
                           group_id="group", message="hello", trigger_type="now", trigger_value=""))
            await db.flush()
            db.add_all([Log(job_id=1, status="failed", response=f"failure-{i:02}") for i in range(51)])
            db.add(Log(job_id=1, status="sent", response="delivered"))
            db.add(Log(job_id=2, status="failed", response="private-failure"))
            await db.commit()

        first = await client.get("/logs?status=failed&job_id=1")
        assert first.status_code == 200
        assert first.text.count('<summary>Show response</summary>') == 50
        assert "Weekly report" in first.text and "Page 1 of 2" in first.text
        assert "delivered" not in first.text and "private-failure" not in first.text
        assert "status=failed&page=2&job_id=1" in first.text
        second = await client.get("/logs?status=failed&job_id=1&page=2")
        assert second.text.count('<summary>Show response</summary>') == 1
        sent = await client.get("/logs?status=sent")
        assert "delivered" in sent.text and "failure-" not in sent.text
        empty = await client.get("/logs?job_id=2")
        assert "private-failure" not in empty.text and "Clear filters" in empty.text
        assert (await client.get("/logs?status=invalid")).status_code == 422
        assert (await client.get("/logs?page=0")).status_code == 422

    asyncio.run(exercise(tmp_path, monkeypatch, lambda request: httpx.Response(500), scenario))


@pytest.mark.parametrize("state", ["connected", "qr_required"])
def test_connection_overview_reads_status_without_fetching_qr(tmp_path, monkeypatch, state):
    calls = []

    def worker(request):
        calls.append(request.url.path)
        assert "/qr" not in request.url.path
        return httpx.Response(200, json={"data": {"id": SESSION, "state": state}})

    async def scenario(client, sessions, context):
        response = await client.get("/tokens/3/whatsapp/status")
        assert response.status_code == 200
        assert response.json() == {"state": state}
        assert "no-store" in response.headers["cache-control"]
        assert len(calls) == 1
        assert (await client.get("/tokens/999/whatsapp/status")).status_code == 404

    asyncio.run(exercise(tmp_path, monkeypatch, worker, scenario))
