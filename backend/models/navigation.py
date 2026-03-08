from typing import Optional
from pydantic import BaseModel
from models.enums import ConnectionType, SpaceType


class RouteStep(BaseModel):
    space_id: str
    display_name: str
    space_type: SpaceType
    floor_index: Optional[int] = None
    building_id: Optional[str] = None
    centroid_x: Optional[float] = None
    centroid_y: Optional[float] = None
    connection_type: Optional[ConnectionType] = None
    instruction: Optional[str] = None
    cost: Optional[float] = None


class FloorChange(BaseModel):
    from_floor: Optional[int]
    to_floor: Optional[int]
    at_space_id: str
    connection_type: ConnectionType


class BuildingChange(BaseModel):
    from_building_id: Optional[str]
    to_building_id: Optional[str]
    at_space_id: str


class Route(BaseModel):
    from_space_id: str
    to_space_id: str
    total_cost: float
    total_distance_m: Optional[float] = None
    steps: list[RouteStep]
    floor_changes: list[FloorChange] = []
    building_changes: list[BuildingChange] = []
