from datetime import date
import os
import pandas as pd
import requests
import streamlit as st

from surf_data import get_window_forecasts
from surf_brain import make_surf_call

st.set_page_config(page_title="Surf Call", page_icon="🏄", layout="centered")
st.title("🏄 Surf Call")
st.caption("Zero-thinking surf recommender powered by Open-Meteo, NOAA Tides, Surfline & Gemini.")


# --- CACHED HELPERS (Multi-User Safe) ---
@st.cache_data(ttl=86400, show_spinner=False)
def geocode_location(query: str) -> tuple[float, float, str] | None:
    """Converts a zip code, neighborhood, or street address to (lat, lon, display_name)."""
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1},
            headers={"User-Agent": "SurfCallApp/1.0"},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        if not results:
            return None
        return float(results[0]["lat"]), float(results[0]["lon"]), results[0]["display_name"]
    except Exception:
        return None


@st.cache_data(ttl=1800, show_spinner=False)
def cached_window_forecasts(
    user_lat: float,
    user_lon: float,
    max_radius_miles: float,
    target_date: date,
    start_hour: int,
    end_hour: int,
    use_surfline: bool,
) -> list[dict]:
    """Caches weather, tide, and Surfline pulls for 30 minutes across concurrent users."""
    return get_window_forecasts(
        user_lat=user_lat,
        user_lon=user_lon,
        max_radius_miles=max_radius_miles,
        target_date=target_date,
        start_hour=start_hour,
        end_hour=end_hour,
        use_surfline=use_surfline,
    )


def get_gemini_api_key() -> str | None:
    """Reads GEMINI_API_KEY from Streamlit Cloud Secrets first, then local .env/environment."""
    try:
        if "GEMINI_API_KEY" in st.secrets:
            return st.secrets["GEMINI_API_KEY"]
    except Exception:
        pass
    return os.environ.get("GEMINI_API_KEY")


# --- LOAD DEFAULTS FROM URL QUERY PARAMS (Zero-Database Personalization) ---
qp = st.query_params
default_loc = qp.get("loc", "92122")
default_radius = int(qp.get("radius", 15))
default_skill = qp.get("skill", "intermediate")
if default_skill not in ["beginner", "intermediate", "advanced"]:
    default_skill = "intermediate"
default_vibe = qp.get("vibe", "clean peeling waves, mid-length friendly")
default_quiver = qp.get("quiver", "7'6\" mid-length|8'6\" funboard|9'6\" longboard").replace("|", "\n")
default_height = int(qp.get("height", 76))
default_weight = int(qp.get("weight", 190))

# --- SIDEBAR: Set Once & Bookmark ---
with st.sidebar:
    st.header("Your Setup (Set Once)")
    location_input = st.text_input(
        "Starting Location (Zip, City, or Address)",
        value=default_loc,
        placeholder="e.g., 92122 or Pacific Beach, CA",
    )
    max_radius = st.slider("Max Radius (miles)", 3, 45, default_radius)

    skill = st.selectbox(
        "Skill Level",
        ["beginner", "intermediate", "advanced"],
        index=["beginner", "intermediate", "advanced"].index(default_skill),
    )
    vibe = st.text_input("Wave Vibe", value=default_vibe)
    quiver_text = st.text_area(
        "Your Quiver (one board per line)",
        value=default_quiver,
        height=90,
    )

    st.divider()
    use_surfline = st.checkbox("Include Surfline ratings (unofficial)", value=True)

    st.divider()
    st.caption("Optional (for board sizing tip)")
    c1, c2 = st.columns(2)
    height_in = c1.number_input("Height (in)", 55, 84, default_height)
    weight_lb = c2.number_input("Weight (lbs)", 90, 300, default_weight)

    if st.button("Save Setup to URL", use_container_width=True):
        quiver_clean = "|".join(b.strip() for b in quiver_text.splitlines() if b.strip())
        st.query_params.update({
            "loc": location_input.strip(),
            "radius": str(max_radius),
            "skill": skill,
            "vibe": vibe.strip(),
            "quiver": quiver_clean,
            "height": str(height_in),
            "weight": str(weight_lb),
        })
        st.success("Saved! Bookmark this page URL to keep your setup.")

# --- MAIN SCREEN: Minimal Inputs ---
d_col, w_col = st.columns([1, 2])
target_date = d_col.date_input("Day", value=date.today())
start_hr, end_hr = w_col.slider("Available Window (24h)", 5, 20, (6, 10), format="%d:00")

window_mins = (end_hr - start_hr) * 60
window_label = f"{target_date.strftime('%A, %b %d')} from {start_hr}:00 to {end_hr}:00"

if st.button("Make the Call", type="primary", use_container_width=True):
    if not location_input.strip():
        st.error("Enter a starting zip code, neighborhood, or address in the sidebar.")
        st.stop()

    with st.spinner("Locating you & checking swell, wind, tides, Surfline & drive times..."):
        geo = geocode_location(location_input.strip())
        if not geo:
            st.error(f"Couldn't find '{location_input}'. Try a zip code or 'City, CA'.")
            st.stop()

        user_lat, user_lon, resolved_name = geo

        forecasts = cached_window_forecasts(
            user_lat=round(user_lat, 3),  # Rounding improves cache hits for nearby users
            user_lon=round(user_lon, 3),
            max_radius_miles=max_radius,
            target_date=target_date,
            start_hour=start_hr,
            end_hour=end_hr,
            use_surfline=use_surfline,
        )

        if not forecasts:
            st.warning(f"No breaks found within {max_radius} miles of {resolved_name}. Increase your radius.")
            st.stop()

        profile = {
            "starting_location": resolved_name,
            "max_radius_miles": max_radius,
            "skill_level": skill,
            "preferred_vibe": vibe,
            "quiver": [b.strip() for b in quiver_text.splitlines() if b.strip()],
            "height_inches": height_in,
            "weight_lbs": weight_lb,
        }

        decision = make_surf_call(
            profile=profile,
            window_label=window_label,
            window_mins=window_mins,
            spot_forecasts=forecasts,
            api_key=get_gemini_api_key(),
        )

    # --- VERDICT CARD ---
    st.caption(f"📍 Starting from: {resolved_name}")
    badge_color = {"GO": "🟢", "MAYBE": "🟡", "SKIP": "🔴"}[decision.verdict]
    st.subheader(f"{badge_color} {decision.verdict} — {decision.spot_name}")

    m1, m2, m3 = st.columns(3)
    m1.metric("Paddle Out", decision.paddle_out_time)
    m2.metric("Grab Board", decision.recommended_board)
    m3.metric("Est. Water Time", f"~{decision.est_water_time_mins} min")

    st.info(decision.the_call)
    st.markdown(f"**Heads up:** {decision.watch_out}")
    st.markdown(f"**Backup spot:** {decision.runner_up}")

    # 1-Click Surfline Cam Link
    matched = next((f["spot"] for f in forecasts if f["spot"]["name"] == decision.spot_name), None)
    if matched and matched.get("surfline_url"):
        st.link_button("Check Surfline Live Cam", matched["surfline_url"])

    with st.expander("Board Sizing & Volume Check"):
        st.write(decision.board_sizing_tip)

    with st.expander("Raw Conditions Checked (Open-Meteo + NOAA + Surfline)"):
        for item in forecasts:
            s = item["spot"]
            st.markdown(
                f"**{s['name']}** — {item['distance_miles']} mi "
                f"(~{item['est_drive_mins_each_way']}m drive + {item['walk_in_mins_each_way']}m walk)"
            )
            st.dataframe(pd.DataFrame(item["hourly_forecast"]), use_container_width=True, hide_index=True)