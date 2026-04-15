"""PostGIS/Supabase synchronization service for building geometry."""

from sqlalchemy import create_engine, Column, String, Float, Integer, DateTime, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from geoalchemy2 import Geometry
from datetime import datetime
import json
from core.config import settings
from shapely.geometry import Polygon

Base = declarative_base()


class BuildingGeometry(Base):
    """Building geometry storage in PostGIS."""
    __tablename__ = "building_geometries"

    id = Column(String, primary_key=True)
    campus_id = Column(String, index=True)
    building_id = Column(String, index=True)
    display_name = Column(String)
    space_type = Column(String)
    floor_index = Column(Integer)
    centroid_x = Column(Float)
    centroid_y = Column(Float)
    centroid_lat = Column(Float)  # Global latitude
    centroid_lng = Column(Float)  # Global longitude
    width_m = Column(Float)
    length_m = Column(Float)
    area_m2 = Column(Float)
    geometry = Column(Geometry("POLYGON", srid=0))  # Local coordinate system
    geometry_global = Column(Geometry("POLYGON", srid=4326))  # WGS84 global polygon
    is_accessible = Column(Boolean, default=True)
    is_navigable = Column(Boolean, default=True)
    is_outdoor = Column(Boolean, default=False)
    capacity = Column(Integer)
    tags = Column(String)  # JSON array as string
    meta_data = Column(String)  # JSON object as string
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FloorPlanGeometry(Base):
    """Floor plan geometry storage in PostGIS."""
    __tablename__ = "floor_plan_geometries"

    id = Column(String, primary_key=True)
    campus_id = Column(String, index=True)
    building_id = Column(String, index=True)
    floor_id = Column(String, index=True)
    floor_index = Column(Integer, index=True)
    display_name = Column(String)
    floor_plan_url = Column(String)
    floor_plan_scale = Column(Float)
    floor_plan_origin_x = Column(Float)
    floor_plan_origin_y = Column(Float)
    bounds_geometry = Column(Geometry("POLYGON", srid=0))  # Local coordinate system
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PostGISService:
    """Handles synchronization of building data to PostGIS."""

    def __init__(self):
        if not settings.supabase_enable_sync or not settings.supabase_db_url:
            self.engine = None
            self.SessionLocal = None
            return

        self.engine = create_engine(
            settings.supabase_db_url,
            echo=False,
            pool_size=5,
            max_overflow=10,
        )
        self.SessionLocal = sessionmaker(
            autocommit=False, autoflush=False, bind=self.engine
        )
        self._init_db()

    def _init_db(self):
        """Create tables if they don't exist."""
        if self.engine:
            Base.metadata.create_all(bind=self.engine)

    def sync_space(self, space_data: dict) -> bool:
        """
        Sync a space from Neo4j to PostGIS.

        Args:
            space_data: Space data dict with keys: id, campus_id, building_id,
                       display_name, space_type, floor_index, centroid_x, centroid_y,
                       width_m, length_m, area_m2, polygon, is_accessible, is_navigable,
                       is_outdoor, capacity, tags, metadata

        Returns:
            True if sync successful, False if disabled or error
        """
        if not self.engine or not settings.supabase_enable_sync:
            return False

        try:
            session = self.SessionLocal()

            geom = None
            geom_global = None
            
            if space_data.get("polygon"):
                try:
                    poly = Polygon(space_data["polygon"])
                    geom = f"SRID=0;POLYGON(({', '.join([f'{x} {y}' for x, y in poly.exterior.coords])}))"
                except Exception as e:
                    print(f"Error creating local polygon for {space_data['id']}: {e}")
                    
            if space_data.get("polygon_global"):
                try:
                    poly_global = Polygon(space_data["polygon_global"])
                    geom_global = f"SRID=4326;POLYGON(({', '.join([f'{y} {x}' for x, y in poly_global.exterior.coords])}))"
                except Exception as e:
                    print(f"Error creating global polygon for {space_data['id']}: {e}")

            record = session.query(BuildingGeometry).filter_by(
                id=space_data["id"]
            ).first()

            if record:
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
            else:
                record = BuildingGeometry(
                    id=space_data["id"],
                    campus_id=space_data.get("campus_id"),
                    building_id=space_data.get("building_id"),
                    display_name=space_data.get("display_name"),
                    space_type=space_data.get("space_type"),
                    floor_index=space_data.get("floor_index"),
                    centroid_x=space_data.get("centroid_x"),
                    centroid_y=space_data.get("centroid_y"),
                    centroid_lat=space_data.get("centroid_lat"),
                    centroid_lng=space_data.get("centroid_lng"),
                    width_m=space_data.get("width_m"),
                    length_m=space_data.get("length_m"),
                    area_m2=space_data.get("area_m2"),
                    is_accessible=space_data.get("is_accessible", True),
                    is_navigable=space_data.get("is_navigable", True),
                    is_outdoor=space_data.get("is_outdoor", False),
                    capacity=space_data.get("capacity"),
                    tags=json.dumps(space_data.get("tags", [])),
                    meta_data=json.dumps(space_data.get("metadata", {})),
                    geometry=geom,
                    geometry_global=geom_global,
                )
                session.add(record)

            session.commit()
            session.close()
            return True

        except Exception as e:
            print(f"Error syncing space {space_data.get('id')} to PostGIS: {e}")
            return False

    def sync_floor_plan(self, floor_data: dict) -> bool:
        """
        Sync a floor plan from Neo4j to PostGIS.

        Args:
            floor_data: Floor data dict with keys: id, campus_id, building_id,
                       floor_id, floor_index, display_name, floor_plan_url,
                       floor_plan_scale, floor_plan_origin_x, floor_plan_origin_y,
                       floor_plan_bounds

        Returns:
            True if sync successful, False if disabled or error
        """
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
                            (min_x, min_y),
                            (max_x, min_y),
                            (max_x, max_y),
                            (min_x, max_y),
                            (min_x, min_y) 
                        ]
                        geom = f"SRID=0;POLYGON(({', '.join([f'{x} {y}' for x, y in poly_coords])}))"
                except Exception as e:
                    print(f"Error creating floor bounds polygon for {floor_data['id']}: {e}")

            record = session.query(FloorPlanGeometry).filter_by(
                id=floor_data["id"]
            ).first()

            if record:
                record.display_name = floor_data.get("display_name")
                record.floor_plan_url = floor_data.get("floor_plan_url")
                record.floor_plan_scale = floor_data.get("floor_plan_scale", 1.0)
                record.floor_plan_origin_x = floor_data.get("floor_plan_origin_x", 0.0)
                record.floor_plan_origin_y = floor_data.get("floor_plan_origin_y", 0.0)
                if geom:
                    record.bounds_geometry = geom
                record.updated_at = datetime.utcnow()
            else:
                record = FloorPlanGeometry(
                    id=floor_data["id"],
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
            print(f"Error syncing floor plan {floor_data.get('id')} to PostGIS: {e}")
            return False

    def get_floor_plan(self, floor_id: str) -> dict | None:
        """Retrieve floor plan data from PostGIS."""
        if not self.engine or not settings.supabase_enable_sync:
            return None

        try:
            session = self.SessionLocal()
            record = session.query(FloorPlanGeometry).filter_by(
                id=floor_id
            ).first()

            if not record:
                session.close()
                return None

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
                "bounds": None,  # Will be populated if geometry exists
            }

            if record.bounds_geometry:
                try:
                    # TO IMPLEMENT
                    pass
                except Exception as e:
                    print(f"Error parsing floor bounds geometry: {e}")

            session.close()
            return result
        except Exception as e:
            print(f"Error retrieving floor plan from PostGIS: {e}")
            return None

    def get_floor_spaces(self, floor_id: str) -> list[dict]:
        """Retrieve all spaces for a floor from PostGIS."""
        if not self.engine or not settings.supabase_enable_sync:
            return []

        try:
            session = self.SessionLocal()
            records = session.query(BuildingGeometry).filter_by(
                floor_index=floor_id
            ).all()
            result = [
                {
                    "id": r.id,
                    "display_name": r.display_name,
                    "space_type": r.space_type,
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
                }
                for r in records
            ]
            session.close()
            return result
        except Exception as e:
            print(f"Error retrieving floor spaces from PostGIS: {e}")
            return []

    def get_campus_spaces(self, campus_id: str) -> list[dict]:
        """Retrieve all spaces for a campus from PostGIS."""
        if not self.engine or not settings.supabase_enable_sync:
            return []

        try:
            session = self.SessionLocal()
            records = session.query(BuildingGeometry).filter_by(
                campus_id=campus_id
            ).all()
            result = [
                {
                    "id": r.id,
                    "display_name": r.display_name,
                    "space_type": r.space_type,
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
