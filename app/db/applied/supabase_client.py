import os
from pathlib import Path

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()
_dev_env = Path(".env.development.local")
if _dev_env.exists():
    load_dotenv(dotenv_path=_dev_env, override=True)

_service_client: Client | None = None


def get_supabase_client() -> Client:
    global _service_client
    if _service_client is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
        if not url or not key:
            raise Exception("Supabase credentials missing")
        _service_client = create_client(url, key)
    return _service_client


def get_fresh_supabase_client() -> Client:
    """Create a new Supabase service-role client with a fresh HTTP connection (not cached).

    Use this after a long-running external call (e.g. Gemini VLM) where the
    persistent HTTP/2 connection held by the singleton may have gone stale.
    """
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise Exception("Supabase credentials missing")
    return create_client(url, key)


def get_user_supabase_client(token: str) -> Client:
    """
    Return a Supabase client scoped to the given user JWT.

    Uses SUPABASE_ANON_KEY as the API key (Kong auth) and overrides the
    PostgREST Authorization header to the user's JWT so RLS policies apply.
    """
    url = os.getenv("SUPABASE_URL")
    anon_key = os.getenv("SUPABASE_ANON_KEY")
    if not url:
        raise Exception("SUPABASE_URL is missing")
    if not anon_key:
        raise Exception("SUPABASE_ANON_KEY is missing — service-role fallback is not permitted to prevent RLS bypass")
    client = create_client(url, anon_key)
    client.postgrest.auth(token)
    return client
