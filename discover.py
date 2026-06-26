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
MB_CONTACT     = os.environ["MB_CONTACT"]
MB_REPO        = os.environ.get("MB_REPO", "github.com/YOUR_ORG/YOUR_REPO")
MB_HEADERS     = {"User-Agent": f"dubstep-nmf-bot/1.0 ({MB_REPO} {MB_CONTACT})"}

# Tags to pull from Last.fm (combined, deduped by ISRC)
TAGS = ["dubstep", "brostep", "riddim", "future bass dubstep"]

# How many tracks to fetch per tag (max 1000 via pagination, 50 per page)
TRACKS_PER_TAG = 100

# Only keep tracks released within the last N days
RELEASE_WINDOW_DAYS = 8  # slightly over a week catches anything that slipped through last Friday

# MusicBrainz release types we care about, in priority order
RELEASE_TYPE_PRIORITY = {"single": 0, "album": 1}

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
    """Pull ISRC, release date, release type, and label from a MusicBrainz recording."""
    meta: dict = {}

    releases = recording.get("releases", [])

    # Release type
    # Walk all releases and pick the highest-priority type we find.
    # MusicBrainz stores type on the release-group, not the release itself,
    # but the search result surfaces it as release["release-group"]["primary-type"].
    best_type_priority = None
    for r in releases:
        rg = r.get("release-group", {})
        rtype = rg.get("primary-type", "").lower()
        priority = RELEASE_TYPE_PRIORITY.get(rtype)
        if priority is not None:
            if best_type_priority is None or priority < best_type_priority:
                best_type_priority = priority
                meta["release_type"] = rtype

    # ── Release date — use earliest across all releases ───────────────────────
    dates = []
    for r in releases:
        d = r.get("date", "")
        if re.match(r"\d{4}-\d{2}-\d{2}", d):
            dates.append(d)
        elif re.match(r"\d{4}", d):
            dates.append(f"{d}-01-01")
    if dates:
        meta["release_date"] = sorted(dates)[0]

    # ── Label — first one we find ─────────────────────────────────────────────
    for r in releases:
        for lc in r.get("label-info", []):
            label = lc.get("label", {}).get("name")
            if label:
                meta["label"] = label
                break
        if "label" in meta:
            break

    # ── ISRCs — separate MB endpoint ─────────────────────────────────────────
    mb_id = recording.get("id")
    if mb_id:
        meta["mb_id"] = mb_id
        try:
            resp = requests.get(
                f"{MB_BASE}recording/{mb_id}",
                params={"inc": "isrcs+release-groups", "fmt": "json"},
                headers=MB_HEADERS,
                timeout=15,
            )
            resp.raise_for_status()
            body = resp.json()
            isrcs = body.get("isrcs", [])
            if isrcs:
                meta["isrc"] = isrcs[0]

            # Also refine release_type from the fuller recording response if
            # the search result didn't surface release-group data.
            if "release_type" not in meta:
                for r in body.get("releases", []):
                    rg = r.get("release-group", {})
                    rtype = rg.get("primary-type", "").lower()
                    if rtype in RELEASE_TYPE_PRIORITY:
                        priority = RELEASE_TYPE_PRIORITY[rtype]
                        if best_type_priority is None or priority < best_type_priority:
                            best_type_priority = priority
                            meta["release_type"] = rtype

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
    seen_keys: set[str] = set()
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

            print(f"  [mb] {artist} — {title}")
            recording = mb_search_recording(artist, title)
            if not recording:
                continue

            mb_meta = extract_mb_metadata(recording)

            # Skip tracks with an unknown or unwanted release type
            if mb_meta.get("release_type") not in RELEASE_TYPE_PRIORITY:
                print(f"    ✗ skipped (release type: {mb_meta.get('release_type', 'unknown')})")
                continue

            if not is_recent(mb_meta.get("release_date")):
                continue

            entry = {
                "title":        title,
                "artist":       artist,
                "release_type": mb_meta.get("release_type"),  # "single" or "album"
                "isrc":         mb_meta.get("isrc"),
                "mb_id":        mb_meta.get("mb_id"),
                "release_date": mb_meta.get("release_date"),
                "label":        mb_meta.get("label"),
                "tags":         [tag],
                "lastfm_url":   t.get("url"),
            }
            results.append(entry)
            print(f"    ✓ added ({mb_meta.get('release_type')}, released {mb_meta.get('release_date')})")

    # Merge tag lists for dupes that came in via different tags
    merged: dict[str, dict] = {}
    for entry in results:
        k = entry.get("isrc") or f"{entry['artist'].lower()}|{entry['title'].lower()}"
        if k in merged:
            merged[k]["tags"] = list(set(merged[k]["tags"] + entry["tags"]))
        else:
            merged[k] = entry

    # Sort: singles first, then album tracks; newest-first within each group
    final = sorted(
        merged.values(),
        key=lambda x: (
            RELEASE_TYPE_PRIORITY.get(x.get("release_type", ""), 99),
            x.get("release_date") or "",
        ),
        reverse=False,  # ascending priority (0=single before 1=album)...
    )
    # ...but newest-first within each group requires a two-key sort where
    # date is descending. Python's sort is stable so we sort by date desc first,
    # then by type asc to get the final order.
    final = sorted(final, key=lambda x: x.get("release_date") or "", reverse=True)
    final = sorted(final, key=lambda x: RELEASE_TYPE_PRIORITY.get(x.get("release_type", ""), 99))

    return final


def main():
    playlist = build_playlist()
    today = date.today().isoformat()

    out_dir = Path("playlists")
    out_dir.mkdir(exist_ok=True)

    out_path = out_dir / f"{today}.json"
    out_path.write_text(json.dumps(playlist, indent=2, ensure_ascii=False))

    singles = sum(1 for t in playlist if t.get("release_type") == "single")
    albums  = sum(1 for t in playlist if t.get("release_type") == "album")
    print(f"\n✅ Done — {len(playlist)} tracks ({singles} singles, {albums} album tracks) → {out_path}")

    latest = out_dir / "latest.json"
    latest.write_text(json.dumps(playlist, indent=2, ensure_ascii=False))
    print(f"✅ Also updated {latest}")


if __name__ == "__main__":
    main()
