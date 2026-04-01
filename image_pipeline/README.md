# Image Pipeline

This module exposes the room-summary API used to analyze four room photos, count detected objects, generate SVG-based summaries, and optionally persist the result back into Neo4j.

The service runs on port `8002` in the project compose setup.

## What This Service Exposes

The public FastAPI app lives in [main.py](./main.py).

### `GET /health`

Simple health check used by the middleware and compose stack.

Response:

```json
{
  "status": "ok"
}
```

### `GET /api/v1/room-summary/rooms`

Returns room names from Neo4j that can be used with the setup endpoint.

What it does:

- Connects to Neo4j
- Finds `Space` nodes whose `space_type` is room-like
- Returns normalized display names in sorted order

Response shape:

```json
{
  "names": ["A101", "A102", "Meeting Room 1"]
}
```

Example request:

```bash
curl http://localhost:8002/api/v1/room-summary/rooms
```

### `POST /api/v1/room-summary`

Summarizes a set of four uploaded room images.

What it does:

- Validates that exactly four images are present
- Decodes the uploaded files with OpenCV
- Runs YOLO object detection on each view
- Aggregates object counts across all four views
- Generates an SVG summary for each uploaded image
- Returns the result without storing anything in Neo4j

Required upload fields:

- `north_image`
- `east_image`
- `south_image`
- `west_image`

The images must be uploaded in clockwise room order: north, east, south, west.

Optional query parameters:

- `model_name`: profile name from `modelConfig.cfg`, for example `nano` or `small`
- `confidence_threshold`: request-level YOLO threshold override between `0.0` and `1.0`

Example request input:

```bash
curl -X POST "http://localhost:8002/api/v1/room-summary?model_name=nano&confidence_threshold=0.35" \
  -F "north_image=@north.jpg" \
  -F "east_image=@east.jpg" \
  -F "south_image=@south.jpg" \
  -F "west_image=@west.jpg"
```

Response shape:

```json
{
  "model": {
    "profile": "nano",
    "path": "/app/room_summary/models/yolo11n.pt"
  },
  "overall_object_counts": {
    "chair": 6,
    "desk": 2
  },
  "counting_strategy": "aggregation across the four uploaded room images",
  "views": [
    {
      "view_index": 1,
      "source_name": "north.jpg",
      "object_counts": {
        "chair": 2
      },
      "svg": "<svg ... />"
    }
  ]
}
```

### `POST /api/v1/room-summary/by-room`

Summarizes four uploaded images and associates the result with a provided room name in the response.

What it does:

- Performs the same image processing as `/api/v1/room-summary`
- Requires a `room_name` form field
- Returns a smaller response shape intended for room-specific consumers
- Does not write anything to Neo4j

Required form field:

- `room_name`

Required upload fields:

- `north_image`
- `east_image`
- `south_image`
- `west_image`

Optional query parameters:

- `model_name`: profile name from `modelConfig.cfg`, for example `nano` or `small`
- `confidence_threshold`: request-level YOLO threshold override between `0.0` and `1.0`

Example request input:

```bash
curl -X POST "http://localhost:8002/api/v1/room-summary/by-room?confidence_threshold=0.4" \
  -F "room_name=A101" \
  -F "north_image=@north.jpg" \
  -F "east_image=@east.jpg" \
  -F "south_image=@south.jpg" \
  -F "west_image=@west.jpg"
```

Response shape:

```json
{
  "room_name": "A101",
  "room_summary": [
    {
      "view_index": 1,
      "svg": "<svg ... />"
    }
  ]
}
```

### `POST /api/v1/room-summary/room-objects/setup`

Creates or replaces stored room-summary metadata for a room that already exists in Neo4j.

What it does:

- Looks up the target room by `display_name`, `short_name`, or `id`
- Runs the same image analysis pipeline as the summary endpoint
- Builds `room_objects` and `room_object_counts`
- Stores the generated room data back into the matched `Space` node
- Embeds the original uploaded images inside SVG wrappers for storage

Required form field:

- `room_name`

Required upload fields:

- `north_image`
- `east_image`
- `south_image`
- `west_image`

Optional query parameters:

- `model_name`: profile name from `modelConfig.cfg`, for example `nano` or `small`
- `confidence_threshold`: request-level YOLO threshold override between `0.0` and `1.0`
- `stored_image_count`: optional number of uploaded images to store in Neo4j, between `1` and `4`
- `stored_views`: optional compass directions to store, for example `north`

Example request input:

```bash
curl -X POST "http://localhost:8002/api/v1/room-summary/room-objects/setup?model_name=small&confidence_threshold=0.3&stored_image_count=2" \
  -F "room_name=A101" \
  -F "north_image=@north.jpg" \
  -F "east_image=@east.jpg" \
  -F "south_image=@south.jpg" \
  -F "west_image=@west.jpg"
```

Example request input storing only the north image:

```bash
curl -X POST "http://localhost:8002/api/v1/room-summary/room-objects/setup?stored_views=north" \
  -F "room_name=A101" \
  -F "north_image=@north.jpg" \
  -F "east_image=@east.jpg" \
  -F "south_image=@south.jpg" \
  -F "west_image=@west.jpg"
```

Example request input storing multiple named views:

```bash
curl -X POST "http://localhost:8002/api/v1/room-summary/room-objects/setup?stored_views=north&stored_views=west" \
  -F "room_name=A101" \
  -F "north_image=@north.jpg" \
  -F "east_image=@east.jpg" \
  -F "south_image=@south.jpg" \
  -F "west_image=@west.jpg"
```

What gets stored on the `Space` node:

- `room_objects`
- `room_object_counts_json`
- `room_images`
- `room_summary_updated_at`
- `metadata.room_summary`

Image storage behavior:

- if neither `stored_image_count` nor `stored_views` is provided, all 4 uploaded images are stored
- if `stored_image_count` is provided without `stored_views`, that many images are chosen randomly and stored
- if `stored_views` is provided, exactly those named directions are stored
- the response tells you how many images were stored and which directions were selected

Response shape:

```json
{
  "room_name": "A101",
  "room_objects": ["chair", "desk"],
  "room_object_counts": {
    "chair": 6,
    "desk": 2
  },
  "stored_image_count": 1,
  "stored_views": ["north"],
  "room_summary": [
    {
      "view_index": 1,
      "svg": "<svg ... />"
    }
  ]
}
```

## Request Rules

- All image endpoints require exactly four images.
- Empty files or invalid image payloads return `400`.
- `room_name` is required for `/by-room` and `/room-objects/setup`.
- `confidence_threshold`, when provided, must be between `0.0` and `1.0`.
- `stored_image_count`, when provided, must be between `1` and `4`.
- `stored_views`, when provided, must be unique values chosen from `north`, `east`, `south`, and `west`.
- if both are provided, `stored_image_count` must match the number of `stored_views`.
- `/room-objects/setup` returns `404` if the named room is not found in Neo4j.
- Unexpected failures return `500`.

## Main Internal Components

These are the core parts behind the exposed services:

- [main.py](./main.py)
  Defines the FastAPI app, request parsing, endpoint wiring, and Neo4j lifecycle.
- [room_summary/RoomSummaryService.py](./room_summary/RoomSummaryService.py)
  Main orchestration layer for detection, vectorization, aggregation, and persistence.
- [room_summary/RoomObjectDetector.py](./room_summary/RoomObjectDetector.py)
  Loads the YOLO model and produces per-image object counts.
- [room_summary/RoomVectorizer.py](./room_summary/RoomVectorizer.py)
  Converts frames into compact SVG summaries and can also wrap the original frame into SVG.
- [room_summary/RoomSummaryRepository.py](./room_summary/RoomSummaryRepository.py)
  Neo4j lookup and persistence logic for room-summary metadata.
- [db.py](./db.py)
  Shared Neo4j driver setup and teardown.

## Configuration

The service is configured by:

- Neo4j connection settings such as `NEO4J_URI`, `NEO4J_USER`, and `NEO4J_PASSWORD`
- model selection settings such as `YOLO_MODEL_PATH`, `YOLO_MODEL_PROFILE`, and `MODEL_CONFIG_PATH`
- detection and class-label settings such as `YOLO_CONFIDENCE_THRESHOLD` and `CLASS_CONFIG_PATH`
- vectorization settings such as `VECTOR_PALETTE_SIZE` and `MAX_VECTOR_WIDTH`

The detailed room-summary configuration options are documented below.

## Configurable Room-Summary Behavior

The room-summary pipeline is configurable at three levels:

### 1. Environment variables

These affect the whole service process:

- `YOLO_MODEL_PROFILE`
  Selects a named model profile from `modelConfig.cfg`.
- `YOLO_MODEL_PATH`
  Uses a concrete model file path instead of selecting a profile.
- `YOLO_CONFIDENCE_THRESHOLD`
  Fallback detection threshold when no profile confidence or request override is used.
- `MODEL_CONFIG_PATH`
  Points to an alternate model profile config file.
- `CLASS_CONFIG_PATH`
  Points to an alternate class-label mapping file.
- `VECTOR_PALETTE_SIZE`
  Controls the SVG vectorization palette size.
- `MAX_VECTOR_WIDTH`
  Controls the maximum width used during vectorization.

### 2. Config files

The default model profile config is [room_summary/modelConfig.cfg](./room_summary/modelConfig.cfg).

It currently looks like this:

```ini
[selection]
active = nano

[nano]
path = models/yolo11n.pt
classes = classConf.cfg
confidence = 0.5

[small]
path = models/yolo11s.pt
classes = classConf.cfg
confidence = 0.5
```

What is configurable in `modelConfig.cfg`:

- the active profile under `[selection]`
- the model file path for each profile
- the class config file for each profile
- the default confidence threshold for each profile

Available built-in profiles in the current file:

- `nano`
- `small`
- `medium`
- `large`
- `xlarge`

The default class mapping file is [room_summary/classConf.cfg](./room_summary/classConf.cfg).

That file maps YOLO class ids to labels, for example:

```ini
39=bottle
56=chair
59=bed
62=tv
66=keyboard
```

### 3. Per-request query parameters

Each image endpoint supports request-level overrides:

- `model_name`
  Picks a specific profile for that request only.
- `confidence_threshold`
  Overrides the resolved confidence threshold for that request only.

Example:

```bash
curl -X POST "http://localhost:8002/api/v1/room-summary?model_name=small&confidence_threshold=0.3" \
  -F "north_image=@north.jpg" \
  -F "east_image=@east.jpg" \
  -F "south_image=@south.jpg" \
  -F "west_image=@west.jpg"
```

## Configuration Resolution Order

Model selection is resolved in this order:

1. `model_name` from the request, if provided
2. `YOLO_MODEL_PROFILE` from the environment
3. active profile from `modelConfig.cfg`
4. fallback direct model path from `YOLO_MODEL_PATH`

Confidence threshold is resolved in this order:

1. `confidence_threshold` from the request, if provided
2. the selected profile's `confidence` value from `modelConfig.cfg`
3. `YOLO_CONFIDENCE_THRESHOLD`

Class labels come from:

1. `CLASS_CONFIG_PATH`, if set
2. the selected profile's `classes` entry
3. the bundled default class config file

## Running Locally

From the `image_pipeline` directory:

```bash
uvicorn main:app --host 0.0.0.0 --port 8002 --reload
```

When the full stack is running:

- direct service docs: `http://localhost:8002/docs`
- gateway API base: `http://localhost:8080/api/v1/room-summary`
- frontend-routed API base: `http://localhost:3000/api/v1/room-summary`

## Notes

- The service caches constructed `RoomSummaryService` instances with `lru_cache`.
- YOLO model loading is also cached so repeated requests do not reload the model each time.
- The returned SVG can be displayed directly in a browser or parsed to recover the embedded source image bytes if needed.
