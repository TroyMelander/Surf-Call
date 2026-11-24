import json
import os
import smtplib
from datetime import date
from email.message import EmailMessage
from pathlib import Path
import requests

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


def format_message(decision) -> tuple[str, str]:
    badge = {"GO": "🟢 GO", "MAYBE": "🟡 MAYBE", "SKIP": "🔴 SKIP"}[decision.verdict]
    title = f"{badge}: {decision.spot_name} @ {decision.paddle_out_time}"
    body = (
        f"Board: {decision.recommended_board} (~{decision.est_water_time_mins} min water time)\n\n"
        f"{decision.the_call}\n\n"
        f"Heads up: {decision.watch_out}\n"
        f"Backup: {decision.runner_up}"
    )
    return title, body


def send_ntfy(topic: str, title: str, body: str, verdict: str) -> None:
    priority = "high" if verdict == "GO" else "default"
    requests.post(
        f"https://ntfy.sh/{topic}",
        data=body.encode("utf-8"),
        headers={"Title": title.encode("utf-8"), "Priority": priority, "Tags": "ocean"},
        timeout=10,
    ).raise_for_status()


def send_email(to_addr: str, from_addr: str, app_password: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(from_addr, app_password)
        smtp.send_message(msg)


def main() -> None:
    profile = load_profile()
    today = date.today()
    start_hr = int(os.getenv("SURF_START_HOUR", "6"))
    end_hr = int(os.getenv("SURF_END_HOUR", "10"))
    window_mins = (end_hr - start_hr) * 60
    window_label = f"{today.strftime('%A, %b %d')} from {start_hr}:00 to {end_hr}:00"

    forecasts = get_window_forecasts(
        user_lat=profile["home_lat"],
        user_lon=profile["home_lon"],
        max_radius_miles=profile["max_radius_miles"],
        target_date=today,
        start_hour=start_hr,
        end_hour=end_hr,
    )
    if not forecasts:
        print("No spots within radius.")
        return

    decision = make_surf_call(profile, window_label, window_mins, forecasts)
    title, body = format_message(decision)
    print(f"\n{title}\n{'-' * 40}\n{body}\n")

    # Optional: skip sending notifications on SKIP days if QUIET_ON_SKIP=true
    if os.getenv("QUIET_ON_SKIP", "false").lower() == "true" and decision.verdict == "SKIP":
        print("Verdict is SKIP and QUIET_ON_SKIP is enabled. Skipping notification.")
        return

    ntfy_topic = os.getenv("NTFY_TOPIC")
    if ntfy_topic:
        send_ntfy(ntfy_topic, title, body, decision.verdict)
        print(f"Sent ntfy push to topic: {ntfy_topic}")

    email_to = os.getenv("EMAIL_TO")
    email_from = os.getenv("EMAIL_FROM")
    email_pass = os.getenv("EMAIL_APP_PASSWORD")
    if email_to and email_from and email_pass:
        send_email(email_to, email_from, email_pass, title, body)
        print(f"Sent email to: {email_to}")


if __name__ == "__main__":
    main()