#!/usr/bin/env python3
"""Read public Telegram posts and publish the latest schedule image for GitHub Pages."""
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from html import unescape
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup
from PIL import Image

CHANNEL = "chernigivoblenergo"
BASE = "https://t.me/s/" + CHANNEL
DOCS = Path(__file__).resolve().parent / "docs"
IMAGE_DIR = DOCS / "assets" / "charts"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Language": "uk,en;q=0.8"}
SCHEDULE = re.compile(r"графік[ауі]?\s+погодинних\s+відключень|графіку\s+погодинних\s+відключень|\bгпв\b", re.I)
CHANGES = re.compile(
    r"маємо\s+зміни|зміни\s+(?:у|в|до)\s+графік|"
    r"графік\s+(?:скориговано|змінено|оновлено)|"
    r"коригування\s+графік|"
    r"додатково\s+відключається|продовжується\s+відключення",
    re.I,
)
CSS_URL = re.compile(r"url\(\s*['\"]?(https?://[^)'\"\s]+)['\"]?\s*\)", re.I)


def parse_posts(html):
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for el in soup.select(".tgme_widget_message[data-post]"):
        code = el.get("data-post", "")
        if not code.startswith(CHANNEL + "/"):
            continue
        try:
            pid = int(code.split("/")[-1])
        except ValueError:
            continue
        text_el = el.select_one(".tgme_widget_message_text")
        text = text_el.get_text("\n", strip=True) if text_el else ""
        time_el = el.select_one("time[datetime]")
        date = time_el.get("datetime", "") if time_el else ""
        photos = []
        for el_img in el.select(".tgme_widget_message_photo_wrap"):
            found = CSS_URL.search(unescape(el_img.get("style", "")))
            if found:
                photos.append(found.group(1))
        posts.append({"id": pid, "text": text, "date": date,
                      "link": "https://t.me/" + CHANNEL + "/" + str(pid),
                      "photos": photos})
    return sorted(posts, key=lambda x: x["id"], reverse=True)


def get_posts(session):
    collected = {}
    before = None
    for _ in range(12):
        params = {"before": before} if before else None
        response = session.get(BASE, params=params, headers=HEADERS, timeout=30)
        response.raise_for_status()
        page = parse_posts(response.text)
        if not page:
            raise RuntimeError("Telegram did not return public posts")
        collected.update({post["id"]: post for post in page})
        charts = [p for p in collected.values()
                  if SCHEDULE.search(p["text"][:600]) and p["photos"]]
        changes = [p for p in collected.values()
                   if SCHEDULE.search(p["text"][:600]) and CHANGES.search(p["text"][:320])]
        if len(charts) >= 2 and changes:
            break
        old = min(p["id"] for p in page)
        if before is not None and old >= before:
            break
        before = old
    return sorted(collected.values(), key=lambda x: x["id"], reverse=True)


def fetch_image(session, url, post_id):
    parts = urlsplit(url)
    domain = (parts.hostname or "").lower()
    allowed = domain == "telesco.pe" or domain.endswith(".telesco.pe") or domain == "telegram-cdn.org" or domain.endswith(".telegram-cdn.org")
    if parts.scheme != "https" or not allowed:
        raise ValueError("Unexpected Telegram media URL")
    response = session.get(url, headers={**HEADERS, "Referer": "https://t.me/"}, stream=True, timeout=35)
    response.raise_for_status()
    raw = bytearray()
    for chunk in response.iter_content(65536):
        raw.extend(chunk)
        if len(raw) > 15 * 1024 * 1024:
            raise ValueError("Image exceeds 15 MB")
    if not raw:
        raise ValueError("Image is empty")
    with Image.open(BytesIO(raw)) as picture:
        picture.verify()
    with Image.open(BytesIO(raw)) as picture:
        kind = picture.format
    ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}.get(kind)
    if not ext:
        raise ValueError("Unsupported image format: " + str(kind))
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    name = str(post_id) + "-" + hashlib.sha256(raw).hexdigest()[:12] + "." + ext
    path = IMAGE_DIR / name
    temp = path.with_suffix("." + ext + ".tmp")
    temp.write_bytes(raw)
    os.replace(temp, path)
    return "assets/charts/" + name


def public_post(post):
    return {key: post[key] for key in ("id", "text", "date", "link")}


def chart_item(session, post):
    item = public_post(post)
    image = fetch_image(session, post["photos"][0], post["id"])
    item["image"] = image
    item["media_key"] = image.rsplit("/", 1)[-1]
    return item


def main():
    DOCS.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    posts = get_posts(session)

    # A schedule with a photo is a graph. Changes often come as text-only posts,
    # and must not replace the chart image.
    charts = [p for p in posts if p["photos"] and SCHEDULE.search(p["text"][:600])]
    normal_charts = [p for p in charts if not CHANGES.search(p["text"][:320])]
    if not charts:
        raise RuntimeError("No schedule images found in recent Telegram posts")

    # Prefer regular graph publications over an attached image in a correction
    # only if the latter is not actually the newer graphic.
    ordered = sorted(charts, key=lambda p: p["id"], reverse=True)
    latest = chart_item(session, ordered[0])
    previous = None
    if len(ordered) > 1:
        try:
            previous = chart_item(session, ordered[1])
        except Exception:
            logging.exception("Could not download previous schedule image")
    elif normal_charts and normal_charts[0]["id"] != ordered[0]["id"]:
        try:
            previous = chart_item(session, normal_charts[0])
        except Exception:
            logging.exception("Could not download older schedule image")

    updates = [p for p in posts
               if SCHEDULE.search(p["text"][:600]) and CHANGES.search(p["text"][:320])]
    notice = public_post(updates[0]) if updates else None
    payload = {
        "channel": CHANNEL,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "latest_chart": latest,
        "previous_chart": previous,
        "latest_change": notice,
        # Legacy clients:
        "latest_notice": notice,
        "history": [item for item in (latest, previous) if item],
    }
    (DOCS / "data.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    logging.info("Latest graph: %s; previous graph: %s; last change: %s",
                 latest["id"], previous["id"] if previous else None,
                 notice["id"] if notice else None)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
