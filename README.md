# Spatial Backend

Indoor spatial mapping and navigation backend for building complexes. Stores maps as a Neo4j graph, computes weighted shortest paths, and serves a canvas-based map editor frontend.

## Quick Start

```bash
docker compose up -d --build
```

This starts the services in this repo and expects a reachable Neo4j instance for the app containers.

| Service          | Port | Description                          |
|------------------|------|--------------------------------------|
| `middleware`     | 8080 | Public gateway and middleware docs   |
| `backend`        | 8000 | Spatial/navigation API in dev compose |
| `assistant`      | 8001 | Chat and embedding service in dev compose |
| `image_pipeline` | 8002 | Room summary/image processing API in dev compose |
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
│ frontend │────>│ middleware │
│ :3000    │     │ :8080      │
│ (nginx)  │     │ (gateway)  │
└──────────┘     └─────┬──────┘
                       ├────────────> backend :8000
                       ├────────────> assistant :8001
                       └────────────> image_pipeline :8002

backend        ────────> neo4j :7687
assistant      ────────> neo4j :7687
image_pipeline ────────> neo4j :7687

backend        ────────> assistant :8001
                     (internal embed during import)
```

- `frontend` proxies `/api/*` and `/health` to `middleware`.
- `middleware` routes `/api/v1/assistant/*` to `assistant`, `/api/v1/room-summary*` to `image_pipeline`, and the remaining `/api/v1/*` routes to `backend`.
- `backend`, `assistant`, and `image_pipeline` all connect directly to Neo4j.
- `backend` also calls `assistant` directly for internal embeddings during map import.

**Graph model:**

```
Campus ─[HAS_BUILDING]─> Building ─[HAS_FLOOR]─> Floor ─[HAS_SPACE]─> Space
                                                         Space ─[HAS_SUBSPACE]─> Space
                                                         Space ─[CONNECTS_TO]─> Space
                                                         Space ─[HAS_LANDMARK]─> Landmark
```

## Backend Modules

```
backend/
├── main.py              FastAPI app, lifespan (schema init), route registration under /api/v1
├── db.py                Neo4j driver singleton (execute / execute_write)
├── core/
│   ├── config.py        pydantic-settings: NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD
│   └── exceptions.py    NotFoundError hierarchy, MapImportError, NavigationError
├── models/
│   ├── enums.py         SpaceType, ConnectionType, DoorType
│   ├── campus.py        Campus / Building / Floor response models
│   ├── space.py         Space response model
│   ├── connection.py    Connection response model
│   ├── navigation.py    NavigationResult, route steps
│   └── map_import.py    MapImportSchema and nested import models
├── repositories/
│   ├── campus_repo.py   CRUD for Campus, Building, Floor nodes
│   ├── space_repo.py    Space CRUD, polygon (de)serialization
│   ├── connection_repo.py  CONNECTS_TO relationship CRUD
│   └── navigation_repo.py  Shortest-path queries (GDS + native fallback)
├── services/
│   ├── import_service.py     Full map import (campus → buildings → floors → spaces → connections)
│   ├── navigation_service.py Route computation, path assembly
│   ├── geometry_service.py   Centroid, area, distance, weight computation
│   └── gds_service.py        GDS graph projection management
├── routes/
│   ├── campuses.py      Campus CRUD, import/export, search
│   ├── buildings.py     Building CRUD, floor listing
│   ├── floors.py        Floor CRUD, space listing, display, connections
│   ├── spaces.py        Space CRUD, connection listing
│   ├── connections.py   Connection CRUD
│   ├── navigation.py    Navigate, refresh GDS projection
│   └── search.py        Fulltext space search
├── cypher/
│   ├── constraints.cypher  Uniqueness constraints
│   └── indexes.cypher      Composite + fulltext indexes
└── scripts/
    ├── init_db.py       Applies schema (constraints + indexes) at startup
    └── import_map.py    CLI tool for importing map JSON files
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
└── main.py   FastAPI gateway with `/health`, optional `/debug/upstreams`, and proxy routes to backend, assistant, and image_pipeline
```

## Frontend Module

```
frontend/
├── index.html   Static map-editor entry page
├── nginx.conf   SPA hosting plus `/api` and `/health` proxying to middleware
└── Dockerfile   Nginx image build for the frontend container
```

## API Endpoints

All routes are prefixed with `/api/v1`.

| Method   | Path                                       | Description                          |
|----------|--------------------------------------------|--------------------------------------|
| `GET`    | `/campuses`                                | List all campuses                    |
| `POST`   | `/campuses`                                | Create a campus                      |
| `GET`    | `/campuses/{campus_id}`                    | Get a campus                         |
| `DELETE` | `/campuses/{campus_id}`                    | Delete a campus                      |
| `POST`   | `/campuses/{campus_id}/import`             | Import full map JSON                 |
| `GET`    | `/campuses/{campus_id}/export`             | Export map as JSON                   |
| `GET`    | `/campuses/{campus_id}/search`             | Fulltext search spaces               |
| `POST`   | `/buildings`                               | Create a building                    |
| `GET`    | `/buildings/{building_id}`                 | Get a building                       |
| `GET`    | `/buildings/{building_id}/floors`          | List floors of a building            |
| `POST`   | `/floors`                                  | Create a floor                       |
| `GET`    | `/floors/{floor_id}`                       | Get a floor                          |
| `GET`    | `/floors/{floor_id}/spaces`               | List spaces on a floor               |
| `GET`    | `/floors/{floor_id}/display`              | Spaces + polygons for map rendering  |
| `GET`    | `/floors/{floor_id}/connections`          | Connections on a floor               |
| `POST`   | `/spaces`                                  | Create a space                       |
| `GET`    | `/spaces/{space_id}`                       | Get a space                          |
| `PATCH`  | `/spaces/{space_id}`                       | Update a space                       |
| `DELETE` | `/spaces/{space_id}`                       | Delete a space                       |
| `GET`    | `/spaces/{space_id}/connections`           | List connections for a space         |
| `POST`   | `/connections`                             | Create a connection                  |
| `GET`    | `/connections/{from_space_id}/{to_space_id}` | Get a connection                   |
| `DELETE` | `/connections/{from_space_id}/{to_space_id}` | Delete a connection                |
| `GET`    | `/navigate?from=&to=&accessible_only=`    | Weighted shortest path               |
| `POST`   | `/navigate/refresh-graph`                  | Rebuild GDS graph projection         |
| `GET`    | `/search/campuses/{campus_id}/spaces`      | Search spaces (alternate route)      |
| `GET`    | `/health`                                  | Health check (no prefix)             |

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

| Label      | Key Properties                                                    |
|------------|-------------------------------------------------------------------|
| `Campus`   | `id`, `name`, `description`                                      |
| `Building` | `id`, `name`, `short_name`, `address`, `origin_lat/lng/bearing`  |
| `Floor`    | `id`, `floor_index`, `display_name`, `elevation_m`               |
| `Space`    | `id`, `display_name`, `space_type`, `polygon` (JSON string), `centroid_x/y`, `tags_text` |
| `Landmark` | `id`, `name`, `space_id`                                         |

### Relationships

| Relationship    | From       | To        | Properties                                                              |
|-----------------|------------|-----------|-------------------------------------------------------------------------|
| `HAS_BUILDING`  | Campus     | Building  | -                                                                       |
| `HAS_FLOOR`     | Building   | Floor     | -                                                                       |
| `HAS_SPACE`     | Floor      | Space     | -                                                                       |
| `HAS_SUBSPACE`  | Space      | Space     | -                                                                       |
| `HAS_LANDMARK`  | Space      | Landmark  | -                                                                       |
| `CONNECTS_TO`   | Space      | Space     | `weight`, `distance_m`, `connection_type`, `is_accessible`, `door_type`, `transition_time_s` |

`tags_text` is a derived property (space tags joined with ` `) stored for Neo4j fulltext indexing.

`polygon` is stored as a JSON string and deserialized on read.

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
