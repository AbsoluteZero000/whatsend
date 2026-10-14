"""Import public playlist links and split PDFs into private series attachments."""
import asyncio
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, urlencode
from uuid import uuid4

from pypdf import PdfReader, PdfWriter
from yt_dlp import YoutubeDL
from yt_dlp.extractor.soundcloud import SoundcloudSetIE
from sqlalchemy import select, func
from app.database import async_session
from app.models.job import Job
from app.models.job_series import SeriesAttachment

MAX_ENTRIES = 1000
MAX_PDF_BYTES = 100 * 1024 * 1024
PDF_SPLIT_LOCK = asyncio.Lock()


def playlist_url(value):
    parts = urlsplit(value.strip())
    if parts.scheme not in {'http', 'https'} or parts.username or parts.password or parts.port:
        raise ValueError('Enter a public YouTube or SoundCloud playlist URL')
    host = (parts.hostname or '').lower()
    if host in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'}:
        identifier = parse_qs(parts.query).get('list', [''])[0]
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identifier):
            raise ValueError('The YouTube URL must include a playlist ID')
        return 'https://www.youtube.com/playlist?' + urlencode({'list': identifier}), 'youtube'
    if host in {'soundcloud.com', 'www.soundcloud.com'} and re.fullmatch(r'/[A-Za-z0-9_-]+/sets/[A-Za-z0-9_-]+/?', parts.path):
        return 'https://soundcloud.com' + parts.path.rstrip('/'), 'soundcloud'
    raise ValueError('Enter a public YouTube or SoundCloud playlist URL')


class QuietLogger:
    def debug(self, message): pass
    def info(self, message): pass
    def warning(self, message): pass
    def error(self, message): pass


class SoundcloudLinksIE(SoundcloudSetIE):
    @classmethod
    def ie_key(cls): return 'SoundcloudSet'

    def _extract_set(self, playlist, token=None):
        # Keep titles/permalinks from the set response. Flat extraction normally
        # discards track titles and can return internal API URLs instead.
        tracks = playlist.get('tracks') or []
        if len(tracks) > MAX_ENTRIES:
            raise ValueError('Playlists can contain up to 1000 items')
        missing = [track for track in tracks if not track.get('permalink_url') or not track.get('title')]
        resolved = {}
        for offset in range(0, len(missing), 50):
            ids = ','.join(str(track['id']) for track in missing[offset:offset + 50] if track.get('id'))
            if ids:
                data = self._call_api(self._API_V2_BASE + 'tracks', str(playlist.get('id')), 'Loading track titles',
                                      query={'ids': ids}, headers=self._HEADERS)
                resolved.update({str(track['id']): track for track in data})
        entries = []
        for track in tracks:
            track = resolved.get(str(track.get('id')), track)
            if track.get('permalink_url'):
                entries.append(self.url_result(track['permalink_url'], 'Soundcloud', str(track.get('id')), track.get('title')))
        return self.playlist_result(entries, str(playlist.get('id')), playlist.get('title'))


def extract_playlist(url, source):
    with YoutubeDL({'extract_flat': 'in_playlist', 'skip_download': True, 'quiet': True,
                    'logger': QuietLogger(), 'socket_timeout': 10, 'retries': 1, 'extractor_retries': 1,
                    'playlistend': MAX_ENTRIES + 1, 'ignoreerrors': False,
                    'allowed_extractors': ['youtube:tab', 'soundcloud:set']}) as client:
        if source == 'soundcloud': client.add_info_extractor(SoundcloudLinksIE())
        info = client.extract_info(url, download=False)
    if not info or info.get('_type') not in {'playlist', 'multi_video'}:
        raise ValueError('This URL did not return a public playlist')
    entries = list(info.get('entries') or [])
    if len(entries) > MAX_ENTRIES:
        raise ValueError('Playlists can contain up to 1000 items')
    items, skipped = [], 0
    for item in entries:
        if not item:
            skipped += 1; continue
        title = str(item.get('title') or 'Untitled item').strip()[:300]
        if item.get('availability') in {'private', 'needs_auth', 'subscriber_only'} or title in {'[Private video]', '[Deleted video]'}:
            skipped += 1; continue
        if source == 'youtube':
            identifier = item.get('id') or ''
            if not re.fullmatch(r'[A-Za-z0-9_-]{11}', identifier):
                skipped += 1; continue
            link = 'https://www.youtube.com/watch?v=' + identifier
        else:
            link = item.get('webpage_url') or item.get('original_url') or item.get('url') or ''
            parsed = urlsplit(link)
            if parsed.scheme not in {'https', 'http'} or parsed.hostname not in {'soundcloud.com', 'www.soundcloud.com'} or parsed.username or parsed.password or parsed.port:
                skipped += 1; continue
            link = 'https://soundcloud.com' + parsed.path
        items.append({'title': title, 'url': link})
    if not items:
        raise ValueError('No public items were available in this playlist')
    return {'title': str(info.get('title') or 'Playlist')[:120], 'items': items, 'skipped': skipped}


async def import_playlist(value, batch_size):
    url, source = playlist_url(value)
    if not 1 <= batch_size <= 20:
        raise ValueError('Choose between 1 and 20 playlist items per send')
    try:
        playlist = await asyncio.wait_for(asyncio.to_thread(extract_playlist, url, source), timeout=60)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('Could not load this playlist. It must be public and accessible. Try again shortly.') from exc
    entries = []
    for offset in range(0, len(playlist['items']), batch_size):
        message = '\n\n'.join(item['title'] + '\n' + item['url'] for item in playlist['items'][offset:offset + batch_size])
        if len(message.encode('utf-8')) > 4096:
            raise ValueError('This batch exceeds WhatsApp’s text limit. Choose fewer items per send.')
        entries.append({'message': message, 'attachments': []})
    return {'entries': entries, 'title': playlist['title'], 'item_count': len(playlist['items']),
            'skipped': playlist['skipped'], 'source_url': url}


def split_pdf(path, directory, filename, start, end, pages_per_send, ranges=None):
    outputs = []
    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError('Remove the PDF password before importing it')
        count = len(reader.pages)
        if count > 10000:
            raise ValueError('PDFs may contain up to 10000 pages')
        if ranges is None:
            end = count if end is None else end
            if not 1 <= start <= end <= count:
                raise ValueError(f'Choose a page range between 1 and {count}')
            if not 1 <= pages_per_send <= 500 or (end - start + pages_per_send) // pages_per_send > MAX_ENTRIES:
                raise ValueError('Choose 1–500 pages per send, with at most 1000 sends')
            ranges = [{'start': first, 'end': min(first + pages_per_send - 1, end)}
                      for first in range(start, end + 1, pages_per_send)]
        if not 1 <= len(ranges) <= MAX_ENTRIES:
            raise ValueError('Add between 1 and 1000 custom page ranges')
        for index, selection in enumerate(ranges, 1):
            if not 1 <= selection['start'] <= selection['end'] <= count:
                raise ValueError(f'Range #{index}: choose start and end pages between 1 and {count}, with start before end')
        for selection in ranges:
            first, last = selection['start'], selection['end']
            writer = PdfWriter()
            for index in range(first - 1, last): writer.add_page(reader.pages[index])
            output = directory / (uuid4().hex + '.pdf')
            outputs.append({'path': str(output.resolve()), 'filename': f'{Path(filename).stem[:60]} - pages {first}-{last}.pdf',
                            'first': first, 'last': last})
            with output.open('wb') as stream: writer.write(stream)
            if output.stat().st_size > 50 * 1024 * 1024:
                raise ValueError('A PDF chunk exceeds 50MB. Choose fewer pages per send.')
        return outputs
    except Exception as exc:
        for item in outputs: Path(item['path']).unlink(missing_ok=True)
        if isinstance(exc, ValueError): raise
        raise ValueError('Could not read this PDF. Upload a valid, unencrypted PDF.') from exc


async def delete_unreferenced_path(db, path):
    job_refs = await db.scalar(select(func.count()).select_from(Job).where(Job.image_path == path))
    file_refs = await db.scalar(select(func.count()).select_from(SeriesAttachment).where(SeriesAttachment.path == path))
    if not job_refs and not file_refs:
        Path(path).unlink(missing_ok=True)


async def cleanup_imports():
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    async with async_session() as db:
        imports = (await db.execute(select(SeriesAttachment).where(
            SeriesAttachment.entry_id.is_(None), SeriesAttachment.created_at < cutoff,
        ))).scalars().all()
        paths = {attachment.path for attachment in imports}
        for attachment in imports: await db.delete(attachment)
        await db.commit()
        for path in paths: await delete_unreferenced_path(db, path)
