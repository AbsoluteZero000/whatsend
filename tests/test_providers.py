"""Contract and application tests using a local provider, never a social API."""
import asyncio

import pytest
from sqlalchemy import select

from app.models.job import Job
from app.models.token import Token
from app.providers import BaseSender, Provider, ProviderContext, get_registry
from app.providers.registry import ProviderRegistry, discover_providers
from app.routers import jobs
from app.services import scheduler
from app.services.crypto import decrypt_token
from test_native_api import exercise
from test_native_jobs import no_schedule
from test_message_series import create_series, plan


HEADERS = {"Cookie": "csrf_token=safe", "X-CSRF-Token": "safe"}


def test_independent_packages_are_discovered_without_registration_edits(tmp_path, monkeypatch):
    root = tmp_path / 'provider_fixtures'
    root.mkdir()
    (root / '__init__.py').write_text('')
    for name in ('alpha', 'beta'):
        folder = root / name
        folder.mkdir()
        (folder / '__init__.py').write_text('')
        (folder / 'provider.py').write_text(
            'from app.providers import Provider\n'
            f'provider = Provider(id="{name}", label="{name}", create_sender=lambda context: None)\n')
    monkeypatch.syspath_prepend(str(tmp_path))
    registry = discover_providers('provider_fixtures')
    assert [item.id for item in registry.all()] == ['alpha', 'beta']
    with pytest.raises(ValueError, match='Duplicate provider'):
        registry.register(registry.get('alpha'))
    with pytest.raises(TypeError, match='BaseSender'):
        registry.sender('alpha', ProviderContext())
    with pytest.raises(ValueError, match='Unsupported provider'):
        registry.get('missing')


@pytest.mark.parametrize('identifier', ['Uppercase', 'x' * 21, '../bad'])
def test_invalid_provider_ids_are_rejected(identifier):
    with pytest.raises(ValueError, match='Provider IDs'):
        ProviderRegistry().register(Provider(identifier, 'Invalid', lambda context: None))


def test_other_providers_cannot_opt_into_whatsapp_delivery_storage():
    with pytest.raises(ValueError, match='reserved'):
        ProviderRegistry().register(Provider('other', 'Other', lambda context: None, delivery='whatsapp_worker'))


@pytest.fixture
def local_provider(monkeypatch):
    calls = []

    class LocalSender(BaseSender):
        async def send(self, chat_id, message, image_path=None, filename=None):
            calls.append((chat_id, message, image_path, filename))
            return {'id': 'local-message'}

    def create(context):
        assert context.api_token == 'local-secret'
        return LocalSender()

    registry = get_registry()
    monkeypatch.setattr(registry, '_providers', dict(registry._providers))
    definition = Provider('local', 'Local test provider', create)
    registry.register(definition)
    return definition, calls


@pytest.mark.parametrize('mode', ['repeat', 'series'])
def test_new_provider_token_api_and_scheduled_delivery(tmp_path, monkeypatch, local_provider, mode):
    definition, calls = local_provider
    monkeypatch.setattr(jobs, 'register_job', no_schedule)

    def worker(request):
        pytest.fail('A different provider must never contact the WhatsApp worker')

    async def scenario(client, sessions, context):
        page = await client.get('/tokens/create')
        assert '<option value="local"' in page.text
        response = await client.post('/tokens/create', headers=HEADERS,
            data={'name': 'Local', 'provider': 'local', 'api_token': 'local-secret'})
        assert response.status_code == 303
        async with sessions() as db:
            token = (await db.execute(select(Token).where(Token.provider == 'local'))).scalar_one()
            token_id = token.id
            assert token.api_token != 'local-secret'
            assert decrypt_token(token.api_token) == 'local-secret'
            # Existing API key remains bound to token 3; select the test provider there too.
            original = await db.get(Token, 3)
            original.provider, original.api_token = 'local', token.api_token
            await db.commit()
        assert 'Local test provider' in (await client.get('/tokens')).text
        assert (await client.get(f'/jobs/groups?token_id={token_id}')).json() == {'groups': []}
        result = await client.post('/api/send', json={'provider': 'local', 'number': 'opaque:user', 'message': 'API'})
        assert result.status_code == 200 and result.json()['success'] is True
        mismatch = await client.post('/api/send', json={'provider': 'whatsapp', 'number': 'opaque:user', 'message': 'No'})
        assert mismatch.status_code == 400
        if mode == 'series':
            response = await create_series(client, plan(['Scheduled']), token_id=str(token_id), group_ids='opaque:user')
        else:
            response = await client.post('/jobs/create', headers=HEADERS, data={
                'token_id': str(token_id), 'group_ids': 'opaque:user', 'message': 'Scheduled', 'trigger_type': 'trigger'})
        assert response.status_code == 303, response.text
        async with sessions() as db:
            job_id = (await db.execute(select(Job.id))).scalar_one()
        await scheduler.send_job(job_id)
        assert [(call[0], call[1]) for call in calls] == [('opaque:user', 'API'), ('opaque:user', 'Scheduled')]
        async with sessions() as db:
            job = await db.get(Job, job_id)
            assert job.status in {'completed', 'trigger'}
        sender = definition.create_sender(ProviderContext('local-secret'))
        with pytest.raises(ValueError, match='does not support attachments'):
            definition.validate_payload(sender, 'opaque:user', '', '/unused.pdf')

    asyncio.run(exercise(tmp_path, monkeypatch, worker, scenario))


def test_recipient_names_are_scoped_to_the_jobs_connection(tmp_path, monkeypatch, local_provider):
    from app.models.job import JobGroup

    class DirectorySender(BaseSender):
        def __init__(self, label):
            self.label = label

        async def send(self, chat_id, message, image_path=None, filename=None):
            pytest.fail('Looking up names must not send messages')

        async def get_groups(self):
            return [{'id': 'same-id', 'name': self.label}]

    registry = get_registry()
    registry._providers['local'] = Provider('local', 'Local',
        lambda context: DirectorySender(context.api_token), supports_groups=True)

    async def scenario(client, sessions, context):
        async with sessions() as db:
            from app.services.crypto import encrypt_token
            for label in ['Account one', 'Account two']:
                token = Token(user_id=7, name=label, provider='local', api_token=encrypt_token(label))
                db.add(token)
                await db.flush()
                job = Job(user_id=7, token_id=token.id, group_id='same-id', message='hello',
                          trigger_type='trigger', trigger_value='', status='trigger')
                job.job_groups = [JobGroup(group_id='same-id')]
                db.add(job)
            await db.commit()
        assert (await client.get('/jobs')).status_code == 200
        async with sessions() as db:
            names = (await db.execute(select(JobGroup.group_name).order_by(JobGroup.id))).scalars().all()
            assert names == ['Account one', 'Account two']

    def worker(request):
        pytest.fail('Name discovery must use only the requested provider connections')

    asyncio.run(exercise(tmp_path, monkeypatch, worker, scenario))
