"""Adapter for bbernhard/signal-cli-rest-api; no user-controlled service URLs."""
import base64
import mimetypes
import hashlib
import hmac
import re
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

from app.config import settings
from app.providers import BaseSender
from app.providers.base import ProviderError


def validate_number(value: str) -> None:
    if not re.fullmatch(r'\+[1-9][0-9]{6,14}', value):
        raise ValueError("Enter the linked Signal account number in international format (+country code and number)")


def connection_token(number: str) -> str:
    """Operator-issued capability for one linked account, not every bridge account."""
    validate_number(number)
    secret = settings.token_encryption_key or settings.secret_key
    signature = hmac.new(secret.encode(), f'signal-account:{number}'.encode(), hashlib.sha256).hexdigest()
    return f'{number}:{signature}'


def validate_credentials(value: str) -> None:
    number, separator, signature = value.partition(':')
    validate_number(number)
    if not separator or not re.fullmatch(r'[a-f0-9]{64}', signature) or not hmac.compare_digest(connection_token(number), value):
        raise ValueError("Enter the Signal connection token issued by your administrator")


class SignalSender(BaseSender):
    def __init__(self, credential: str, timeout: int = 60):
        validate_credentials(credential)
        number = credential.split(':', 1)[0]
        self.number = number
        self.timeout = timeout
        origin = settings.signal_api_url.rstrip('/')
        parts = urlsplit(origin)
        if (parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password
                or parts.path or parts.query or parts.fragment or any(ord(c) <= 32 for c in origin)):
            raise ValueError("An administrator must configure SIGNAL_API_URL as the private Signal service origin")
        self._url = origin
        self._headers = {'Authorization': f'Bearer {settings.signal_api_token}'} if settings.signal_api_token else {}

    def is_recipient_id(self, value: str) -> bool:
        return bool(re.fullmatch(r"\+[1-9][0-9]{6,14}|group\.[A-Za-z0-9+/=_-]+", value))

    def validate_payload(self, recipient, message, image_path=None):
        if not self.is_recipient_id(recipient):
            raise ValueError("Signal needs an international phone number or a group ID from the picker")
        if not message and not image_path:
            raise ValueError("A Signal message or attachment is required")
        if len(message.encode('utf-8')) > 4096:
            raise ValueError("Signal messages in this app may contain at most 4096 UTF-8 bytes")
        if image_path and not 0 < Path(image_path).stat().st_size <= 50 * 1024 * 1024:
            raise ValueError("Signal attachment must contain 1–50 MB")

    async def _request(self, method, path, **kwargs):
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.request(method, self._url + path, headers=self._headers, **kwargs)
        except httpx.RequestError:
            raise ProviderError("Signal service acknowledgement is uncertain; check Signal before resending", unknown=method == 'POST') from None
        if response.is_error:
            raise ProviderError(f"Signal service rejected the request ({response.status_code}); check the linked account and service",
                                unknown=method == 'POST' and response.status_code >= 500)
        try:
            return response.json()
        except ValueError:
            raise ProviderError("Signal service returned an invalid acknowledgement", unknown=method == 'POST') from None

    async def send(self, chat_id, message, image_path=None, filename=None):
        self.validate_payload(chat_id, message, image_path)
        payload = {'number': self.number, 'recipients': [chat_id], 'message': message}
        if image_path:
            path = Path(image_path)
            name = (filename or path.name).replace('\r', '').replace('\n', '').replace(';', '_')
            mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
            payload['base64_attachments'] = [f'data:{mime};filename={name.replace(",", "_")};base64,' + base64.b64encode(path.read_bytes()).decode('ascii')]
        result = await self._request('POST', '/v2/send', json=payload)
        if not isinstance(result, dict) or not str(result.get('timestamp', '')).isdigit():
            raise ProviderError("Signal service returned an invalid send acknowledgement", unknown=True)
        return {'timestamp': str(result['timestamp'])}

    async def get_groups(self):
        payload = await self._request('GET', f'/v1/groups/{quote(self.number, safe="")}')
        if not isinstance(payload, list):
            raise ProviderError("Signal service returned an invalid group list")
        groups = []
        for group in payload:
            if not isinstance(group, dict) or not isinstance(group.get('id'), str) or not group['id'].startswith('group.'):
                raise ProviderError("Signal service returned an invalid group")
            groups.append({'id': group['id'], 'name': group.get('name') or group['id']})
        return groups
