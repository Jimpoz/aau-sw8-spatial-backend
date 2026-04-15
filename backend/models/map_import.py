from typing import Optional, Union
from pydantic import BaseModel, Field, validator
from shared.models.enums import ConnectionType, DoorType, SpaceType


class SpaceImport(BaseModel):
    id: str
    display_name: str
    short_name: Optional[str] = None
    space_type: Union[SpaceType, str]  # Allow string that will be converted
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

    @validator('space_type', pre=True)
    def validate_space_type(cls, v):
        if isinstance(v, str):
            return SpaceType(v) 
        return v

    @validator('polygon', pre=True)
    def validate_polygon(cls, v):
        if v is None:
            return v
        if isinstance(v, list) and len(v) > 0:
            try:
                return [[float(coord[0]), float(coord[1])] for coord in v]
            except (IndexError, TypeError, ValueError):
                raise ValueError("Polygon must be a list of [x, y] coordinate pairs")
        return v


SpaceImport.model_rebuild()


class FloorImport(BaseModel):
    id: str
    floor_index: int
    display_name: str
    elevation_m: Optional[float] = None
    floor_plan_url: Optional[str] = None
    floor_plan_scale: Optional[float] = Field(default=1.0)
    floor_plan_origin_x: Optional[float] = Field(default=0.0)
    floor_plan_origin_y: Optional[float] = Field(default=0.0)
    spaces: list[SpaceImport] = []
    floor_plan_bounds: Optional[list[list[float]]] = None 


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
    building_bounds: Optional[list[list[float]]] = None


class ConnectionImport(BaseModel):
    from_space_id: str
    to_space_id: str
    connection_type: Union[ConnectionType, str]
    is_accessible: bool = True
    door_type: Union[DoorType, str, None] = None
    requires_access_level: Optional[str] = None
    transition_time_s: Optional[float] = None
    weight_override: Optional[float] = None

    @validator('connection_type', pre=True)
    def validate_connection_type(cls, v):
        if isinstance(v, str):
            return ConnectionType(v)
        return v

    @validator('door_type', pre=True)
    def validate_door_type(cls, v):
        if v is None or isinstance(v, DoorType):
            return v
        if isinstance(v, str):
            if v.upper() == "NONE":
                return None
            return DoorType(v)
        return v


class CampusImport(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    buildings: list[BuildingImport] = []
    outdoor_spaces: list[SpaceImport] = []
    connections: list[ConnectionImport] = []


class MapImportSchema(BaseModel):
    schema_version: str = "1.0"
    campus: CampusImport
