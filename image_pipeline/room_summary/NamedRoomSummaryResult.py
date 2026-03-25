from dataclasses import dataclass

from .ViewSummary import ViewSummary


@dataclass(slots=True)
class NamedRoomSummaryResult:
    room_name: str
    room_summary: list[ViewSummary]

    def to_dict(self) -> dict[str, object]:
        return {
            "room_name": self.room_name,
            "room_summary": [view.to_summary_dict() for view in self.room_summary],
        }
