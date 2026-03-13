from pydantic import BaseModel

class ChatRequest(BaseModel):
    user_query: str
    campus_id: str

class ChatResponse(BaseModel):
    answer: str
    sources: list[str] = []