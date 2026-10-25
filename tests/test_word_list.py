import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.models.job import Job
from app.routers import jobs
from app.services import native_jobs, scheduler, word_list
from test_native_api import exercise, RECIPIENT

HEADERS = {"Cookie": "csrf_token=safe", "X-CSRF-Token": "safe"}


def job_with(items, end="repeat", next_index=0):
    return SimpleNamespace(word_list=json.dumps(items), word_list_end=end, word_list_next=next_index)


def test_validate_accepts_words_sentences_numbers_and_ranges():
    items, end, start = word_list.validate("Today: {word}", "Patience\n\n  A whole sentence here.  \n42\n1-5\n", "stop", 9)
    assert items == ["Patience", "A whole sentence here.", "42", "1-5"] and end == "stop" and start == 3
    assert word_list.validate("No list here", "", "repeat") == ([], "repeat", 0)
    for message, raw, end, error in [
        ("Has {word}", "  \n ", "repeat", "Add at least one item"),
        ("Missing the token", "A\nB", "repeat", "Put {word} in your message"),
        ("{word}", "\n".join(str(n) for n in range(501)), "repeat", "up to 500 items"),
        ("{word}", "x" * 1001, "repeat", "up to 1000 characters"),
        ("{word}", "A", "sometimes", "Choose what happens"),
    ]:
        with pytest.raises(ValueError, match=error.replace("{", r"\{").replace("}", r"\}")):
            word_list.validate(message, raw, end)
    assert word_list.longest_message("Page {word}!", ["1", "100-200", "3"]) == "Page 100-200!"


def test_fill_moves_through_the_list_and_retries_reuse_the_last_item():
    job = job_with(["A", "B", "C"])
    assert [word_list.fill(job, "x {word}") for _ in range(4)] == ["x A", "x B", "x C", "x A"]
    assert word_list.fill(job, "{word}", retry=True) == "A" and job.word_list_next == 1
    assert word_list.next_item(job) == "B" and not word_list.finished(job)

    stopping = job_with(["A", "B"], "stop")
    assert [word_list.fill(stopping, "{word}") for _ in range(2)] == ["A", "B"]
    assert word_list.finished(stopping) and word_list.next_item(stopping) is None
    assert word_list.fill(stopping, "{word}", retry=True) == "B"
    assert word_list.fill(SimpleNamespace(word_list=None, word_list_end="repeat", word_list_next=0), "plain {word}") == "plain {word}"


def sent_texts(calls):
    return [json.loads(request.content)["text"] for request in calls if request.method == "POST"]


@pytest.mark.parametrize("end", ["repeat", "stop"])
def test_changing_text_from_the_composer_to_whatsapp(tmp_path, monkeypatch, end):
    calls = []

    async def no_schedule(job):
        pass
    monkeypatch.setattr(jobs, "register_job", no_schedule)

    def worker(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(202, json={"data": {"id": str(uuid4()), "status": "pending"}})
        return httpx.Response(200, json={"data": {"id": request.url.path.rsplit("/", 1)[-1], "status": "sent"}})

    async def scenario(client, sessions, context):
        scheduler.scheduler.remove_all_jobs()  # earlier tests can leave jobs with the same ids behind
        page = await client.get("/jobs/create")
        assert 'id="word_list_add"' in page.text and "Add text that changes each time" in page.text
        fields = {"token_id": "3", "group_ids": RECIPIENT, "message": "Today's reading: {word}", "trigger_type": "cron",
                  "cron_freq": "daily", "cron_time": "09:00", "word_list_end": end}

        missing = await client.post("/jobs/create", data={**fields, "message": "No token", "word_list": "Pages 1-5"}, headers=HEADERS)
        assert missing.status_code == 400 and "Put {word} in your message" in missing.text
        assert "Pages 1-5</textarea>" in missing.text  # the list survives the error
        empty = await client.post("/jobs/create", data={**fields, "word_list": ""}, headers=HEADERS)
        assert empty.status_code == 400 and "Add at least one item" in empty.text

        created = await client.post("/jobs/create", data={**fields, "word_list": "Pages 1-5\nThe whole of chapter two\n42"}, headers=HEADERS)
        assert created.status_code == 303
        async with sessions() as db:
            job = (await db.execute(select(Job))).scalar_one()
            job_id = job.id
            assert json.loads(job.word_list) == ["Pages 1-5", "The whole of chapter two", "42"]
            assert job.word_list_end == end and job.word_list_next == 0
            job.skip_count = 1
            await db.commit()

        listing = await client.get("/jobs")
        assert "Next text:" in listing.text and "Pages 1-5" in listing.text
        await scheduler.send_job(job_id)  # skipped: the item isn't used up
        assert sent_texts(calls) == []
        for _ in range(4):
            await scheduler.send_job(job_id)
            await native_jobs.poll_deliveries()
        expected = ["Pages 1-5", "The whole of chapter two", "42"] + (["Pages 1-5"] if end == "repeat" else [])
        assert sent_texts(calls) == ["Today's reading: " + item for item in expected]
        async with sessions() as db:
            job = await db.get(Job, job_id)
            assert job.status == ("active" if end == "repeat" else "completed")
            assert job.word_list_next == (1 if end == "repeat" else 3)

        detail = await client.get(f"/jobs/{job_id}")
        assert "Changing text" in detail.text and "<bdi>42</bdi>" in detail.text
        if end == "repeat":
            assert 'class="is-next"><bdi>The whole of chapter two</bdi>' in detail.text
            edit = await client.get(f"/jobs/{job_id}/edit")
            assert 'data-initial="1"' in edit.text and "Pages 1-5\nThe whole of chapter two\n42</textarea>" in edit.text
            saved = await client.post(f"/jobs/{job_id}/edit", data={**fields, "word_list": "One\nTwo\nThree", "word_list_next": "2"}, headers=HEADERS)
            assert saved.status_code == 303
            async with sessions() as db:
                job = await db.get(Job, job_id)
                assert json.loads(job.word_list) == ["One", "Two", "Three"] and job.word_list_next == 2
            cloned = await client.post(f"/jobs/{job_id}/clone", headers=HEADERS)
            assert cloned.status_code == 303
            async with sessions() as db:
                copy = (await db.execute(select(Job).where(Job.id != job_id))).scalar_one()
                assert copy.word_list == job.word_list and copy.word_list_next == 0
        else:
            assert 'class=" is-used"><bdi>42</bdi>' in detail.text
            await scheduler.send_job(job_id)  # a used-up list sends nothing more
            assert len(sent_texts(calls)) == 3

    asyncio.run(exercise(tmp_path, monkeypatch, worker, scenario))
