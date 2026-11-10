import asyncio
import base64
import json

import httpx
import pytest

from app.config import settings
from app.providers.base import ProviderError
from app.providers.signal.sender import SignalSender, connection_token
from test_telegram import transport

NUMBER = '+201234567890'


@pytest.fixture
def sender(monkeypatch):
    monkeypatch.setattr(settings, 'signal_api_url', 'https://signal.internal')
    monkeypatch.setattr(settings, 'signal_api_token', 'private-bridge-token')
    return SignalSender(connection_token(NUMBER))


def test_signal_messages_and_attachments(tmp_path, monkeypatch, sender):
    file = tmp_path / 'uuid.pdf'; file.write_bytes(b'%PDF-test')
    def handler(request):
        assert request.url == 'https://signal.internal/v2/send'
        assert request.headers['authorization'] == 'Bearer private-bridge-token'
        payload = json.loads(request.content)
        assert payload['number'] == NUMBER and payload['recipients'] == ['group.YWJjZA==']
        assert payload['message'] == 'hello'
        assert payload['base64_attachments'] == ['data:application/pdf;filename=Original.pdf;base64,' + base64.b64encode(b'%PDF-test').decode()]
        return httpx.Response(201, json={'timestamp': '1730000000000'})
    transport(monkeypatch, handler)
    assert asyncio.run(sender.send('group.YWJjZA==', 'hello', str(file), 'Original.pdf')) == {'timestamp': '1730000000000'}


def test_signal_group_discovery_is_account_scoped(monkeypatch, sender):
    def handler(request):
        assert request.url.path == f'/v1/groups/{NUMBER}'
        return httpx.Response(200, json=[{'id': 'group.YWJj', 'name': 'Team'}])
    transport(monkeypatch, handler)
    assert asyncio.run(sender.get_groups()) == [{'id': 'group.YWJj', 'name': 'Team'}]


def test_signal_capability_cannot_be_used_for_another_account(sender):
    token = connection_token(NUMBER)
    with pytest.raises(ValueError): SignalSender(token.replace(NUMBER, '+201111111111'))
    with pytest.raises(ValueError): SignalSender(NUMBER)
    with pytest.raises(ValueError): SignalSender(NUMBER + ':forged')


def test_signal_needs_operator_configured_service(monkeypatch):
    for url in ['', 'https://user:password@signal.example', 'https://signal.example/path', 'ftp://signal.example']:
        monkeypatch.setattr(settings, 'signal_api_url', url)
        with pytest.raises(ValueError): SignalSender(connection_token(NUMBER))


@pytest.mark.parametrize('status,payload,unknown', [(401, {'error': 'private-bridge-token'}, False),
    (500, {'error': 'server'}, True), (200, {}, True)])
def test_signal_failures_are_redacted(monkeypatch, sender, status, payload, unknown):
    transport(monkeypatch, lambda request: httpx.Response(status, json=payload))
    with pytest.raises(ProviderError) as exc: asyncio.run(sender.send('+201111111111', 'hello'))
    assert exc.value.unknown is unknown and 'private-bridge-token' not in str(exc.value)
