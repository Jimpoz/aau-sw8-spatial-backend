from pydantic import BaseModel


class ChatRequest(BaseModel):
    user_query: str
    campus_id: str
    building_id: str | None = None
    user_lat: float | None = None
    user_lon: float | None = None
    floor_index: int | None = None
    # The forced/landmark snap (the red dot). When set it overrides GPS for
    # locating the user, so "where am I" reports the room the dot is in.
    current_location_space_id: str | None = None

class ChatResponse(BaseModel):
    answer: str
    sources: list[str] = []

class EmbedRequest(BaseModel):
    texts: list[str]

class EmbedResponse(BaseModel):
    vectors: list[list[float]]
