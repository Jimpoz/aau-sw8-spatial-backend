from fastapi import APIRouter, Depends
from db import Database, get_db
from db_pg import PostgresDatabase, get_pg_db
from models.assistant import ChatRequest, ChatResponse
from services.assistant_service import AssistantService

router = APIRouter(prefix="/assistant", tags=["assistant"])

@router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(
    request: ChatRequest,
    db: Database = Depends(get_db),
    pg_db: PostgresDatabase = Depends(get_pg_db),
):
    service = AssistantService(db, pg_db=pg_db)
    result = await service.chat(
        request.user_query,
        request.campus_id,
        building_id=request.building_id,
        user_lat=request.user_lat,
        user_lon=request.user_lon,
    )
    return result
