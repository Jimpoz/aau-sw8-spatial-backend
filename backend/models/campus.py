from typing import Optional
from pydantic import BaseModel

from shared.models.enums import EntityType


class OrganizationCreate(BaseModel):
    id: str
    name: str
    entity_type: EntityType = EntityType.OTHER
    description: Optional[str] = None


class Organization(BaseModel):
    id: str
    name: str
    entity_type: EntityType = EntityType.OTHER
    description: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class CampusCreate(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    organization_id: Optional[str] = None


class Campus(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    organization_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class BuildingCreate(BaseModel):
    id: str
    campus_id: str
    organization_id: Optional[str] = None
    name: str
    short_name: Optional[str] = None
    address: Optional[str] = None
    origin_lat: Optional[float] = None
    origin_lng: Optional[float] = None
    origin_bearing: float = 0.0
    floor_count: Optional[int] = None


class Building(BaseModel):
    id: str
    campus_id: str
    organization_id: Optional[str] = None
    name: str
    short_name: Optional[str] = None
    address: Optional[str] = None
    origin_lat: Optional[float] = None
    origin_lng: Optional[float] = None
    origin_bearing: Optional[float] = None
    floor_count: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class FloorCreate(BaseModel):
    id: str
    building_id: str
    floor_index: int
    display_name: str
    elevation_m: Optional[float] = None
    floor_plan_url: Optional[str] = None
    floor_plan_scale: Optional[float] = None
    floor_plan_origin_x: Optional[float] = None
    floor_plan_origin_y: Optional[float] = None


class Floor(BaseModel):
    id: str
    building_id: str
    floor_index: int
    display_name: str
    elevation_m: Optional[float] = None
    floor_plan_url: Optional[str] = None
    floor_plan_scale: Optional[float] = None
    floor_plan_origin_x: Optional[float] = None
    floor_plan_origin_y: Optional[float] = None
