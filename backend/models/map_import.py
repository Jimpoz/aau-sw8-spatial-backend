from typing import Optional
from pydantic import BaseModel
from models.enums import SpaceType


class SpaceImport(BaseModel):
    id: str
    display_name: str
    short_name: Optional[str] = None
    space_type: SpaceType
    centroid_x: Optional[float] = None
    centroid_y: Optional[float] = None
    polygon: Optional[list[list[float]]] = None
    width_m: Optional[float] = None
    length_m: Optional[float] = None
    area_m2: Optional[float] = None
    is_accessible: bool = True
    is_navigable: bool = True
    is_outdoor: bool = False
    capacity: Optional[int] = None
    tags: list[str] = []
    metadata: Optional[dict] = None
    subspaces: list["SpaceImport"] = []


SpaceImport.model_rebuild()


class FloorImport(BaseModel):
    id: str
    floor_index: int
    display_name: str
    elevation_m: Optional[float] = None
    floor_plan_url: Optional[str] = None
    floor_plan_scale: Optional[float] = None
    floor_plan_origin_x: Optional[float] = None
    floor_plan_origin_y: Optional[float] = None
    spaces: list[SpaceImport] = []


class BuildingImport(BaseModel):
    id: str
    name: str
    short_name: Optional[str] = None
    address: Optional[str] = None
    origin_lat: Optional[float] = None
    origin_lng: Optional[float] = None
    origin_bearing: float = 0.0
    floor_count: Optional[int] = None
    floors: list[FloorImport] = []


class ConnectionNodeImport(BaseModel):
    id: str
    display_name: str = "Door"
    space_type: SpaceType = SpaceType.DOOR_STANDARD
    connects: list[str]  # exactly 2 space IDs
    centroid_x: Optional[float] = None
    centroid_y: Optional[float] = None
    polygon: Optional[list[list[float]]] = None
    is_accessible: bool = True
    tags: list[str] = []
    transition_time_s: Optional[float] = None


class CampusImport(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    buildings: list[BuildingImport] = []
    outdoor_spaces: list[SpaceImport] = []
    connections: list[ConnectionNodeImport] = []


class MapImportSchema(BaseModel):
    schema_version: str = "1.0"
    campus: CampusImport
