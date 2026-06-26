import json
import time
import os
import re
from datetime import date
from pathlib import Path

import requests

# Config

LASTFM_API_KEY = os.environ["LASTFM_API_KEY"]
LASTFM_BASE    = "https://ws.audioscrobbler.com/2.0/"
MB_BASE        = "https://musicbrainz.org/ws/2/"
MB_HEADERS     = {"User-Agent": "dubstep-friday-bot/1.0 (github.com/phillipstr/r-dubstep-nmf)"}

# Tags to pull from Last.fm (combined, deduped by ISRC)
TAGS = ["dubstep", "brostep", "riddim", "future bass dubstep"]

# How many tracks to fetch per tag (max 1000 via pagination, 50 per page)
TRACKS_PER_TAG = 100

# Only keep tracks released within the last N days
RELEASE_WINDOW_DAYS = 8  # slightly over a week catches anything that slipped through last Friday

# Last.fm helpers

def lastfm_get(method: str, **params) -> dict:
    resp = requests.get(
        LASTFM_BASE,
        params={"method": method, "api_key": LASTFM_API_KEY, "format": "json", **params},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_tag_tracks(tag: str, limit: int = TRACKS_PER_TAG) -> list[dict]:
    """Return top tracks for a Last.fm tag, newest-biased via toptracks."""
    tracks = []
    page, per_page = 1, 50

    while len(tracks) < limit:
        data = lastfm_get("tag.gettoptracks", tag=tag, limit=per_page, page=page)
        batch = data.get("tracks", {}).get("track", [])
        if not batch:
            break
        tracks.extend(batch)
        page += 1
        time.sleep(0.25)  # be polite to the API

    return tracks[:limit]


def fetch_track_info(artist: str, title: str) -> dict:
    """Get extended track info (wiki, tags) from Last.fm."""
    try:
        data = lastfm_get("track.getInfo", artist=artist, track=title)
        return data.get("track", {})
    except Exception:
        return {}


# MusicBrainz helpers

def mb_search_recording(artist: str, title: str) -> dict | None:
    """Search MusicBrainz for a recording and return the best match."""
    query = f'recording:"{title}" AND artist:"{artist}"'
    try:
        resp = requests.get(
            f"{MB_BASE}recording/",
            params={"query": query, "limit": 1, "fmt": "json"},
            headers=MB_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        recordings = resp.json().get("recordings", [])
        return recordings[0] if recordings else None
    except Exception:
        return None


def extract_mb_metadata(recording: dict) -> dict:
    """Pull ISRC, release date, and label out of a MusicBrainz recording."""
    meta: dict = {}

    # Release date — use earliest release
    releases = recording.get("releases", [])
    dates = []
    for r in releases:
        d = r.get("date", "")
        if re.match(r"\d{4}-\d{2}-\d{2}", d):
            dates.append(d)
        elif re.match(r"\d{4}", d):
            dates.append(f"{d}-01-01")
    if dates:
        meta["release_date"] = sorted(dates)[0]

    # Label
    for r in releases:
        for lc in r.get("label-info", []):
            label = lc.get("label", {}).get("name")
            if label:
                meta["label"] = label
                break
        if "label" in meta:
            break

    # ISRCs are on a separate endpoint — fetch if we have an ID
    mb_id = recording.get("id")
    if mb_id:
        meta["mb_id"] = mb_id
        try:
            resp = requests.get(
                f"{MB_BASE}recording/{mb_id}",
                params={"inc": "isrcs", "fmt": "json"},
                headers=MB_HEADERS,
                timeout=15,
            )
            resp.raise_for_status()
            isrcs = resp.json().get("isrcs", [])
            if isrcs:
                meta["isrc"] = isrcs[0]
        except Exception:
            pass
        time.sleep(1.1)  # MusicBrainz rate limit: 1 req/sec

    return meta


# Release date filter

def is_recent(release_date_str: str | None, window_days: int = RELEASE_WINDOW_DAYS) -> bool:
    if not release_date_str:
        return False  # drop tracks we can't date
    try:
        rd = date.fromisoformat(release_date_str[:10])
        return (date.today() - rd).days <= window_days
    except ValueError:
        return False


# Main pipeline

def build_playlist() -> list[dict]:
    seen_keys: set[str] = set()   # dedupe by (artist_lower, title_lower)
    results: list[dict] = []

    for tag in TAGS:
        print(f"[last.fm] Fetching tag: {tag}")
        raw_tracks = fetch_tag_tracks(tag)
        print(f"  → {len(raw_tracks)} tracks returned")

        for t in raw_tracks:
            artist = t.get("artist", {}).get("name", "").strip()
            title  = t.get("name", "").strip()
            if not artist or not title:
                continue

            key = (artist.lower(), title.lower())
            if key in seen_keys:
                continue
            seen_keys.add(key)

            # MusicBrainz lookup for date + ISRC
            print(f"  [mb] {artist} — {title}")
            recording = mb_search_recording(artist, title)
            if not recording:
                continue

            mb_meta = extract_mb_metadata(recording)

            if not is_recent(mb_meta.get("release_date")):
                continue  # skip older tracks

            entry = {
                "title":        title,
                "artist":       artist,
                "isrc":         mb_meta.get("isrc"),
                "mb_id":        mb_meta.get("mb_id"),
                "release_date": mb_meta.get("release_date"),
                "label":        mb_meta.get("label"),
                "tags":         [tag],
                "lastfm_url":   t.get("url"),
            }
            results.append(entry)
            print(f"    ✓ added (released {mb_meta.get('release_date')})")

    # Merge tag lists for dupes that slipped through via different tags
    merged: dict[str, dict] = {}
    for entry in results:
        k = entry.get("isrc") or f"{entry['artist'].lower()}|{entry['title'].lower()}"
        if k in merged:
            merged[k]["tags"] = list(set(merged[k]["tags"] + entry["tags"]))
        else:
            merged[k] = entry

    final = sorted(merged.values(), key=lambda x: x.get("release_date") or "", reverse=True)
    return final


def main():
    playlist = build_playlist()
    today = date.today().isoformat()

    out_dir = Path("playlists")
    out_dir.mkdir(exist_ok=True)

    out_path = out_dir / f"{today}.json"
    out_path.write_text(json.dumps(playlist, indent=2, ensure_ascii=False))

    print(f"\n✅ Done — {len(playlist)} tracks written to {out_path}")

    # Also overwrite a stable "latest.json" for easy downstream consumption
    latest = out_dir / "latest.json"
    latest.write_text(json.dumps(playlist, indent=2, ensure_ascii=False))
    print(f"✅ Also updated {latest}")


if __name__ == "__main__":
    main()
