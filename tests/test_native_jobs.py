import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.models.job import Job
from app.models.delivery_attempt import DeliveryAttempt
from app.models.log import Log
from app.services import scheduler, native_jobs
from app.routers import jobs
from test_native_api import exercise, SESSION, RECIPIENT, MESSAGE


async def no_schedule(job):
    pass


@pytest.mark.parametrize("media", ["text", "png", "pdf", "docx"])
def test_native_jobs_enqueue_and_confirm_delivery(tmp_path, monkeypatch, media):
    calls = []
    upstream_state = ["processing"]
    monkeypatch.setattr(jobs, "register_job", no_schedule)
    monkeypatch.setattr(jobs, "UPLOAD_DIR", tmp_path)

    def relay(request):
        assert request.headers["authorization"] == "Bearer relay-secret"
        if request.url.path.endswith("/groups"):
            return httpx.Response(200, json={"data": [{"external_chat_id": RECIPIENT, "name": "My Relay group"}], "next_cursor": ""})
        calls.append(request)
        if request.method == "POST":
            assert request.url.path == f"/api/v1/sessions/{SESSION}/messages"
            assert request.headers["Idempotency-Key"].startswith("echosend-job:")
            if media != "text":
                assert b'name="file"' in request.content and b'name="caption"' in request.content
                assert ('filename="attachment.' + media + '"').encode() in request.content
            else:
                assert json.loads(request.content) == {"recipient": RECIPIENT, "text": "Scheduled message"}
            return httpx.Response(202, json={"data": {"id": MESSAGE, "status": "pending"}})
        assert request.url.path.endswith(f"/messages/{MESSAGE}")
        return httpx.Response(200, json={"data": {"id": MESSAGE, "status": upstream_state[0]}})

    async def scenario(client, sessions, context):
        page = await client.get("/jobs/create")
        assert page.status_code == 200 and 'data-provider="native"' in page.text
        groups = await client.get("/jobs/groups?token_id=3")
        assert groups.json()["groups"] == [{"id": RECIPIENT, "name": "My Relay group"}]
        assert (await client.get("/jobs/groups?token_id=999")).status_code == 400
        fields = {"token_id": "3", "group_ids": RECIPIENT, "message": "Scheduled message", "trigger_type": "now"}
        attachments = {"png": (b"\x89PNG\r\n\x1a\nimage", "image/png"), "pdf": (b"%PDF-1.7", "application/pdf"),
                       "docx": (b"PK\x03\x04office", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
        files = {"image": ("attachment." + media, *attachments[media])} if media != "text" else None
        result = await client.post("/jobs/create", data=fields, files=files,
                                   headers={"Cookie": "csrf_token=safe", "X-CSRF-Token": "safe"})
        assert result.status_code == 303
        async with sessions() as db:
            job = (await db.execute(select(Job))).scalar_one()
            job_id = job.id
        await asyncio.gather(scheduler.send_job(job_id), scheduler.send_job(job_id))
        await scheduler.send_job(job_id)  # duplicate run cannot enqueue again
        async with sessions() as db:
            assert (await db.get(Job, job_id)).status == "queued"
            attempt = (await db.execute(select(DeliveryAttempt))).scalar_one()
            assert attempt.status == "queued" and attempt.relay_message_id == MESSAGE
            assert attempt.relay_base_url == "https://native.test"
        await native_jobs.poll_deliveries()
        async with sessions() as db:
            assert (await db.execute(select(DeliveryAttempt))).scalar_one().status == "processing"
            assert (await db.get(Job, job_id)).status == "queued"
        upstream_state[0] = "sent"
        await native_jobs.poll_deliveries()
        await native_jobs.poll_deliveries()
        async with sessions() as db:
            assert (await db.get(Job, job_id)).status == "completed"
            assert (await db.execute(select(DeliveryAttempt))).scalar_one().status == "sent"
            assert [log.status for log in (await db.execute(select(Log).order_by(Log.id))).scalars()] == ["queued", "sent"]
        assert len(calls) == 3

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


@pytest.mark.parametrize("trigger", ["cron", "trigger", "date"])
def test_relay_job_terminal_failure_and_poll_error(tmp_path, monkeypatch, trigger):
    calls = []
    fail_poll = [True]

    def relay(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(202, json={"data": {"id": MESSAGE, "status": "pending"}})
        if fail_poll[0]:
            raise httpx.ReadTimeout("poll failure", request=request)
        return httpx.Response(200, json={"data": {"id": MESSAGE, "status": "failed"}})

    async def scenario(client, sessions, context):
        async with sessions() as db:
            job = Job(user_id=7, token_id=3, group_id=RECIPIENT, message="Hello", trigger_type=trigger,
                      trigger_value="0 9 * * *" if trigger == "cron" else "", status="trigger" if trigger == "trigger" else "active")
            db.add(job)
            await db.commit()
            job_id = job.id
        if trigger == "trigger":
            result = await client.post(f"/jobs/{job_id}/send-now",
                                       headers={"Cookie": "csrf_token=safe", "X-CSRF-Token": "safe"})
            assert result.status_code == 303
        else:
            await scheduler.send_job(job_id)
        await native_jobs.poll_deliveries()
        async with sessions() as db:
            attempt = (await db.execute(select(DeliveryAttempt))).scalar_one()
            assert attempt.status == "queued" and attempt.relay_message_id == MESSAGE
        fail_poll[0] = False
        await native_jobs.poll_deliveries()
        await native_jobs.poll_deliveries()
        async with sessions() as db:
            job = await db.get(Job, job_id)
            assert job.status == {"cron": "active", "trigger": "trigger", "date": "failed"}[trigger]
            assert job.retry_at is None
            assert (await db.execute(select(DeliveryAttempt))).scalar_one().status == "failed"
        assert len(calls) == 3

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


@pytest.mark.parametrize("failure", ["timeout", "500", "403", "409", "malformed"])
def test_native_jobs_never_retry_uncertain_enqueue(tmp_path, monkeypatch, failure):
    calls = []

    def relay(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("sensitive upstream error", request=request)
        if failure == "malformed":
            return httpx.Response(202, json={"data": {"id": "invalid", "status": "pending"}})
        return httpx.Response(int(failure), json={"error": {"message": "sensitive upstream error"}})

    async def scenario(client, sessions, context):
        async with sessions() as db:
            job = Job(user_id=7, token_id=3, group_id=RECIPIENT, message="Hello", trigger_type="now", trigger_value="", status="active")
            db.add(job)
            await db.commit()
            job_id = job.id
        await scheduler.send_job(job_id)
        await scheduler.send_job(job_id, retry_only=True)
        await native_jobs.poll_deliveries()
        async with sessions() as db:
            job = await db.get(Job, job_id)
            expected = "failed" if failure == "403" else "unknown"
            assert job.status == expected and job.retry_at is None
            attempt = (await db.execute(select(DeliveryAttempt))).scalar_one()
            assert attempt.status == expected
            assert "sensitive upstream error" not in attempt.response
        assert len(calls) == 1

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


def test_native_jobs_recover_unacknowledged_run_without_resending(tmp_path, monkeypatch):
    def relay(request):
        pytest.fail("An interrupted run was resubmitted")

    async def scenario(client, sessions, context):
        async with sessions() as db:
            job = Job(user_id=7, token_id=3, group_id=RECIPIENT, message="Hello", trigger_type="now", trigger_value="", status="queued")
            db.add(job)
            await db.flush()
            db.add(DeliveryAttempt(job_id=job.id, run_id=str(uuid4()), group_id=RECIPIENT, status="submitting",
                                   relay_token_id=3, relay_session_id=SESSION, relay_base_url="https://native.test",
                                   created_at=datetime.now(timezone.utc) - timedelta(minutes=5)))
            await db.commit()
            job_id = job.id
        await native_jobs.poll_deliveries()
        await scheduler.send_job(job_id)
        async with sessions() as db:
            assert (await db.get(Job, job_id)).status == "unknown"

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))


def test_relay_job_validates_media_and_text_before_scheduling(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "UPLOAD_DIR", tmp_path)

    def relay(request):
        pytest.fail("Invalid job contacted Relay")

    async def scenario(client, sessions, context):
        fields = {"token_id": "3", "group_ids": RECIPIENT, "message": "x" * 4097, "trigger_type": "now"}
        headers = {"Cookie": "csrf_token=safe", "X-CSRF-Token": "safe"}
        assert (await client.post("/jobs/create", data=fields, headers=headers)).status_code == 400
        fields["message"] = "Caption"
        result = await client.post("/jobs/create", data=fields, headers=headers,
                                   files={"image": ("report.gif", b"GIF89aimage", "image/gif")})
        assert result.status_code == 400 and "JPEG" in result.text
        assert not list(tmp_path.glob("*.gif"))
        async with sessions() as db:
            assert not (await db.execute(select(Job))).scalars().all()

    asyncio.run(exercise(tmp_path, monkeypatch, relay, scenario))
