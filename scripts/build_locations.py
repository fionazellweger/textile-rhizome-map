#!/usr/bin/env python3
"""Build data/locations.geojson from the entries of https://textilerhizome.ch.

Every entry page embeds a Google Maps iframe. Its `pb` parameter contains the
place/address (`!2s...`) and the map centre (`!2d<lon>!3d<lat>`). The address is
geocoded with Nominatim (OpenStreetMap); the map centre is used as fallback or
when the entry has no street address (only a business name).

Only the Python standard library is used.
"""

import html
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://textilerhizome.ch"
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT = DATA_DIR / "locations.geojson"
CACHE = DATA_DIR / "geocode-cache.json"
MISSING = DATA_DIR / "missing.json"

USER_AGENT = "textile-rhizome-map/1.0 (https://github.com/; weekly build script)"
SITE_DELAY = 1.0  # seconds between requests to textilerhizome.ch
NOMINATIM_DELAY = 1.1  # Nominatim usage policy: max 1 request per second
MAX_DISTANCE_KM = 3.0  # geocoded point must be this close to the embed centre
STOP_AFTER_MISSES = 5  # consecutive unknown ids before probing stops
CITY_SPAN_M = 20000  # embed view wider than this = city level, not a place
COUNTRY_SPAN_M = 1000000  # embed view wider than this = whole country, no location

EMBED_RE = re.compile(
    r"google\.com(?:\\u002F|/)maps(?:\\u002F|/)embed\?pb=([^\"\\\s<>']+)"
)
INDEX_LINK_RE = re.compile(r"/2_index/(\d+)_")
CANONICAL_RE = re.compile(r'<link rel="canonical" href="([^"]+)"')
TITLE_RE = re.compile(r"<title>([^<]*)</title>")


def fetch(url):
    """Return the response text, or None for HTTP 404."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def discover_ids():
    """Entry ids from the (server rendered) index, plus probing for more.

    The index only renders the first 25 entries; the rest is loaded by
    JavaScript. Detail pages resolve by id alone, so ids after the highest
    known one are probed until several consecutive ids do not exist.
    """
    page = fetch(f"{BASE}/?page=1") or ""
    ids = {int(i) for i in INDEX_LINK_RE.findall(page)}
    if not ids:
        sys.exit("No entries found on the index page - did the site change?")
    return ids


def parse_entry(entry_id):
    page = fetch(f"{BASE}/2_index/{entry_id}_x")
    time.sleep(SITE_DELAY)
    if page is None:
        return None
    title_match = TITLE_RE.search(page)
    title = html.unescape(title_match.group(1)) if title_match else str(entry_id)
    title = re.sub(r"^Textile Rhizome\s*[-–]\s*", "", title).strip()
    canonical = CANONICAL_RE.search(page)
    url = html.unescape(canonical.group(1)) if canonical else f"{BASE}/2_index/{entry_id}_x"
    embed = EMBED_RE.search(page)
    entry = {"id": entry_id, "name": title, "url": url, "pb": embed.group(1) if embed else None}
    return entry


def parse_pb(pb):
    """Return (address_or_name, (lon, lat) or None, view_span_m or None) from a Google embed `pb`."""
    place = re.search(r"!2s([^!]+)", pb)
    centre = re.search(r"!2d(-?\d+(?:\.\d+)?)!3d(-?\d+(?:\.\d+)?)", pb)
    span = re.search(r"!1d(\d+(?:\.\d+)?)", pb)
    label = urllib.parse.unquote(place.group(1)) if place else None
    coords = (float(centre.group(1)), float(centre.group(2))) if centre else None
    return label, coords, float(span.group(1)) if span else None


def distance_km(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 6371 * 2 * math.asin(math.sqrt(h))


def load_cache():
    if CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    return {}


def geocode(address, cache):
    """Nominatim lookup, cached. Returns (lon, lat) or None."""
    if address in cache:
        value = cache[address]
        return tuple(value) if value else None
    query = urllib.parse.urlencode(
        {"q": address, "format": "jsonv2", "limit": 1, "countrycodes": "ch"}
    )
    raw = fetch(f"https://nominatim.openstreetmap.org/search?{query}")
    time.sleep(NOMINATIM_DELAY)
    results = json.loads(raw) if raw else []
    point = (float(results[0]["lon"]), float(results[0]["lat"])) if results else None
    cache[address] = list(point) if point else None
    return point


def looks_like_address(label):
    return bool(label) and bool(re.search(r"\d", label))


def locate(entry, cache):
    """Return (point, source, address) for an entry, or (None, reason, None)."""
    if not entry["pb"]:
        return None, "no google maps embed", None
    label, centre, span = parse_pb(entry["pb"])
    if span and span > COUNTRY_SPAN_M:
        return None, "country-level only, no location", label
    if span and span > CITY_SPAN_M and centre:
        return centre, "embed centre (city-level only, approximate)", label
    if looks_like_address(label):
        point = geocode(label, cache)
        if point and (not centre or distance_km(point, centre) <= MAX_DISTANCE_KM):
            return point, "geocoded", label
        if centre:
            reason = "geocode failed" if not point else "geocode far from embed centre"
            return centre, f"embed centre ({reason})", label
        return point, "geocoded", label
    if centre:
        return centre, "embed centre (no street address)", label
    return None, "no coordinates in embed", label


def main():
    DATA_DIR.mkdir(exist_ok=True)
    cache = load_cache()

    known_ids = discover_ids()
    entries = {}
    candidates = sorted(known_ids)
    for entry_id in candidates:
        entry = parse_entry(entry_id)
        if entry:
            entries[entry_id] = entry

    # Probe beyond the highest id (index only renders the first page) and fill gaps.
    low, high = min(known_ids), max(known_ids)
    for entry_id in range(low, high + 1):
        if entry_id not in entries:
            entry = parse_entry(entry_id)
            if entry:
                entries[entry_id] = entry
    misses, entry_id = 0, high + 1
    while misses < STOP_AFTER_MISSES:
        entry = parse_entry(entry_id)
        if entry:
            entries[entry_id] = entry
            misses = 0
        else:
            misses += 1
        entry_id += 1

    features, missing = [], []
    for entry_id in sorted(entries):
        entry = entries[entry_id]
        point, source, address = locate(entry, cache)
        print(f"{entry_id:>3} {entry['name'][:45]:<45} {source:<40} {address or ''}")
        if point is None:
            missing.append({"id": entry_id, "name": entry["name"], "url": entry["url"], "reason": source})
            continue
        approximate = "approximate" in source or "geocode" in source and source != "geocoded"
        if approximate:
            missing.append({"id": entry_id, "name": entry["name"], "url": entry["url"], "reason": source})
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(point[0], 6), round(point[1], 6)]},
                "properties": {
                    "name": entry["name"],
                    "url": entry["url"],
                    "address": address or "",
                    "approximate": approximate,
                },
            }
        )

    OUTPUT.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    MISSING.write_text(json.dumps(missing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n{len(features)} of {len(entries)} entries written to {OUTPUT.relative_to(ROOT)}")
    print(f"{len(missing)} entries skipped or approximate (see data/missing.json)")


if __name__ == "__main__":
    main()
