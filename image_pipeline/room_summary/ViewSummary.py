from dataclasses import dataclass


@dataclass(slots=True)
class ViewSummary:
    view_index: int
    source_name: str
    object_counts: dict[str, int]
    text_counts: dict[str, int]
    svg: str

    def to_summary_dict(self) -> dict[str, object]:
        return {
            "view_index": self.view_index,
            "svg": self.svg,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "view_index": self.view_index,
            "source_name": self.source_name,
            "object_counts": self.object_counts,
            "text_counts": self.text_counts,
            "svg": self.svg,
        }
