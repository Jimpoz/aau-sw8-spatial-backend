from pathlib import Path
from random import sample
from typing import Any, Sequence

from .model_config import resolve_model_selection
from .NamedRoomSummaryResult import NamedRoomSummaryResult
from .Neo4jQueryRunner import Neo4jQueryRunner
from .RoomImageInput import RoomImageInput
from .RoomObjectDetectionSetupResult import RoomObjectDetectionSetupResult
from .RoomObjectDetector import RoomObjectDetector
from .RoomSummaryRepository import RoomSummaryRepository
from .RoomSummaryResult import RoomSummaryResult
from .RoomVectorizer import RoomVectorizer
from .ViewSummary import ViewSummary


class RoomSummaryService:
    def __init__(
        self,
        model_path: str | Path | None = None,
        model_config_path: str | Path | None = None,
        model_profile: str | None = None,
        class_config_path: str | Path | None = None,
        confidence_threshold: float | None = None,
        fallback_confidence_threshold: float = 0.25,
        vector_palette_size: int = 8,
        max_vector_width: int = 480,
    ) -> None:
        selection = resolve_model_selection(
            config_path=model_config_path,
            requested_profile=model_profile,
            fallback_model_path=model_path,
            fallback_confidence_threshold=fallback_confidence_threshold,
        )

        explicit_class_config = (
            Path(class_config_path) if class_config_path is not None else None
        )
        # Let each request override confidence while still inheriting the profile default.
        effective_confidence_threshold = (
            confidence_threshold
            if confidence_threshold is not None
            else selection.confidence_threshold
        )
        self._detector = RoomObjectDetector(
            model_path=selection.model_path,
            class_config_path=explicit_class_config or selection.class_config_path,
            confidence_threshold=effective_confidence_threshold,
        )
        self._vectorizer = RoomVectorizer(
            vector_palette_size=vector_palette_size,
            max_vector_width=max_vector_width,
        )

        self.model_profile = selection.profile_name
        self.model_path = self._detector.model_path
        self.class_config_path = self._detector.class_config_path
        self.confidence_threshold = self._detector.confidence_threshold

    def list_room_names(self, conn: Any) -> list[str]:
        return self._room_summary_repository(conn).list_room_names()

    def summarize_images(
        self,
        images: Sequence[RoomImageInput],
        expected_views: int = 4,
    ) -> RoomSummaryResult:
        if len(images) != expected_views:
            raise ValueError(f"Exactly {expected_views} images are required.")

        overall_object_counts: dict[str, int] = {}
        views: list[ViewSummary] = []

        # Build the detection counts and the SVG summary from the same frame in one pass.
        for view_index, image in enumerate(images, start=1):
            counts = dict(sorted(self._detector.detect_counts(image.frame).items()))
            overall_object_counts = self._merge_object_counts(
                overall_object_counts,
                counts,
            )

            views.append(
                ViewSummary(
                    view_index=view_index,
                    source_name=image.source_name,
                    object_counts=counts,
                    svg=self._vectorizer.vectorize_view(
                        frame=image.frame,
                        view_index=view_index,
                        source_name=image.source_name,
                        object_counts=counts,
                    ),
                )
            )

        return RoomSummaryResult(
            model_profile=self.model_profile,
            model_path=str(self.model_path),
            overall_object_counts=overall_object_counts,
            views=views,
        )

    def summarize_room(
        self,
        room_name: str,
        images: Sequence[RoomImageInput],
        expected_views: int = 4,
    ) -> NamedRoomSummaryResult:
        normalized_room_name = room_name.strip()
        if not normalized_room_name:
            raise ValueError("Room name is required.")

        result = self.summarize_images(
            images=images,
            expected_views=expected_views,
        )
        return NamedRoomSummaryResult(
            room_name=normalized_room_name,
            room_summary=result.views,
        )

    def setup_room_object_detection(
        self,
        room_name: str,
        images: Sequence[RoomImageInput],
        conn: Any,
        expected_views: int = 4,
        stored_image_count: int | None = None,
        stored_views: Sequence[str] | None = None,
    ) -> RoomObjectDetectionSetupResult:
        normalized_room_name = room_name.strip()
        if not normalized_room_name:
            raise ValueError("Room name is required.")

        result = self.summarize_images(
            images=images,
            expected_views=expected_views,
        )
        room_objects = self._build_room_objects(result.overall_object_counts)
        room_object_counts = self._build_room_object_counts(
            result.overall_object_counts,
        )
        stored_images, selected_views = self._select_stored_images(
            images=images,
            stored_image_count=stored_image_count,
            stored_views=stored_views,
        )
        # Persist both the generated summaries and the original frames for later graph queries.
        stored_room_name = self._room_summary_repository(conn).replace_room_detection_setup(
            room_name=normalized_room_name,
            room_objects=room_objects,
            room_object_counts=room_object_counts,
            room_images=[
                self._vectorizer.embed_frame_svg(image.frame, image.source_name)
                for image in stored_images
            ],
            stored_views=selected_views,
            room_summary=[view.to_dict() for view in result.views],
        )
        return RoomObjectDetectionSetupResult(
            room_name=stored_room_name,
            room_objects=room_objects,
            room_object_counts=room_object_counts,
            stored_image_count=len(stored_images),
            stored_views=selected_views,
            room_summary=result.views,
        )

    @staticmethod
    def _merge_object_counts(
        current_counts: dict[str, int],
        new_counts: dict[str, int],
    ) -> dict[str, int]:
        merged_counts = dict(current_counts)
        for label, count in new_counts.items():
            merged_counts[label] = merged_counts.get(label, 0) + count
        return dict(sorted(merged_counts.items()))

    @staticmethod
    def _build_room_objects(object_counts: dict[str, int]) -> list[str]:
        return [label for label, count in sorted(object_counts.items()) if count > 0]

    @staticmethod
    def _build_room_object_counts(object_counts: dict[str, int]) -> dict[str, int]:
        return dict(sorted(object_counts.items()))

    @staticmethod
    def _select_stored_images(
        images: Sequence[RoomImageInput],
        stored_image_count: int | None,
        stored_views: Sequence[str] | None,
    ) -> tuple[list[RoomImageInput], list[str]]:
        total_images = len(images)
        available_directions = [image.direction for image in images]
        direction_to_image = {image.direction: image for image in images}

        if stored_views:
            selected_views: list[str] = []
            seen_views: set[str] = set()
            for raw_view in stored_views:
                view = raw_view.strip().lower()
                if view not in direction_to_image:
                    raise ValueError(
                        "stored_views must be chosen from: "
                        + ", ".join(available_directions)
                        + "."
                    )
                if view in seen_views:
                    raise ValueError("stored_views must not contain duplicates.")
                seen_views.add(view)
                selected_views.append(view)

            if (
                stored_image_count is not None
                and stored_image_count != len(selected_views)
            ):
                raise ValueError(
                    "stored_image_count must match the number of stored_views."
                )

            return (
                [direction_to_image[view] for view in selected_views],
                selected_views,
            )

        selected_count = stored_image_count or total_images
        if selected_count < 1 or selected_count > total_images:
            raise ValueError(f"stored_image_count must be between 1 and {total_images}.")

        if selected_count == total_images:
            selected_views = available_directions
        else:
            # Randomly store a subset when the caller requests fewer images but no explicit views.
            selected_views = sorted(sample(available_directions, k=selected_count))

        return (
            [direction_to_image[view] for view in selected_views],
            selected_views,
        )

    @staticmethod
    def _room_summary_repository(conn: Any) -> RoomSummaryRepository:
        return RoomSummaryRepository(
            Neo4jQueryRunner(conn),
        )
