import asyncio
from fastapi import APIRouter
from models.assistant import EmbedRequest, EmbedResponse
from services.assistant_service import get_embedder

router = APIRouter(tags=["internal"])

@router.post("/internal/embed", response_model=EmbedResponse)
async def embed_texts(request: EmbedRequest):
    embedder = get_embedder()
    vectors = await asyncio.to_thread(
        lambda: embedder.encode(request.texts, convert_to_numpy=True).tolist()
    )
    return EmbedResponse(vectors=vectors)
