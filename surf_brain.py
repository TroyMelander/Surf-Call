import json
import os
from typing import Literal
from google import genai
from google.genai import types
from pydantic import BaseModel, Field


class SurfDecision(BaseModel):
    verdict: Literal["GO", "MAYBE", "SKIP"] = Field(
        description="GO if fun/clean, MAYBE if marginal/playable, SKIP if blown out, flat, or logistics don't work."
    )
    spot_name: str = Field(description="Best break to surf, or 'Stay Dry' if SKIP.")
    paddle_out_time: str = Field(description="Exact recommended paddle-out time, e.g. '7:00 AM'.")
    recommended_board: str = Field(description="Best board from the surfer's quiver for this session.")
    est_water_time_mins: int = Field(description="Estimated minutes actually in the water after drive + walk-in.")
    the_call: str = Field(description="2 punchy, no-fluff sentences explaining why (swell, tide, wind, logistics).")
    watch_out: str = Field(description="1 short caveat (e.g. tide draining fast, onshore picking up after 9 AM, crowd).")
    runner_up: str = Field(description="Backup spot name and 1-sentence reason in case the top pick is packed.")
    board_sizing_tip: str = Field(
        description="Optional 1-sentence board volume/length check tailored to surfer height, weight, skill, and today's wave energy."
    )


def make_surf_call(profile: dict, window_label: str, window_mins: int, spot_forecasts: list[dict], api_key: str | None = None) -> SurfDecision:
    client = genai.Client(api_key=api_key or os.environ.get("GEMINI_API_KEY"))

    prompt = f"""
You are a veteran local surf forecaster acting as a zero-friction decision engine.
Your job: remove all thinking for the surfer and make ONE confident, honest call.

SURFER PROFILE:
{json.dumps(profile, indent=2)}

AVAILABLE WINDOW:
{window_label} ({window_mins} total minutes available door-to-door)

CANDIDATE BREAKS (Local Knowledge + Live Open-Meteo Marine/Weather + NOAA Tides):
{json.dumps(spot_forecasts, indent=2)}

DECISION RULES:
1. Cross-reference each break's `optimal_swell_deg`, `optimal_tide_ft`, `offshore_wind_deg`, and `notes` against the live hourly data.
2. Respect logistics: subtract `round_trip_logistics_mins` from {window_mins} mins. Do NOT pick a spot that leaves fewer than 45 minutes in the water unless no better option exists.
3. Match the wave steepness and swell period to the surfer's `quiver` and `preferred_vibe`.
4. Be brutally honest: if winds are strong onshore (>12 mph from W/NW/SW) or swell is tiny/junky across all spots, return verdict="SKIP".
5. Keep language crisp, natural, and local—no robotic filler.
"""

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SurfDecision,
            temperature=0.2,
        ),
    )
    return SurfDecision.model_validate_json(response.text)