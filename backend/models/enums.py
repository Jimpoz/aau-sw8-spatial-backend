from enum import Enum


class SpaceType(str, Enum):
    # Rooms
    ROOM_GENERIC = "ROOM_GENERIC"
    ROOM_OFFICE = "ROOM_OFFICE"
    ROOM_CLASSROOM = "ROOM_CLASSROOM"
    ROOM_LECTURE_HALL = "ROOM_LECTURE_HALL"
    ROOM_LAB = "ROOM_LAB"
    ROOM_MEETING = "ROOM_MEETING"
    ROOM_STORAGE = "ROOM_STORAGE"
    ROOM_UTILITY = "ROOM_UTILITY"
    RESTROOM = "RESTROOM"
    RESTROOM_ACCESSIBLE = "RESTROOM_ACCESSIBLE"
    # Horizontal circulation
    CORRIDOR = "CORRIDOR"
    CORRIDOR_SEGMENT = "CORRIDOR_SEGMENT"
    LOBBY = "LOBBY"
    WAITING_AREA = "WAITING_AREA"
    RECEPTION = "RECEPTION"
    # Entrances/exits
    ENTRANCE = "ENTRANCE"
    ENTRANCE_SECONDARY = "ENTRANCE_SECONDARY"
    EXIT_EMERGENCY = "EXIT_EMERGENCY"
    # Vertical circulation
    STAIRCASE = "STAIRCASE"
    STAIRCASE_LANDING = "STAIRCASE_LANDING"
    ELEVATOR = "ELEVATOR"
    ELEVATOR_LOBBY = "ELEVATOR_LOBBY"
    ESCALATOR = "ESCALATOR"
    RAMP = "RAMP"
    # Cross-building connectors
    BRIDGE = "BRIDGE"
    TUNNEL = "TUNNEL"
    COVERED_WALKWAY = "COVERED_WALKWAY"
    # Outdoor
    OUTDOOR_PATH = "OUTDOOR_PATH"
    OUTDOOR_PLAZA = "OUTDOOR_PLAZA"
    OUTDOOR_COURTYARD = "OUTDOOR_COURTYARD"
    OUTDOOR_STAIRS = "OUTDOOR_STAIRS"
    PARKING = "PARKING"
    # Amenities
    CAFETERIA = "CAFETERIA"
    CAFE = "CAFE"
    LIBRARY = "LIBRARY"
    GYM = "GYM"
    AUDITORIUM = "AUDITORIUM"
    SHOP = "SHOP"
    # Special
    INACCESSIBLE = "INACCESSIBLE"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def _missing_(cls, value):
        if not isinstance(value, str):
            return super()._missing_(value)

        candidate = value.strip().replace("-", "_").replace(" ", "_").upper()
        synonyms = {
            "HALLWAY": "CORRIDOR",
            "CLASSROOM": "ROOM_CLASSROOM",
            "LECTURE_HALL": "ROOM_LECTURE_HALL",
            "OFFICE": "ROOM_OFFICE",
            "LAB": "ROOM_LAB",
            "MEETING": "ROOM_MEETING",
            "STORAGE": "ROOM_STORAGE",
            "UTILITY": "ROOM_UTILITY",
            "ACCESSIBLE_RESTROOM": "RESTROOM_ACCESSIBLE",
            "EMERGENCY_EXIT": "EXIT_EMERGENCY",
            "EXIT": "EXIT_EMERGENCY",
        }

        if candidate in cls.__members__:
            return cls[ candidate ]
        if candidate in synonyms:
            return cls[synonyms[candidate]]
        return super()._missing_(value)


class ConnectionType(str, Enum):
    WALKWAY = "WALKWAY"
    DOORWAY = "DOORWAY"
    STAIRCASE_UP = "STAIRCASE_UP"
    STAIRCASE_DOWN = "STAIRCASE_DOWN"
    ELEVATOR_UP = "ELEVATOR_UP"
    ELEVATOR_DOWN = "ELEVATOR_DOWN"
    ESCALATOR_UP = "ESCALATOR_UP"
    ESCALATOR_DOWN = "ESCALATOR_DOWN"
    OUTDOOR_PATH = "OUTDOOR_PATH"
    BRIDGE = "BRIDGE"
    TUNNEL = "TUNNEL"
    RAMP_UP = "RAMP_UP"
    RAMP_DOWN = "RAMP_DOWN"

    @classmethod
    def _missing_(cls, value):
        if not isinstance(value, str):
            return super()._missing_(value)

        candidate = value.strip().replace("-", "_").replace(" ", "_").upper()
        synonyms = {
            "DOOR": "DOORWAY",
            "OPEN_PASSAGE": "WALKWAY",
            "OPEN": "WALKWAY",
            "STAIRS_UP": "STAIRCASE_UP",
            "STAIRS_DOWN": "STAIRCASE_DOWN",
        }

        if candidate in cls.__members__:
            return cls[candidate]
        if candidate in synonyms:
            return cls[synonyms[candidate]]
        return super()._missing_(value)


class DoorType(str, Enum):
    NONE = "NONE"
    STANDARD = "STANDARD"
    AUTOMATIC = "AUTOMATIC"
    LOCKED = "LOCKED"
    EMERGENCY_ONLY = "EMERGENCY_ONLY"

    @classmethod
    def _missing_(cls, value):
        if value is None:
            return cls.NONE
        if not isinstance(value, str):
            return super()._missing_(value)

        candidate = value.strip().replace("-", "_").replace(" ", "_").upper()
        synonyms = {
            "SINGLE_SWING": "STANDARD",
            "STANDARD": "STANDARD",
            "AUTOMATIC": "AUTOMATIC",
            "LOCKED": "LOCKED",
            "EMERGENCY_ONLY": "EMERGENCY_ONLY",
            "EMERGENCYONLY": "EMERGENCY_ONLY",
        }

        if candidate in cls.__members__:
            return cls[candidate]
        if candidate in synonyms:
            return cls[synonyms[candidate]]
        return super()._missing_(value)
