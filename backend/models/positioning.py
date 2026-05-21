from typing import Dict, Optional

from pydantic import BaseModel, Field


class FingerprintIn(BaseModel):
    """A Wi-Fi fingerprint captured while standing in a space."""
    space_id: str
    floor_id: Optional[str] = None
    # bssid -> rssi in dBm (negative).
    readings: Dict[str, float] = Field(default_factory=dict)
    # bssid -> distance in mm from 802.11mc FTM ranging, if available.
    rtt_distances_mm: Optional[Dict[str, float]] = None
    sample_count: int = 1


class FingerprintOut(BaseModel):
    id: str
    space_id: str
    floor_id: Optional[str] = None
    sample_count: int = 1


class LocateRequest(BaseModel):
    floor_id: str
    readings: Dict[str, float] = Field(default_factory=dict)
    rtt_distances_mm: Optional[Dict[str, float]] = None


class LocateResponse(BaseModel):
    space_id: Optional[str] = None
    confidence: float = 0.0
    supporting_count: int = 0
    # (x, y) on-floor coordinates when RTT trilateration succeeds.
    x: Optional[float] = None
    y: Optional[float] = None
    method: str = "rssi_knn"


class AccessPointIn(BaseModel):
    bssid: str
    ssid: Optional[str] = None
    floor_id: Optional[str] = None
    x: Optional[float] = None
    y: Optional[float] = None
    supports_rtt: bool = False


class FloorSurveyResponse(BaseModel):
    floor_id: str
    total_fingerprints: int
    per_space_counts: Dict[str, int] = Field(default_factory=dict)
