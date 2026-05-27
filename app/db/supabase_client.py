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
    Falls back to the service-role key if SUPABASE_ANON_KEY is not set.
    """
    url = os.getenv("SUPABASE_URL")
    key = (
        os.getenv("SUPABASE_ANON_KEY")
        or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_SERVICE_KEY")
    )
    if not url or not key:
        raise Exception("Supabase credentials missing")
    client = create_client(url, key)
    client.postgrest.auth(token)
    return client
