from db import Database
from models.map_import import MapImportSchema, SpaceImport, ConnectionImport
from models.campus import CampusCreate, BuildingCreate, FloorCreate
from models.space import SpaceCreate
from models.connection import ConnectionCreate
from repositories.campus_repo import CampusRepository
from repositories.space_repo import SpaceRepository
from repositories.connection_repo import ConnectionRepository
from services.geometry_service import (
    centroid_from_polygon,
    area_from_polygon,
    distance_m,
    compute_weight,
)
from sentence_transformers import SentenceTransformer

embedder = SentenceTransformer("all-MiniLM-L6-v2")

class ImportService:
    def __init__(self, db: Database):
        self.campus_repo = CampusRepository(db)
        self.space_repo = SpaceRepository(db)
        self.conn_repo = ConnectionRepository(db)
        self._centroids: dict[str, tuple[float, float]] = {}

    def import_map(self, schema: MapImportSchema) -> dict:
        campus = schema.campus

        # 1. Campus
        self.campus_repo.create_campus(
            CampusCreate(id=campus.id, name=campus.name, description=campus.description)
        )

        # 2. Buildings → Floors → Spaces
        for building in campus.buildings:
            self.campus_repo.create_building(
                BuildingCreate(
                    id=building.id,
                    campus_id=campus.id,
                    name=building.name,
                    short_name=building.short_name,
                    address=building.address,
                    origin_lat=building.origin_lat,
                    origin_lng=building.origin_lng,
                    origin_bearing=building.origin_bearing,
                    floor_count=building.floor_count,
                )
            )
            for floor in building.floors:
                self.campus_repo.create_floor(
                    FloorCreate(
                        id=floor.id,
                        building_id=building.id,
                        floor_index=floor.floor_index,
                        display_name=floor.display_name,
                        elevation_m=floor.elevation_m,
                        floor_plan_url=floor.floor_plan_url,
                        floor_plan_scale=floor.floor_plan_scale,
                        floor_plan_origin_x=floor.floor_plan_origin_x,
                        floor_plan_origin_y=floor.floor_plan_origin_y,
                    )
                )
                for space in floor.spaces:
                    self._import_space(
                        space,
                        floor_id=floor.id,
                        building_id=building.id,
                        campus_id=campus.id,
                        floor_index=floor.floor_index,
                        parent_id=None,
                    )

        # 3. Outdoor spaces (no floor/building context)
        for space in campus.outdoor_spaces:
            self._import_space(
                space,
                floor_id=None,
                building_id=None,
                campus_id=campus.id,
                floor_index=None,
                parent_id=None,
            )

        # 4. Connections
        counts = {"spaces": len(self._centroids), "connections": 0}
        for conn in campus.connections:
            self._import_connection(conn)
            counts["connections"] += 1

        return {
            "campus_id": campus.id,
            "spaces_imported": counts["spaces"],
            "connections_imported": counts["connections"],
        }

    def _import_space(
        self,
        space: SpaceImport,
        floor_id: str | None,
        building_id: str | None,
        campus_id: str,
        floor_index: int | None,
        parent_id: str | None,
    ) -> None:
        cx, cy = space.centroid_x, space.centroid_y
        area = space.area_m2

        if space.polygon and (cx is None or cy is None):
            cx, cy = centroid_from_polygon(space.polygon)
        if space.polygon and area is None:
            area = area_from_polygon(space.polygon)

        if cx is not None and cy is not None:
            self._centroids[space.id] = (cx, cy)
            
        text_to_embed = f"{space.display_name}. Type: {space.space_type}. Tags: {' '.join(space.tags)}"
        
        vector = embedder.encode(text_to_embed).tolist()

        self.space_repo.create_space(
            SpaceCreate(
                id=space.id,
                display_name=space.display_name,
                short_name=space.short_name,
                space_type=space.space_type,
                floor_index=floor_index,
                building_id=building_id,
                campus_id=campus_id,
                floor_id=floor_id,
                parent_space_id=parent_id,
                width_m=space.width_m,
                length_m=space.length_m,
                area_m2=area,
                centroid_x=cx,
                centroid_y=cy,
                polygon=space.polygon,
                is_accessible=space.is_accessible,
                is_navigable=space.is_navigable,
                is_outdoor=space.is_outdoor,
                capacity=space.capacity,
                tags=space.tags,
                metadata=space.metadata,
                embedding=vector,
            )
        )

        for subspace in space.subspaces:
            self._import_space(
                subspace,
                floor_id=floor_id,
                building_id=building_id,
                campus_id=campus_id,
                floor_index=floor_index,
                parent_id=space.id,
            )

    def _import_connection(self, conn: ConnectionImport) -> None:
        weight = conn.weight_override
        dist: float | None = None

        if weight is None:
            c_from = self._centroids.get(conn.from_space_id)
            c_to = self._centroids.get(conn.to_space_id)
            if c_from and c_to:
                dist = distance_m(c_from[0], c_from[1], c_to[0], c_to[1])
            weight = compute_weight(
                conn.connection_type.value, dist, conn.transition_time_s
            )

        self.conn_repo.create_connection(
            ConnectionCreate(
                from_space_id=conn.from_space_id,
                to_space_id=conn.to_space_id,
                connection_type=conn.connection_type,
                weight=weight,
                distance_m=dist,
                is_accessible=conn.is_accessible,
                door_type=conn.door_type,
                requires_access_level=conn.requires_access_level,
                transition_time_s=conn.transition_time_s,
            )
        )
