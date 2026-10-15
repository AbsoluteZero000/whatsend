import asyncio

import httpx

from test_native_api import exercise


def test_message_mode_and_variables_follow_advanced_setting(tmp_path, monkeypatch):
    def worker(request):
        return httpx.Response(200, json={"groups": [], "data": []})

    async def scenario(client, sessions, context):
        hidden = (await client.get('/jobs/create')).text
        assert '<div class="composer-extras" hidden>' in hidden
        assert '<div class="message-variables" hidden>' in hidden
        assert 'name="message_mode"' in hidden  # still submits "repeat"

        shown = (await client.get('/jobs/create', headers={"Cookie": "composer_extras=1"})).text
        assert '<div class="composer-extras" >' in shown
        assert '<div class="message-variables" >' in shown

    asyncio.run(exercise(tmp_path, monkeypatch, worker, scenario))
