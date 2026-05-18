from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, text


VALID_ROLES = ("owner", "editor", "viewer")


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__)
        return 2

    _, email, org_id, role = argv
    role = role.lower()
    if role not in VALID_ROLES:
        print(f"role must be one of {VALID_ROLES!r}, got {role!r}")
        return 2

    # SQLAlchemy SQLEnum(OrgRole) stores the enum NAME (uppercase) in
    # Postgres — see services/postgis_service.py:55.
    role_db = role.upper()

    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("SUPABASE_DB_URL is not set in the environment")
        return 1

    engine = create_engine(db_url, future=True)
    with engine.begin() as conn:
        user_row = conn.execute(
            text("SELECT id FROM app_users WHERE lower(email) = lower(:email)"),
            {"email": email},
        ).first()
        if not user_row:
            print(f"No app_users row for email {email!r}")
            return 1
        user_id = user_row[0]

        org_row = conn.execute(
            text("SELECT id FROM organizations WHERE id = :id"),
            {"id": org_id},
        ).first()
        if not org_row:
            print(f"No organizations row for id {org_id!r}")
            return 1

        conn.execute(
            text(
                """
                INSERT INTO organization_members (user_id, organization_id, role, created_at)
                VALUES (:uid, :oid, :role, now())
                ON CONFLICT (user_id, organization_id)
                DO UPDATE SET role = EXCLUDED.role
                """
            ),
            {"uid": user_id, "oid": org_id, "role": role_db},
        )

    print(f"Granted {role} on {org_id!r} to {email!r} (user_id={user_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
