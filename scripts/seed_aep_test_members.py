"""
seed_aep_test_members.py
-------------------------
Creates 10 test members in the AEP Properties CC organisation.
Run once from the project root:
    python scripts/seed_aep_test_members.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from supabase import create_client  # noqa: E402

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
TEMP_PASSWORD = "AepTest2026!"
ORG_NAME_SEARCH = "AEP Properties"

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

# ── 1. Find the org ───────────────────────────────────────────────────────────
orgs = (
    supabase.table("organisations")
    .select("id, name")
    .ilike("name", f"%{ORG_NAME_SEARCH}%")
    .execute()
)
if not orgs.data:
    sys.exit(f"ERROR: No organisation found matching '{ORG_NAME_SEARCH}'")

org = orgs.data[0]
org_id = org["id"]
print(f"Organisation: {org['name']} ({org_id})\n")

# ── 2. Define test members ────────────────────────────────────────────────────
members = [
    {"name": "Kevin Barr",         "email": "kevin.barr@aeptest.co.za",     "role": "owner"},
    {"name": "Sandra Nkosi",       "email": "sandra.nkosi@aeptest.co.za",    "role": "admin"},
    {"name": "James Pillay",       "email": "james.pillay@aeptest.co.za",    "role": "admin"},
    {"name": "Nomsa Dlamini",      "email": "nomsa.dlamini@aeptest.co.za",   "role": "accountant"},
    {"name": "Thabo Mokoena",      "email": "thabo.mokoena@aeptest.co.za",   "role": "accountant"},
    {"name": "Priya Govender",     "email": "priya.govender@aeptest.co.za",  "role": "reviewer"},
    {"name": "Craig Botha",        "email": "craig.botha@aeptest.co.za",     "role": "reviewer"},
    {"name": "Amahle Zulu",        "email": "amahle.zulu@aeptest.co.za",     "role": "viewer"},
    {"name": "Ruan van der Merwe", "email": "ruan.vdmerwe@aeptest.co.za",    "role": "viewer"},
    {"name": "Fatima Ismail",      "email": "fatima.ismail@aeptest.co.za",   "role": "client"},
]

now_iso = datetime.now(timezone.utc).isoformat()

# ── 3. Create each user ───────────────────────────────────────────────────────
created = []
for m in members:
    print(f"  Creating {m['name']:<22} ({m['role']:<12}) ...", end=" ", flush=True)
    try:
        # Create Supabase auth user (email already confirmed, immediately active)
        auth_result = supabase.auth.admin.create_user({
            "email": m["email"],
            "password": TEMP_PASSWORD,
            "email_confirm": True,
            "user_metadata": {"full_name": m["name"]},
        })
        user_id = auth_result.user.id

        # Link to the organisation with the assigned role
        supabase.table("organisation_users").insert({
            "organisation_id": org_id,
            "user_id": user_id,
            "role": m["role"],
            "status": "active",
            "invited_email": m["email"],
            "invited_at": now_iso,
            "accepted_at": now_iso,
        }).execute()

        print("OK")
        created.append({**m, "user_id": user_id})
    except Exception as exc:
        err = str(exc)
        if "already registered" in err or "already been registered" in err or "User already exists" in err:
            print("ALREADY EXISTS (skipped)")
        else:
            print(f"FAILED - {err}")

# ── 4. Summary ────────────────────────────────────────────────────────────────
print(f"\n{'-' * 70}")
print(f"  {len(created)}/10 members created in '{org['name']}'")
print(f"  Password for all: {TEMP_PASSWORD}")
print(f"{'-' * 70}")
for c in created:
    print(f"  [{c['role']:<12}]  {c['name']:<22}  {c['email']}")
print()
