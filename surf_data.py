import json
import math
from datetime import date, datetime, timezone
from pathlib import Path
import requests

MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
NOAA_URL = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"

SURFLINE_WAVE_URL = "https://services.surfline.com/kbyg/spots/forecasts/wave"
SURFLINE_RATING_URL = "https://services.surfline.com/kbyg/spots/forecasts/rating"
SURFLINE_HEADERS = {"User-Agent": "Mozilla/5.0"}

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


def fetch_surfline_overlay(spot_id: str | None, target_date: date) -> dict[str, dict]:
    """
    Optionally fetches Surfline's unofficial hourly wave height & rating (POOR/FAIR/GOOD).
    Returns a dict keyed by 'YYYY-MM-DD HH:00' in local time, or {} on any error.
    """
    if not spot_id:
        return {}

    days_ahead = max(1, (target_date - date.today()).days + 1)
    if days_ahead > 5:  # Unofficial unauthenticated limit is typically 5 days
        return {}

    params = {"spotId": spot_id, "days": days_ahead, "intervalHours": 1}
    overlay: dict[str, dict] = {}

    try:
        w_resp = requests.get(SURFLINE_WAVE_URL, params=params, headers=SURFLINE_HEADERS, timeout=8)
        r_resp = requests.get(SURFLINE_RATING_URL, params=params, headers=SURFLINE_HEADERS, timeout=8)
        w_resp.raise_for_status()
        r_resp.raise_for_status()

        waves = w_resp.json().get("data", {}).get("wave", [])
        ratings = r_resp.json().get("data", {}).get("rating", [])

        rating_by_ts = {
            item["timestamp"]: item.get("rating", {}).get("key")
            for item in ratings
        }

        for item in waves:
            ts = item.get("timestamp")
            utc_offset = item.get("utcOffset", -8)
            if ts is None:
                continue

            # Convert Unix timestamp + Surfline's utcOffset to local 'YYYY-MM-DD HH:00'
            local_dt = datetime.fromtimestamp(ts + utc_offset * 3600, tz=timezone.utc)
            if local_dt.date() != target_date:
                continue

            key = local_dt.strftime("%Y-%m-%d %H:00")
            surf = item.get("surf", {})
            overlay[key] = {
                "surfline_min_ft": surf.get("min"),
                "surfline_max_ft": surf.get("max"),
                "surfline_desc": surf.get("humanRelation"),
                "surfline_rating": rating_by_ts.get(ts),
            }
    except Exception:
        # Fail-safe: silently fall back to Open-Meteo + NOAA
        return {}

    return overlay


def fetch_spot_forecast(
    spot: dict,
    target_date: date,
    start_hour: int,
    end_hour: int,
    tides: dict,
    use_surfline: bool = True,
) -> list[dict]:
    """Pulls Open-Meteo marine + weather data and merges with NOAA tides & optional Surfline data."""
    d_iso = target_date.isoformat()

    marine_params = {
        "latitude": spot["lat"],
        "longitude": spot["lon"],
        "hourly": ",".join([
            "wave_height",
            "swell_wave_height",
            "swell_wave_period",
            "swell_wave_direction",
            "secondary_swell_wave_height",
            "secondary_swell_wave_period",
            "secondary_swell_wave_direction",
            "wind_wave_height",
        ]),
        "length_unit": "imperial",
        "timezone": "auto",
        "start_date": d_iso,
        "end_date": d_iso,
    }
    weather_params = {
        "latitude": spot["lat"],
        "longitude": spot["lon"],
        "hourly": "wind_speed_10m,wind_direction_10m,wind_gusts_10m,temperature_2m",
        "wind_speed_unit": "mph",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
        "start_date": d_iso,
        "end_date": d_iso,
    }

    m_data = requests.get(MARINE_URL, params=marine_params, timeout=15).json().get("hourly", {})
    w_data = requests.get(WEATHER_URL, params=weather_params, timeout=15).json().get("hourly", {})
    sl_overlay = fetch_surfline_overlay(spot.get("surfline_spot_id"), target_date) if use_surfline else {}

    times = m_data.get("time", [])
    rows = []
    for i, t_iso in enumerate(times):
        hour = int(t_iso[11:13])
        if not (start_hour <= hour <= end_hour):
            continue

        t_key = t_iso.replace("T", " ")
        tide_info = tides.get(t_key, {"tide_ft": None, "tide_trend": "unknown"})
        sl_info = sl_overlay.get(t_key, {})

        swell_dir = m_data.get("swell_wave_direction", [None])[i]
        sec_dir = m_data.get("secondary_swell_wave_direction", [None])[i]
        wind_dir = w_data.get("wind_direction_10m", [None])[i]
        wind_mph = w_data.get("wind_speed_10m", [None])[i]

        tags = tag_conditions(spot, swell_dir, wind_dir, wind_mph, tide_info["tide_ft"])

        rows.append({
            "time": t_iso[11:16],
            "total_wave_ft": m_data.get("wave_height", [None])[i],
            "primary_swell_ft": m_data.get("swell_wave_height", [None])[i],
            "primary_period_s": m_data.get("swell_wave_period", [None])[i],
            "primary_dir": f"{swell_dir}° ({deg_to_compass(swell_dir)})" if swell_dir is not None else "N/A",
            "secondary_swell_ft": m_data.get("secondary_swell_wave_height", [None])[i],
            "secondary_period_s": m_data.get("secondary_swell_wave_period", [None])[i],
            "secondary_dir": f"{sec_dir}° ({deg_to_compass(sec_dir)})" if sec_dir is not None else "N/A",
            "wind_wave_ft": m_data.get("wind_wave_height", [None])[i],
            "wind_mph": wind_mph,
            "wind_gust_mph": w_data.get("wind_gusts_10m", [None])[i],
            "wind_dir": f"{wind_dir}° ({deg_to_compass(wind_dir)})" if wind_dir is not None else "N/A",
            "air_temp_f": w_data.get("temperature_2m", [None])[i],
            "tide_ft": tide_info["tide_ft"],
            "tide_trend": tide_info["tide_trend"],
            **tags,
            **sl_info,
        })
    return rows


def get_window_forecasts(
    user_lat: float,
    user_lon: float,
    max_radius_miles: float,
    target_date: date,
    start_hour: int,
    end_hour: int,
    use_surfline: bool = True,
) -> list[dict]:
    """Filters spots by radius and returns merged forecast + logistics per spot."""
    spots = load_spots()
    tide_cache: dict[str, dict] = {}
    results = []

    for spot in spots:
        dist = haversine_miles(user_lat, user_lon, spot["lat"], spot["lon"])
        if dist > max_radius_miles:
            continue

        station = spot["noaa_station"]
        if station not in tide_cache:
            tide_cache[station] = fetch_noaa_tides(station, target_date)

        # Rough coastal drive estimate (no Google Maps key required for MVP)
        est_drive_mins = round(dist * 2.2 + 5)
        hourly = fetch_spot_forecast(
            spot,
            target_date,
            start_hour,
            end_hour,
            tide_cache[station],
            use_surfline=use_surfline,
        )

        results.append({
            "spot": spot,
            "distance_miles": dist,
            "est_drive_mins_each_way": est_drive_mins,
            "walk_in_mins_each_way": spot["walk_in_mins"],
            "round_trip_logistics_mins": (est_drive_mins + spot["walk_in_mins"]) * 2,
            "hourly_forecast": hourly,
        })

    return sorted(results, key=lambda x: x["distance_miles"])


if __name__ == "__main__":
    # Quick CLI smoke test
    data = get_window_forecasts(32.867, -117.213, 15.0, date.today(), 6, 10, use_surfline=True)
    print(f"Loaded {len(data)} spots within radius.")
    if data:
        print(json.dumps(data[0], indent=2))