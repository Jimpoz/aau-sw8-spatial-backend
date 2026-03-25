from dataclasses import dataclass

from .ViewSummary import ViewSummary


@dataclass(slots=True)
class RoomObjectDetectionSetupResult:
    room_name: str
    room_objects: list[str]
    room_object_counts: dict[str, int]
    stored_image_count: int
    stored_views: list[str]
    room_summary: list[ViewSummary]

    def to_dict(self) -> dict[str, object]:
        return {
            "room_name": self.room_name,
            "room_objects": self.room_objects,
            "room_object_counts": self.room_object_counts,
            "stored_image_count": self.stored_image_count,
            "stored_views": self.stored_views,
            "room_summary": [view.to_summary_dict() for view in self.room_summary],
        }
