class NotFoundError(Exception):
    def __init__(self, resource: str, id: str):
        self.resource = resource
        self.id = id
        super().__init__(f"{resource} '{id}' not found")


class OrganizationNotFound(NotFoundError):
    def __init__(self, id: str):
        super().__init__("Organization", id)


class CampusNotFound(NotFoundError):
    def __init__(self, id: str):
        super().__init__("Campus", id)


class BuildingNotFound(NotFoundError):
    def __init__(self, id: str):
        super().__init__("Building", id)


class FloorNotFound(NotFoundError):
    def __init__(self, id: str):
        super().__init__("Floor", id)


class SpaceNotFound(NotFoundError):
    def __init__(self, id: str):
        super().__init__("Space", id)


class ConnectionNotFound(NotFoundError):
    def __init__(self, from_id: str, to_id: str):
        super().__init__("Connection", f"{from_id}->{to_id}")


class MapImportError(Exception):
    pass


class NavigationError(Exception):
    pass
