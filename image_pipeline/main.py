import os
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile

from db import close_neo4j, get_neo4j_driver
from room_summary.RoomImageInput import RoomImageInput
from room_summary.RoomSummaryService import RoomSummaryService

ROOM_SUMMARY_DIR = Path(__file__).resolve().parent / "room_summary"
PREFIX = "/api/v1"
CLOCKWISE_DIRECTIONS = ("north", "east", "south", "west")


def _default_model_config_path() -> Path:
    configured_path = os.getenv("MODEL_CONFIG_PATH")
    if configured_path:
        return Path(configured_path)

    return ROOM_SUMMARY_DIR / "modelConfig.cfg"


def _default_class_config_path() -> Path:
    for candidate_name in ("classConfig.cfg", "classConf.cfg"):
        candidate_path = ROOM_SUMMARY_DIR / candidate_name
        if candidate_path.exists():
            return candidate_path

    return ROOM_SUMMARY_DIR / "classConf.cfg"


def _resolve_class_config_path(use_model_config: bool) -> Path | None:
    configured_path = os.getenv("CLASS_CONFIG_PATH")
    if configured_path:
        return Path(configured_path)
    if use_model_config:
        return None
    return _default_class_config_path()


@lru_cache(maxsize=32)
def get_room_summary_service(
    model_name: str | None = None,
    confidence_threshold: float | None = None,
) -> RoomSummaryService:
    configured_model_path = os.getenv("YOLO_MODEL_PATH")
    requested_profile = model_name or os.getenv("YOLO_MODEL_PROFILE")
    # Prefer the profile/config flow unless a concrete model path is explicitly supplied.
    use_model_config = requested_profile is not None or configured_model_path is None
    fallback_confidence_threshold = float(os.getenv("YOLO_CONFIDENCE_THRESHOLD", "0.25"))

    return RoomSummaryService(
        model_path=(
            Path(configured_model_path)
            if configured_model_path
            else ROOM_SUMMARY_DIR / "models" / "yolo11n.pt"
        ),
        model_config_path=_default_model_config_path() if use_model_config else None,
        model_profile=requested_profile,
        class_config_path=_resolve_class_config_path(use_model_config),
        confidence_threshold=confidence_threshold,
        fallback_confidence_threshold=fallback_confidence_threshold,
        vector_palette_size=int(os.getenv("VECTOR_PALETTE_SIZE", "8")),
        max_vector_width=int(os.getenv("MAX_VECTOR_WIDTH", "480")),
    )


async def _decode_upload_images(images: list[UploadFile]) -> list[RoomImageInput]:
    if len(images) != 4:
        raise ValueError("Exactly 4 images are required.")

    decoded_images: list[RoomImageInput] = []
    for image_index, image in enumerate(images, start=1):
        direction = CLOCKWISE_DIRECTIONS[image_index - 1]
        payload = await image.read()
        if not payload:
            raise ValueError(f"Uploaded {direction} image is empty.")

        buffer = np.frombuffer(payload, dtype=np.uint8)
        frame = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError(
                f"Uploaded file {image.filename or f'image_{image_index}'} is not a valid image."
            )

        decoded_images.append(
            RoomImageInput(
                direction=direction,
                source_name=image.filename or f"{direction}.png",
                frame=frame,
            )
        )

    return decoded_images


def _ordered_upload_images(
    north_image: UploadFile,
    east_image: UploadFile,
    south_image: UploadFile,
    west_image: UploadFile,
) -> list[UploadFile]:
    return [north_image, east_image, south_image, west_image]


async def _run_room_summary(
    images: list[UploadFile],
    model_name: str | None,
    confidence_threshold: float | None,
) -> dict[str, object]:
    try:
        decoded_images = await _decode_upload_images(images)
        return get_room_summary_service(model_name, confidence_threshold).summarize_images(
            images=decoded_images,
        ).to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Room summary failed: {exc}") from exc
    finally:
        for image in images:
            await image.close()


async def _run_named_room_summary(
    room_name: str,
    images: list[UploadFile],
    model_name: str | None,
    confidence_threshold: float | None,
) -> dict[str, object]:
    try:
        decoded_images = await _decode_upload_images(images)
        return get_room_summary_service(model_name, confidence_threshold).summarize_room(
            room_name=room_name,
            images=decoded_images,
        ).to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Room summary failed: {exc}") from exc
    finally:
        for image in images:
            await image.close()


def _get_room_names() -> dict[str, list[str]]:
    try:
        return {
            "names": get_room_summary_service().list_room_names(
                conn=get_neo4j_driver(),
            )
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


async def _run_room_object_detection_setup(
    room_name: str,
    images: list[UploadFile],
    model_name: str | None,
    confidence_threshold: float | None,
    stored_image_count: int | None,
    stored_views: list[str] | None,
) -> dict[str, object]:
    try:
        decoded_images = await _decode_upload_images(images)
        return get_room_summary_service(
            model_name,
            confidence_threshold,
        ).setup_room_object_detection(
            room_name=room_name,
            images=decoded_images,
            conn=get_neo4j_driver(),
            stored_image_count=stored_image_count,
            stored_views=stored_views,
        ).to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Room object detection setup failed: {exc}",
        ) from exc
    finally:
        for image in images:
            await image.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_neo4j_driver()
    yield
    close_neo4j()


app = FastAPI(
    title="Indoor Room Summary API",
    version="0.1.0",
    description=(
        "Upload four room images captured from different directions and receive four "
        "vectorized room summaries plus best-effort object counts. Upload the images "
        "in clockwise order: north, east, south, west."
    ),
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get(f"{PREFIX}/room-summary/rooms")
def get_room_names() -> dict[str, list[str]]:
    return _get_room_names()


@app.get(f"{PREFIX}/room-summary/similarity")
def compare_rooms_similarity(
    room_a: str = Query(..., description="First room name to compare."),
    room_b: str = Query(..., description="Second room name to compare."),
    mode: str = Query(
        "room",
        description="'room' for pooled embedding cosine, 'max_view' for best per-view pair.",
    ),
) -> dict[str, object]:
    try:
        return get_room_summary_service().compare_rooms(
            room_a=room_a,
            room_b=room_b,
            conn=get_neo4j_driver(),
            mode=mode,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Similarity failed: {exc}") from exc


@app.get(f"{PREFIX}/room-summary/similarity/nearest")
def nearest_rooms(
    room: str = Query(..., description="Anchor room name."),
    top_k: int = Query(5, ge=1, le=100, description="Number of nearest rooms to return."),
    include_self: bool = Query(False, description="Include the anchor room in results."),
) -> dict[str, object]:
    try:
        return get_room_summary_service().nearest_rooms(
            room=room,
            conn=get_neo4j_driver(),
            top_k=top_k,
            include_self=include_self,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Nearest rooms failed: {exc}") from exc


@app.post(f"{PREFIX}/room-summary/similarity/ad-hoc")
async def compare_two_images(
    image_a: UploadFile = File(..., description="First image."),
    image_b: UploadFile = File(..., description="Second image."),
) -> dict[str, object]:
    images = [image_a, image_b]
    try:
        frames: list[np.ndarray] = []
        for index, upload in enumerate(images, start=1):
            payload = await upload.read()
            if not payload:
                raise ValueError(f"Uploaded image_{index} is empty.")
            buffer = np.frombuffer(payload, dtype=np.uint8)
            frame = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
            if frame is None:
                raise ValueError(
                    f"Uploaded file {upload.filename or f'image_{index}'} is not a valid image."
                )
            frames.append(frame)
        return get_room_summary_service().compare_frames(frames[0], frames[1])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Image comparison failed: {exc}") from exc
    finally:
        for image in images:
            await image.close()


@app.post(f"{PREFIX}/room-summary")
async def summarize_room(
    north_image: UploadFile = File(..., description="First image in clockwise order: north."),
    east_image: UploadFile = File(..., description="Second image in clockwise order: east."),
    south_image: UploadFile = File(..., description="Third image in clockwise order: south."),
    west_image: UploadFile = File(..., description="Fourth image in clockwise order: west."),
    model_name: str | None = Query(
        None,
        description="Optional profile name from modelConfig.cfg, for example nano or small.",
    ),
    confidence_threshold: float | None = Query(
        None,
        ge=0.0,
        le=1.0,
        description="Optional YOLO confidence threshold override for this request.",
    ),
) -> dict[str, object]:
    return await _run_room_summary(
        images=_ordered_upload_images(north_image, east_image, south_image, west_image),
        model_name=model_name,
        confidence_threshold=confidence_threshold,
    )


@app.post(f"{PREFIX}/room-summary/by-room")
async def summarize_named_room(
    room_name: str = Form(
        ...,
        description="Room name for the uploaded image set.",
    ),
    north_image: UploadFile = File(..., description="First image in clockwise order: north."),
    east_image: UploadFile = File(..., description="Second image in clockwise order: east."),
    south_image: UploadFile = File(..., description="Third image in clockwise order: south."),
    west_image: UploadFile = File(..., description="Fourth image in clockwise order: west."),
    model_name: str | None = Query(
        None,
        description="Optional profile name from modelConfig.cfg, for example nano or small.",
    ),
    confidence_threshold: float | None = Query(
        None,
        ge=0.0,
        le=1.0,
        description="Optional YOLO confidence threshold override for this request.",
    ),
) -> dict[str, object]:
    return await _run_named_room_summary(
        room_name=room_name,
        images=_ordered_upload_images(north_image, east_image, south_image, west_image),
        model_name=model_name,
        confidence_threshold=confidence_threshold,
    )


@app.post(
    f"{PREFIX}/room-summary/room-objects/setup",
    summary="Room object detection setup",
)
async def setup_room_object_detection(
    room_name: str = Form(
        ...,
        description="Room name from Neo4j for the uploaded image set.",
    ),
    north_image: UploadFile = File(..., description="First image in clockwise order: north."),
    east_image: UploadFile = File(..., description="Second image in clockwise order: east."),
    south_image: UploadFile = File(..., description="Third image in clockwise order: south."),
    west_image: UploadFile = File(..., description="Fourth image in clockwise order: west."),
    model_name: str | None = Query(
        None,
        description="Optional profile name from modelConfig.cfg, for example nano or small.",
    ),
    confidence_threshold: float | None = Query(
        None,
        ge=0.0,
        le=1.0,
        description="Optional YOLO confidence threshold override for this request.",
    ),
    stored_image_count: int | None = Query(
        None,
        ge=1,
        le=4,
        description=(
            "Optional number of uploaded images to store in Neo4j. "
            "If omitted, all four are stored. If this is set without stored_views, "
            "that many views are selected at random."
        ),
    ),
    stored_views: list[str] | None = Query(
        None,
        description=(
            "Optional compass directions to store in Neo4j, for example "
            "stored_views=north&stored_views=west. If provided, "
            "stored_image_count must be omitted or match the number of selected views."
        ),
    ),
) -> dict[str, object]:
    return await _run_room_object_detection_setup(
        room_name=room_name,
        images=_ordered_upload_images(north_image, east_image, south_image, west_image),
        model_name=model_name,
        confidence_threshold=confidence_threshold,
        stored_image_count=stored_image_count,
        stored_views=stored_views,
    )
