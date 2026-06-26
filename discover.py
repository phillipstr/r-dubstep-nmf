import json
import time
import os
import re
from datetime import date, timedelta
from pathlib import Path

import requests

# Config 

LASTFM_API_KEY = os.environ["LASTFM_API_KEY"]
LASTFM_BASE    = "https://ws.audioscrobbler.com/2.0/"
MB_BASE        = "https://musicbrainz.org/ws/2/"
MB_CONTACT     = os.environ["MB_CONTACT"]
MB_REPO        = os.environ.get("MB_REPO", "github.com/phillipstr/r-dubstep-nmf")
MB_HEADERS     = {"User-Agent": f"dubstep-friday-bot/1.0 ({MB_REPO} {MB_CONTACT})"}

# MusicBrainz genre tags to search for new releases
MB_TAGS = ["dubstep", "brostep", "riddim", "future bass"]

# How many releases to fetch per tag from MB
RELEASES_PER_TAG = 50

# Only keep tracks released within the last N days
RELEASE_WINDOW_DAYS = 8

# MusicBrainz release types we care about, in priority order
RELEASE_TYPE_PRIORITY = {"single": 0, "album": 1}

# MusicBrainz helpers

def mb_get(path: str, **params) -> dict:
    """Single MB request with error surfacing. Raises on non-2xx."""
    resp = requests.get(
        f"{MB_BASE}{path}",
        params={"fmt": "json", **params},
        headers=MB_HEADERS,
        timeout=15,
    )
    if not resp.ok:
        raise requests.HTTPError(
            f"MusicBrainz {resp.status_code} for {path!r}: {resp.text[:200]}",
            response=resp,
        )
    return resp.json()


def fetch_recent_releases_by_tag(tag: str, since: date, limit: int = RELEASES_PER_TAG) -> list[dict]:
    """
    Search MB for releases with the given tag released on or after `since`.
    Returns a list of release dicts.
    """
    since_str = since.isoformat()
    # MB Lucene query: tag + date range + only single/album primary types
    query = (
        f'tag:"{tag}" AND date:[{since_str} TO *] '
        f'AND (primarytype:single OR primarytype:album)'
    )
    releases = []
    offset = 0
    per_page = 25

    while len(releases) < limit:
        try:
            data = mb_get(
                "release/",
                query=query,
                limit=per_page,
                offset=offset,
                inc="artist-credits+release-groups+labels",
            )
        except requests.HTTPError as exc:
            print(f"  [mb] HTTP error fetching releases for tag '{tag}': {exc}")
            break
        except Exception as exc:
            print(f"  [mb] Unexpected error for tag '{tag}': {exc}")
            break

        batch = data.get("releases", [])
        if not batch:
            break
        releases.extend(batch)
        offset += per_page
        time.sleep(1.1)  # MB rate limit

        if offset >= data.get("release-count", 0):
            break

    return releases[:limit]


def fetch_recordings_for_release(release_id: str) -> list[dict]:
    """Fetch all recordings on a release, including ISRCs."""
    try:
        data = mb_get(
            f"release/{release_id}",
            inc="recordings+isrcs+artist-credits",
        )
        time.sleep(1.1)
        tracks = []
        for medium in data.get("media", []):
            tracks.extend(medium.get("tracks", []))
        return tracks
    except Exception as exc:
        print(f"  [mb] Error fetching recordings for release {release_id}: {exc}")
        return []


# Last.fm helpers

def lastfm_get(method: str, **params) -> dict:
    resp = requests.get(
        LASTFM_BASE,
        params={"method": method, "api_key": LASTFM_API_KEY, "format": "json", **params},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_lastfm_url(artist: str, title: str) -> str | None:
    """Look up a track on Last.fm and return its URL."""
    try:
        data = lastfm_get("track.getInfo", artist=artist, track=title)
        return data.get("track", {}).get("url")
    except Exception:
        return None


# Release date filter

def parse_date(d: str) -> date | None:
    if re.match(r"\d{4}-\d{2}-\d{2}", d):
        try:
            return date.fromisoformat(d[:10])
        except ValueError:
            return None
    if re.match(r"\d{4}-\d{2}$", d):
        try:
            return date.fromisoformat(f"{d}-01")
        except ValueError:
            return None
    if re.match(r"\d{4}$", d):
        return date(int(d), 1, 1)
    return None


def is_recent(release_date_str: str | None, window_days: int = RELEASE_WINDOW_DAYS) -> bool:
    if not release_date_str:
        return False
    rd = parse_date(release_date_str)
    if not rd:
        return False
    return (date.today() - rd).days <= window_days


# Main pipeline

def build_playlist() -> list[dict]:
    since = date.today() - timedelta(days=RELEASE_WINDOW_DAYS)
    print(f"[config] Searching for releases since {since}\n")

    # Collect releases keyed by MB release ID to dedupe across tags
    releases_by_id: dict[str, dict] = {}

    for tag in MB_TAGS:
        print(f"[mb] Searching releases for tag: {tag}")
        releases = fetch_recent_releases_by_tag(tag, since)
        print(f"  → {len(releases)} releases found")

        for r in releases:
            rid = r.get("id")
            if not rid or rid in releases_by_id:
                continue
            releases_by_id[rid] = r

    print(f"\n[mb] {len(releases_by_id)} unique releases to process\n")

    results: list[dict] = []
    seen_isrcs: set[str] = set()

    for release in releases_by_id.values():
        rid         = release.get("id")
        rdate       = release.get("date", "")
        rg          = release.get("release-group", {})
        rtype       = rg.get("primary-type", "").lower()
        release_title = release.get("title", "")

        # Artist name from artist-credits
        artist_credits = release.get("artist-credit", [])
        artist = "".join(
            (ac.get("name") or ac.get("artist", {}).get("name", "")) + ac.get("joinphrase", "")
            for ac in artist_credits
            if isinstance(ac, dict)
        ).strip()

        # Label
        label = None
        for li in release.get("label-info", []):
            label = li.get("label", {}).get("name")
            if label:
                break

        if rtype not in RELEASE_TYPE_PRIORITY:
            print(f"  ✗ {artist} — {release_title} (type: {rtype or 'unknown'}, skipped)")
            continue

        if not is_recent(rdate):
            print(f"  ✗ {artist} — {release_title} (date: {rdate}, outside window)")
            continue

        print(f"  ✓ {artist} — {release_title} ({rtype}, {rdate})")

        # Fetch individual recordings for this release to get ISRCs
        tracks = fetch_recordings_for_release(rid)
        for track in tracks:
            rec   = track.get("recording", {})
            title = rec.get("title", track.get("title", ""))
            isrcs = rec.get("isrcs", [])
            isrc  = isrcs[0] if isrcs else None

            # Dedupe by ISRC if available
            dedup_key = isrc if isrc else f"{artist.lower()}|{title.lower()}"
            if dedup_key in seen_isrcs:
                continue
            seen_isrcs.add(dedup_key)

            lastfm_url = fetch_lastfm_url(artist, title)

            entry = {
                "title":        title,
                "artist":       artist,
                "release_type": rtype,
                "release_title": release_title,
                "isrc":         isrc,
                "mb_release_id": rid,
                "mb_recording_id": rec.get("id"),
                "release_date": rdate,
                "label":        label,
                "lastfm_url":   lastfm_url,
            }
            results.append(entry)

    # Sort: singles first, then album tracks; newest-first within each group
    final = sorted(results, key=lambda x: x.get("release_date") or "", reverse=True)
    final = sorted(final, key=lambda x: RELEASE_TYPE_PRIORITY.get(x.get("release_type", ""), 99))

    return final


def main():
    print(f"[config] MusicBrainz User-Agent: {MB_HEADERS['User-Agent']}")
    print(f"[config] Release window: {RELEASE_WINDOW_DAYS} days\n")

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
