from functools import lru_cache
from typing import Sequence

import cv2
import numpy as np

from .runtime_env import configure_runtime_env

# open_clip imports torch during module init; cache dirs must exist first.
configure_runtime_env()

import open_clip  # noqa: E402
import torch  # noqa: E402
from PIL import Image  # noqa: E402


CLIP_MODEL_NAME = "ViT-B-32"
CLIP_PRETRAINED = "laion2b_s34b_b79k"
MODEL_ID = f"open_clip/{CLIP_MODEL_NAME}/{CLIP_PRETRAINED}"
EMBEDDING_DIM = 512


@lru_cache(maxsize=1)
def _load_model(model_name: str, pretrained: str) -> tuple:
    model, _, preprocess = open_clip.create_model_and_transforms(
        model_name,
        pretrained=pretrained,
    )
    model.eval()
    return model, preprocess


class RoomImageEmbedder:
    def __init__(
        self,
        model_name: str = CLIP_MODEL_NAME,
        pretrained: str = CLIP_PRETRAINED,
    ) -> None:
        self.model_name = model_name
        self.pretrained = pretrained
        self.model_id = f"open_clip/{model_name}/{pretrained}"

    def _model_and_preprocess(self) -> tuple:
        return _load_model(self.model_name, self.pretrained)

    @staticmethod
    def _bgr_to_pil(frame: np.ndarray) -> Image.Image:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        return Image.fromarray(rgb)

    def embed(self, frame: np.ndarray) -> np.ndarray:
        return self.embed_batch([frame])[0]

    def embed_batch(self, frames: Sequence[np.ndarray]) -> np.ndarray:
        if not frames:
            return np.empty((0, EMBEDDING_DIM), dtype=np.float32)

        model, preprocess = self._model_and_preprocess()
        tensors = [preprocess(self._bgr_to_pil(frame)) for frame in frames]
        batch = torch.stack(tensors, dim=0)

        with torch.no_grad():
            features = model.encode_image(batch)
            features = features / features.norm(dim=-1, keepdim=True).clamp_min(1e-12)

        return features.cpu().numpy().astype(np.float32)

    @staticmethod
    def mean_pool(vectors: np.ndarray) -> np.ndarray:
        if vectors.size == 0:
            return np.zeros(EMBEDDING_DIM, dtype=np.float32)
        pooled = vectors.mean(axis=0)
        norm = float(np.linalg.norm(pooled))
        if norm < 1e-12:
            return pooled.astype(np.float32)
        return (pooled / norm).astype(np.float32)
