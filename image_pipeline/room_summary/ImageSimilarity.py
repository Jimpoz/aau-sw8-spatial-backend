from typing import Sequence

import numpy as np


def _as_vector(value: Sequence[float] | np.ndarray) -> np.ndarray:
    return np.asarray(value, dtype=np.float32).reshape(-1)


def cosine(a: Sequence[float] | np.ndarray, b: Sequence[float] | np.ndarray) -> float:
    va = _as_vector(a)
    vb = _as_vector(b)
    if va.shape != vb.shape or va.size == 0:
        return 0.0
    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na < 1e-12 or nb < 1e-12:
        return 0.0
    return float(np.dot(va, vb) / (na * nb))


def best_view_match(
    a_views: dict[str, Sequence[float]],
    b_views: dict[str, Sequence[float]],
) -> tuple[float, str | None, str | None]:
    best_score = -1.0
    best_a: str | None = None
    best_b: str | None = None
    for a_name, a_vec in a_views.items():
        for b_name, b_vec in b_views.items():
            score = cosine(a_vec, b_vec)
            if score > best_score:
                best_score = score
                best_a = a_name
                best_b = b_name
    if best_a is None:
        return 0.0, None, None
    return best_score, best_a, best_b
