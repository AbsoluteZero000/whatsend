"""Official Content Posting API client. Initialization is not publication."""
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.providers.base import ProviderError


def validate_token(value):
    if not value or len(value) > 256 or any(ord(char) <= 32 for char in value):
        raise ValueError("Enter a TikTok user access token authorized for video.publish")


class TikTokPublisher:
    def __init__(self, access_token: str):
        validate_token(access_token)
        self._headers = {'Authorization': f'Bearer {access_token}'}

    async def _api(self, path, payload=None, *, mutating=False):
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post('https://open.tiktokapis.com/v2/post/publish/' + path,
                                            headers=self._headers, json=payload or {})
        except httpx.RequestError:
            raise ProviderError("TikTok request could not be confirmed; check the post status before trying again", unknown=mutating) from None
        try:
            body = response.json()
        except ValueError:
            raise ProviderError("TikTok returned an invalid acknowledgement", unknown=mutating) from None
        if not isinstance(body, dict) or not isinstance(body.get('error'), dict):
            raise ProviderError("TikTok returned an invalid response", unknown=mutating)
        if response.is_error or body['error'].get('code') != 'ok':
            code = body['error'].get('code')
            # Return a bounded error code only; arbitrary upstream messages may contain secrets.
            code = code if isinstance(code, str) and code.replace('_', '').isalnum() and len(code) <= 80 else 'request_rejected'
            raise ProviderError(f"TikTok rejected the request ({code}); check authorization, publishing permissions and account limits",
                                unknown=mutating and response.status_code >= 500)
        if not isinstance(body.get('data'), dict):
            raise ProviderError("TikTok returned an invalid data envelope", unknown=mutating)
        return body['data']

    async def creator_info(self):
        data = await self._api('creator_info/query/')
        if (not isinstance(data.get('creator_username'), str) or not data['creator_username']
                or not isinstance(data.get('creator_nickname'), str)
                or not isinstance(data.get('privacy_level_options'), list)
                or not data['privacy_level_options']
                or any(value not in {'PUBLIC_TO_EVERYONE', 'MUTUAL_FOLLOW_FRIENDS', 'FOLLOWER_OF_CREATOR', 'SELF_ONLY'} for value in data['privacy_level_options'])
                or any(not isinstance(data.get(key), bool) for key in ['comment_disabled', 'duet_disabled', 'stitch_disabled'])
                or type(data.get('max_video_post_duration_sec')) is not int
                or data['max_video_post_duration_sec'] <= 0):
            raise ProviderError("TikTok returned incomplete creator permissions; publishing is unavailable")
        return data

    async def initialize(self, post_info, *, video_path=None, photo_urls=None):
        if video_path:
            size = Path(video_path).stat().st_size
            payload = {'post_info': post_info, 'source_info': {
                'source': 'FILE_UPLOAD', 'video_size': size, 'chunk_size': size, 'total_chunk_count': 1}}
            data = await self._api('video/init/', payload, mutating=True)
        else:
            photo_info = dict(post_info)
            is_aigc = photo_info.pop('is_aigc', False)
            photo_info.pop('disable_duet', None)
            photo_info.pop('disable_stitch', None)
            data = await self._api('content/init/', {'media_type': 'PHOTO', 'post_mode': 'DIRECT_POST',
                'post_info': photo_info, 'is_aigc': is_aigc, 'source_info': {
                    'source': 'PULL_FROM_URL', 'photo_cover_index': 0, 'photo_images': photo_urls}}, mutating=True)
        publish_id = data.get('publish_id')
        if not isinstance(publish_id, str) or not 1 <= len(publish_id) <= 64:
            raise ProviderError("TikTok did not return a valid publishing ID", unknown=True)
        return data

    @staticmethod
    def validate_upload_url(value):
        if not isinstance(value, str):
            raise ProviderError("TikTok returned an invalid upload address", unknown=True)
        try:
            parts = urlsplit(value)
            port = parts.port
        except ValueError:
            raise ProviderError("TikTok returned an invalid upload address", unknown=True) from None
        host = (parts.hostname or '').lower()
        if (parts.scheme != 'https' or not host.endswith('.tiktokapis.com') or parts.username or parts.password
                or port not in {None, 443} or parts.fragment or len(value) > 2048
                or any(ord(char) <= 32 for char in value)):
            raise ProviderError("TikTok returned an untrusted upload address", unknown=True)

    async def upload(self, upload_url, path):
        self.validate_upload_url(upload_url)
        size = Path(path).stat().st_size
        async def content():
            with open(path, 'rb') as stream:
                while chunk := stream.read(1024 * 1024):
                    yield chunk
        try:
            # No Authorization header and no redirects to the signed upload destination.
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.put(upload_url, content=content(), headers={
                    'Content-Type': 'video/mp4', 'Content-Length': str(size), 'Content-Range': f'bytes 0-{size - 1}/{size}'})
        except httpx.RequestError:
            raise ProviderError("TikTok upload outcome is uncertain; the existing publishing ID will be checked", unknown=True) from None
        if response.status_code != 201:
            raise ProviderError("TikTok did not confirm the complete upload; the existing publishing ID will be checked", unknown=True)

    async def status(self, publish_id):
        data = await self._api('status/fetch/', {'publish_id': publish_id})
        if data.get('status') not in {'PROCESSING_UPLOAD', 'PROCESSING_DOWNLOAD', 'SEND_TO_USER_INBOX', 'PUBLISH_COMPLETE', 'FAILED'}:
            raise ProviderError("TikTok returned an unknown publication status")
        return data
