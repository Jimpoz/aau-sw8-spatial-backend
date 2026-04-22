from db import Database
from models.map_import import MapImportSchema, SpaceImport, ConnectionNodeImport
from models.campus import (
    CampusCreate,
    BuildingCreate,
    FloorCreate,
    OrganizationCreate,
)
from models.space import SpaceCreate
from repositories.campus_repo import CampusRepository, OrganizationRepository
from repositories.space_repo import SpaceRepository
from repositories.connection_repo import ConnectionRepository
from services.geometry_service import (
    centroid_from_polygon,
    area_from_polygon,
    distance_m,
    compute_traversal_cost,
    local_to_global_coordinates,
    polygon_local_to_global,
)
from services.postgis_service import PostGISService
from sentence_transformers import SentenceTransformer

embedder = SentenceTransformer("all-MiniLM-L6-v2")

class ImportService:
    def __init__(self, db: Database):
        self.org_repo = OrganizationRepository(db)
        self.campus_repo = CampusRepository(db)
        self.space_repo = SpaceRepository(db)
        self.conn_repo = ConnectionRepository(db)
        self.postgis = PostGISService()
        self._centroids: dict[str, tuple[float, float]] = {}

    def import_map(self, schema: MapImportSchema) -> dict:
        campus = schema.campus
        organization = schema.organization

        # 0. Organization (optional). If provided, both Neo4j and PostGIS get it.
        organization_id = None
        if organization is not None:
            organization_id = organization.id
            self.org_repo.create_organization(
                OrganizationCreate(
                    id=organization.id,
                    name=organization.name,
                    entity_type=organization.entity_type,
                    description=organization.description,
                )
            )
            self.postgis.sync_organization({
                "id": organization.id,
                "name": organization.name,
                "entity_type": organization.entity_type,
                "description": organization.description,
            })
        else:
            organization_id = campus.organization_id

        self.postgis.sync_import(
            campus_id=campus.id,
            schema_version=schema.schema_version,
            payload=schema.model_dump(mode="json"),
            organization_id=organization_id,
        )

        # 1. Campus
        self.campus_repo.create_campus(
            CampusCreate(
                id=campus.id,
                name=campus.name,
                description=campus.description,
                organization_id=organization_id,
            )
        )
        self.postgis.sync_campus({
            "id": campus.id,
            "organization_id": organization_id,
            "name": campus.name,
            "description": campus.description,
        })

        # 2. Buildings → Floors → Spaces
        for building in campus.buildings:
            building_org_id = building.organization_id or organization_id
            self.campus_repo.create_building(
                BuildingCreate(
                    id=building.id,
                    campus_id=campus.id,
                    organization_id=building_org_id,
                    name=building.name,
                    short_name=building.short_name,
                    address=building.address,
                    origin_lat=building.origin_lat,
                    origin_lng=building.origin_lng,
                    origin_bearing=building.origin_bearing,
                    floor_count=building.floor_count,
                )
            )
            self.postgis.sync_building({
                "id": building.id,
                "campus_id": campus.id,
                "organization_id": building_org_id,
                "name": building.name,
                "short_name": building.short_name,
                "address": building.address,
                "origin_lat": building.origin_lat,
                "origin_lng": building.origin_lng,
                "origin_bearing": building.origin_bearing,
                "floor_count": building.floor_count,
            })
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

                self.postgis.sync_floor({
                    "id": f"{building.id}_{floor.id}",
                    "organization_id": building_org_id,
                    "campus_id": campus.id,
                    "building_id": building.id,
                    "floor_id": floor.id,
                    "floor_index": floor.floor_index,
                    "display_name": floor.display_name,
                    "floor_plan_url": floor.floor_plan_url,
                    "floor_plan_scale": floor.floor_plan_scale,
                    "floor_plan_origin_x": floor.floor_plan_origin_x,
                    "floor_plan_origin_y": floor.floor_plan_origin_y,
                    "floor_plan_bounds": floor.floor_plan_bounds,
                })

                for space in floor.spaces:
                    self._import_space(
                        space,
                        floor_id=floor.id,
                        building_id=building.id,
                        campus_id=campus.id,
                        organization_id=building_org_id,
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
                organization_id=organization_id,
                floor_index=None,
                parent_id=None,
            )

        # 4. Connection nodes
        counts = {"spaces": len(self._centroids), "connections": 0}
        for conn in campus.connections:
            self._import_connection_node(conn)
            counts["connections"] += 1

        return {
            "organization_id": organization_id,
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
        organization_id: str | None,
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

        global_lat, global_lng = None, None
        global_polygon = None

        if building_id and cx is not None and cy is not None:
            building = self.campus_repo.get_building(building_id)
            if building.get("origin_lat") is not None and building.get("origin_lng") is not None:
                global_lat, global_lng = local_to_global_coordinates(
                    cx, cy,
                    building["origin_lat"],
                    building["origin_lng"],
                    building.get("origin_bearing") or 0.0
                )

                if space.polygon:
                    global_polygon = polygon_local_to_global(
                        space.polygon,
                        building["origin_lat"],
                        building["origin_lng"],
                        building.get("origin_bearing") or 0.0
                    )

        text_to_embed = f"{space.display_name}. Type: {space.space_type}. Tags: {' '.join(space.tags)}"

        vector = embedder.encode([text_to_embed])[0].tolist()

        traversal_cost = compute_traversal_cost(
            space.space_type.value if hasattr(space.space_type, 'value') else str(space.space_type),
            space.width_m,
            space.length_m,
            None,
        )

        self.space_repo.create_space(
            SpaceCreate(
                id=space.id,
                display_name=space.display_name,
                short_name=space.short_name,
                space_type=space.space_type,
                floor_index=floor_index,
                building_id=building_id,
                campus_id=campus_id,
                organization_id=organization_id,
                floor_id=floor_id,
                parent_space_id=parent_id,
                width_m=space.width_m,
                length_m=space.length_m,
                area_m2=area,
                centroid_x=cx,
                centroid_y=cy,
                centroid_lat=global_lat,
                centroid_lng=global_lng,
                polygon=space.polygon,
                polygon_global=global_polygon,
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

        # Sync to PostGIS with global coordinates
        self.postgis.sync_space({
            "id": space.id,
            "organization_id": organization_id,
            "campus_id": campus_id,
            "building_id": building_id,
            "floor_id": floor_id,
            "display_name": space.display_name,
            "space_type": space.space_type.value if hasattr(space.space_type, 'value') else str(space.space_type),
            "floor_index": floor_index,
            "centroid_x": cx,
            "centroid_y": cy,
            "centroid_lat": global_lat,
            "centroid_lng": global_lng,
            "width_m": space.width_m,
            "length_m": space.length_m,
            "area_m2": area,
            "polygon": space.polygon,
            "polygon_global": global_polygon,
            "is_accessible": space.is_accessible,
            "is_navigable": space.is_navigable,
            "is_outdoor": space.is_outdoor,
            "capacity": space.capacity,
            "tags": space.tags,
            "metadata": space.metadata,
        })

        for subspace in space.subspaces:
            self._import_space(
                subspace,
                floor_id=floor_id,
                building_id=building_id,
                campus_id=campus_id,
                organization_id=organization_id,
                floor_index=floor_index,
                parent_id=space.id,
            )

    def _import_connection_node(self, conn: ConnectionNodeImport) -> None:
        self.conn_repo.create_connection(conn.from_space_id, conn.to_space_id)
        connection_type = (
            conn.connection_type.value
            if hasattr(conn.connection_type, "value")
            else (str(conn.connection_type) if conn.connection_type is not None else None)
        )
        self.postgis.sync_direct_edge(
            from_space_id=conn.from_space_id,
            to_space_id=conn.to_space_id,
            connection_type=connection_type,
            is_accessible=bool(getattr(conn, "is_accessible", True)),
        )
