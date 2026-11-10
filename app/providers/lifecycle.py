"""Publishing packages own their routes, persistence and scheduler lifecycle."""
from importlib import import_module
from app.providers.registry import get_registry


def register_routes(app):
    for provider in get_registry().all():
        if provider.kind == 'publishing':
            app.include_router(import_module(f'app.providers.{provider.id}.routes').router)


def register_jobs(scheduler):
    for provider in get_registry().all():
        if provider.kind == 'publishing':
            import_module(f'app.providers.{provider.id}.jobs').register_jobs(scheduler)


async def delete_connection(db, token):
    if get_registry().get(token.provider).kind == 'publishing':
        await import_module(f'app.providers.{token.provider}.jobs').delete_connection(db, token)
