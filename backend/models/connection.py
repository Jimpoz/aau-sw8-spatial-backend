from typing import Optional
from pydantic import BaseModel
from models.enums import ConnectionType, DoorType


class ConnectionCreate(BaseModel):
    from_space_id: str
    to_space_id: str
    connection_type: ConnectionType
    weight: Optional[float] = None
    distance_m: Optional[float] = None
    is_accessible: bool = True
    door_type: DoorType = DoorType.NONE
    requires_access_level: Optional[str] = None
    transition_time_s: Optional[float] = None


class Connection(BaseModel):
    from_space_id: str
    to_space_id: str
    connection_type: ConnectionType
    weight: Optional[float] = None
    distance_m: Optional[float] = None
    is_accessible: bool = True
    door_type: DoorType = DoorType.NONE
    requires_access_level: Optional[str] = None
    transition_time_s: Optional[float] = None
