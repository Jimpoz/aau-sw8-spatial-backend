from __future__ import annotations

import os
from typing import Optional

import httpx

from models.space import SpaceUpdate


_EMBED_TIMEOUT_S = 10.0


def _assistant_url() -> str:
    return os.getenv("ASSISTANT_URL", "http://assistant:8001").rstrip("/")


def embed_text(text: str) -> Optional[list[float]]:
    """Fetch a 384-d sentence-transformer vector for ``text``.
    Returns ``None`` on any failure - the caller treats regeneration as
    best-effort so a transient assistant outage doesn't fail a user's
    mapmaker save."""
    if not text:
        return None
    try:
        resp = httpx.post(
            f"{_assistant_url()}/internal/embed",
            json={"texts": [text]},
            timeout=_EMBED_TIMEOUT_S,
        )
        resp.raise_for_status()
        vectors = resp.json().get("vectors") or []
        if not vectors or not isinstance(vectors[0], list):
            return None
        return [float(v) for v in vectors[0]]
    except Exception as exc:
        print(f"[embed_client] embed failed: {exc}")
        return None


def maybe_regenerate_embedding(
    data: SpaceUpdate,
    existing: dict,
) -> SpaceUpdate:
    """When ``data`` touches display_name, space_type, or tags - the
    fields that feed the embedding text template - compute a fresh
    embedding and fold it into the returned ``SpaceUpdate``.

    Geometry-only edits (move, resize, render_order, accessibility
    toggle) skip this and keep the existing vector untouched. If the
    assistant call fails the original patch is returned as-is so the
    edit still goes through - the embedding just stays stale until
    the next successful regeneration.
    """
    if data.display_name is None and data.space_type is None and data.tags is None:
        return data

    new_name = (
        data.display_name
        if data.display_name is not None
        else (existing.get("display_name") or "")
    )
    new_type_raw = (
        data.space_type
        if data.space_type is not None
        else (existing.get("space_type") or "")
    )
    new_type = new_type_raw.value if hasattr(new_type_raw, "value") else str(new_type_raw)
    new_tags = data.tags if data.tags is not None else (existing.get("tags") or [])

    # Mirror the template used at import time
    # (import_service.py: f"{display_name}. Type: {space_type}. Tags: {' '.join(tags)}")
    # so a re-embedded room sits in the same vector space as a freshly
    # imported one - otherwise cosine similarity scores would drift
    # between imported and edited rooms.
    text_to_embed = f"{new_name}. Type: {new_type}. Tags: {' '.join(new_tags)}"
    vector = embed_text(text_to_embed)
    if vector is None:
        return data
    return data.model_copy(update={"embedding": vector})
