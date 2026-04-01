import json
from datetime import datetime, timezone
from typing import Any

from models.enums import SpaceType
from .Neo4jQueryRunner import Neo4jQueryRunner


class RoomSummaryRepository:
    _ROOM_SPACE_TYPES = (
        SpaceType.ROOM_GENERIC.value,
        SpaceType.ROOM_OFFICE.value,
        SpaceType.ROOM_CLASSROOM.value,
        SpaceType.ROOM_LECTURE_HALL.value,
        SpaceType.ROOM_LAB.value,
        SpaceType.ROOM_MEETING.value,
        SpaceType.ROOM_STORAGE.value,
        SpaceType.ROOM_UTILITY.value,
        SpaceType.RESTROOM.value,
        SpaceType.RESTROOM_ACCESSIBLE.value,
    )

    def __init__(self, query_runner: Neo4jQueryRunner) -> None:
        self._query_runner = query_runner

    @staticmethod
    def _normalize(text: str) -> str:
        return text.strip().lower()

    @staticmethod
    def _deserialize_metadata(raw_metadata: Any) -> dict[str, Any]:
        if isinstance(raw_metadata, dict):
            return dict(raw_metadata)
        if not isinstance(raw_metadata, str) or not raw_metadata.strip():
            return {}

        try:
            parsed = json.loads(raw_metadata)
        except json.JSONDecodeError:
            return {}

        return parsed if isinstance(parsed, dict) else {}

    def list_room_names(self) -> list[str]:
        rows = self._query_runner.run(
            """
            MATCH (space:Space)
            WHERE space.space_type IN $room_space_types
            RETURN coalesce(space.display_name, space.short_name, toString(space.id)) AS name
            ORDER BY name
            """,
            room_space_types=list(self._ROOM_SPACE_TYPES),
        )
        return sorted({str(row["name"]) for row in rows if row["name"] is not None})

    def replace_room_detection_setup(
        self,
        room_name: str,
        room_objects: list[str],
        room_object_counts: dict[str, int],
        room_images: list[str],
        stored_views: list[str],
        room_summary: list[dict[str, object]],
    ) -> str:
        rows = self._query_runner.run(
            """
            MATCH (space:Space)
            WHERE
              space.space_type IN $room_space_types
              AND (
                toLower(trim(coalesce(space.display_name, ""))) = $normalized_room_name
                OR toLower(trim(coalesce(space.short_name, ""))) = $normalized_room_name
                OR toLower(trim(coalesce(toString(space.id), ""))) = $normalized_room_name
              )
            WITH space,
                 CASE
                   WHEN toLower(trim(coalesce(space.display_name, ""))) = $normalized_room_name THEN 0
                   WHEN toLower(trim(coalesce(space.short_name, ""))) = $normalized_room_name THEN 1
                   ELSE 2
                 END AS match_rank
            ORDER BY match_rank, coalesce(space.display_name, space.short_name, toString(space.id))
            LIMIT 1
            RETURN space.id AS space_id,
                   coalesce(space.display_name, space.short_name, toString(space.id)) AS room_name,
                   space.metadata AS metadata
            """,
            normalized_room_name=self._normalize(room_name),
            room_space_types=list(self._ROOM_SPACE_TYPES),
        )

        if not rows:
            raise LookupError(f"Room {room_name!r} was not found in Neo4j.")

        space_id = str(rows[0]["space_id"])
        updated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        metadata = self._deserialize_metadata(rows[0].get("metadata"))
        metadata["room_summary"] = {
            "room_objects": room_objects,
            "room_object_counts": dict(sorted(room_object_counts.items())),
            "room_images": room_images,
            "stored_image_count": len(room_images),
            "stored_views": stored_views,
            "views": room_summary,
            "updated_at": updated_at,
        }
        room_object_counts_json = json.dumps(
            dict(sorted(room_object_counts.items())),
            separators=(",", ":"),
        )

        stored_rows = self._query_runner.run(
            """
            MATCH (space:Space {id: $space_id})
            REMOVE space.roomObjects,
                   space.roomObjectCountsJson,
                   space.roomImages,
                   space.roomSummaryUpdatedAt
            SET space.room_objects = $room_objects,
                space.room_object_counts_json = $room_object_counts_json,
                space.room_images = $room_images,
                space.room_summary_updated_at = $room_summary_updated_at,
                space.metadata = $metadata
            RETURN coalesce(space.display_name, space.short_name, toString(space.id)) AS room_name
            """,
            space_id=space_id,
            room_objects=room_objects,
            room_object_counts_json=room_object_counts_json,
            room_images=room_images,
            room_summary_updated_at=updated_at,
            metadata=json.dumps(metadata, separators=(",", ":"), sort_keys=True),
        )

        stored_room_name = stored_rows[0]["room_name"] if stored_rows else rows[0]["room_name"]
        return str(stored_room_name or room_name)
