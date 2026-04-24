from dataclasses import dataclass, field

from .ViewSummary import ViewSummary


@dataclass(slots=True)
class RoomSummaryResult:
    model_profile: str | None
    model_path: str
    overall_object_counts: dict[str, int]
    overall_text_counts: dict[str, int]
    views: list[ViewSummary]
    embedding_model: str | None = None
    room_embedding: list[float] | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {
            "model": {
                "profile": self.model_profile,
                "path": self.model_path,
            },
            "overall_object_counts": self.overall_object_counts,
            "overall_text_counts": self.overall_text_counts,
            "counting_strategy": "aggregation across the four uploaded room images",
            "views": [view.to_dict() for view in self.views],
        }
        if self.embedding_model is not None:
            data["embedding_model"] = self.embedding_model
        if self.room_embedding is not None:
            data["room_embedding"] = self.room_embedding
        return data
