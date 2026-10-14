import json
from uuid import uuid4
from typing import Literal
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select, update
from app.models.job_series import SeriesEntry, SeriesAttachment
from app.services.message_variables import render_message
from app.services.native_sender import NativeSender


class AttachmentRef(BaseModel):
    id: str = Field(min_length=1, max_length=36)
    filename: str = Field(default='', max_length=255)


class EntryDefinition(BaseModel):
    message: str = Field(default='', max_length=4096)
    attachments: list[AttachmentRef] = Field(default_factory=list, max_length=20)


class PageRange(BaseModel):
    start: int = Field(ge=1, strict=True)
    end: int = Field(ge=1, strict=True)


class SourceDefinition(BaseModel):
    kind: Literal['messages', 'pdf', 'playlist'] = 'messages'
    name: str = Field(default='Messages', max_length=120)
    source_url: str = Field(default='', max_length=2048)
    start_page: int = Field(default=1, ge=1)
    end_page: int | None = Field(default=None, ge=1)
    pages_per_send: int = Field(default=5, ge=1, le=500)
    range_mode: Literal['chunks', 'custom'] = 'chunks'
    ranges: list[PageRange] = Field(default_factory=list, max_length=1000)
    items_per_send: int = Field(default=2, ge=1, le=20)
    skipped: int = Field(default=0, ge=0)
    entries: list[EntryDefinition] = Field(min_length=1, max_length=1000)


class SeriesPlan(BaseModel):
    strategy: Literal['sequential', 'parallel'] = 'sequential'
    sources: list[SourceDefinition] = Field(min_length=1, max_length=20)


def flatten_plan(plan):
    if plan.strategy == 'sequential':
        return [entry for source in plan.sources for entry in source.entries]
    entries = []
    for index in range(max(len(source.entries) for source in plan.sources)):
        current = [source.entries[index] for source in plan.sources if index < len(source.entries)]
        entries.append(EntryDefinition(message='\n\n'.join(e.message for e in current if e.message),
                       attachments=[attachment for e in current for attachment in e.attachments]))
    return entries


async def validate_plan(db, raw, user_id, token, recipients, common_message, job=None):
    if token.provider not in {'native', 'whatsapp'}:
        raise ValueError('Finite plans need a WhatsApp QR or Whapi connection')
    if len(raw.encode('utf-8')) > 800000:
        raise ValueError('This plan is too large. Use fewer entries or shorter messages.')
    try:
        plan = SeriesPlan.model_validate_json(raw)
        entries = flatten_plan(plan)
    except (ValidationError, TypeError):
        raise ValueError('Add at least one valid source. Each source needs content, with no more than 20 attachments per send.')
    if not 1 <= len(entries) <= 1000:
        raise ValueError('Plans must contain between 1 and 1000 scheduled sends')
    ids = {attachment.id for entry in entries for attachment in entry.attachments}
    assets = (await db.execute(select(SeriesAttachment).where(
        SeriesAttachment.id.in_(ids), SeriesAttachment.user_id == user_id,
    ))).scalars().all() if ids else []
    by_id = {asset.id: asset for asset in assets}
    if set(by_id) != ids:
        raise ValueError('An attachment is unavailable. Upload it again.')
    for asset in assets:
        if asset.entry_id is not None:
            owner_job = await db.scalar(select(SeriesEntry.job_id).where(SeriesEntry.id == asset.entry_id))
            if not job or owner_job != job.id:
                raise ValueError('This attachment is already part of another plan. Upload it again or clone that plan.')
    for index, entry in enumerate(entries, 1):
        text = '\n\n'.join(part for part in [common_message, entry.message] if part)
        text = render_message(text, index, len(entries), 'UTC')
        if not text and not entry.attachments:
            raise ValueError(f'Send #{index} needs a message or an attachment')
        if len(text.encode('utf-8')) > 4096:
            raise ValueError(f'Send #{index} exceeds 4096 UTF-8 bytes. Shorten the messages or reduce the batch size.')
        for reference in entry.attachments:
            asset = by_id[reference.id]
            from pathlib import Path
            if not Path(asset.path).is_file():
                raise ValueError('An attachment is missing. Upload it again.')
            if token.provider == 'native':
                for recipient in recipients:
                    NativeSender.validate_media(recipient, asset.path, '')
        if token.provider == 'native' and text:
            for recipient in recipients: NativeSender.validate_text(recipient, text)
    return plan, entries, by_id


async def persist_plan(db, job, plan, entries, by_id):
    old_entries = (await db.execute(select(SeriesEntry).where(SeriesEntry.job_id == job.id))).scalars().all()
    old_ids = [entry.id for entry in old_entries]
    old_assets = (await db.execute(select(SeriesAttachment).where(SeriesAttachment.entry_id.in_(old_ids)))).scalars().all() if old_ids else []
    if old_ids:
        await db.execute(update(SeriesAttachment).where(SeriesAttachment.entry_id.in_(old_ids)).values(entry_id=None))
        for entry in old_entries: await db.delete(entry)
        await db.flush()
    kept = set(by_id)
    removed_paths = []
    for asset in old_assets:
        if asset.id not in kept:
            removed_paths.append(asset.path); await db.delete(asset)
    used = set()
    for index, definition in enumerate(entries, 1):
        entry = SeriesEntry(job_id=job.id, position=index, message=definition.message, status='pending')
        db.add(entry); await db.flush()
        for position, reference in enumerate(definition.attachments):
            asset = by_id[reference.id]
            if reference.id in used:
                asset = SeriesAttachment(id=str(uuid4()), user_id=job.user_id, path=asset.path, filename=asset.filename)
                db.add(asset)
            used.add(reference.id)
            asset.entry_id, asset.position = entry.id, position
    job.message_mode, job.series_cursor, job.series_active_run = 'series', 0, None
    job.series_total = len(entries)
    job.series_plan = plan.model_dump_json()
    return removed_paths
