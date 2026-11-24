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


def create_horizontal_chart(image_path, post_id):
    """Keep the heading above; set the upper and lower GROUP ROWS side by side.

    Never split through group cells. If the row separators cannot be detected
    confidently, keep displaying the original picture instead.
    """
    try:
        with Image.open(image_path) as opened:
            original = opened.convert("RGB")
        width, height = original.size
        if width < 180 or height < 220:
            return None

        small = original.copy()
        small.thumbnail((680, 1100), Image.Resampling.LANCZOS)
        sw, sh = small.size
        pixels = small.load()
        step = 2
        threshold = max(8, int(sw * .065 / step))

        # Orange / peach coloured tiles define graph rows in the source artwork.
        active_rows = []
        for y in range(sh):
            warm = 0
            for x in range(0, sw, step):
                red, green, blue = pixels[x, y]
                if (red >= 145 and 65 <= green <= 246 and
                        red - green >= 12 and green - blue >= 11):
                    warm += 1
            active_rows.append(warm >= threshold)

        runs = []
        start = None
        for y, present in enumerate(active_rows + [False]):
            if present and start is None:
                start = y
            elif not present and start is not None:
                runs.append((start, y))
                start = None

        # Heal tiny holes within a tile caused by text or compression noise.
        merged = []
        max_hole = max(2, int(sh * .006))
        for a, b in runs:
            if merged and a - merged[-1][1] <= max_hole:
                merged[-1] = (merged[-1][0], b)
            else:
                merged.append((a, b))
        min_row_height = max(10, int(sh * .025))
        rows = [(a, b) for a, b in merged if b - a >= min_row_height]
        if len(rows) < 2 or len(rows) % 2 or len(rows) > 10:
            logging.info("Skip horizontal chart %s: found %s group rows", post_id, len(rows))
            return None

        first_row = rows[0][0]
        if not (.06 * sh <= first_row <= .50 * sh):
            return None
        halfway = len(rows) // 2
        cut_sample = (rows[halfway - 1][1] + rows[halfway][0]) / 2
        # Separators must be free of coloured table cells.
        if rows[halfway][0] - rows[halfway - 1][1] < 3:
            return None

        sy = height / sh
        top_groups = max(0, round((first_row - max(3, sh * .009)) * sy))
        divide = round(cut_sample * sy)
        if not (28 < top_groups < divide - 50 < height - 100):
            return None
        before_height = divide - top_groups
        after_height = height - divide
        if min(before_height, after_height) / max(before_height, after_height) < .52:
            return None

        # Keep only the table, cutting away the original heading entirely.
        # Use the orange-coloured group cells to find safe outer bounds.
        left_x, right_x = sw, 0
        for y in range(rows[0][0], rows[-1][1], 2):
            for x in range(0, sw, 2):
                red, green, blue = pixels[x, y]
                if (red >= 145 and 65 <= green <= 246 and
                        red - green >= 12 and green - blue >= 11):
                    left_x = min(left_x, x)
                    right_x = max(right_x, x)
        if right_x <= left_x or (right_x - left_x) < sw * .18:
            return None

        sx = width / sw
        pad_x = max(9, round(width * .019))
        pad_y = max(8, round(height * .012))
        x0 = max(0, int(left_x * sx) - pad_x)
        x1 = min(width, int((right_x + 2) * sx) + pad_x)
        top_first = max(0, round(rows[0][0] * sy) - pad_y)
        bottom_first = min(height, round(rows[halfway - 1][1] * sy) + pad_y)
        top_second = max(0, round(rows[halfway][0] * sy) - pad_y)
        bottom_second = min(height, round(rows[-1][1] * sy) + pad_y)
        if (top_first >= bottom_first or top_second >= bottom_second or
                bottom_first >= top_second or x1 - x0 < 100):
            return None

        first = original.crop((x0, top_first, x1, bottom_first))
        second = original.crop((x0, top_second, x1, bottom_second))
        gutter = max(12, round((x1 - x0) * .025))
        out_width = first.width + second.width + gutter
        out_height = max(first.height, second.height)
        combined = Image.new("RGB", (out_width, out_height), "#f8fafb")
        combined.paste(first, (0, 0))
        combined.paste(second, (first.width + gutter, 0))
        stream = BytesIO()
        combined.save(stream, "JPEG", quality=94, optimize=True)
        raw = stream.getvalue()
        digest = hashlib.sha256(raw).hexdigest()[:12]
        name = str(post_id) + "-horizontal-v3-" + digest + ".jpg"
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        dest = IMAGE_DIR / name
        dest.write_bytes(raw)
        logging.info("Horizontal layout %s: rows=%s, %sx%s -> %sx%s",
                     post_id, len(rows), width, height, out_width, out_height)
        return "assets/charts/" + name
    except Exception:
        logging.exception("Horizontal layout failed for Telegram post %s", post_id)
        return None


# Date of the SCHEDULE, not the Telegram publication date.
MONTH_NAMES = ("січня", "лютого", "березня", "квітня", "травня", "червня",
               "липня", "серпня", "вересня", "жовтня", "листопада", "грудня")
MONTH_PATTERN = re.compile(
    r"(?<!\d)([0-3]?\d)\s+(" + "|".join(MONTH_NAMES) +
    r")(?:\s+(\d{4})\s*(?:р(?:оку|\.)?)?)?\b", re.I
)
DATE_PATTERN = re.compile(r"(?<!\d)([0-3]?\d)[./]([01]?\d)[./](\d{2,4})(?!\d)")


def date_from_graph_description(text):
    """Read the graph's date from the original Telegram message; never guess."""
    # The operator's date normally precedes "Графік погодинних відключень".
    introduction = re.split(r"⚠️|Нагадуємо|Якщо ситуація", text, maxsplit=1)[0][:500]
    found = []
    for match in MONTH_PATTERN.finditer(introduction):
        day, month, year = match.groups()
        if 1 <= int(day) <= 31:
            found.append((match.start(), f"{int(day)} {month.lower()}" +
                          (f" {year}" if year else "")))
    for match in DATE_PATTERN.finditer(introduction):
        day, month, year = match.groups()
        if 1 <= int(day) <= 31 and 1 <= int(month) <= 12:
            year = ("20" + year) if len(year) == 2 else year
            found.append((match.start(), f"{int(day)} {MONTH_NAMES[int(month)-1]} {year}"))
    return min(found, key=lambda item: item[0])[1] if found else None


def public_post(post):
    return {key: post[key] for key in ("id", "text", "date", "link")}


def chart_item(session, post):
    item = public_post(post)
    image = fetch_image(session, post["photos"][0], post["id"])
    item["image"] = image
    item["media_key"] = image.rsplit("/", 1)[-1]
    item["graph_date"] = date_from_graph_description(post["text"])
    horizontal = create_horizontal_chart(DOCS / image, post["id"])
    if horizontal:
        item["landscape_image"] = horizontal
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
