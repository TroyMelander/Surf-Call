import json
import os
import time
from typing import Literal
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field

load_dotenv()


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


def make_surf_call(
    profile: dict,
    window_label: str,
    window_mins: int,
    spot_forecasts: list[dict],
    api_key: str | None = None,
) -> SurfDecision:
    client = genai.Client(api_key=api_key or os.environ.get("GEMINI_API_KEY"))

    prompt = f"""
You are a veteran San Diego surf forecaster acting as a zero-friction decision engine.
Your job: remove all thinking for the surfer and make ONE confident, honest call.

SURFER PROFILE:
{json.dumps(profile, indent=2)}

AVAILABLE WINDOW:
{window_label} ({window_mins} total minutes available door-to-door)

CANDIDATE BREAKS (Local Knowledge + Open-Meteo + NOAA Tides + Pre-Tagged Fits + Optional Surfline Ratings):
{json.dumps(spot_forecasts, indent=2)}

DECISION RULES:
1. Trust the pre-computed hourly tags first: prioritize hours where `wind_quality` is "glassy" or "offshore", `tide_fit` is "optimal", and `swell_fit` is "in_window".
2. Weigh Surfline data when present (`surfline_rating`, `surfline_min_ft`, `surfline_max_ft`, `surfline_desc`):
   - Surfline models nearshore bathymetry/refraction better than raw offshore buoy data.
   - If Open-Meteo shows solid swell but `surfline_rating` is "POOR" or "VERY_POOR" (or `surfline_max_ft` is <= 1 ft), downgrade the spot or flag the mismatch.
   - If Surfline fields are missing for a spot, rely cleanly on Open-Meteo, NOAA tides, and the spot's `notes`.
3. Respect logistics: subtract `round_trip_logistics_mins` from {window_mins} mins. Do NOT pick a spot that leaves fewer than 45 minutes in the water unless no better option exists.
4. Match wave size, steepness, and swell period to the surfer's `quiver`, `skill_level`, and `preferred_vibe`. Never send a beginner/intermediate to a heavy reef (e.g., Windansea, Black's) on a pumping swell unless it fits their profile.
5. Be brutally honest: if winds are strong onshore (>12 mph and `wind_quality` is "onshore_or_cross") or waves are tiny/junky across all spots, return verdict="SKIP" and spot_name="Stay Dry".
6. Keep language crisp, natural, and local—no robotic filler.
"""

    models_to_try = [
        "gemini-3.8-flash",
        "gemini-4-hydrogen",
        "gemini-3.5-flash-lite",
    ]

    for model_name in models_to_try:
        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=SurfDecision,
                        temperature=0.2,
                    ),
                )
                return SurfDecision.model_validate_json(response.text)
            except (errors.ServerError, errors.ClientError) as e:
                print(f"[SurfBrain] {model_name} attempt {attempt + 1} failed: {e}")
                if attempt == 0:
                    time.sleep(1.5)
                else:
                    break

    # --- DETERMINISTIC FALLBACK (Never crashes on Gemini 503 outages) ---
    return _fallback_surf_call(profile, window_mins, spot_forecasts)


def _fallback_surf_call(profile: dict, window_mins: int, spot_forecasts: list[dict]) -> SurfDecision:
    """Scores spots directly from live tags if Gemini is temporarily overloaded."""
    rating_pts = {"EPIC": 5, "GOOD": 4, "FAIR_TO_GOOD": 3, "FAIR": 2, "POOR_TO_FAIR": 1, "POOR": -1, "VERY_POOR": -3}
    scored = []

    for item in spot_forecasts:
        water_mins = max(0, window_mins - item["round_trip_logistics_mins"])
        if water_mins < 30 or not item["hourly_forecast"]:
            continue

        best_hr = max(
            item["hourly_forecast"],
            key=lambda h: (
                (3 if h.get("wind_quality") == "glassy" else 2 if h.get("wind_quality") == "offshore" else -2)
                + (2 if h.get("tide_fit") == "optimal" else -1)
                + (2 if h.get("swell_fit") == "in_window" else 0)
                + rating_pts.get(h.get("surfline_rating"), 0)
            ),
        )
        score = (
            (3 if best_hr.get("wind_quality") == "glassy" else 2 if best_hr.get("wind_quality") == "offshore" else -2)
            + (2 if best_hr.get("tide_fit") == "optimal" else -1)
            + (2 if best_hr.get("swell_fit") == "in_window" else 0)
            + rating_pts.get(best_hr.get("surfline_rating"), 0)
        )
        scored.append((score, item, best_hr, water_mins))

    scored.sort(key=lambda x: x[0], reverse=True)
    quiver = profile.get("quiver") or ["mid-length"]

    if not scored or scored[0][0] < 1:
        return SurfDecision(
            verdict="SKIP",
            spot_name="Stay Dry",
            paddle_out_time="N/A",
            recommended_board=quiver[0],
            est_water_time_mins=0,
            the_call="Gemini is temporarily overloaded, and live buoy/wind tags show poor conditions across your radius.",
            watch_out="Check the raw conditions table below for details.",
            runner_up="None — wait for cleaner wind or a better tide window.",
            board_sizing_tip="Save your energy for a cleaner window.",
        )

    top_score, top_item, top_hr, water_mins = scored[0]
    verdict = "GO" if top_score >= 5 else "MAYBE"
    runner = scored[1][1]["spot"]["name"] if len(scored) > 1 else "None within radius"
    wave_ft = top_hr.get("surfline_max_ft") or top_hr.get("total_wave_ft") or 2.0
    board = quiver[-1] if wave_ft < 3.0 else quiver[0]

    return SurfDecision(
        verdict=verdict,
        spot_name=top_item["spot"]["name"],
        paddle_out_time=top_hr["time"],
        recommended_board=board,
        est_water_time_mins=water_mins,
        the_call=(
            f"Best live window scores {top_hr['wind_quality']} wind, {top_hr['tide_fit']} tide "
            f"({top_hr['tide_ft']} ft), and ~{wave_ft} ft surf. "
            "(Gemini API is currently at capacity—call generated directly from live buoy, NOAA & Surfline data.)"
        ),
        watch_out=f"Wind is {top_hr.get('wind_mph')} mph ({top_hr.get('wind_dir')}); verify on the live cam before driving.",
        runner_up=f"{runner} — next highest-scoring break in your radius.",
        board_sizing_tip=f"Defaulting to your {board} for ~{wave_ft} ft conditions.",
    )