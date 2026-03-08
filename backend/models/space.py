from typing import Optional, Any
from pydantic import BaseModel
from models.enums import SpaceType


class SpaceCreate(BaseModel):
    id: str
    display_name: str
    short_name: Optional[str] = None
    space_type: SpaceType
    floor_index: Optional[int] = None
    building_id: Optional[str] = None
    campus_id: Optional[str] = None
    floor_id: Optional[str] = None
    parent_space_id: Optional[str] = None
    width_m: Optional[float] = None
    length_m: Optional[float] = None
    area_m2: Optional[float] = None
    centroid_x: Optional[float] = None
    centroid_y: Optional[float] = None
    polygon: Optional[list[list[float]]] = None
    is_accessible: bool = True
    is_navigable: bool = True
    is_outdoor: bool = False
    capacity: Optional[int] = None
    tags: list[str] = []
    metadata: Optional[dict] = None


class SpaceUpdate(BaseModel):
    display_name: Optional[str] = None
    short_name: Optional[str] = None
    space_type: Optional[SpaceType] = None
    centroid_x: Optional[float] = None
    centroid_y: Optional[float] = None
    polygon: Optional[list[list[float]]] = None
    is_accessible: Optional[bool] = None
    is_navigable: Optional[bool] = None
    is_outdoor: Optional[bool] = None
    capacity: Optional[int] = None
    tags: Optional[list[str]] = None
    metadata: Optional[dict[str, Any]] = None


class Space(BaseModel):
    id: str
    display_name: str
    short_name: Optional[str] = None
    space_type: SpaceType
    floor_index: Optional[int] = None
    building_id: Optional[str] = None
    campus_id: Optional[str] = None
    width_m: Optional[float] = None
    length_m: Optional[float] = None
    area_m2: Optional[float] = None
    centroid_x: Optional[float] = None
    centroid_y: Optional[float] = None
    polygon: Optional[list[list[float]]] = None
    is_accessible: bool = True
    is_navigable: bool = True
    is_outdoor: bool = False
    capacity: Optional[int] = None
    tags: list[str] = []
    metadata: Optional[dict] = None
