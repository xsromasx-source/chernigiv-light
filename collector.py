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
from PIL import Image, ImageChops, ImageFilter

CHANNEL = "chernigivoblenergo"
BASE = "https://t.me/s/" + CHANNEL
DOCS = Path(__file__).resolve().parent / "docs"
IMAGE_DIR = DOCS / "assets" / "charts"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Language": "uk,en;q=0.8"}
# Only actual posts containing a published graph can fill the image blocks.
GRAPH = re.compile(r"\bграфік\s+погодинних\s+відключень\b", re.I)
# The real channel uses "маємо зміни у Графіку ...", but people also say "зміни в ...".
CHANGES = re.compile(
    r"\b(?:маємо\s+)?зміни\s+(?:в|у)\s+графіку\s+погодинних\s+відключень\b",
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


def is_change_post(post):
    return bool(CHANGES.search(post["text"]))


def is_graph_post(post):
    return bool(post["photos"] and GRAPH.search(post["text"]) and
                not is_change_post(post))


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
        charts = [p for p in collected.values() if is_graph_post(p)]
        changes = [p for p in collected.values() if is_change_post(p)]
        if len(charts) >= 2 and changes:
            break
        old = min(p["id"] for p in page)
        if before is not None and old >= before:
            break
        before = old
    return sorted(collected.values(), key=lambda x: x["id"], reverse=True)


def trim_white_borders(raw):
    """Conservatively remove near-white outer padding without cropping the graph."""
    with Image.open(BytesIO(raw)) as original:
        original.load()
        image_format = original.format
        if image_format not in ("JPEG", "PNG", "WEBP"):
            raise ValueError("Unsupported image format: " + str(image_format))

        # Detect the visible chart on a reduced copy; preserve the full resolution.
        sample = original.convert("RGB")
        sample.thumbnail((1300, 1300), Image.Resampling.LANCZOS)
        white = Image.new("RGB", sample.size, (255, 255, 255))
        difference = ImageChops.difference(sample, white).convert("L")
        visible = difference.point(lambda v: 255 if v >= 25 else 0)
        # Isolated compression artefacts must not dictate the bounding box.
        visible = visible.filter(ImageFilter.MedianFilter(3))
        bounds = visible.getbbox()
        if not bounds:
            return raw, image_format, False

        scale_x = original.width / sample.width
        scale_y = original.height / sample.height
        pad_x = max(8, round(original.width * 0.015))
        pad_y = max(8, round(original.height * 0.015))
        left = max(0, int(bounds[0] * scale_x) - pad_x)
        top = max(0, int(bounds[1] * scale_y) - pad_y)
        right = min(original.width, int(bounds[2] * scale_x + 0.999) + pad_x)
        bottom = min(original.height, int(bounds[3] * scale_y + 0.999) + pad_y)
        new_width, new_height = right - left, bottom - top
        if (new_width < 60 or new_height < 60 or
                (new_width * new_height) > (original.width * original.height * 0.92)):
            return raw, image_format, False

        cropped = original.crop((left, top, right, bottom))
        stream = BytesIO()
        if image_format == "JPEG":
            cropped.convert("RGB").save(stream, format="JPEG", quality=94, optimize=True)
        elif image_format == "WEBP":
            cropped.save(stream, format="WEBP", quality=94, method=4)
        else:
            cropped.save(stream, format="PNG", optimize=True)
        logging.info("Cropped white border: %s -> %s pixels",
                     original.size, cropped.size)
        return stream.getvalue(), image_format, True


def fetch_image(session, url, post_id):
    parts = urlsplit(url)
    domain = (parts.hostname or "").lower()
    allowed = (domain == "telesco.pe" or domain.endswith(".telesco.pe") or
               domain == "telegram-cdn.org" or domain.endswith(".telegram-cdn.org"))
    if parts.scheme != "https" or not allowed:
        raise ValueError("Unexpected Telegram media URL")
    response = session.get(url, headers={**HEADERS, "Referer": "https://t.me/"},
                           stream=True, timeout=35)
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

    image_bytes, kind, cropped = trim_white_borders(bytes(raw))
    ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}[kind]
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    # Hash the output image to invalidate cached copies when the crop changes.
    digest = hashlib.sha256(image_bytes).hexdigest()[:12]
    name = str(post_id) + ("-trim-" if cropped else "-") + digest + "." + ext
    path = IMAGE_DIR / name
    temp = path.with_suffix("." + ext + ".tmp")
    temp.write_bytes(image_bytes)
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

    # Correction posts may also contain images: never show them as charts.
    # Telegram message IDs increase over time, so the first two are newest.
    charts = [p for p in posts if is_graph_post(p)]
    if not charts:
        raise RuntimeError("No graph posts with an image and the required phrase found")

    latest = chart_item(session, charts[0])
    previous = None
    if len(charts) > 1:
        try:
            previous = chart_item(session, charts[1])
        except Exception:
            logging.exception("Could not download previous graph")

    updates = [p for p in posts if is_change_post(p)]
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
