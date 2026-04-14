from dataclasses import dataclass

from .ViewSummary import ViewSummary


@dataclass(slots=True)
class RoomSummaryResult:
    model_profile: str | None
    model_path: str
    overall_object_counts: dict[str, int]
    overall_text_counts: dict[str, int]
    views: list[ViewSummary]

    def to_dict(self) -> dict[str, object]:
        return {
            "model": {
                "profile": self.model_profile,
                "path": self.model_path,
            },
            "overall_object_counts": self.overall_object_counts,
            "overall_text_counts": self.overall_text_counts,
            "counting_strategy": "aggregation across the four uploaded room images",
            "views": [view.to_dict() for view in self.views],
        }
