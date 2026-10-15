import asyncio
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject
from sqlalchemy import select

from app.models.job import Job
from app.models.job_series import SeriesEntry, SeriesAttachment
from app.models.delivery_attempt import DeliveryAttempt
from app.models.log import Log
from app.routers import jobs
from app.services import native_jobs, scheduler, series_imports
from app.services.message_variables import render_message
from app.services.series_forms import SeriesPlan, flatten_plan
from test_native_api import exercise, RECIPIENT, SESSION
from test_native_jobs import no_schedule

HEADERS = {'Cookie': 'csrf_token=safe', 'X-CSRF-Token': 'safe'}


def pdf_bytes(pages=8):
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')}))
    for number in range(1, pages + 1):
        page = writer.add_blank_page(width=300, height=400)
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        content = DecodedStreamObject(); content.set_data(f'BT /F1 24 Tf 30 320 Td (Source page {number}) Tj ET\n30 40 240 240 re S'.encode())
        page[NameObject('/Contents')] = writer._add_object(content)
    buffer = io.BytesIO(); writer.write(buffer); return buffer.getvalue()


def plan(messages, attachments=None, strategy='sequential'):
    return {'strategy': strategy, 'sources': [{'kind': 'messages', 'name': 'Lessons', 'entries': [
        {'message': message, 'attachments': (attachments or {}).get(index, [])} for index, message in enumerate(messages)
    ]}]}


async def create_series(client, definition, **fields):
    return await client.post('/jobs/create', data={'token_id': '3', 'group_ids': [RECIPIENT, '+201234567890'],
        'group_names': ['Group', 'Person'], 'message_mode': 'series', 'message': '', 'trigger_type': 'cron',
        'cron_freq': 'daily', 'cron_time': '09:00', 'series_definition': json.dumps(definition), **fields}, headers=HEADERS)


@pytest.mark.parametrize('strategy,expected', [('sequential',['A1','A2','B1','B2','B3']), ('parallel',['A1\n\nB1','A2\n\nB2','B3'])])
def test_sources_run_together_or_sequentially(strategy, expected):
    value = {'strategy': strategy, 'sources': [{'kind': kind, 'name': kind, 'entries': [{'message': text} for text in texts]}
             for kind, texts in [('messages',['A1','A2']), ('playlist',['B1','B2','B3'])]]}
    assert [entry.message for entry in flatten_plan(SeriesPlan.model_validate(value))] == expected


def test_variables_use_occurrence_and_schedule_timezone():
    now = datetime(2026, 10, 14, 1, 15, tzinfo=timezone.utc)
    text = '{send_number}/{total_sends} {counter} {today} {date} {time} {literal}'
    assert render_message(text, 2, 10, 'America/New_York', now) == '2/10 2 2026-10-13 2026-10-13 21:15 {literal}'
    assert render_message('{send_number} {total_sends}', 7, None, 'UTC', now) == '7 ∞'


def mixed_lines(strategy='parallel', inside='sequential'):
    return {'strategy':strategy,'lanes':[
        {'id':'videos','name':'Video lessons','strategy':inside},
        {'id':'books','name':'Reading','strategy':'sequential'}],
        'sources':[{'kind':kind,'lane_id':lane,'name':name,'entries':[{'message':text} for text in texts]}
            for kind,lane,name,texts in [
                ('playlist','videos','Playlist A',['A1','A2']), ('playlist','videos','Playlist B',['B1','B2']),
                ('pdf','books','Book A',['C1']), ('pdf','books','Book B',['D1','D2','D3','D4'])]]}


@pytest.mark.parametrize('strategy,inside,expected',[
    ('parallel','sequential',['A1\n\nC1','A2\n\nD1','B1\n\nD2','B2\n\nD3','D4']),
    ('sequential','sequential',['A1\n\nC1','A2\n\nD1','B1\n\nD2','B2\n\nD3','D4']),
    ('parallel','parallel',['A1\n\nC1','A2\n\nD1','B1\n\nD2','B2\n\nD3','D4'])])
def test_lines_combine_parallel_and_sequential_content(strategy,inside,expected):
    parsed = SeriesPlan.model_validate(mixed_lines(strategy,inside))
    assert parsed.strategy == 'parallel'
    assert all(lane.strategy == 'sequential' for lane in parsed.lanes)
    assert [entry.message for entry in flatten_plan(parsed)] == expected


def test_single_line_runs_all_sources_in_sequence():
    definition=mixed_lines()
    definition['lanes']=[{'id':'only','name':'One sequence'}]
    for source in definition['sources']: source['lane_id']='only'
    parsed=SeriesPlan.model_validate(definition)
    assert [entry.message for entry in flatten_plan(parsed)] == ['A1','A2','B1','B2','C1','D1','D2','D3','D4']


@pytest.mark.parametrize('mutation',['duplicate','empty','missing','unassigned','orphan'])
def test_plan_rejects_invalid_line_membership(mutation):
    from pydantic import ValidationError
    definition=mixed_lines()
    if mutation=='duplicate': definition['lanes'].append(definition['lanes'][0])
    if mutation=='empty': definition['lanes'].append({'id':'empty','name':'Empty'})
    if mutation=='missing': definition['sources'][0]['lane_id']='missing'
    if mutation=='unassigned': definition['sources'][0].pop('lane_id')
    if mutation=='orphan': definition['lanes']=[]
    with pytest.raises(ValidationError): SeriesPlan.model_validate(definition)


def test_mixed_lines_save_edit_clone_and_send_until_longest_line_finishes(tmp_path,monkeypatch):
    monkeypatch.setattr(jobs,'register_job',no_schedule)
    calls=[]
    def worker(request):
        if request.method=='POST':
            calls.append(json.loads(request.content)['text'])
            return httpx.Response(202,json={'data':{'id':str(uuid4()),'status':'pending'}})
        return httpx.Response(200,json={'data':{'id':request.url.path.rsplit('/',1)[-1],'status':'sent'}})
    async def scenario(client,sessions,context):
        definition=mixed_lines()
        result=await create_series(client,definition,group_ids=RECIPIENT,message='Lesson {counter}/{total_sends}')
        assert result.status_code==303
        async with sessions() as db:
            job_id=(await db.execute(select(Job.id))).scalar_one()
            job=await db.get(Job,job_id)
            assert job.series_total==5
            assert json.loads(job.series_plan)['lanes']==definition['lanes']
        definition['lanes'][0]['name']='Videos renamed'
        fields={'token_id':'3','group_ids':RECIPIENT,'message_mode':'series','message':'Lesson {counter}/{total_sends}',
                'trigger_type':'cron','cron_time':'09:00','series_definition':json.dumps(definition)}
        assert (await client.post(f'/jobs/{job_id}/edit',data=fields,headers=HEADERS)).status_code==303
        assert (await client.post(f'/jobs/{job_id}/clone',headers=HEADERS)).status_code==303
        async with sessions() as db:
            clone=(await db.execute(select(Job).where(Job.id!=job_id))).scalar_one()
            assert json.loads(clone.series_plan)['lanes']==definition['lanes']
        for _ in range(5):
            await scheduler.send_job(job_id);await native_jobs.poll_deliveries()
        await scheduler.send_job(job_id)
        assert calls==[f'Lesson {i}/5\n\n{content}' for i,content in enumerate(['A1\n\nC1','A2\n\nD1','B1\n\nD2','B2\n\nD3','D4'],1)]
        async with sessions() as db:
            job=await db.get(Job,job_id);assert job.status=='completed' and job.series_cursor==5
    asyncio.run(exercise(tmp_path,monkeypatch,worker,scenario))


@pytest.mark.parametrize('frequency', ['daily', 'weekdays', 'monthly', 'raw'])
def test_native_series_claims_each_occurrence_once_and_finishes(tmp_path, monkeypatch, frequency):
    monkeypatch.setattr(jobs, 'register_job', no_schedule)
    monkeypatch.setattr(jobs, 'UPLOAD_DIR', tmp_path)
    submitted = []
    states = {}
    def worker(request):
        if request.method == 'POST':
            identifier = str(uuid4()); states[identifier] = 'sent'; submitted.append(request)
            return httpx.Response(202, json={'data': {'id': identifier, 'status': 'pending'}})
        identifier = request.url.path.rsplit('/', 1)[-1]
        return httpx.Response(200, json={'data': {'id': identifier, 'status': states[identifier]}})
    async def scenario(client, sessions, context):
        files = []
        for name in ['One.pdf','Two.pdf']:
            result = await client.post('/jobs/series-files', files={'file': (name, pdf_bytes(1), 'application/pdf')}, headers=HEADERS)
            assert result.status_code == 201; files.append(result.json())
        definition = plan(['Hello', ''], {1: files})
        result = await create_series(client, definition, message='Send {send_number} of {total_sends}', cron_freq=frequency, cron_raw='0 9 * * 1,3')
        assert result.status_code == 303
        async with sessions() as db:
            job_id = (await db.execute(select(Job.id))).scalar_one()
            job = await db.get(Job, job_id)
            assert job.series_total == 2 and job.series_cursor == 0
            await scheduler.register_job(job)
            await db.commit()
        await asyncio.gather(scheduler.send_job(job_id), scheduler.send_job(job_id), scheduler.send_job(job_id))
        assert len(submitted) == 2
        assert json.loads(submitted[0].content)['text'] == 'Send 1 of 2\n\nHello'
        await native_jobs.poll_deliveries()
        async with sessions() as db:
            assert (await db.get(Job, job_id)).status == 'active'
        await scheduler.send_job(job_id)
        assert len(submitted) == 8  # Two recipients, each receives text + two PDFs.
        assert any(b'filename="One.pdf"' in request.content for request in submitted)
        assert any(b'filename="Two.pdf"' in request.content for request in submitted)
        progress = (await client.get(f'/jobs/{job_id}/progress')).json()
        assert progress['total'] == 2 and progress['counts']['pending'] == 2
        await native_jobs.poll_deliveries()
        await scheduler.send_job(job_id)
        async with sessions() as db:
            job = await db.get(Job, job_id)
            assert job.status == 'completed' and job.series_cursor == 2 and not job.series_active_run
            assert [entry.status for entry in (await db.execute(select(SeriesEntry).order_by(SeriesEntry.position))).scalars()] == ['sent','sent']
        assert len(submitted) == 8
        assert scheduler.scheduler.get_job(str(job_id)) is None
        assert (await client.get(f'/jobs/{job_id}/edit')).status_code == 303
    asyncio.run(exercise(tmp_path, monkeypatch, worker, scenario))


def test_pdf_import_preserves_selected_pages_and_private_access(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, 'UPLOAD_DIR', tmp_path)
    async def scenario(client, sessions, context):
        result = await client.post('/jobs/series-pdf', data={'start_page':'2','end_page':'7','pages_per_send':'2'},
                                   files={'file': ('Book.pdf',pdf_bytes(),'application/pdf')}, headers=HEADERS)
        assert result.status_code == 201
        entries = result.json()['entries']; assert len(entries) == 3
        for index, entry in enumerate(entries):
            asset = entry['attachments'][0]
            response = await client.get('/jobs/series-files/' + asset['id'])
            reader = PdfReader(io.BytesIO(response.content))
            assert len(reader.pages) == 2
            assert [page.extract_text() for page in reader.pages] == [f'Source page {index*2+2}', f'Source page {index*2+3}']
            assert tuple(reader.pages[0].mediabox) == (0, 0, 300, 400)
        assert len(list(tmp_path.glob('*.pdf'))) == 3  # Source removed after splitting.
        with monkeypatch.context() as patch:
            patch.setattr(jobs, 'require_user', lambda request: {'sub':'8'})
            assert (await client.get('/jobs/series-files/' + entries[0]['attachments'][0]['id'])).status_code == 404
        invalid = await client.post('/jobs/series-pdf', data={'start_page':'4','end_page':'2'},
                                    files={'file':('Book.pdf',pdf_bytes(),'application/pdf')}, headers=HEADERS)
        assert invalid.status_code == 400 and len(list(tmp_path.glob('*.pdf'))) == 3
        result = await create_series(client, {'strategy':'parallel','sources':[{'kind':'pdf','name':'Book','entries':entries}]})
        assert result.status_code == 303
    asyncio.run(exercise(tmp_path, monkeypatch, lambda request: httpx.Response(500), scenario))


def test_custom_pdf_ranges_preserve_order_overlap_and_parallel_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, 'UPLOAD_DIR', tmp_path)
    monkeypatch.setattr(jobs, 'register_job', no_schedule)
    ranges = [{'start':20,'end':30}, {'start':30,'end':50}, {'start':2,'end':3}]
    async def scenario(client, sessions, context):
        result = await client.post('/jobs/series-pdf', data={'range_mode':'custom','custom_ranges':json.dumps(ranges)},
                                   files={'file':('Book.pdf',pdf_bytes(300),'application/pdf')}, headers=HEADERS)
        assert result.status_code == 201, result.text
        imported = result.json()
        assert imported['ranges'] == ranges and imported['range_mode'] == 'custom'
        entries = imported['entries']
        for entry, selection in zip(entries, ranges, strict=True):
            response = await client.get('/jobs/series-files/' + entry['attachments'][0]['id'])
            reader = PdfReader(io.BytesIO(response.content))
            assert [page.extract_text() for page in reader.pages] == [f'Source page {i}' for i in range(selection['start'],selection['end']+1)]
            assert tuple(reader.pages[0].mediabox) == (0,0,300,400)
        definition = {'strategy':'parallel','sources':[
            {'kind':'pdf','name':'Book','range_mode':'custom','ranges':ranges,'entries':entries},
            {'kind':'playlist','name':'Videos','entries':[{'message':f'Video {i}','attachments':[]} for i in range(5)]}]}
        created = await create_series(client, definition)
        assert created.status_code == 303
        async with sessions() as db:
            job_id = (await db.execute(select(Job.id))).scalar_one()
            job = await db.get(Job, job_id)
            assert job.series_total == 5
            assert json.loads(job.series_plan)['sources'][0]['ranges'] == ranges
            entries_saved = (await db.execute(select(SeriesEntry).where(SeriesEntry.job_id==job_id).order_by(SeriesEntry.position))).scalars().all()
            assert entries_saved[0].message == 'Pages 20-30\n\nVideo 0'
            assert entries_saved[3].message == 'Video 3'
        clone = await client.post(f'/jobs/{job_id}/clone', headers=HEADERS)
        assert clone.status_code == 303
        async with sessions() as db:
            cloned = (await db.execute(select(Job).where(Job.id != job_id))).scalar_one()
            assert json.loads(cloned.series_plan)['sources'][0]['ranges'] == ranges
    asyncio.run(exercise(tmp_path, monkeypatch, lambda request: httpx.Response(500), scenario))


@pytest.mark.parametrize('ranges', ['bad', '{}', '[]', '[true]', '[{"start":true,"end":2}]',
    '[{"start":1.5,"end":2}]', '[{"start":0,"end":2}]', '[{"start":2,"end":1}]',
    '[{"start":1,"end":9}]', '[{"start":1,"end":2},{"start":7,"end":9}]',
    json.dumps([{'start':1,'end':1}]*1001)])
def test_custom_pdf_ranges_reject_invalid_input_without_leaving_files(tmp_path, monkeypatch, ranges):
    monkeypatch.setattr(jobs, 'UPLOAD_DIR', tmp_path)
    async def scenario(client, sessions, context):
        result = await client.post('/jobs/series-pdf', data={'range_mode':'custom','custom_ranges':ranges},
                                   files={'file':('Book.pdf',pdf_bytes(),'application/pdf')}, headers=HEADERS)
        assert result.status_code == 400, result.text
        assert not list(tmp_path.glob('*.pdf'))
        async with sessions() as db:
            assert not (await db.execute(select(SeriesAttachment))).scalars().all()
    asyncio.run(exercise(tmp_path, monkeypatch, lambda request: httpx.Response(500), scenario))


def test_edit_clone_and_delete_keep_shared_series_files_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, 'UPLOAD_DIR', tmp_path); monkeypatch.setattr(jobs, 'register_job', no_schedule)
    async def scenario(client, sessions, context):
        asset = (await client.post('/jobs/series-files', files={'file':('Lesson.pdf',pdf_bytes(1),'application/pdf')}, headers=HEADERS)).json()
        definition = plan(['First','Second'], {0:[asset]})
        assert (await create_series(client, definition)).status_code == 303
        async with sessions() as db:
            job_id = (await db.execute(select(Job.id))).scalar_one()
        definition['sources'][0]['entries'][1]['message'] = 'Updated second'
        fields = {'token_id':'3','group_ids':RECIPIENT,'message_mode':'series','message':'','trigger_type':'cron','cron_time':'09:00','series_definition':json.dumps(definition)}
        edited = await client.post(f'/jobs/{job_id}/edit', data=fields, headers=HEADERS)
        assert edited.status_code == 303, edited.text
        assert (await client.get('/jobs/series-files/'+asset['id'])).status_code == 200
        assert (await client.post(f'/jobs/{job_id}/clone',headers=HEADERS)).status_code == 303
        async with sessions() as db:
            clone = (await db.execute(select(Job).where(Job.id != job_id))).scalar_one()
            clone_id = clone.id
            assert clone.message_mode == 'series' and clone.series_total == 2 and clone.series_cursor == 0
            cloned_asset = (await db.execute(select(SeriesAttachment).join(SeriesEntry).where(SeriesEntry.job_id == clone_id))).scalar_one()
            path = Path(cloned_asset.path); assert path.is_file()
            assert cloned_asset.id != asset['id']
            assert json.loads(clone.series_plan)['sources'][0]['entries'][0]['attachments'][0]['id'] == cloned_asset.id
        await client.post(f'/jobs/{job_id}/delete',headers=HEADERS); assert path.is_file()
        await client.post(f'/jobs/{clone_id}/delete',headers=HEADERS); assert not path.exists()
    asyncio.run(exercise(tmp_path,monkeypatch,lambda request:httpx.Response(500),scenario))


def test_invalid_plans_do_not_create_schedules(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs,'register_job',no_schedule)
    async def scenario(client,sessions,context):
        for definition in [plan(['']), plan(['Hello'],{0:[{'id':str(uuid4()),'filename':'Private.pdf'}]}), {'strategy':'bad','sources':[]}]:
            response = await create_series(client,definition)
            assert response.status_code == 400
        assert (await create_series(client,plan(['Hello']),trigger_type='now')).status_code == 400
        async with sessions() as db: assert not (await db.execute(select(Job))).scalars().all()
    asyncio.run(exercise(tmp_path,monkeypatch,lambda r:httpx.Response(500),scenario))


def test_playlist_url_validation_and_batch_order(monkeypatch):
    for invalid in ['http://127.0.0.1/playlist?list=x','https://youtube.com.evil.com/playlist?list=x','https://user:pass@youtube.com/playlist?list=x','https://soundcloud.com/user/track']:
        with pytest.raises(ValueError): series_imports.playlist_url(invalid)
    captured=[]
    def extract(url,source):
        captured.append((url,source));return {'title':'Lessons','skipped':1,'items':[{'title':f'Item {i}','url':f'https://example.com/{i}'} for i in range(5)]}
    monkeypatch.setattr(series_imports,'extract_playlist',extract)
    for url,source in [('https://youtube.com/watch?v=123&list=PL123','youtube'),('https://soundcloud.com/user/sets/lessons','soundcloud')]:
        result = asyncio.run(series_imports.import_playlist(url,2))
        assert len(result['entries']) == 3 and result['skipped'] == 1 and result['item_count'] == 5
        assert result['entries'][0]['message'].endswith('https://example.com/1')
        assert result['entries'][2]['message'] == 'Item 4\nhttps://example.com/4'
        assert captured[-1][1] == source


def test_interrupted_series_recovers_without_replaying_and_ends_with_issues(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs,'register_job',no_schedule)
    submissions=[]; states={}
    def worker(request):
        if request.method=='POST':
            identifier=str(uuid4());states[identifier]='sent';submissions.append(json.loads(request.content)['text'])
            return httpx.Response(202,json={'data':{'id':identifier,'status':'pending'}})
        identifier=request.url.path.rsplit('/',1)[-1]
        return httpx.Response(200,json={'data':{'id':identifier,'status':states[identifier]}})
    async def scenario(client,sessions,context):
        assert (await create_series(client,plan(['First','Second']),group_ids=RECIPIENT)).status_code==303
        run_id=str(uuid4())
        async with sessions() as db:
            job=(await db.execute(select(Job))).scalar_one(); job_id=job.id
            entry=(await db.execute(select(SeriesEntry).where(SeriesEntry.position==1))).scalar_one()
            job.series_cursor=1;job.series_active_run=run_id;job.status='queued';entry.run_id=run_id;entry.status='submitting'
            db.add(DeliveryAttempt(job_id=job.id,run_id=run_id,series_entry_id=entry.id,group_id=RECIPIENT,status='submitting',
                                   relay_token_id=3,relay_session_id=SESSION,relay_base_url='https://native.test',created_at=datetime.now(timezone.utc)-timedelta(minutes=5)))
            await db.commit()
        await native_jobs.poll_deliveries()
        async with sessions() as db:
            job=await db.get(Job,job_id);assert job.status=='active' and job.series_cursor==1 and not job.series_active_run
        await scheduler.send_job(job_id);await native_jobs.poll_deliveries();await scheduler.send_job(job_id)
        assert submissions==['Second']
        async with sessions() as db:
            job=await db.get(Job,job_id);assert job.status=='unknown' and job.series_cursor==2
    asyncio.run(exercise(tmp_path,monkeypatch,worker,scenario))


def test_pause_resume_and_skip_preserve_series_position(tmp_path,monkeypatch):
    monkeypatch.setattr(jobs,'register_job',no_schedule)
    calls=[]; ids=[]
    def worker(request):
        if request.method=='POST':
            identifier=str(uuid4());ids.append(identifier);calls.append(json.loads(request.content)['text'])
            return httpx.Response(202,json={'data':{'id':identifier,'status':'pending'}})
        return httpx.Response(200,json={'data':{'id':request.url.path.rsplit('/',1)[-1],'status':'sent'}})
    async def scenario(client,sessions,context):
        await create_series(client,plan(['One','Two']),group_ids=RECIPIENT)
        async with sessions() as db:
            job=(await db.execute(select(Job))).scalar_one();job_id=job.id;job.status='active';job.skip_count=1;await db.commit()
        await scheduler.send_job(job_id);assert calls==[]
        async with sessions() as db: assert (await db.get(Job,job_id)).series_cursor==0
        await scheduler.send_job(job_id);assert calls==['One']
        assert (await client.post(f'/jobs/{job_id}/pause',headers=HEADERS)).status_code==303
        await native_jobs.poll_deliveries();await scheduler.send_job(job_id);assert calls==['One']
        async with sessions() as db:
            job=await db.get(Job,job_id);assert job.status=='paused' and job.series_cursor==1
        await client.post(f'/jobs/{job_id}/resume',headers=HEADERS)
        await scheduler.send_job(job_id)
        await client.post(f'/jobs/{job_id}/pause',headers=HEADERS)
        await native_jobs.poll_deliveries();assert calls==['One','Two']
        async with sessions() as db: assert (await db.get(Job,job_id)).status=='completed'
    asyncio.run(exercise(tmp_path,monkeypatch,worker,scenario))


def test_whapi_series_uses_same_finite_plan_without_retries(tmp_path,monkeypatch):
    monkeypatch.setattr(jobs,'register_job',no_schedule)
    calls=[]
    def worker(request):
        calls.append(json.loads(request.content)['body'])
        return httpx.Response(403,json={'error':'Not allowed'}) if len(calls)==1 else httpx.Response(200,json={'sent':True})
    async def scenario(client,sessions,context):
        from app.models.token import Token
        async with sessions() as db:
            token=await db.get(Token,3);token.provider='whatsapp';await db.commit()
        await create_series(client,plan(['First','Second']),group_ids=RECIPIENT)
        async with sessions() as db: job_id=(await db.execute(select(Job.id))).scalar_one()
        await scheduler.send_job(job_id);await scheduler.send_job(job_id);await scheduler.send_job(job_id)
        assert calls==['First','Second']
        async with sessions() as db:
            job=await db.get(Job,job_id);assert job.status=='failed' and job.series_cursor==2 and job.retry_at is None
    asyncio.run(exercise(tmp_path,monkeypatch,worker,scenario))


def test_repeating_messages_keep_running_and_expand_counter(tmp_path,monkeypatch):
    monkeypatch.setattr(jobs,'register_job',no_schedule)
    calls=[]
    def worker(request):
        if request.method=='POST':
            calls.append(json.loads(request.content)['text']);return httpx.Response(202,json={'data':{'id':str(uuid4()),'status':'pending'}})
        return httpx.Response(200,json={'data':{'id':request.url.path.rsplit('/',1)[-1],'status':'sent'}})
    async def scenario(client,sessions,context):
        await client.post('/jobs/create',data={'token_id':'3','group_ids':RECIPIENT,'message':'Reminder #{counter}','trigger_type':'cron','cron_time':'09:00'},headers=HEADERS)
        async with sessions() as db: job_id=(await db.execute(select(Job.id))).scalar_one()
        for _ in range(3): await scheduler.send_job(job_id);await native_jobs.poll_deliveries()
        assert calls==['Reminder #1','Reminder #2','Reminder #3']
        async with sessions() as db:
            job=await db.get(Job,job_id);assert job.message_mode=='repeat' and job.status=='active' and job.send_count==3
    asyncio.run(exercise(tmp_path,monkeypatch,worker,scenario))


def test_scheduler_refreshes_edited_plan_after_claiming_occurrence(tmp_path,monkeypatch):
    monkeypatch.setattr(jobs,'register_job',no_schedule)
    calls=[]
    def worker(request):
        calls.append(json.loads(request.content))
        return httpx.Response(202,json={'data':{'id':str(uuid4()),'status':'pending'}})
    async def scenario(client,sessions,context):
        from app.services.series import submit_series
        from app.models.token import Token
        from types import SimpleNamespace
        await create_series(client,plan(['Old content']),group_ids=RECIPIENT)
        async with sessions() as stale:
            job=(await stale.execute(select(Job))).scalar_one();token=await stale.get(Token,3)
            edited=await client.post(f'/jobs/{job.id}/edit',data={'token_id':'3','group_ids':'+201234567891','message_mode':'series','message':'New prefix',
                                     'trigger_type':'cron','cron_time':'09:00','series_definition':json.dumps(plan(['New content']))},headers=HEADERS)
            assert edited.status_code==303
            await submit_series(stale,job,token,[SimpleNamespace(group_id=RECIPIENT,group_name='Old group')])
        assert calls==[{'recipient':'+201234567891','text':'New prefix\n\nNew content'}]
    asyncio.run(exercise(tmp_path,monkeypatch,worker,scenario))
