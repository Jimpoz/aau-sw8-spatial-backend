# Spatial Backend

Indoor spatial mapping and navigation backend for building complexes.
Stores topology as a **Neo4j** graph (the source of truth), mirrors
geometry to **Supabase PostGIS** for spatial queries, computes weighted
shortest paths via Neo4j GDS Dijkstra, and serves a canvas-based map
editor frontend. Multi-tenant via `Organization` nodes.

This compose stack runs four services behind a single public gateway:
`middleware` (gateway, `:8080`), `backend` (graph + geometry CRUD,
`:8000`), `assistant` (LLM + RAG, `:8001`), `image_pipeline` (YOLO room
summaries, `:8002`), plus a static `frontend` (mapmaker, `:3000`).
Real-time vision lives in a separate repo, [`aau-sw8-ml-vision`](../aau-sw8-ml-vision/README.md).

## Quick Start

```bash
docker compose up -d --build
```

This starts the services in this repo and expects a reachable Neo4j instance for the app containers.

| Service          | Port | Description                          |
|------------------|------|--------------------------------------|
| `middleware`     | 8080 | Public gateway and middleware docs   |
| `backend`        | 8000 | Spatial/navigation API + auth in dev compose |
| `assistant`      | 8001 | Chat and embedding service in dev compose |
| `image_pipeline` | 8002 | Room summary/image processing API in dev compose |
| `email`          | 8003 | Internal-only outbound mail (SMTP or dry-run) |
| `frontend`       | 3000 | Nginx serving the map editor         |

Default Neo4j credentials: `neo4j` / `password`.

## Gateway Docs And Debugging

- Middleware docs: `http://localhost:8080/docs`
- Middleware OpenAPI JSON: `http://localhost:8080/openapi.json`
- Middleware health: `http://localhost:8080/health`
- Optional middleware upstream debug view: `http://localhost:8080/debug/upstreams`

Notes:

- The middleware docs now show middleware-owned routes only.
- `/debug/upstreams` is disabled by default and only exists when `MIDDLEWARE_DEBUG_UPSTREAMS=true`.
- The debug endpoint shows each upstream base URL, which public path prefixes it owns, and whether the middleware can reach that upstream's `/health` and `/openapi.json`.

Enable the debug endpoint temporarily with:

```bash
MIDDLEWARE_DEBUG_UPSTREAMS=true docker compose up -d --build middleware
```

## Dev Compose

`docker-compose.dev.yml` is an optional development override layered on top of `docker-compose.yml`.

Use it when you want direct access to the internal FastAPI services during local development:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

What the `.dev` override changes:

- publishes `backend` on `:8000`
- publishes `assistant` on `:8001`
- publishes `image_pipeline` on `:8002`
- bind-mounts `./middleware` into the container for reload-friendly middleware edits

Without the `.dev` override, those internal services stay behind the gateway and you normally interact through `middleware` on `:8080`.

With the `.dev` override enabled, the service docs are also available directly:

- Backend docs: `http://localhost:8000/docs`
- Assistant docs: `http://localhost:8001/docs`
- Image pipeline docs: `http://localhost:8002/docs`

## Architecture

```
┌──────────┐     ┌────────────┐
│ frontend │────>│ middleware │  X-Api-Key on every public route; optional
│ :3000    │     │ :8080      │  Authorization: Bearer <jwt> for per-user auth
│ (nginx)  │     │ (gateway)  │  (also bridges WS to ml_vision)
└──────────┘     └─────┬──────┘
                       ├──> backend         :8000   graph + geometry CRUD, navigation, import/export, /auth/*
                       ├──> assistant       :8001   chat (LLM + RAG) + internal /embed
                       ├──> image_pipeline  :8002   YOLO room summaries, CLIP image similarity
                       ├──> email           :8003   internal-only outbound mail (SMTP / dry-run)
                       └──> ml_vision       :8000   real-time detection + WS (separate repo)

backend        ───> neo4j :7687     (source of truth)
backend        ───> postgis         (geometry mirror + auth tables, written on every CUD)
backend        ───> email :8003     (password reset / future flows; internal token)
assistant      ───> neo4j :7687
image_pipeline ───> neo4j :7687

backend ───> assistant :8001        (internal /embed during map import)
```

- `frontend` proxies `/api/*` and `/health` to `middleware`.
- `middleware` routes `/api/v1/assistant/*` → `assistant`, `/api/v1/room-summary*` → `image_pipeline`, `/api/v1/ml-vision/*` (incl. `WS /ws/...`) → `ml_vision`, `/api/v1/mobile/*` is middleware-owned, and everything else `/api/v1/*` → `backend`.
- Writes always hit Neo4j first, then sync to PostGIS in the same HTTP request. Reads that need geometry prefer PostGIS and fall back to Neo4j.
- `backend` also calls `assistant` directly for internal embeddings during map import.

**Graph model:**

```
Organization ─[OWNS]─> Campus ─[HAS_BUILDING]─> Building ─[HAS_FLOOR]─> Floor ─[HAS_SPACE]─> Space
                                                                                Space ─[HAS_SUBSPACE]─> Space
                                                                                Space ─[CONNECTS_TO]─> Space
                                                                                Space ─[HAS_LANDMARK]─> Landmark
```

Multi-tenant: every `Campus` is owned by exactly one `Organization`. Deletes cascade *down the tree* — deleting an `Organization` removes its campuses, buildings, floors, spaces, connections, and landmarks; deleting a `Campus` or `Building` does the same for its subtree. Cascades happen explicitly in repository code (PostGIS FKs do not have `ON DELETE CASCADE`).

## Authentication & Multi-Tenancy

Per-user, per-org auth is **opt-in**: when `AUTH_JWT_SECRET` is unset, every service behaves exactly as it did before users existed and the single shared `X-Api-Key` is the only credential. Setting the secret turns the auth router on, the gateway starts verifying bearer tokens, and `require_role` / `require_org_match` start failing closed on mutating routes. Each step below is reversible — you can roll any of them back without touching the others.

### Environment variables

```
# --- Auth (mounts /api/v1/auth/* and turns enforcement on) ---
AUTH_JWT_SECRET=                       # >=32 chars; python -c "import secrets; print(secrets.token_urlsafe(32))"
AUTH_JWT_ISSUER=ariadne-backend
AUTH_JWT_TTL_SECONDS=43200             # 12h
AUTH_BCRYPT_ROUNDS=12
AUTH_LOGIN_RATE_LIMIT=10               # failed attempts per email per 15min, 0 disables
AUTH_MFA_CHALLENGE_TTL_SECONDS=300

# --- PostGIS row-level security (Slice 6) ---
AUTH_RLS_ENABLED=false                 # apply RLS policies on campuses/buildings/floors/imports

# --- Email service (Slice 7) ---
EMAIL_SERVICE_URL=http://email:8003
INTERNAL_EMAIL_TOKEN=                  # shared secret between backend and email container
SMTP_HOST=                             # unset -> email service runs in dry-run mode
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_USE_TLS=true
SMTP_FROM_ADDRESS=no-reply@ariadne.local
SMTP_FROM_NAME=Ariadne
```

The backend refuses to boot when `AUTH_JWT_SECRET` is set but shorter than 32 characters — HS256 keys at that length are trivially brute-forced.

### Bootstrapping users

1. Bring the stack up with `AUTH_JWT_SECRET` set. The auth router mounts at `/api/v1/auth/*`.
2. Mirror Neo4j organizations into the Postgres `organizations` table and create the first owner account:

```bash
docker compose exec backend python scripts/seed_users.py \
    --owner-email founder@example.com \
    --owner-password 's3cret-please-change' \
    --organization-id aau
```

The script is idempotent: re-running it never rotates an existing password and only adds memberships that are missing.

3. Owners can promote other users to `editor`/`owner` directly in Postgres for now (a self-serve membership API is out of scope for Slice 7). New users sign up with `POST /api/v1/auth/signup` and start as `viewer` of the org they signed up under.

### JWT lifecycle

- `POST /api/v1/auth/signup` and `POST /api/v1/auth/login` mint an HS256 access token signed with `AUTH_JWT_SECRET`. TTL is `AUTH_JWT_TTL_SECONDS` (12h by default). The token carries `sub`, `org_id`, `role`, and `typ=access`.
- The client sends the token as `Authorization: Bearer <token>` on every request through the gateway.
- The gateway verifies the signature, decodes the claims, **strips any caller-supplied identity headers** (anti-spoofing), and forwards trusted `X-User-Id` / `X-Org-Id` / `X-User-Role` to the backend over the private Docker network.
- To revoke a token before its `exp`, deactivate the user (`UPDATE app_users SET is_active = false WHERE email = ?`). `GET /auth/me` and the auth principal lookup re-check `is_active` so revocation propagates within one request.

### Multi-factor authentication (TOTP)

Users can enrol any RFC 6238 TOTP authenticator app (Google Authenticator, 1Password, Authy, …):

| Step           | Endpoint                          | Body              | Returns                                            |
|----------------|-----------------------------------|-------------------|----------------------------------------------------|
| Begin enrol    | `POST /api/v1/auth/mfa/setup`     | (bearer access JWT) | `{secret, provisioning_uri, recovery_codes[10]}` |
| Confirm code   | `POST /api/v1/auth/mfa/confirm`   | `{code}`          | `{mfa_enabled: true}`                              |
| Disable        | `POST /api/v1/auth/mfa/disable`   | `{password}`      | `{mfa_enabled: false}`                             |

The `provisioning_uri` is an `otpauth://...` URL clients can render as a QR code. The 10 recovery codes are returned **once** at enrolment — only their bcrypt hashes are persisted.

After enrolment, the login flow becomes two-step:

1. `POST /api/v1/auth/login` with `{email, password}` returns

   ```json
   { "mfa_required": true, "challenge_token": "...", "challenge_expires_at": "..." }
   ```

2. `POST /api/v1/auth/login/mfa` with `{challenge_token, code}` returns the real access JWT. The 6-digit TOTP, or one of the user's remaining recovery codes, is accepted; a used recovery code is removed from the stored set.

Failed password attempts are rate-limited per email (`AUTH_LOGIN_RATE_LIMIT` per 15-minute window) and login responses use a single generic "Invalid credentials" string so an attacker can't enumerate which emails exist.

### Roles and authorization

| Role     | Reads (own org) | Writes / mutations | Manage members & orgs |
|----------|-----------------|--------------------|------------------------|
| `viewer` | yes             | no                 | no                     |
| `editor` | yes             | yes                | no                     |
| `owner`  | yes             | yes                | yes                    |

Every mutating route on the backend depends on `Depends(require_role(...))` and calls `require_org_match(principal, resource.organization_id)` before touching the data store. Cross-organization connection creation is rejected at 422 to prevent tenancy escape.

When `AUTH_RLS_ENABLED=true`, the backend applies Postgres row-level security on `campuses`, `buildings`, `floors`, and `imports` at startup. Each request opens a SQLAlchemy session that sets `app.org_id` and `app.is_service` GUCs scoped to the transaction; the policies allow access only when `current_setting('app.is_service', true) = 'true'` (service-to-service / shadow-mode bypass) or `organization_id = current_setting('app.org_id', true)`. The `building_spaces` joined-inheritance hierarchy is not yet covered — tracked as a follow-up.

### Audit log

`audit_log` is append-only and lives in Postgres. Every privileged mutation writes one row:

| Column            | Notes                                                                |
|-------------------|----------------------------------------------------------------------|
| `action`          | `signup`, `login`, `login_mfa`, `mfa_setup`, `mfa_confirm`, `mfa_disable`, `create_campus`, `delete_building`, `import_map`, `create_connection`, … |
| `success`         | `true` on clean exit, `false` on any raised exception                |
| `subject_user_id` | Authenticated user (NULL on failed signups where the email was new) |
| `organization_id` | Active org at the time of the action                                 |
| `ip_address`      | First entry of `X-Forwarded-For`, falls back to client peer          |
| `user_agent`      | Raw `User-Agent` header                                              |
| `detail`          | Action-specific JSONB; never includes passwords, tokens, or secrets |

Auth events commit in the same transaction as the action they describe (`auth_service`); non-auth mutations use the `audit_action` context manager (`services/audit_service.py`), which writes in a separate short-lived transaction so a missing audit row never breaks the response. `404` and `422` are treated as benign user errors and not audited — except for `import_map`, which audits `422` because the payload may have been malicious.

### Email service

The dedicated `email` container (`email-service/`, internal port 8003) handles outbound mail. The backend POSTs to `${EMAIL_SERVICE_URL}/send` with `X-Internal-Token: ${INTERNAL_EMAIL_TOKEN}`; the email service compares the token in constant time and refuses to start if `INTERNAL_EMAIL_TOKEN` is unset (anti-open-relay). Leaving `SMTP_HOST` unset puts the service into **dry-run** mode — it accepts requests and logs payloads to stdout instead of dialing real SMTP. That's the default for local dev so a fresh `docker compose up` works without provisioning a mailer.

The email service is **internal-only**: the gateway does not expose it on `:8080`. Anything that needs to send mail (today: future password-reset / email-verification flows, ad-hoc operator tooling) must call it from inside the Docker network.

## Backend Modules

```
backend/
├── main.py              FastAPI app, lifespan (schema init), route registration under /api/v1
├── db.py                Neo4j driver singleton (execute / execute_write)
├── core/
│   ├── config.py        pydantic-settings: NEO4J_*, SUPABASE_*, AUTH_*, EMAIL_*
│   ├── auth_principal.py Principal dataclass + require_user / require_role / require_org_match
│   ├── request_context.py contextvars stamped per-request, read by PostGIS for RLS GUCs
│   └── exceptions.py    NotFoundError hierarchy, MapImportError, NavigationError
├── models/
│   ├── enums.py         SpaceType, ConnectionType, DoorType
│   ├── campus.py        Campus / Building / Floor response models
│   ├── space.py         Space response model
│   ├── connection.py    Connection response model
│   ├── navigation.py    NavigationResult, route steps
│   └── map_import.py    MapImportSchema and nested import models
├── repositories/
│   ├── campus_repo.py     CRUD for Organization, Campus, Building, Floor + cascade-delete subtree
│   ├── space_repo.py      Space CRUD, polygon (de)serialization
│   ├── connection_repo.py CONNECTS_TO relationship CRUD (door-node 4-edge pattern)
│   └── navigation_repo.py Shortest-path queries (GDS + native fallback)
├── services/
│   ├── import_service.py     Full map import (campus → buildings → floors → spaces → connections)
│   ├── navigation_service.py Route computation, path assembly
│   ├── geometry_service.py   Centroid, area, distance, weight computation
│   ├── gds_service.py        GDS graph projection management
│   ├── postgis_service.py    PostGIS mirror — sync_/get_/delete_ + RLS policies + auth tables (app_users, organization_members, audit_log)
│   ├── space_sync.py         Helpers used after Neo4j writes to push the same row into PostGIS
│   ├── auth_service.py       Signup, login, JWT issue/decode, MFA (TOTP + recovery codes), rate limit
│   ├── audit_service.py      write_audit_log + audit_action context manager for non-auth mutations
│   └── email_client.py       Thin urllib client for the email container
├── routes/
│   ├── auth.py          /auth/signup, /auth/login (+ /login/mfa), /auth/me, /auth/mfa/{setup,confirm,disable}
│   ├── organizations.py Organization CRUD, list-org-campuses
│   ├── campuses.py      Campus CRUD, import/export, search
│   ├── buildings.py     Building CRUD (incl. DELETE), floor listing
│   ├── floors.py        Floor CRUD, space listing, display, geometry, map-overlay, connections
│   ├── spaces.py        Space CRUD, connection listing
│   ├── connections.py   Connection CRUD
│   ├── navigation.py    Navigate, refresh GDS projection
│   └── search.py        Fulltext space search
├── cypher/
│   ├── constraints.cypher  Uniqueness constraints
│   └── indexes.cypher      Composite + fulltext indexes
└── scripts/
    ├── init_db.py       Applies schema (constraints + indexes) at startup
    ├── import_map.py    CLI tool for importing map JSON files
    └── seed_users.py    Mirror Neo4j orgs into Postgres + bootstrap the first owner account
```

## Assistant Modules

```
assistant/
├── main.py                 FastAPI app, lifespan, public chat router, internal embed router
├── db.py                   Neo4j helper used by repositories and routes
├── core/
│   └── config.py           pydantic-settings for Neo4j, HF token, and assistant model selection
├── models/
│   └── assistant.py        Chat and embedding request/response schemas
├── repositories/
│   └── assistant_repo.py   Vector search, anchor lookup, and distance-query graph access
├── routes/
│   ├── assistant.py        Public `/api/v1/assistant` chat endpoint
│   └── embed.py            Internal embedding endpoint used by other services
└── services/
    └── assistant_service.py Model loading, embedding cache, retrieval context, and answer generation
```

## Image Pipeline Modules

```
image_pipeline/
├── main.py                     FastAPI app for room-summary uploads and room-object setup
├── db.py                       Neo4j driver lifecycle for room lookup and persistence
├── models/
│   └── enums.py                SpaceType enum subset used when matching room-like spaces
└── room_summary/
    ├── RoomSummaryService.py   Orchestrates room summarization, named summaries, and persistence setup
    ├── RoomObjectDetector.py   YOLO model loading, validation, class-label mapping, and object counting
    ├── RoomVectorizer.py       SVG/vector rendering plus embedded-image export helpers
    ├── RoomSummaryRepository.py Room lookup plus summary/object persistence back into Neo4j
    ├── Neo4jQueryRunner.py     Thin query adapter around the Neo4j driver/session
    ├── model_config.py         Profile-based model/class-config resolution from cfg files
    ├── model_download.py       Model path resolution and on-demand YOLO download support
    ├── download_model.py       CLI helper for downloading a configured detection model
    ├── class_labels.py         Optional class-id to label override loading
    ├── runtime_env.py          Runtime environment setup before importing ultralytics
    ├── RoomImageInput.py       Uploaded image wrapper passed into the summarizer
    ├── ViewSummary.py          Per-view SVG and object-count response model
    ├── RoomSummaryResult.py    Full multi-view summary response model
    ├── NamedRoomSummaryResult.py Named-room summary response model
    ├── RoomObjectDetectionSetupResult.py Persisted room-object setup response model
    └── __init__.py             Direct package exports for the room-summary types
```

## Middleware Module

```
middleware/
└── main.py   FastAPI gateway with `/health`, mobile endpoints, optional `/debug/upstreams`, and proxy routes to backend, assistant, and image_pipeline
```

### Mobile Endpoints

Lightweight endpoints for mobile clients, available through the middleware on port 8080.

| Method | Path                                          | Description                                      |
|--------|-----------------------------------------------|--------------------------------------------------|
| `GET`  | `/api/v1/mobile/campuses`                     | List all campuses (id + name only)               |
| `GET`  | `/api/v1/mobile/campuses/{campus_id}/map`     | Full map download (same as backend export)        |
| `GET`  | `/api/v1/mobile/campuses/{campus_id}/map/light` | Map download without SVG/image data             |

The `/map/light` endpoint strips embedded room images and view SVGs from the response, significantly reducing payload size for clients that don't need image data.

## Frontend Module

```
frontend/
├── index.html   Static map-editor entry page
├── nginx.conf   SPA hosting plus `/api` and `/health` proxying to middleware
└── Dockerfile   Nginx image build for the frontend container
```

## API Endpoints

All routes are prefixed with `/api/v1` and require `X-Api-Key` (enforced by the middleware on `:8080`). Mutating routes additionally require `Authorization: Bearer <jwt>` once `AUTH_JWT_SECRET` is configured — see *Authentication & Multi-Tenancy* above. Auth endpoints (`/auth/*`) only mount when `AUTH_JWT_SECRET` is set.

| Method   | Path                                          | Description                          |
|----------|-----------------------------------------------|--------------------------------------|
| `POST`   | `/auth/signup`                                | Create a new user                    |
| `POST`   | `/auth/login`                                 | Verify password — returns access JWT or MFA challenge |
| `POST`   | `/auth/login/mfa`                             | Exchange a challenge token + TOTP/recovery code for an access JWT |
| `GET`    | `/auth/me`                                    | Resolve the active principal         |
| `POST`   | `/auth/mfa/setup`                             | Begin TOTP enrolment (returns secret + recovery codes) |
| `POST`   | `/auth/mfa/confirm`                           | Confirm TOTP code; flips `mfa_enabled=true` |
| `POST`   | `/auth/mfa/disable`                           | Re-confirm password and disable MFA  |
| `GET`    | `/organizations`                              | List all organizations               |
| `POST`   | `/organizations`                              | Create an organization               |
| `GET`    | `/organizations/{organization_id}`            | Get an organization                  |
| `GET`    | `/organizations/{organization_id}/campuses`   | List campuses owned by an org        |
| `DELETE` | `/organizations/{organization_id}`            | Delete an org and its full subtree   |
| `GET`    | `/campuses`                                   | List all campuses                    |
| `POST`   | `/campuses`                                   | Create a campus (must reference an org) |
| `GET`    | `/campuses/{campus_id}`                       | Get a campus                         |
| `DELETE` | `/campuses/{campus_id}`                       | Delete a campus and its subtree      |
| `POST`   | `/campuses/{campus_id}/import`                | Import full map JSON                 |
| `GET`    | `/campuses/{campus_id}/export`                | Export map as JSON                   |
| `GET`    | `/campuses/{campus_id}/search`                | Fulltext search spaces               |
| `POST`   | `/buildings`                                  | Create a building                    |
| `GET`    | `/buildings/{building_id}`                    | Get a building                       |
| `GET`    | `/buildings/{building_id}/floors`             | List floors of a building            |
| `DELETE` | `/buildings/{building_id}`                    | Delete a building and its subtree    |
| `POST`   | `/floors`                                     | Create a floor                       |
| `GET`    | `/floors/{floor_id}`                          | Get a floor                          |
| `GET`    | `/floors/{floor_id}/spaces`                   | List spaces on a floor               |
| `GET`    | `/floors/{floor_id}/display`                  | Spaces + polygons for map rendering (PostGIS-first) |
| `GET`    | `/floors/{floor_id}/connections`              | Connections on a floor               |
| `GET`    | `/floors/{floor_id}/geometry`                 | Floor + rooms (polygons, centroids) for iOS |
| `GET`    | `/floors/{floor_id}/map-overlay`              | Floor-plan bounds/scale/origin for MapKit overlay |
| `POST`   | `/spaces`                                     | Create a space                       |
| `GET`    | `/spaces/{space_id}`                          | Get a space                          |
| `PATCH`  | `/spaces/{space_id}`                          | Update a space                       |
| `DELETE` | `/spaces/{space_id}`                          | Delete a space                       |
| `GET`    | `/spaces/{space_id}/connections`              | List connections for a space         |
| `POST`   | `/connections`                                | Create a connection                  |
| `GET`    | `/connections/{from_space_id}/{to_space_id}`  | Get a connection                     |
| `DELETE` | `/connections/{from_space_id}/{to_space_id}`  | Delete a connection                  |
| `GET`    | `/navigate?from=&to=&accessible_only=`        | Weighted shortest path               |
| `POST`   | `/navigate/refresh-graph`                     | Rebuild GDS graph projection         |
| `GET`    | `/search/campuses/{campus_id}/spaces`         | Search spaces (alternate route)      |
| `GET`    | `/health`                                     | Health check (no prefix)             |

Cascade deletes (`organization` / `campus` / `building`) clean up Neo4j first, then mirror the deletion in PostGIS in the same request. The mapmaker on `:3000` exposes confirm dialogs for these — deleting an organization additionally requires typing the org name to confirm.

## Map JSON Format

The import endpoint (`POST /api/v1/campuses/{campus_id}/import`) accepts a `MapImportSchema`:

```json
{
  "schema_version": "1.0",
  "campus": {
    "id": "campus-aau",
    "name": "AAU Campus",
    "description": null,
    "buildings": [],
    "outdoor_spaces": [],
    "connections": []
  }
}
```

### CampusImport

| Field             | Type               | Required | Description                              |
|-------------------|--------------------|----------|------------------------------------------|
| `id`              | `string`           | yes      | Unique campus identifier                 |
| `name`            | `string`           | yes      | Display name                             |
| `description`     | `string \| null`   | no       | Optional description                     |
| `buildings`       | `BuildingImport[]` | no       | List of buildings (default `[]`)         |
| `outdoor_spaces`  | `SpaceImport[]`    | no       | Outdoor spaces not in buildings          |
| `connections`     | `ConnectionImport[]` | no     | Connections between any spaces           |

### BuildingImport

| Field            | Type             | Required | Description                                    |
|------------------|------------------|----------|------------------------------------------------|
| `id`             | `string`         | yes      | Unique building identifier                     |
| `name`           | `string`         | yes      | Display name                                   |
| `short_name`     | `string \| null` | no       | Abbreviation (e.g. "B3")                       |
| `address`        | `string \| null` | no       | Street address                                 |
| `origin_lat`     | `float \| null`  | no       | Building origin latitude (WGS84)               |
| `origin_lng`     | `float \| null`  | no       | Building origin longitude (WGS84)              |
| `origin_bearing` | `float`          | no       | Rotation in degrees from north (default `0.0`) |
| `floor_count`    | `int \| null`    | no       | Total number of floors                         |
| `floors`         | `FloorImport[]`  | no       | List of floors (default `[]`)                  |

### FloorImport

| Field                | Type            | Required | Description                                 |
|----------------------|-----------------|----------|---------------------------------------------|
| `id`                 | `string`        | yes      | Unique floor identifier                     |
| `floor_index`        | `int`           | yes      | Floor number (0 = ground, -1 = basement)    |
| `display_name`       | `string`        | yes      | Display name (e.g. "Ground Floor")          |
| `elevation_m`        | `float \| null` | no       | Elevation above ground in meters            |
| `floor_plan_url`     | `string \| null`| no       | URL to floor plan image                     |
| `floor_plan_scale`   | `float \| null` | no       | Pixels per meter on the floor plan          |
| `floor_plan_origin_x`| `float \| null`| no       | X offset of the floor plan origin           |
| `floor_plan_origin_y`| `float \| null`| no       | Y offset of the floor plan origin           |
| `spaces`             | `SpaceImport[]` | no       | Spaces on this floor (default `[]`)         |

### SpaceImport

| Field           | Type                  | Required | Description                                       |
|-----------------|-----------------------|----------|---------------------------------------------------|
| `id`            | `string`              | yes      | Unique space identifier                           |
| `display_name`  | `string`              | yes      | Display name                                      |
| `short_name`    | `string \| null`      | no       | Short label for map rendering                     |
| `space_type`    | `SpaceType`           | yes      | Type of space (see enum below)                    |
| `centroid_x`    | `float \| null`       | no       | X coordinate of centroid (auto-computed if polygon given) |
| `centroid_y`    | `float \| null`       | no       | Y coordinate of centroid                          |
| `polygon`       | `float[][] \| null`   | no       | List of `[x, y]` vertex pairs defining the shape  |
| `width_m`       | `float \| null`       | no       | Width in meters                                   |
| `length_m`      | `float \| null`       | no       | Length in meters                                   |
| `area_m2`       | `float \| null`       | no       | Area in m² (auto-computed from polygon if absent) |
| `is_accessible` | `bool`                | no       | Wheelchair accessible (default `true`)            |
| `is_navigable`  | `bool`                | no       | Included in navigation graph (default `true`)     |
| `is_outdoor`    | `bool`                | no       | Outdoor space (default `false`)                   |
| `capacity`      | `int \| null`         | no       | Room capacity                                     |
| `tags`          | `string[]`            | no       | Searchable tags (default `[]`)                    |
| `metadata`      | `object \| null`      | no       | Arbitrary key-value data                          |
| `subspaces`     | `SpaceImport[]`       | no       | Nested child spaces (default `[]`)                |

### ConnectionImport

| Field                 | Type               | Required | Description                                    |
|-----------------------|--------------------|----------|------------------------------------------------|
| `from_space_id`       | `string`           | yes      | Source space ID                                |
| `to_space_id`         | `string`           | yes      | Target space ID                                |
| `connection_type`     | `ConnectionType`   | yes      | Type of connection (see enum below)            |
| `is_accessible`       | `bool`             | no       | Wheelchair accessible (default `true`)         |
| `door_type`           | `DoorType`         | no       | Door type at this connection (default `NONE`)  |
| `requires_access_level` | `string \| null` | no      | Required access level (e.g. "STAFF")           |
| `transition_time_s`   | `float \| null`    | no       | Override traversal time in seconds             |
| `weight_override`     | `float \| null`    | no       | Override computed weight directly              |

### Enums

#### SpaceType

| Group          | Values                                                                                       |
|----------------|----------------------------------------------------------------------------------------------|
| Rooms          | `ROOM_GENERIC`, `ROOM_OFFICE`, `ROOM_CLASSROOM`, `ROOM_LECTURE_HALL`, `ROOM_LAB`, `ROOM_MEETING`, `ROOM_STORAGE`, `ROOM_UTILITY`, `RESTROOM`, `RESTROOM_ACCESSIBLE` |
| Circulation    | `CORRIDOR`, `CORRIDOR_SEGMENT`, `LOBBY`, `WAITING_AREA`, `RECEPTION`                        |
| Entrances      | `ENTRANCE`, `ENTRANCE_SECONDARY`, `EXIT_EMERGENCY`                                          |
| Vertical       | `STAIRCASE`, `STAIRCASE_LANDING`, `ELEVATOR`, `ELEVATOR_LOBBY`, `ESCALATOR`, `RAMP`         |
| Connectors     | `BRIDGE`, `TUNNEL`, `COVERED_WALKWAY`                                                        |
| Outdoor        | `OUTDOOR_PATH`, `OUTDOOR_PLAZA`, `OUTDOOR_COURTYARD`, `OUTDOOR_STAIRS`, `PARKING`           |
| Amenities      | `CAFETERIA`, `CAFE`, `LIBRARY`, `GYM`, `AUDITORIUM`, `SHOP`                                 |
| Special        | `INACCESSIBLE`, `UNKNOWN`                                                                    |

#### ConnectionType

`WALKWAY`, `DOORWAY`, `STAIRCASE_UP`, `STAIRCASE_DOWN`, `ELEVATOR_UP`, `ELEVATOR_DOWN`, `ESCALATOR_UP`, `ESCALATOR_DOWN`, `OUTDOOR_PATH`, `BRIDGE`, `TUNNEL`, `RAMP_UP`, `RAMP_DOWN`

#### DoorType

`NONE`, `STANDARD`, `AUTOMATIC`, `LOCKED`, `EMERGENCY_ONLY`

### Minimal Working Example

```json
{
  "schema_version": "1.0",
  "campus": {
    "id": "campus-1",
    "name": "Example Campus",
    "buildings": [
      {
        "id": "building-1",
        "name": "Main Building",
        "floors": [
          {
            "id": "floor-1",
            "floor_index": 0,
            "display_name": "Ground Floor",
            "spaces": [
              {
                "id": "space-lobby",
                "display_name": "Main Lobby",
                "space_type": "LOBBY",
                "polygon": [[0,0], [10,0], [10,8], [0,8]]
              },
              {
                "id": "space-hall",
                "display_name": "Corridor A",
                "space_type": "CORRIDOR",
                "polygon": [[10,0], [30,0], [30,3], [10,3]]
              },
              {
                "id": "space-101",
                "display_name": "Room 101",
                "space_type": "ROOM_CLASSROOM",
                "polygon": [[10,3], [20,3], [20,8], [10,8]],
                "capacity": 30,
                "tags": ["teaching", "projector"]
              }
            ]
          }
        ]
      }
    ],
    "connections": [
      {
        "from_space_id": "space-lobby",
        "to_space_id": "space-hall",
        "connection_type": "DOORWAY"
      },
      {
        "from_space_id": "space-hall",
        "to_space_id": "space-101",
        "connection_type": "DOORWAY",
        "door_type": "STANDARD"
      }
    ]
  }
}
```

## Neo4j Graph Model

### Nodes

| Label          | Key Properties                                                                  |
|----------------|---------------------------------------------------------------------------------|
| `Organization` | `id`, `name`, `description`                                                     |
| `Campus`       | `id`, `name`, `description`, `organization_id`                                  |
| `Building`     | `id`, `name`, `short_name`, `address`, `origin_lat/lng/bearing`, `floor_count`  |
| `Floor`        | `id`, `floor_index`, `display_name`, `elevation_m`, `floor_plan_*`              |
| `Space`        | `id`, `display_name`, `space_type`, `polygon` (JSON string), `centroid_x/y`, `tags_text` |
| `Landmark`     | `id`, `name`, `space_id`                                                        |

### Relationships

| Relationship    | From         | To         | Properties                                                              |
|-----------------|--------------|------------|-------------------------------------------------------------------------|
| `OWNS`          | Organization | Campus     | -                                                                       |
| `HAS_BUILDING`  | Campus       | Building   | -                                                                       |
| `HAS_FLOOR`     | Building     | Floor      | -                                                                       |
| `HAS_SPACE`     | Floor        | Space      | -                                                                       |
| `HAS_SUBSPACE`  | Space        | Space      | -                                                                       |
| `HAS_LANDMARK`  | Space        | Landmark   | -                                                                       |
| `CONNECTS_TO`   | Space        | Space      | `weight`, `distance_m`, `connection_type`, `is_accessible`, `door_type`, `transition_time_s` |

`tags_text` is a derived property (space tags joined with ` `) stored for Neo4j fulltext indexing.

`polygon` is stored as a JSON string and deserialized on read.

### PostGIS Mirror

When `SUPABASE_DB_URL` is configured, every CUD on Organization/Campus/Building/Floor/Space goes Neo4j first, then `PostGISService` upserts the same row into PostGIS via SQLAlchemy + GeoAlchemy2 (joined-table inheritance: `spatial_entities` → `organizations` / `campuses` / `buildings` / `floors` / `spaces`). Polygons are stored as `geometry(Polygon)` for `ST_*` queries; the floor `display` and `geometry` endpoints prefer PostGIS and fall back to Neo4j if PostGIS is unset or empty.

## Navigation and Weights

The navigation system computes weighted shortest paths:

1. **Primary**: GDS Dijkstra on `CONNECTS_TO.weight` (walking-second equivalents)
2. **Fallback**: Native `shortestPath` (hop-count) if GDS is unavailable

### Weight Computation

Weight represents traversal time in seconds:

```
weight = distance_m / speed_m_s
```

| Connection Type                                    | Speed (m/s) |
|----------------------------------------------------|-------------|
| `WALKWAY`, `DOORWAY`, `OUTDOOR_PATH`, `BRIDGE`, `TUNNEL` | 1.4    |
| `ESCALATOR_UP`, `ESCALATOR_DOWN`                   | 0.8         |
| `RAMP_DOWN`                                        | 0.9         |
| `STAIRCASE_DOWN`, `RAMP_UP`                        | 0.7         |
| `STAIRCASE_UP`                                     | 0.5         |
| `ELEVATOR_UP`, `ELEVATOR_DOWN`                     | 30s flat (or `transition_time_s`) |

Use `weight_override` on a connection to bypass computed weights. Use `accessible_only=true` on the navigate endpoint to exclude non-accessible connections.

## Frontend

Single-page canvas-based map editor served by nginx on port 3000. Supports:

- Floor plan rendering with space polygons
- Space creation and editing
- Connection drawing between spaces
- Full map import/export via the backend API
