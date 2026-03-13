from fastapi import APIRouter, Depends
from db import Database, get_db
from models.assistant import ChatRequest, ChatResponse
from services.assistant_service import AssistantService

router = APIRouter(prefix="/assistant", tags=["assistant"])

@router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest, db: Database = Depends(get_db)):
    service = AssistantService(db)
    result = await service.chat(request.user_query, request.campus_id)
    return result