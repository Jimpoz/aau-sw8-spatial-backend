"""Seed Postgres auth tables from Neo4j and (optionally) bootstrap the first
owner account for an organization."""

from __future__ import annotations
import argparse
import os
import sys
import uuid
from db import get_db
from services.auth_service import hash_password
from services.postgis_service import (
    AppUser,
    Organization,
    OrganizationMember,
    OrgRole,
    PostGISService,
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def seed_organizations(pg: PostGISService) -> list[dict]:
    """Mirror every Neo4j Organization node into the Postgres organizations table."""
    db = get_db()
    rows = db.execute("MATCH (o:Organization) RETURN o ORDER BY o.name")
    seeded: list[dict] = []
    for record in rows:
        node = record["o"]
        org = {
            "id": node.get("id"),
            "name": node.get("name", node.get("id")),
            "entity_type": node.get("entity_type"),
            "description": node.get("description"),
        }
        if not org["id"]:
            print(f"[seed_users] Skipping org with no id: {node!r}")
            continue
        ok = pg.sync_organization(org)
        if ok:
            print(f"[seed_users] Upserted org: {org['id']} ({org['name']})")
            seeded.append(org)
        else:
            print(f"[seed_users] Failed to sync org: {org['id']}")
    return seeded


def create_owner(
    pg: PostGISService,
    email: str,
    password: str,
    organization_id: str,
    full_name: str | None,
) -> None:
    """If the user exists, only their membership is reconciled
    (promoted to OWNER if missing); the password is left alone so re-runs of
    the script don't silently rotate it."""
    if not pg.SessionLocal:
        raise RuntimeError("Postgres is not configured; cannot create owner")

    email = email.strip().lower()
    session = pg.SessionLocal()
    try:
        org = session.query(Organization).filter_by(id=organization_id).first()
        if not org:
            raise SystemExit(
                f"[seed_users] Organization {organization_id} not found in Postgres. "
                f"Run the script without --owner-email first to seed organizations."
            )

        user = session.query(AppUser).filter_by(email=email).first()
        if user is None:
            user = AppUser(
                id=str(uuid.uuid4()),
                email=email,
                password_hash=hash_password(password),
                full_name=full_name,
                is_active=True,
            )
            session.add(user)
            session.flush()
            print(f"[seed_users] Created owner user: {email}")
        else:
            print(f"[seed_users] User already exists, leaving password untouched: {email}")

        membership = session.query(OrganizationMember).filter_by(
            user_id=user.id, organization_id=org.id,
        ).first()
        if membership is None:
            session.add(OrganizationMember(
                user_id=user.id,
                organization_id=org.id,
                role=OrgRole.OWNER,
            ))
            print(f"[seed_users] Granted OWNER on {org.id} to {email}")
        elif membership.role != OrgRole.OWNER:
            membership.role = OrgRole.OWNER
            print(f"[seed_users] Promoted {email} to OWNER on {org.id}")
        else:
            print(f"[seed_users] {email} is already OWNER on {org.id}")

        session.commit()
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed Postgres organizations from Neo4j and bootstrap an owner."
    )
    parser.add_argument("--owner-email", default=None,
                        help="Email of an owner account to ensure exists.")
    parser.add_argument("--owner-password", default=None,
                        help="Initial password (>=8 chars). Required with --owner-email.")
    parser.add_argument("--owner-name", default=None,
                        help="Optional full name for the owner account.")
    parser.add_argument("--organization-id", default=None,
                        help="Org to grant OWNER on. Defaults to the first org seeded.")
    args = parser.parse_args()

    pg = PostGISService()
    if not pg.engine:
        raise SystemExit(
            "[seed_users] Postgres is not configured (SUPABASE_DB_URL unset or sync disabled)."
        )

    seeded = seed_organizations(pg)

    if not args.owner_email:
        return

    if not args.owner_password:
        raise SystemExit("[seed_users] --owner-password is required when --owner-email is given.")

    org_id = args.organization_id
    if not org_id:
        if not seeded:
            raise SystemExit(
                "[seed_users] No organizations were seeded; cannot pick a default org for the owner."
            )
        org_id = seeded[0]["id"]
        print(f"[seed_users] No --organization-id given; defaulting to {org_id}")

    create_owner(
        pg=pg,
        email=args.owner_email,
        password=args.owner_password,
        organization_id=org_id,
        full_name=args.owner_name,
    )


if __name__ == "__main__":
    main()
