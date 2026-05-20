from typing import Optional
from pydantic import BaseModel


class Landmark(BaseModel):
    """A user-registered visual landmark anchored to a Space."""
    id: str
    name: str
    space_id: str
    floor_id: Optional[str] = None
    building_id: Optional[str] = None
    campus_id: Optional[str] = None
    organization_id: Optional[str] = None
    image_width: Optional[int] = None
    image_height: Optional[int] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None

