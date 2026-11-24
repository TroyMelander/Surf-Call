import json
from datetime import date
from pathlib import Path
import pandas as pd
import streamlit as st

from surf_data import get_window_forecasts
from surf_brain import make_surf_call

PROFILE_PATH = Path("profile.json")

DEFAULT_PROFILE = {
    "home_lat": 32.867,
    "home_lon": -117.213,
    "max_radius_miles": 15,
    "skill_level": "intermediate",
    "preferred_vibe": "clean peeling waves, mid-length friendly",
    "quiver": ["7'6\" mid-length", "8'6\" funboard", "9'6\" longboard"],
    "height_inches": 76,
    "weight_lbs": 190,
}


def load_profile() -> dict:
    if PROFILE_PATH.exists():
        return {**DEFAULT_PROFILE, **json.loads(PROFILE_PATH.read_text())}
    return DEFAULT_PROFILE.copy()


def save_profile(profile: dict) -> None:
    PROFILE_PATH.write_text(json.dumps(profile, indent=2))


st.set_page_config(page_title="Surf Call", page_icon="🏄", layout="centered")
st.title("🏄 Surf Call")
st.caption("Zero-thinking surf recommender powered by Open-Meteo, NOAA Tides & Gemini.")

profile = load_profile()

# --- SIDEBAR: Set Once, Adjust Anytime ---
with st.sidebar:
    st.header("Your Setup (Set Once)")
    col1, col2 = st.columns(2)
    home_lat = col1.number_input("Home Lat", value=float(profile["home_lat"]), format="%.4f")
    home_lon = col2.number_input("Home Lon", value=float(profile["home_lon"]), format="%.4f")
    max_radius = st.slider("Max Radius (miles)", 3, 35, int(profile["max_radius_miles"]))

    skill = st.selectbox(
        "Skill Level",
        ["beginner", "intermediate", "advanced"],
        index=["beginner", "intermediate", "advanced"].index(profile["skill_level"]),
    )
    vibe = st.text_input("Wave Vibe", value=profile["preferred_vibe"])
    quiver_text = st.text_area(
        "Your Quiver (one board per line)",
        value="\n".join(profile["quiver"]),
        height=90,
    )

    st.divider()
    st.caption("Optional (for board sizing tip)")
    c1, c2 = st.columns(2)
    height_in = c1.number_input("Height (in)", 55, 84, int(profile["height_inches"]))
    weight_lb = c2.number_input("Weight (lbs)", 90, 300, int(profile["weight_lbs"]))

    if st.button("Save Setup", use_container_width=True):
        updated = {
            "home_lat": home_lat,
            "home_lon": home_lon,
            "max_radius_miles": max_radius,
            "skill_level": skill,
            "preferred_vibe": vibe,
            "quiver": [b.strip() for b in quiver_text.splitlines() if b.strip()],
            "height_inches": height_in,
            "weight_lbs": weight_lb,
        }
        save_profile(updated)
        profile = updated
        st.success("Saved!")

# --- MAIN SCREEN: Minimal Inputs ---
d_col, w_col = st.columns([1, 2])
target_date = d_col.date_input("Day", value=date.today())
start_hr, end_hr = w_col.slider("Available Window (24h)", 5, 20, (6, 10), format="%d:00")

window_mins = (end_hr - start_hr) * 60
window_label = f"{target_date.strftime('%A, %b %d')} from {start_hr}:00 to {end_hr}:00"

if st.button("Make the Call", type="primary", use_container_width=True):
    with st.spinner("Checking swell, wind, tides & drive times..."):
        forecasts = get_window_forecasts(
            user_lat=home_lat,
            user_lon=home_lon,
            max_radius_miles=max_radius,
            target_date=target_date,
            start_hour=start_hr,
            end_hour=end_hr,
        )

        if not forecasts:
            st.warning("No breaks found within your radius. Increase your radius in the sidebar.")
            st.stop()

        decision = make_surf_call(profile, window_label, window_mins, forecasts)

    # --- VERDICT CARD ---
    badge_color = {"GO": "🟢", "MAYBE": "🟡", "SKIP": "🔴"}[decision.verdict]
    st.subheader(f"{badge_color} {decision.verdict} — {decision.spot_name}")

    m1, m2, m3 = st.columns(3)
    m1.metric("Paddle Out", decision.paddle_out_time)
    m2.metric("Grab Board", decision.recommended_board)
    m3.metric("Est. Water Time", f"~{decision.est_water_time_mins} min")

    st.info(decision.the_call)
    st.markdown(f"**Heads up:** {decision.watch_out}")
    st.markdown(f"**Backup spot:** {decision.runner_up}")

    with st.expander("Board Sizing & Volume Check"):
        st.write(decision.board_sizing_tip)

    with st.expander("Raw Conditions Checked (Open-Meteo + NOAA)"):
        for item in forecasts:
            s = item["spot"]
            st.markdown(
                f"**{s['name']}** — {item['distance_miles']} mi "
                f"(~{item['est_drive_mins_each_way']}m drive + {item['walk_in_mins_each_way']}m walk)"
            )
            st.dataframe(pd.DataFrame(item["hourly_forecast"]), use_container_width=True, hide_index=True)
    
        matched = next((f["spot"] for f in forecasts if f["spot"]["name"] == decision.spot_name), None)
    if matched and matched.get("surfline_url"):
        st.link_button("Check Surfline Live Cam", matched["surfline_url"])