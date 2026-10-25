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


def job_with(lists, end="repeat", next_index=0):
    return SimpleNamespace(word_list=json.dumps(lists), word_list_end=end, word_list_next=next_index)


def test_validate_accepts_words_sentences_numbers_and_ranges():
    lists, end, start = word_list.validate("Today: {word}", ["Patience\n\n  A whole sentence here.  \n42\n1-5\n"], "stop", 9)
    assert lists == [["Patience", "A whole sentence here.", "42", "1-5"]] and end == "stop" and start == 3
    assert word_list.validate("No list here", [], "repeat") == ([], "repeat", 0)
    assert word_list.validate("No list here", [""], "repeat") == ([], "repeat", 0)
    for message, raws, end, error in [
        ("Has {word}", ["  \n "], "repeat", "Add at least one item to the changing text list"),
        ("Missing the token", ["A\nB"], "repeat", "Put {word} in your message"),
        ("{word}", ["\n".join(str(n) for n in range(501))], "repeat", "up to 500 items"),
        ("{word}", ["x" * 1001], "repeat", "up to 1000 characters"),
        ("{word}", ["A"], "sometimes", "Choose what happens"),
        ("{word} {word2}", ["A"], "repeat", "uses {word2} but has no list"),
        ("{word}", ["A", "B"], "repeat", "Put {word2} in your message where changing part 2"),
        ("{word} {word2} {word3}", ["A", "", "C"], "repeat", "Add at least one item to changing part 2, or remove {word2}"),
        (" ".join("{word%d}" % n for n in range(1, 7)), ["x"] * 6, "repeat", "up to 5 changing parts"),
    ]:
        with pytest.raises(ValueError, match=error.replace("{", r"\{").replace("}", r"\}")):
            word_list.validate(message, raws, end)
    with pytest.raises(ValueError, match="الجزء المتغير 2"):
        word_list.validate("{word}", ["A", "B"], "repeat", lang="ar")
    assert word_list.validate("{word1} and {word2}", ["A", "B\nC"], "repeat") == ([["A"], ["B", "C"]], "repeat", 0)
    assert word_list.longest_message("Page {word}, verse {word2}!", [["1", "100-200"], ["7", "286"]]) == "Page 100-200, verse 286!"


def test_lists_move_together_and_shorter_ones_start_over():
    job = job_with([["1-5", "6-10", "11-15"], ["Mon", "Tue"]])
    sends = [word_list.fill(job, "Read {word} on {word2}") for _ in range(4)]
    assert sends == ["Read 1-5 on Mon", "Read 6-10 on Tue", "Read 11-15 on Mon", "Read 1-5 on Mon"]
    assert word_list.fill(job, "{word}/{word2}", retry=True) == "1-5/Mon" and word_list.next_items(job) == ["6-10", "Tue"]

    stopping = job_with([["A", "B"], ["x", "y", "z"]], "stop")
    assert [word_list.fill(stopping, "{word}{word2}") for _ in range(3)] == ["Ax", "By", "Az"]
    assert word_list.finished(stopping) and word_list.next_items(stopping) == []
    assert word_list.fill(stopping, "{word}{word2}", retry=True) == "Az"


def test_messages_saved_with_one_flat_list_keep_working():
    job = job_with(["A", "B", "C"])
    assert word_list.lists(job) == [["A", "B", "C"]]
    assert [word_list.fill(job, "x {word}") for _ in range(4)] == ["x A", "x B", "x C", "x A"]
    assert word_list.fill(job, "{word}", retry=True) == "A" and job.word_list_next == 1
    assert word_list.next_items(job) == ["B"] and not word_list.finished(job)
    stopping = job_with(["A", "B"], "stop")
    assert [word_list.fill(stopping, "{word}") for _ in range(2)] == ["A", "B"] and word_list.finished(stopping)
    plain = SimpleNamespace(word_list=None, word_list_end="repeat", word_list_next=0)
    assert word_list.fill(plain, "plain {word}") == "plain {word}" and word_list.lists(plain) == []


def sent_texts(calls):
    return [json.loads(request.content)["text"] for request in calls if request.method == "POST"]


def worker_for(calls):
    def worker(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(202, json={"data": {"id": str(uuid4()), "status": "pending"}})
        return httpx.Response(200, json={"data": {"id": request.url.path.rsplit("/", 1)[-1], "status": "sent"}})
    return worker


async def no_schedule(job):
    pass


@pytest.mark.parametrize("end", ["repeat", "stop"])
def test_changing_text_from_the_composer_to_whatsapp(tmp_path, monkeypatch, end):
    calls = []
    monkeypatch.setattr(jobs, "register_job", no_schedule)

    async def scenario(client, sessions, context):
        scheduler.scheduler.remove_all_jobs()  # earlier tests can leave jobs with the same ids behind
        page = await client.get("/jobs/create")
        assert 'id="word_list_add"' in page.text and "Add text that changes each time" in page.text
        fields = {"token_id": "3", "group_ids": RECIPIENT, "message": "Today's reading: {word}", "trigger_type": "cron",
                  "cron_freq": "daily", "cron_time": "09:00", "word_list_end": end}

        missing = await client.post("/jobs/create", data={**fields, "message": "No token", "word_list": "Pages 1-5"}, headers=HEADERS)
        assert missing.status_code == 400 and "Put {word} in your message" in missing.text
        assert 'id="word_lists" name="word_lists" value="[&#34;Pages 1-5&#34;]"' in missing.text  # the list survives the error
        empty = await client.post("/jobs/create", data={**fields, "word_list": ""}, headers=HEADERS)
        assert empty.status_code == 400 and "Add at least one item" in empty.text

        created = await client.post("/jobs/create", data={**fields, "word_list": "Pages 1-5\nThe whole of chapter two\n42"}, headers=HEADERS)
        assert created.status_code == 303
        async with sessions() as db:
            job = (await db.execute(select(Job))).scalar_one()
            job_id = job.id
            assert json.loads(job.word_list) == [["Pages 1-5", "The whole of chapter two", "42"]]
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
            assert 'data-initial="1"' in edit.text and "[&#34;Pages 1-5\\nThe whole of chapter two\\n42&#34;]" in edit.text
            saved = await client.post(f"/jobs/{job_id}/edit", data={**fields, "word_lists": json.dumps(["One\nTwo\nThree"]),
                                                                    "word_list_next": "2"}, headers=HEADERS)
            assert saved.status_code == 303
            async with sessions() as db:
                job = await db.get(Job, job_id)
                assert json.loads(job.word_list) == [["One", "Two", "Three"]] and job.word_list_next == 2
            cloned = await client.post(f"/jobs/{job_id}/clone", headers=HEADERS)
            assert cloned.status_code == 303
            async with sessions() as db:
                copy = (await db.execute(select(Job).where(Job.id != job_id))).scalar_one()
                assert copy.word_list == job.word_list and copy.word_list_next == 0
        else:
            assert 'class=" is-used"><bdi>42</bdi>' in detail.text
            await scheduler.send_job(job_id)  # a used-up list sends nothing more
            assert len(sent_texts(calls)) == 3

    asyncio.run(exercise(tmp_path, monkeypatch, worker_for(calls), scenario))


def test_two_changing_parts_from_the_composer_to_whatsapp(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(jobs, "register_job", no_schedule)

    async def scenario(client, sessions, context):
        scheduler.scheduler.remove_all_jobs()
        fields = {"token_id": "3", "group_ids": RECIPIENT, "message": "Read pages {word}, then memorize verse {word2}",
                  "trigger_type": "cron", "cron_freq": "daily", "cron_time": "09:00", "word_list_end": "stop"}
        no_list = await client.post("/jobs/create", data={**fields, "word_lists": json.dumps(["1-5"])}, headers=HEADERS)
        assert no_list.status_code == 400 and "uses {word2} but has no list" in no_list.text
        created = await client.post("/jobs/create", data={**fields, "word_lists": json.dumps(["1-5\n6-10\n11-15", "1\n2"])}, headers=HEADERS)
        assert created.status_code == 303
        async with sessions() as db:
            job = (await db.execute(select(Job))).scalar_one()
            job_id = job.id
            assert json.loads(job.word_list) == [["1-5", "6-10", "11-15"], ["1", "2"]]
        listing = await client.get("/jobs")
        assert "<bdi>1-5</bdi></mark> · <mark><bdi>1</bdi>" in listing.text
        detail = await client.get(f"/jobs/{job_id}")
        assert "Changing part 2" in detail.text and "<code>{word2}</code>" in detail.text
        for _ in range(4):
            await scheduler.send_job(job_id)
            await native_jobs.poll_deliveries()
        assert sent_texts(calls) == ["Read pages 1-5, then memorize verse 1", "Read pages 6-10, then memorize verse 2",
                                     "Read pages 11-15, then memorize verse 1"]
        async with sessions() as db:
            assert (await db.get(Job, job_id)).status == "completed"

    asyncio.run(exercise(tmp_path, monkeypatch, worker_for(calls), scenario))
