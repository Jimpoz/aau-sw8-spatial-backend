from dataclasses import dataclass, field

import numpy as np


@dataclass(slots=True)
class RoomImageInput:
    direction: str
    source_name: str
    frame: np.ndarray = field(repr=False)
