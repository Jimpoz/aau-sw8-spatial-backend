from typing import Optional
from pydantic import BaseModel


class Landmark(BaseModel):
    """A user-registered visual landmark anchored to a Space.

    The landmark also stores where in the Space it physically is, so a
    visual match can place the user at the landmark itself instead of
    snapping them to the room's geometric centroid. Coordinates are
    optional for backwards compatibility with landmarks captured before
    the field was added.
    """
    id: str
    name: str
    space_id: str
    floor_id: Optional[str] = None
    building_id: Optional[str] = None
    campus_id: Optional[str] = None
    organization_id: Optional[str] = None
    image_width: Optional[int] = None
    image_height: Optional[int] = None
    centroid_x: Optional[float] = None
    centroid_y: Optional[float] = None
    centroid_lat: Optional[float] = None
    centroid_lng: Optional[float] = None
    created_by: Optional[str] = None
    created_at: Optional[str] = None

