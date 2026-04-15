from pydantic import BaseModel


class ChatRequest(BaseModel):
    user_query: str
    campus_id: str

class ChatResponse(BaseModel):
    answer: str
    sources: list[str] = []

class EmbedRequest(BaseModel):
    texts: list[str]

class EmbedResponse(BaseModel):
    vectors: list[list[float]]
