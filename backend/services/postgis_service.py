"""
Schema:
  organizations   (entity that owns campuses, e.g. a university/corporation)
  campuses        (belongs to an organization)
  buildings       (belongs to a campus + organization)
  building_spaces (base)
    ├── rooms
    ├── amenities
    ├── corridors
    ├── stairs
    ├── elevators
    ├── doors
    ├── connectors
    ├── outdoor_spaces
    └── other_spaces
  floors          (floor plans + bounds)
  imports         (raw import JSON payloads)
"""

from sqlalchemy import (
    create_engine, Column, String, Float, Integer, DateTime, Boolean, ForeignKey, Enum as SQLEnum,
    or_,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.dialects.postgresql import JSONB
from geoalchemy2 import Geometry
from geoalchemy2.shape import to_shape
from datetime import datetime
from urllib.parse import urlparse, urlunparse, quote
import json
import enum
from core.config import settings
from shapely.geometry import Polygon

Base = declarative_base()


class EntityType(enum.Enum):
    """Organization entity types."""
    UNIVERSITY = "UNIVERSITY"
    CORPORATION = "CORPORATION"
    PUBLIC_HEALTH = "PUBLIC_HEALTH"
    HOSPITAL = "HOSPITAL"
    GOVERNMENT = "GOVERNMENT"
    OTHER = "OTHER"


def _safe_db_url(raw_url: str) -> str:
    try:
        parsed = urlparse(raw_url)
        if parsed.username is None or parsed.password is None:
            return raw_url
        userinfo = f"{quote(parsed.username, safe='%')}:{quote(parsed.password, safe='%')}"
        netloc = f"{userinfo}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse((
            parsed.scheme, netloc, parsed.path,
            parsed.params, parsed.query, parsed.fragment,
        ))
    except Exception as exc:
        print(f"[PostGISService] Could not encode DB URL: {exc}")
        return raw_url


# --- Organization & Campus Models ---

class Organization(Base):
    """Organization/entity that owns one or more campuses."""
    __tablename__ = "organizations"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    entity_type = Column(SQLEnum(EntityType), default=EntityType.OTHER)
    description = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Campus(Base):
    """Campus that belongs to an organization."""
    __tablename__ = "campuses"

    id = Column(String, primary_key=True)
    organization_id = Column(
        String, ForeignKey("organizations.id"), nullable=True, index=True,
    )
    name = Column(String, nullable=False)
    description = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Building(Base):
    """Building that belongs to a campus."""
    __tablename__ = "buildings"

    id = Column(String, primary_key=True)
    campus_id = Column(String, ForeignKey("campuses.id"), nullable=False, index=True)
    organization_id = Column(
        String, ForeignKey("organizations.id"), nullable=True, index=True,
    )
    name = Column(String, nullable=False)
    short_name = Column(String)
    address = Column(String)
    origin_lat = Column(Float)
    origin_lng = Column(Float)
    origin_bearing = Column(Float, default=0.0)
    floor_count = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# --- Categories ---
_ROOM_TYPES = {
    "ROOM_GENERIC", "ROOM_OFFICE", "ROOM_CLASSROOM", "ROOM_LECTURE_HALL",
    "ROOM_LAB", "ROOM_MEETING", "ROOM_STORAGE", "ROOM_UTILITY",
    "RESTROOM", "RESTROOM_ACCESSIBLE",
}
_AMENITY_TYPES = {"CAFETERIA", "CAFE", "LIBRARY", "GYM", "AUDITORIUM", "SHOP"}
_CORRIDOR_TYPES = {
    "CORRIDOR", "CORRIDOR_SEGMENT", "LOBBY", "WAITING_AREA", "RECEPTION",
    "ENTRANCE", "ENTRANCE_SECONDARY", "EXIT_EMERGENCY",
}
_STAIRS_TYPES = {"STAIRCASE", "ESCALATOR", "OUTDOOR_STAIRS", "RAMP"}
_ELEVATOR_TYPES = {"ELEVATOR"}
_DOOR_TYPES = {
    "DOOR_STANDARD", "DOOR_AUTOMATIC", "DOOR_LOCKED", "DOOR_EMERGENCY",
    "PASSAGE", "OPEN",
}
_CONNECTOR_TYPES = {"BRIDGE", "TUNNEL", "COVERED_WALKWAY"}
_OUTDOOR_TYPES = {"OUTDOOR_PATH", "OUTDOOR_PLAZA", "OUTDOOR_COURTYARD", "PARKING"}


def _category_for(space_type: str | None) -> str:
    if not space_type:
        return "other"
    s = space_type.upper()
    if s in _ROOM_TYPES:
        return "room"
    if s in _AMENITY_TYPES:
        return "amenity"
    if s in _CORRIDOR_TYPES:
        return "corridor"
    if s in _STAIRS_TYPES:
        return "stairs"
    if s in _ELEVATOR_TYPES:
        return "elevator"
    if s in _DOOR_TYPES:
        return "door"
    if s in _CONNECTOR_TYPES:
        return "connector"
    if s in _OUTDOOR_TYPES:
        return "outdoor"
    return "other"


# --- Models ---

class BuildingSpace(Base):
    """Base table for every space."""
    __tablename__ = "building_spaces"

    id = Column(String, primary_key=True)
    space_category = Column(String, index=True, nullable=False)
    organization_id = Column(String, ForeignKey("organizations.id"), index=True)
    campus_id = Column(String, index=True)
    building_id = Column(String, index=True)
    floor_id = Column(String, index=True)
    display_name = Column(String)
    space_type = Column(String, index=True)
    floor_index = Column(Integer)
    centroid_x = Column(Float)
    centroid_y = Column(Float)
    centroid_lat = Column(Float)
    centroid_lng = Column(Float)
    width_m = Column(Float)
    length_m = Column(Float)
    area_m2 = Column(Float)
    geometry = Column(Geometry("POLYGON", srid=0))
    geometry_global = Column(Geometry("POLYGON", srid=4326))
    is_accessible = Column(Boolean, default=True)
    is_navigable = Column(Boolean, default=True)
    is_outdoor = Column(Boolean, default=False)
    capacity = Column(Integer)
    tags = Column(String)
    meta_data = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __mapper_args__ = {
        "polymorphic_on": space_category,
        "polymorphic_identity": "space",
    }


def _subclass(table_name: str, identity: str):
    """Factory for joined-inheritance subclasses with zero extra columns.
    Allows queries like `session.query(Room).all()` to hit only that table."""
    return type(
        identity.capitalize(),
        (BuildingSpace,),
        {
            "__tablename__": table_name,
            "id": Column(
                String, ForeignKey("building_spaces.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            "__mapper_args__": {"polymorphic_identity": identity},
        },
    )


Room = _subclass("rooms", "room")
Amenity = _subclass("amenities", "amenity")
Corridor = _subclass("corridors", "corridor")
Stairs = _subclass("stairs", "stairs")
Elevator = _subclass("elevators", "elevator")
Door = _subclass("doors", "door")
Connector = _subclass("connectors", "connector")
OutdoorSpace = _subclass("outdoor_spaces", "outdoor")
OtherSpace = _subclass("other_spaces", "other")

_MODEL_BY_CATEGORY = {
    "room": Room,
    "amenity": Amenity,
    "corridor": Corridor,
    "stairs": Stairs,
    "elevator": Elevator,
    "door": Door,
    "connector": Connector,
    "outdoor": OutdoorSpace,
    "other": OtherSpace,
}


class Floor(Base):
    """Floor plan metadata + bounding polygon."""
    __tablename__ = "floors"

    id = Column(String, primary_key=True)
    organization_id = Column(String, ForeignKey("organizations.id"), index=True)
    campus_id = Column(String, index=True)
    building_id = Column(String, index=True)
    floor_id = Column(String, index=True)
    floor_index = Column(Integer, index=True)
    display_name = Column(String)
    floor_plan_url = Column(String)
    floor_plan_scale = Column(Float)
    floor_plan_origin_x = Column(Float)
    floor_plan_origin_y = Column(Float)
    bounds_geometry = Column(Geometry("POLYGON", srid=0))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ImportRecord(Base):
    """Raw JSON payload from /import endpoint, one row per campus.
    Class is named ImportRecord (not Import) because `import` is a Python
    keyword; the table itself is called `imports`."""
    __tablename__ = "imports"

    organization_id = Column(String, ForeignKey("organizations.id"), index=True)
    campus_id = Column(String, primary_key=True)
    schema_version = Column(String)
    payload = Column(JSONB)
    imported_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
    )


class SpaceConnection(Base):
    """Directed mapmaker-created edge between two spaces via a door/passage.

    Mirror of the four Neo4j CONNECTS_TO edges that are produced when the user
    wires a room to a corridor (or any two spaces) through a door in the mapmaker.
    Stored as two rows per created connection: (from, door) and (door, to) for the
    forward direction, and the inverse pair for the reverse direction. The
    `connection_group_id` ties all four rows to the same user-created link so
    the whole group can be removed atomically on delete."""
    __tablename__ = "space_connections"

    id = Column(String, primary_key=True)
    connection_group_id = Column(String, index=True)
    from_space_id = Column(String, index=True, nullable=False)
    to_space_id = Column(String, index=True, nullable=False)
    door_space_id = Column(String, index=True)
    connection_type = Column(String)
    is_accessible = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# --- Service ---

class PostGISService:
    """Handles synchronization of building data to PostGIS."""

    def __init__(self):
        if not settings.supabase_enable_sync or not settings.supabase_db_url:
            self.engine = None
            self.SessionLocal = None
            return

        self.engine = create_engine(
            _safe_db_url(settings.supabase_db_url),
            echo=False,
            pool_size=5,
            max_overflow=10,
        )
        self.SessionLocal = sessionmaker(
            autocommit=False, autoflush=False, bind=self.engine,
        )
        self._init_db()

    def _init_db(self):
        if self.engine:
            Base.metadata.create_all(bind=self.engine)

    # --- organizations ---

    def sync_organization(self, org_data: dict) -> bool:
        """Upsert an organization row."""
        if not self.engine or not settings.supabase_enable_sync:
            return False
        try:
            session = self.SessionLocal()
            record = session.query(Organization).filter_by(
                id=org_data["id"]
            ).first()

            entity_type = org_data.get("entity_type")
            if entity_type is not None and not isinstance(entity_type, EntityType):
                raw = entity_type.value if hasattr(entity_type, "value") else str(entity_type)
                try:
                    entity_type = EntityType(raw)
                except ValueError:
                    entity_type = EntityType.OTHER

            if record:
                record.name = org_data.get("name", record.name)
                if entity_type is not None:
                    record.entity_type = entity_type
                record.description = org_data.get("description", record.description)
                record.updated_at = datetime.utcnow()
            else:
                record = Organization(
                    id=org_data["id"],
                    name=org_data.get("name", ""),
                    entity_type=entity_type or EntityType.OTHER,
                    description=org_data.get("description"),
                )
                session.add(record)
            session.commit()
            session.close()
            return True
        except Exception as e:
            print(f"Error syncing organization {org_data.get('id')} to PostGIS: {e}")
            return False

    def delete_organization(self, organization_id: str) -> bool:
        if not self.engine or not settings.supabase_enable_sync:
            return False
        try:
            session = self.SessionLocal()
            record = session.query(Organization).filter_by(id=organization_id).first()
            if record:
                session.delete(record)
                session.commit()
            session.close()
            return True
        except Exception as e:
            print(f"Error deleting organization {organization_id} from PostGIS: {e}")
            return False

    # --- campuses ---

    def sync_campus(self, campus_data: dict) -> bool:
        if not self.engine or not settings.supabase_enable_sync:
            return False
        try:
            session = self.SessionLocal()
            record = session.query(Campus).filter_by(id=campus_data["id"]).first()
            if record:
                record.organization_id = campus_data.get(
                    "organization_id", record.organization_id
                )
                record.name = campus_data.get("name", record.name)
                record.description = campus_data.get("description", record.description)
                record.updated_at = datetime.utcnow()
            else:
                record = Campus(
                    id=campus_data["id"],
                    organization_id=campus_data.get("organization_id"),
                    name=campus_data.get("name", ""),
                    description=campus_data.get("description"),
                )
                session.add(record)
            session.commit()
            session.close()
            return True
        except Exception as e:
            print(f"Error syncing campus {campus_data.get('id')} to PostGIS: {e}")
            return False

    def delete_campus(self, campus_id: str) -> bool:
        if not self.engine or not settings.supabase_enable_sync:
            return False
        try:
            session = self.SessionLocal()
            record = session.query(Campus).filter_by(id=campus_id).first()
            if record:
                session.delete(record)
                session.commit()
            session.close()
            return True
        except Exception as e:
            print(f"Error deleting campus {campus_id} from PostGIS: {e}")
            return False

    # --- buildings ---

    def sync_building(self, building_data: dict) -> bool:
        if not self.engine or not settings.supabase_enable_sync:
            return False
        try:
            session = self.SessionLocal()
            record = session.query(Building).filter_by(id=building_data["id"]).first()
            fields = [
                "campus_id", "organization_id", "name", "short_name", "address",
                "origin_lat", "origin_lng", "origin_bearing", "floor_count",
            ]
            if record:
                for f in fields:
                    if f in building_data:
                        setattr(record, f, building_data[f])
                record.updated_at = datetime.utcnow()
            else:
                record = Building(
                    id=building_data["id"],
                    **{f: building_data.get(f) for f in fields if f in building_data},
                )
                session.add(record)
            session.commit()
            session.close()
            return True
        except Exception as e:
            print(f"Error syncing building {building_data.get('id')} to PostGIS: {e}")
            return False

    def delete_building(self, building_id: str) -> bool:
        if not self.engine or not settings.supabase_enable_sync:
            return False
        try:
            session = self.SessionLocal()
            record = session.query(Building).filter_by(id=building_id).first()
            if record:
                session.delete(record)
                session.commit()
            session.close()
            return True
        except Exception as e:
            print(f"Error deleting building {building_id} from PostGIS: {e}")
            return False

    def delete_building_cascade(
        self,
        building_id: str,
        space_ids: list[str],
        floor_ids: list[str],
    ) -> bool:
        """Delete every PostGIS row belonging to a building: space_connections
        touching any of its spaces, the spaces themselves (incl. subclass rows
        via the FK cascade on `building_spaces`), the floor metadata rows, and
        finally the building row.

        `space_ids` is the full set of Space node IDs from Neo4j (rooms,
        subspaces, and any door/passage node that used to connect to them).
        `floor_ids` are the bare Floor node IDs; the PostGIS floors table keys
        on `{building_id}_{floor_id}`.
        """
        if not self.engine or not settings.supabase_enable_sync:
            return False
        try:
            session = self.SessionLocal()

            if space_ids:
                session.query(SpaceConnection).filter(
                    or_(
                        SpaceConnection.from_space_id.in_(space_ids),
                        SpaceConnection.to_space_id.in_(space_ids),
                        SpaceConnection.door_space_id.in_(space_ids),
                        SpaceConnection.connection_group_id.in_(space_ids),
                    )
                ).delete(synchronize_session=False)

                session.query(BuildingSpace).filter(
                    BuildingSpace.id.in_(space_ids)
                ).delete(synchronize_session=False)

            if floor_ids:
                composed = [f"{building_id}_{fid}" for fid in floor_ids]
                session.query(Floor).filter(
                    Floor.id.in_(composed)
                ).delete(synchronize_session=False)

            session.query(Building).filter_by(id=building_id).delete(
                synchronize_session=False
            )

            session.commit()
            session.close()
            return True
        except Exception as e:
            print(f"Error cascading building delete {building_id} in PostGIS: {e}")
            return False

    # --- spaces ---

    def sync_space(self, space_data: dict) -> bool:
        if not self.engine or not settings.supabase_enable_sync:
            return False

        try:
            session = self.SessionLocal()
            category = _category_for(space_data.get("space_type"))
            ModelClass = _MODEL_BY_CATEGORY[category]

            geom = None
            if space_data.get("polygon"):
                try:
                    poly = Polygon(space_data["polygon"])
                    geom = f"SRID=0;POLYGON(({', '.join([f'{x} {y}' for x, y in poly.exterior.coords])}))"
                except Exception as e:
                    print(f"Error creating local polygon for {space_data['id']}: {e}")

            geom_global = None
            if space_data.get("polygon_global"):
                try:
                    poly_global = Polygon(space_data["polygon_global"])
                    geom_global = f"SRID=4326;POLYGON(({', '.join([f'{y} {x}' for x, y in poly_global.exterior.coords])}))"
                except Exception as e:
                    print(f"Error creating global polygon for {space_data['id']}: {e}")

            existing = session.query(BuildingSpace).filter_by(
                id=space_data["id"]
            ).first()

            # If the category changed, remove the old row so the space lands
            # in the correct subclass table.
            if existing and existing.space_category != category:
                session.delete(existing)
                session.flush()
                existing = None

            if existing:
                record = existing
            else:
                record = ModelClass(id=space_data["id"])
                session.add(record)

            record.organization_id = space_data.get("organization_id")
            record.campus_id = space_data.get("campus_id")
            record.building_id = space_data.get("building_id")
            record.floor_id = space_data.get("floor_id")
            record.display_name = space_data.get("display_name")
            record.space_type = space_data.get("space_type")
            record.floor_index = space_data.get("floor_index")
            record.centroid_x = space_data.get("centroid_x")
            record.centroid_y = space_data.get("centroid_y")
            record.centroid_lat = space_data.get("centroid_lat")
            record.centroid_lng = space_data.get("centroid_lng")
            record.width_m = space_data.get("width_m")
            record.length_m = space_data.get("length_m")
            record.area_m2 = space_data.get("area_m2")
            record.is_accessible = space_data.get("is_accessible", True)
            record.is_navigable = space_data.get("is_navigable", True)
            record.is_outdoor = space_data.get("is_outdoor", False)
            record.capacity = space_data.get("capacity")
            record.tags = json.dumps(space_data.get("tags", []))
            record.meta_data = json.dumps(space_data.get("metadata", {}))
            if geom:
                record.geometry = geom
            if geom_global:
                record.geometry_global = geom_global
            record.updated_at = datetime.utcnow()

            session.commit()
            session.close()
            return True
        except Exception as e:
            print(f"Error syncing space {space_data.get('id')} to PostGIS: {e}")
            return False

    # --- floors ---

    def sync_floor(self, floor_data: dict) -> bool:
        if not self.engine or not settings.supabase_enable_sync:
            return False

        try:
            session = self.SessionLocal()

            geom = None
            if floor_data.get("floor_plan_bounds"):
                try:
                    bounds = floor_data["floor_plan_bounds"]
                    if len(bounds) == 2:
                        min_x, min_y = bounds[0]
                        max_x, max_y = bounds[1]
                        poly_coords = [
                            (min_x, min_y), (max_x, min_y),
                            (max_x, max_y), (min_x, max_y), (min_x, min_y),
                        ]
                        geom = f"SRID=0;POLYGON(({', '.join([f'{x} {y}' for x, y in poly_coords])}))"
                except Exception as e:
                    print(f"Error creating floor bounds polygon for {floor_data['id']}: {e}")

            record = session.query(Floor).filter_by(id=floor_data["id"]).first()
            if record:
                record.organization_id = floor_data.get("organization_id")
                record.display_name = floor_data.get("display_name")
                record.floor_plan_url = floor_data.get("floor_plan_url")
                record.floor_plan_scale = floor_data.get("floor_plan_scale", 1.0)
                record.floor_plan_origin_x = floor_data.get("floor_plan_origin_x", 0.0)
                record.floor_plan_origin_y = floor_data.get("floor_plan_origin_y", 0.0)
                if geom:
                    record.bounds_geometry = geom
                record.updated_at = datetime.utcnow()
            else:
                record = Floor(
                    id=floor_data["id"],
                    organization_id=floor_data.get("organization_id"),
                    campus_id=floor_data.get("campus_id"),
                    building_id=floor_data.get("building_id"),
                    floor_id=floor_data.get("floor_id"),
                    floor_index=floor_data.get("floor_index"),
                    display_name=floor_data.get("display_name"),
                    floor_plan_url=floor_data.get("floor_plan_url"),
                    floor_plan_scale=floor_data.get("floor_plan_scale", 1.0),
                    floor_plan_origin_x=floor_data.get("floor_plan_origin_x", 0.0),
                    floor_plan_origin_y=floor_data.get("floor_plan_origin_y", 0.0),
                    bounds_geometry=geom,
                )
                session.add(record)

            session.commit()
            session.close()
            return True
        except Exception as e:
            print(f"Error syncing floor {floor_data.get('id')} to PostGIS: {e}")
            return False

    # Backwards-compatible alias (ImportService still calls this name).
    def sync_floor_plan(self, floor_data: dict) -> bool:
        return self.sync_floor(floor_data)

    # --- deletes ---

    def delete_space(self, space_id: str) -> bool:
        """Remove a space from PostGIS. The FK cascade from the subclass
        table to building_spaces ensures both rows are cleaned up."""
        if not self.engine or not settings.supabase_enable_sync:
            return False
        try:
            session = self.SessionLocal()
            record = session.query(BuildingSpace).filter_by(id=space_id).first()
            if record:
                session.delete(record)
                session.commit()
            session.close()
            return True
        except Exception as e:
            print(f"Error deleting space {space_id} from PostGIS: {e}")
            return False

    def delete_floor(self, floor_pk: str) -> bool:
        """Remove a floor row. `floor_pk` is the composed `{building_id}_{floor_id}`
        primary key used by the floors table (matches how sync_floor writes it)."""
        if not self.engine or not settings.supabase_enable_sync:
            return False
        try:
            session = self.SessionLocal()
            record = session.query(Floor).filter_by(id=floor_pk).first()
            if record:
                session.delete(record)
                session.commit()
            session.close()
            return True
        except Exception as e:
            print(f"Error deleting floor {floor_pk} from PostGIS: {e}")
            return False

    # --- connections ---

    def sync_connection(
        self,
        from_space_id: str,
        to_space_id: str,
        door_space_id: str,
        connection_type: str | None,
        is_accessible: bool = True,
    ) -> bool:
        """Mirror the four CONNECTS_TO edges produced by create_connection() as
        rows in space_connections. The `connection_group_id` lets us identify
        all rows belonging to the same user-created connection so the whole
        group can be removed atomically on delete."""
        if not self.engine or not settings.supabase_enable_sync:
            return False
        try:
            session = self.SessionLocal()
            group_id = door_space_id
            # Remove any pre-existing rows for this group (idempotent re-sync).
            session.query(SpaceConnection).filter_by(connection_group_id=group_id).delete()
            edges = [
                (from_space_id, door_space_id),
                (door_space_id, from_space_id),
                (to_space_id, door_space_id),
                (door_space_id, to_space_id),
            ]
            for idx, (a, b) in enumerate(edges):
                session.add(SpaceConnection(
                    id=f"{group_id}:{idx}",
                    connection_group_id=group_id,
                    from_space_id=a,
                    to_space_id=b,
                    door_space_id=door_space_id,
                    connection_type=connection_type,
                    is_accessible=is_accessible,
                ))
            session.commit()
            session.close()
            return True
        except Exception as e:
            print(f"Error syncing connection {from_space_id}→{to_space_id} to PostGIS: {e}")
            return False

    def delete_connection_group(self, door_space_id: str) -> bool:
        """Delete all rows tied to a door's connection group."""
        if not self.engine or not settings.supabase_enable_sync:
            return False
        try:
            session = self.SessionLocal()
            session.query(SpaceConnection).filter_by(connection_group_id=door_space_id).delete()
            session.commit()
            session.close()
            return True
        except Exception as e:
            print(f"Error deleting connection group {door_space_id} from PostGIS: {e}")
            return False

    def sync_direct_edge(
        self,
        from_space_id: str,
        to_space_id: str,
        connection_type: str | None = None,
        is_accessible: bool = True,
    ) -> bool:
        """Mirror a single direct CONNECTS_TO edge (no intermediate door node)
        into space_connections. Used by bulk import where the schema describes
        edges directly rather than the door-node pattern."""
        if not self.engine or not settings.supabase_enable_sync:
            return False
        try:
            session = self.SessionLocal()
            row_id = f"direct:{from_space_id}->{to_space_id}"
            existing = session.query(SpaceConnection).filter_by(id=row_id).first()
            if existing:
                existing.connection_type = connection_type
                existing.is_accessible = is_accessible
            else:
                session.add(SpaceConnection(
                    id=row_id,
                    connection_group_id=None,
                    from_space_id=from_space_id,
                    to_space_id=to_space_id,
                    door_space_id=None,
                    connection_type=connection_type,
                    is_accessible=is_accessible,
                ))
            session.commit()
            session.close()
            return True
        except Exception as e:
            print(f"Error syncing direct edge {from_space_id}->{to_space_id} to PostGIS: {e}")
            return False

    def delete_edges_for_space(self, space_id: str) -> bool:
        """Remove any space_connections rows touching the given space: edges
        where it's the endpoint, or where it's the door node of a door-pattern
        group. Used on space deletion to keep the mirror consistent."""
        if not self.engine or not settings.supabase_enable_sync:
            return False
        try:
            session = self.SessionLocal()
            session.query(SpaceConnection).filter(
                (SpaceConnection.from_space_id == space_id)
                | (SpaceConnection.to_space_id == space_id)
                | (SpaceConnection.door_space_id == space_id)
                | (SpaceConnection.connection_group_id == space_id)
            ).delete(synchronize_session=False)
            session.commit()
            session.close()
            return True
        except Exception as e:
            print(f"Error deleting edges for space {space_id} from PostGIS: {e}")
            return False

    def update_connection_group_access(self, door_space_id: str, is_accessible: bool) -> bool:
        """Propagate a door's is_accessible change to every row in its
        connection group. No-op if the space isn't a door."""
        if not self.engine or not settings.supabase_enable_sync:
            return False
        try:
            session = self.SessionLocal()
            session.query(SpaceConnection).filter_by(
                connection_group_id=door_space_id
            ).update({SpaceConnection.is_accessible: is_accessible})
            session.commit()
            session.close()
            return True
        except Exception as e:
            print(f"Error updating connection group {door_space_id} accessibility: {e}")
            return False

    # --- imports ---

    def sync_import(
        self,
        campus_id: str,
        schema_version: str,
        payload: dict,
        organization_id: str | None = None,
    ) -> bool:
        """Store the raw import JSON payload keyed by campus_id."""
        if not self.engine or not settings.supabase_enable_sync:
            return False

        try:
            session = self.SessionLocal()
            record = session.query(ImportRecord).filter_by(
                campus_id=campus_id
            ).first()
            if record:
                if organization_id is not None:
                    record.organization_id = organization_id
                record.schema_version = schema_version
                record.payload = payload
                record.imported_at = datetime.utcnow()
            else:
                record = ImportRecord(
                    organization_id=organization_id,
                    campus_id=campus_id,
                    schema_version=schema_version,
                    payload=payload,
                )
                session.add(record)
            session.commit()
            session.close()
            return True
        except Exception as e:
            print(f"Error syncing import for {campus_id}: {e}")
            return False

    # --- reads ---

    def get_floor_plan(self, floor_id: str) -> dict | None:
        if not self.engine or not settings.supabase_enable_sync:
            return None

        try:
            session = self.SessionLocal()
            record = session.query(Floor).filter_by(id=floor_id).first()
            if not record:
                session.close()
                return None

            bounds = None
            if record.bounds_geometry:
                try:
                    shape = to_shape(record.bounds_geometry)
                    bounds = [[x, y] for x, y in shape.exterior.coords]
                except Exception as e:
                    print(f"Error parsing floor bounds geometry: {e}")

            result = {
                "id": record.id,
                "campus_id": record.campus_id,
                "building_id": record.building_id,
                "floor_id": record.floor_id,
                "floor_index": record.floor_index,
                "display_name": record.display_name,
                "floor_plan_url": record.floor_plan_url,
                "floor_plan_scale": record.floor_plan_scale,
                "floor_plan_origin_x": record.floor_plan_origin_x,
                "floor_plan_origin_y": record.floor_plan_origin_y,
                "bounds": bounds,
            }
            session.close()
            return result
        except Exception as e:
            print(f"Error retrieving floor plan from PostGIS: {e}")
            return None

    def get_floor_spaces(self, floor_id: str) -> list[dict]:
        """Return all spaces for a floor across every subclass table,
        shaped to match the iOS SpaceDisplayItem contract."""
        if not self.engine or not settings.supabase_enable_sync:
            return []

        try:
            session = self.SessionLocal()
            records = session.query(BuildingSpace).filter_by(
                floor_id=floor_id
            ).all()

            result = []
            for r in records:
                polygon_global = None
                if r.geometry_global is not None:
                    try:
                        shape = to_shape(r.geometry_global)
                        # PostGIS stores (lon, lat); iOS wants [[lat, lng], ...]
                        polygon_global = [[lat, lng] for lng, lat in shape.exterior.coords]
                    except Exception as e:
                        print(f"Error parsing geometry_global for {r.id}: {e}")

                polygon_local = None
                if r.geometry is not None:
                    try:
                        shape_local = to_shape(r.geometry)
                        polygon_local = [[x, y] for x, y in shape_local.exterior.coords]
                    except Exception as e:
                        print(f"Error parsing local geometry for {r.id}: {e}")

                result.append({
                    "id": r.id,
                    "display_name": r.display_name,
                    "space_type": r.space_type,
                    "space_category": r.space_category,
                    "floor_index": r.floor_index,
                    "centroid_x": r.centroid_x,
                    "centroid_y": r.centroid_y,
                    "centroid_lat": r.centroid_lat,
                    "centroid_lon": r.centroid_lng,  # iOS field name
                    "polygon": polygon_local,
                    "polygon_global": polygon_global,
                    "width_m": r.width_m,
                    "length_m": r.length_m,
                    "area_m2": r.area_m2,
                    "is_accessible": r.is_accessible,
                    "is_navigable": r.is_navigable,
                    "is_outdoor": r.is_outdoor,
                    "capacity": r.capacity,
                    "tags": json.loads(r.tags) if r.tags else [],
                    "metadata": json.loads(r.meta_data) if r.meta_data else {},
                    "building_id": r.building_id,
                })

            session.close()
            return result
        except Exception as e:
            print(f"Error retrieving floor spaces from PostGIS: {e}")
            return []

    def get_campus_spaces(self, campus_id: str) -> list[dict]:
        if not self.engine or not settings.supabase_enable_sync:
            return []

        try:
            session = self.SessionLocal()
            records = session.query(BuildingSpace).filter_by(
                campus_id=campus_id
            ).all()
            result = [
                {
                    "id": r.id,
                    "display_name": r.display_name,
                    "space_type": r.space_type,
                    "space_category": r.space_category,
                    "floor_index": r.floor_index,
                    "centroid_x": r.centroid_x,
                    "centroid_y": r.centroid_y,
                    "width_m": r.width_m,
                    "length_m": r.length_m,
                    "area_m2": r.area_m2,
                    "is_accessible": r.is_accessible,
                    "is_navigable": r.is_navigable,
                    "is_outdoor": r.is_outdoor,
                    "capacity": r.capacity,
                    "tags": json.loads(r.tags) if r.tags else [],
                    "metadata": json.loads(r.meta_data) if r.meta_data else {},
                    "building_id": r.building_id,
                }
                for r in records
            ]
            session.close()
            return result
        except Exception as e:
            print(f"Error retrieving campus spaces from PostGIS: {e}")
            return []
