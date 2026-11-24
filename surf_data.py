import json
import math
from datetime import date
from pathlib import Path
import requests

MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
NOAA_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
           "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def deg_to_compass(deg: float | None) -> str:
    if deg is None:
        return "N/A"
    idx = int((deg + 11.25) / 22.5) % 16
    return COMPASS[idx]


def in_angle_range(deg: float | None, bounds: list[int]) -> bool:
    if deg is None:
        return False
    low, high = bounds
    if low <= high:
        return low <= deg <= high
    return deg >= low or deg <= high  # Handles wrap-around across 360°/0°


def tag_conditions(
    spot: dict,
    swell_dir: float | None,
    wind_dir: float | None,
    wind_mph: float | None,
    tide_ft: float | None,
) -> dict:
    # Wind quality
    if wind_mph is None or wind_mph < 4:
        wind_q = "glassy"
    elif in_angle_range(wind_dir, spot["offshore_wind_deg"]):
        wind_q = "offshore"
    else:
        wind_q = "onshore_or_cross"

    # Tide fit
    t_low, t_high = spot["optimal_tide_ft"]
    if tide_ft is None:
        tide_q = "unknown"
    elif t_low <= tide_ft <= t_high:
        tide_q = "optimal"
    elif tide_ft < t_low:
        tide_q = "too_low"
    else:
        tide_q = "too_high"

    # Swell window
    swell_q = "in_window" if in_angle_range(swell_dir, spot["optimal_swell_deg"]) else "outside_window"

    return {"wind_quality": wind_q, "tide_fit": tide_q, "swell_fit": swell_q}


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return round(2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 1)


def load_spots(path: str = "spots.json") -> list[dict]:
    return json.loads(Path(path).read_text())


def fetch_noaa_tides(station_id: str, target_date: date) -> dict[str, dict]:
    """Returns hourly tide height (ft MLLW) and trend keyed by 'YYYY-MM-DD HH:MM'."""
    d_str = target_date.strftime("%Y%m%d")
    params = {
        "begin_date": d_str,
        "end_date": d_str,
        "station": station_id,
        "product": "predictions",
        "datum": "MLLW",
        "time_zone": "lst_ldt",
        "interval": "h",
        "units": "english",
        "application": "SurfCallMVP",
        "format": "json",
    }
    resp = requests.get(NOAA_URL, params=params, timeout=15)
    resp.raise_for_status()
    preds = resp.json().get("predictions", [])

    tides = {}
    for i, item in enumerate(preds):
        val = round(float(item["v"]), 2)
        if i > 0:
            prev = float(preds[i - 1]["v"])
            trend = "rising" if val > prev + 0.05 else ("falling" if val < prev - 0.05 else "slack")
        elif i + 1 < len(preds):
            nxt = float(preds[i + 1]["v"])
            trend = "rising" if nxt > val + 0.05 else ("falling" if nxt < val - 0.05 else "slack")
        else:
            trend = "slack"
        tides[item["t"]] = {"tide_ft": val, "tide_trend": trend}
    return tides


def fetch_spot_forecast(
    spot: dict,
    target_date: date,
    start_hour: int,
    end_hour: int,
    tides: dict,
) -> list[dict]:
    """Pulls Open-Meteo marine + weather data and merges with NOAA tides for the window."""
    d_iso = target_date.isoformat()

    marine_params = {
        "latitude": spot["lat"],
        "longitude": spot["lon"],
        "hourly": ",".join([
            "wave_