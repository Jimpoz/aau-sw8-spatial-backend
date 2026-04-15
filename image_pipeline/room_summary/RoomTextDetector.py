from collections import Counter
from functools import lru_cache
from typing import Sequence

import numpy as np

from .runtime_env import configure_runtime_env

# EasyOCR imports torch during module init, so make sure cache dirs exist first.
configure_runtime_env()

import easyocr


@lru_cache(maxsize=4)
def _load_reader(languages: tuple[str, ...]) -> "easyocr.Reader":
    return easyocr.Reader(list(languages), gpu=False, verbose=False)


class RoomTextDetector:
    def __init__(
        self,
        languages: Sequence[str] | None = None,
        confidence_threshold: float = 0.4,
    ) -> None:
        self.languages: tuple[str, ...] = tuple(languages) if languages else ("en",)
        self.confidence_threshold = confidence_threshold

    def detect_text(self, frame: np.ndarray) -> Counter[str]:
        reader = _load_reader(self.languages)
        detections = reader.readtext(frame, detail=1)

        counts: Counter[str] = Counter()
        seen_keys: dict[str, str] = {}

        for _bbox, text, confidence in detections:
            if confidence < self.confidence_threshold:
                continue
            cleaned = text.strip()
            if not cleaned:
                continue
            # Deduplicate case-insensitively while keeping the first-seen casing as the label.
            key = cleaned.casefold()
            label = seen_keys.setdefault(key, cleaned)
            counts[label] += 1

        return counts
