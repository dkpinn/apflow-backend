from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

if __package__:
    from scripts.bootstrap_dev_platform_owner import (
        PRODUCTION_PROJECT_REF,
        find_user_id,
        project_ref,
    )
else:
    from bootstrap_dev_platform_owner import (
        PRODUCTION_PROJECT_REF,
        find_user_id,
        project_ref,
    )


DEMO_ORGANISATION_ID = "11111111-1111-1111-1111-111111111111"
DEFAULT_PASSWORD = "AepTest2026!"
USERS = (
    ("Demo Platform Owner", "demo@apflow.test", "owner"),
    ("Kevin Barr", "kevin.barr@aeptest.co.za", "owner"),
    ("Sandra Nkosi", "sandra.nkosi@aeptest.co.za", "admin"),
    ("James Pillay", "james.pillay@aeptest.co.za", "admin"),
    ("Nomsa Dlamini", "nomsa.dlamini@aeptest.co.za", "accountant"),
    ("Thabo Mokoena", "thabo.mokoena@aeptest.co.za", "accountant"),
    ("Priya Govender", "priya.govender@aeptest.co.za", "reviewer"),
    ("Craig Botha", "craig.botha@aeptest.co.za", "reviewer"),
    ("Amahle Zulu", "amahle.zulu@aeptest.co.za", "viewer"),
    ("Ruan van der Merwe", "ruan.vdmerwe@aeptest.co.za", "viewer"),
    ("Fatima Ismail", "fatima.ismail@aeptest.co.za", "client"),
)


def development_client(env_file: str):
    load_dotenv(env_file, override=True)
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
        raise SystemExit("Refusing seed: APFLOW_ENV must be development")
    if not url or not service_key or not expected_ref:
        raise SystemExit(
            "Refusing seed: development URL, service key, and "
            "DEV_SUPABASE_PROJECT_REF are required"
        )
    if actual_ref == PRODUCTION_PROJECT_REF:
        raise SystemExit("Refusing seed: production Supabase project detected")
    if actual_ref != expected_ref:
        raise SystemExit(
            f"Refusing seed: URL project ref {actual_ref!r} does not match "
            "DEV_SUPABASE_PROJECT_REF"
        )

    return create_client(url, service_key), actual_ref


def ensure_user(client, name: str, email: str, password: str) -> str:
    user_id = find_user_id(client, email)
    attributes = {
        "password": password,
        "email_confirm": True,
        "user_metadata": {"full_name": name},
    }
    if user_id:
        client.auth.admin.update_user_by_id(user_id, attributes)
        return user_id

    response = client.auth.admin.create_user({"email": email, **attributes})
    return str(response.user.id)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed guarded development demo users and organisation."
    )
    parser.add_argument("--env-file", default=".env.development.local")
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    args = parser.parse_args()

    client, actual_ref = development_client(args.env_file)
    now = datetime.now(timezone.utc).isoformat()

    client.table("organisations").upsert(
        {
            "id": DEMO_ORGANISATION_ID,
            "name": "AEP Properties CC Demo",
            "legal_name": "AEP Properties CC Demo",
            "country": "ZA",
            "currency": "ZAR",
            "base_currency": "ZAR",
            "organisation_type": "close_corporation",
            "financial_year_end": "02-28",
            "status": "active",
        },
        on_conflict="id",
    ).execute()

    seeded = []
    for name, email, role in USERS:
        user_id = ensure_user(client, name, email, args.password)
        client.table("organisation_users").upsert(
            {
                "organisation_id": DEMO_ORGANISATION_ID,
                "user_id": user_id,
                "role": role,
                "status": "active",
                "invited_email": email,
                "invited_at": now,
                "accepted_at": now,
                "invoice_approver": role in {"owner", "admin"},
                "permissions": {},
            },
            on_conflict="organisation_id,user_id",
        ).execute()
        seeded.append((email, role))

    print(
        f"Seeded {len(seeded)} development users in project {actual_ref} "
        f"and organisation {DEMO_ORGANISATION_ID}"
    )
    for email, role in seeded:
        print(f"  {role:<10} {email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
