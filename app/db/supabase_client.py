import os

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()


def get_supabase_client() -> Client:
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
