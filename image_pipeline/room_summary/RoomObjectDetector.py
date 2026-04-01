from collections import Counter
from functools import lru_cache
from pathlib import Path

import numpy as np

from .runtime_env import configure_runtime_env

# Ultralytics/Torch inspects runtime flags during import, so configure them first.
configure_runtime_env()

from ultralytics import YOLO

from .class_labels import load_class_labels, resolve_class_label
from .model_download import ensure_model_path


@lru_cache(maxsize=4)
def _load_model(model_path: str) -> YOLO:
    return YOLO(model_path)


class RoomObjectDetector:
    def __init__(
        self,
        model_path: str | Path,
        class_config_path: str | Path | None = None,
        confidence_threshold: float = 0.25,
    ) -> None:
        self.model_path = ensure_model_path(model_path)
        self.class_config_path = (
            Path(class_config_path) if class_config_path is not None else None
        )
        self.confidence_threshold = confidence_threshold

        self._validate_model_path()
        self.class_overrides = load_class_labels(self.class_config_path)

    def detect_counts(self, frame: np.ndarray) -> Counter[str]:
        result = _load_model(str(self.model_path)).predict(
            source=frame,
            conf=self.confidence_threshold,
            verbose=False,
        )[0]

        counts: Counter[str] = Counter()
        model_names = result.names

        for box in result.boxes:
            class_id = int(box.cls[0].item())
            # Allow a project-specific class config to rename or remap model labels.
            label = resolve_class_label(
                model_names=model_names,
                class_id=class_id,
                overrides=self.class_overrides,
            )
            counts[label] += 1

        return counts

    def _validate_model_path(self) -> None:
        if not self.model_path.exists():
            raise FileNotFoundError(f"YOLO model not found: {self.model_path}")

        if self.model_path.stat().st_size < 1024:
            header = self.model_path.read_text(encoding="utf-8", errors="ignore")[:128]
            if header.startswith("version https://git-lfs.github.com/spec/v1"):
                raise FileNotFoundError(
                    "YOLO model file is a Git LFS pointer, not the real weights: "
                    f"{self.model_path}. Pull the actual .pt weights with Git LFS "
                    "or point the selected profile/path to a real model file."
                )
