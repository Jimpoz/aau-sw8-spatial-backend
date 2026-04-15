from pydantic import BaseModel
from models.enums import SpaceType


class ConnectionCreate(BaseModel):
    from_space_id: str
    to_space_id: str
    space_type: SpaceType = SpaceType.DOOR_STANDARD
    display_name: str = "Door"
    is_accessible: bool = True


class Connection(BaseModel):
    from_space_id: str
    to_space_id: str
    door_node_id: str
