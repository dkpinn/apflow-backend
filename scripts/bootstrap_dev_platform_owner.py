from __future__ import annotations

import argparse
import os
from urllib.parse import urlparse

from dotenv import load_dotenv
from supabase import create_client


PRODUCTION_PROJECT_REF = "arueantocclxnziipwdf"
DEFAULT_OWNER_EMAIL = "demo@apflow.test"


def project_ref(url: str) -> str:
    hostname = urlparse(url).hostname or ""
    return hostname.split(".", 1)[0]


def find_user_id(client, email: str) -> str | None:
    page = 1
    while True:
        response = client.auth.admin.list_users(page=page, per_page=1000)
        users = list(getattr(response, "users", response) or [])
        for user in users:
            if str(getattr(user, "email", "")).lower() == email.lower():
                return str(user.id)
        if len(users) < 1000:
            return None
        page += 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Grant development-only Platform Owner access."
    )
    parser.add_argument("--email", default=DEFAULT_OWNER_EMAIL)
    parser.add_argument(
        "--env-file",
        default=".env.development.local",
        help="Untracked environment file containing development Supabase credentials.",
    )
    args = parser.parse_args()

    load_dotenv(args.env_file, override=True)
    environment = os.getenv("APFLOW_ENV", "").strip().lower()
    url = os.getenv("SUPABASE_URL", "").strip()
    service_key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_SERVICE_KEY")
        or ""
    ).strip()
    expected_ref = os.getenv("DEV_SUPABASE_PROJECT_REF", "").strip()
    actual_ref = project_ref(url)

    if environment != "development":
        raise SystemExit("Refusing bootstrap: APFLOW_ENV must be development")
    if not url or not service_key or not expected_ref:
        raise SystemExit(
            "Refusing bootstrap: development URL, service key, and "
            "DEV_SUPABASE_PROJECT_REF are required"
        )
    if actual_ref == PRODUCTION_PROJECT_REF:
        raise SystemExit("Refusing bootstrap: production Supabase project detected")
    if actual_ref != expected_ref:
        raise SystemExit(
            f"Refusing bootstrap: URL project ref {actual_ref!r} does not match "
            "DEV_SUPABASE_PROJECT_REF"
        )

    client = create_client(url, service_key)
    user_id = find_user_id(client, args.email)
    if not user_id:
        raise SystemExit(
            f"User {args.email!r} does not exist in the development project"
        )

    client.table("platform_admin_users").upsert(
        {
            "user_id": user_id,
            "role": "owner",
            "status": "active",
        },
        on_conflict="user_id",
    ).execute()
    print(f"Platform Owner enabled for {args.email} in development project {actual_ref}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

