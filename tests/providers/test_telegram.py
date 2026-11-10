import asyncio
import json

import httpx
import pytest

from app.providers.base import ProviderError
from app.providers.telegram.sender import TelegramSender

TOKEN = '123456:abcdefghijklmnopqrstuvwxyz0123456789'
CLIENT = httpx.AsyncClient


def transport(monkeypatch, handler):
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: CLIENT(transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.parametrize('extension,method,field', [('', 'sendMessage', None), ('.jpg', 'sendPhoto', 'photo'),
    ('.mp4', 'sendVideo', 'video'), ('.pdf', 'sendDocument', 'document'), ('.webp', 'sendDocument', 'document')])
def test_telegram_payloads_and_original_filename(tmp_path, monkeypatch, extension, method, field):
    def handler(request):
        assert request.url.host == 'api.telegram.org'
        assert request.url.path == f'/bot{TOKEN}/{method}'
        if field:
            assert f'name="{field}"'.encode() in request.content
            assert 'filename="Original name'.encode() in request.content
            assert b'name="caption"' in request.content
        else:
            assert json.loads(request.content) == {'chat_id': '-100123456789', 'text': 'hello'}
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': 42}})
    transport(monkeypatch, handler)
    path = None
    if extension:
        file = tmp_path / ('uuid' + extension); file.write_bytes(b'content'); path = str(file)
    result = asyncio.run(TelegramSender(TOKEN).send('-100123456789', 'hello', path, 'Original name' + extension))
    assert result == {'message_id': 42}


@pytest.mark.parametrize('payload,status,unknown', [({'ok': False, 'description': TOKEN}, 401, False),
    ({'ok': True, 'result': {}}, 200, True), ({'ok': False}, 500, True), ([], 200, True)])
def test_telegram_rejects_invalid_responses_without_exposing_credentials(monkeypatch, payload, status, unknown):
    transport(monkeypatch, lambda request: httpx.Response(status, json=payload))
    with pytest.raises(ProviderError) as exc:
        asyncio.run(TelegramSender(TOKEN).send('@channel_name', 'hello'))
    assert TOKEN not in str(exc.value)
    assert exc.value.unknown is unknown


def test_telegram_network_failure_is_uncertain_and_redacted(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout(f'Error for {request.url}', request=request)
    transport(monkeypatch, handler)
    with pytest.raises(ProviderError) as exc:
        asyncio.run(TelegramSender(TOKEN).send('1234', 'hello'))
    assert exc.value.unknown and TOKEN not in str(exc.value)


def test_telegram_validates_utf16_limits_and_bot_credentials(tmp_path):
    sender = TelegramSender(TOKEN)
    with pytest.raises(ValueError): sender.validate_payload('not a chat', 'hello')
    with pytest.raises(ValueError): sender.validate_payload('1234', '😀' * 2049)
    with pytest.raises(ValueError): sender.validate_payload('1234', '')
    with pytest.raises(ValueError): TelegramSender('invalid')
    path = tmp_path / 'photo.jpg'; path.write_bytes(b'x')
    with pytest.raises(ValueError): sender.validate_payload('1234', '😀' * 513, str(path))
