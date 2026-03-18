from db import Database
from models.map_import import MapImportSchema, SpaceImport, ConnectionNodeImport
from models.campus import CampusCreate, BuildingCreate, FloorCreate
from models.space import SpaceCreate
from repositories.campus_repo import CampusRepository
from repositories.space_repo import SpaceRepository
from repositories.connection_repo import ConnectionRepository
from services.geometry_service import (
    centroid_from_polygon,
    area_from_polygon,
    compute_traversal_cost,
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

        # 4. Connection nodes
        counts = {"spaces": len(self._centroids), "connections": 0}
        for conn in campus.connections:
            self._import_connection_node(conn, campus_id=campus.id)
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

        traversal_cost = compute_traversal_cost(
            space.space_type.value,
            space.width_m,
            space.length_m,
            None,
        )

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
                traversal_cost=traversal_cost,
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

    def _import_connection_node(
        self,
        conn: ConnectionNodeImport,
        campus_id: str,
    ) -> None:
        # Compute centroid as midpoint of connected spaces if not provided
        cx, cy = conn.centroid_x, conn.centroid_y
        if cx is None or cy is None:
            c_a = self._centroids.get(conn.connects[0])
            c_b = self._centroids.get(conn.connects[1])
            if c_a and c_b:
                cx = (c_a[0] + c_b[0]) / 2.0
                cy = (c_a[1] + c_b[1]) / 2.0

        traversal_cost = compute_traversal_cost(
            conn.space_type.value,
            None,
            None,
            conn.transition_time_s,
        )

        # Create the connection node as a Space
        self.space_repo.create_space(
            SpaceCreate(
                id=conn.id,
                display_name=conn.display_name,
                space_type=conn.space_type,
                campus_id=campus_id,
                centroid_x=cx,
                centroid_y=cy,
                polygon=conn.polygon,
                is_accessible=conn.is_accessible,
                is_navigable=True,
                tags=conn.tags,
                traversal_cost=traversal_cost,
            )
        )

        if cx is not None and cy is not None:
            self._centroids[conn.id] = (cx, cy)

        # Create 4 bare CONNECTS_TO edges (A→Node, Node→A, B→Node, Node→B)
        space_a = conn.connects[0]
        space_b = conn.connects[1]
        self.conn_repo.create_connection(space_a, conn.id)
        self.conn_repo.create_connection(conn.id, space_a)
        self.conn_repo.create_connection(space_b, conn.id)
        self.conn_repo.create_connection(conn.id, space_b)
