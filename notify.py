import json
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv

from surf_data import get_window_forecasts
from surf_brain import DailyDigest, WindowCall, make_daily_digest

load_dotenv()

PACIFIC_TZ = ZoneInfo("America/Los_Angeles")


def load_subscribers() -> list[dict]:
    """Reads subscribers from GitHub Secret SUBSCRIBERS_JSON first, then local subscribers.json."""
    env_json = os.environ.get("SUBSCRIBERS_JSON")
    if env_json:
        return json.loads(env_json)
    local_path = Path("subscribers.json")
    if local_path.exists():
        return json.loads(local_path.read_text())
    raise RuntimeError("No subscribers found. Set SUBSCRIBERS_JSON or create subscribers.json.")


def geocode_location(query: str) -> tuple[float, float, str]:
    resp = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": query, "format": "json", "limit": 1},
        headers={"User-Agent": "SurfCallDigest/1.0"},
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise ValueError(f"Could not geocode location: {query}")
    return float(results[0]["lat"]), float(results[0]["lon"]), results[0]["display_name"]


def build_html_email(
    sub_name: str,
    date_str: str,
    digest: DailyDigest,
    spot_urls: dict[str, str],
) -> str:
    badge_styles = {
        "GO": ("#16a34a", "🟢 GO"),
        "MAYBE": ("#ca8a04", "🟡 MAYBE"),
        "SKIP": ("#dc2626", "🔴 SKIP"),
    }

    def render_card(w: WindowCall) -> str:
        color, badge = badge_styles[w.verdict]
        cam_url = spot_urls.get(w.spot_name)
        cam_btn = (
            f'<p style="margin:8px 0 0 0;"><a href="{cam_url}" style="color:#2563eb;text-decoration:none;font-weight:600;">📹 Check Surfline Live Cam →</a></p>'
            if cam_url and w.verdict != "SKIP"
            else ""
        )
        return f"""
        <div style="border:1px solid #e5e7eb;border-left:5px solid {color};border-radius:8px;padding:14px 16px;margin-bottom:14px;background:#ffffff;">
            <div style="font-size:12px;text-transform:uppercase;letter-spacing:0.5px;color:#6b7280;font-weight:700;">{w.window_name}</div>
            <div style="font-size:18px;font-weight:700;margin:4px 0 6px 0;color:#111827;">{badge} — {w.spot_name}</div>
            <div style="font-size:13px;color:#374151;margin-bottom:8px;">
                <strong>Paddle Out:</strong> {w.paddle_out_time} &nbsp;|&nbsp; <strong>Board:</strong> {w.recommended_board}
            </div>
            <p style="margin:0;font-size:14px;line-height:1.45;color:#1f2937;">{w.summary}</p>
            {cam_btn}
        </div>
        """

    cards_html = (
        render_card(digest.dawn_patrol)
        + render_card(digest.mid_day)
        + render_card(digest.evening_glass_off)
    )

    return f"""
    <html>
      <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f9fafb;padding:20px;color:#111827;">
        <div style="max-width:560px;margin:0 auto;">
          <h2 style="margin:0 0 4px 0;">🏄 Surf Call — {date_str}</h2>
          <p style="margin:0 0 16px 0;color:#4b5563;font-size:14px;">Hey {sub_name} — {digest.headline}</p>
          {cards_html}
          <p style="font-size:12px;color:#9ca3af;margin-top:20px;">
            Powered by Open-Meteo, NOAA Tides, Surfline & Gemini.
          </p>
        </div>
      </body>
    </html>
    """


def send_email(to_email: str, subject: str, html_body: str) -> None:
    gmail_user = os.environ["GMAIL_USER"]
    gmail_pass = os.environ["GMAIL_APP_PASSWORD"]

    msg = MIMEMultipart("alternative")
    msg["From"] = f"Surf Call <{gmail_user}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_pass)
        server.sendmail(gmail_user, to_email, msg.as_string())


def main() -> None:
    today = datetime.now(PACIFIC_TZ).date()
    date_str = today.strftime("%A, %b %d")
    subscribers = load_subscribers()

    # Cache geocoding and forecasts per unique (location, radius)
    geo_cache: dict[str, tuple[float, float, str]] = {}
    forecast_cache: dict[tuple[str, int], list[dict]] = {}

    for sub in subscribers:
        loc = sub["location"]
        radius = int(sub.get("max_radius_miles", 15))

        if loc not in geo_cache:
            geo_cache[loc] = geocode_location(loc)
        lat, lon, resolved_name = geo_cache[loc]

        cache_key = (loc, radius)
        if cache_key not in forecast_cache:
            forecast_cache[cache_key] = get_window_forecasts(
                user_lat=lat,
                user_lon=lon,
                max_radius_miles=radius,
                target_date=today,
                start_hour=6,
                end_hour=18,
                use_surfline=True,
            )

        forecasts = forecast_cache[cache_key]
        if not forecasts:
            print(f"[Skip] No spots within {radius} mi of {resolved_name} for {sub['name']}")
            continue

        digest = make_daily_digest(
            profile=sub,
            target_date_str=date_str,
            full_day_forecasts=forecasts,
        )

        spot_urls = {f["spot"]["name"]: f["spot"].get("surfline_url", "") for f in forecasts}
        top_verdict = digest.dawn_patrol.verdict
        subject = f"🏄 Surf Call ({date_str}): {top_verdict} Dawn Patrol — {digest.dawn_patrol.spot_name}"
        html = build_html_email(sub["name"], date_str, digest, spot_urls)

        send_email(sub["email"], subject, html)
        print(f"[Sent] Digest emailed to {sub['name']} ({sub['email']})")


if __name__ == "__main__":
    main()