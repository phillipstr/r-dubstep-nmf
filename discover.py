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

# MusicBrainz genre tags — kept broad; date filter does the recency work
MB_TAGS = [
    "dubstep", "brostep", "riddim", "future bass",
    "wave dubstep", "melodic dubstep", "tearout", "halftime",
]

# Last.fm tags for weekly chart supplementation
LASTFM_TAGS = ["dubstep", "brostep", "riddim"]

# How many releases to fetch per MB tag
RELEASES_PER_TAG = 50

# Only keep tracks released within the last N days
RELEASE_WINDOW_DAYS = 8

# MusicBrainz release types we care about, in priority order
RELEASE_TYPE_PRIORITY = {"single": 0, "album": 1}

# ── MusicBrainz helpers ───────────────────────────────────────────────────────

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
    """Search MB for releases with the given tag released on or after `since`."""
    since_str = since.isoformat()
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
            print(f"  [mb] HTTP error for tag '{tag}': {exc}")
            break
        except Exception as exc:
            print(f"  [mb] Error for tag '{tag}': {exc}")
            break

        batch = data.get("releases", [])
        if not batch:
            break
        releases.extend(batch)
        offset += per_page
        time.sleep(1.1)

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
        print(f"  [mb] Error fetching recordings for {release_id}: {exc}")
        return []


def fetch_recent_releases_by_date(since: date, limit: int = 100) -> list[dict]:
    """
    Search MB for any electronic releases in the date window, regardless of tag.
    We then check release-group tags to filter for dubstep-adjacent content.
    This catches releases that are tagged 'electronic' broadly but not yet
    tagged with a specific dubstep subgenre.
    """
    since_str = since.isoformat()
    # Broader electronic umbrella — dubstep almost always falls under this
    query = (
        f'tag:"electronic" AND date:[{since_str} TO *] '
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
                inc="artist-credits+release-groups+labels+tags",
            )
        except Exception as exc:
            print(f"  [mb] Error in broad date search: {exc}")
            break

        batch = data.get("releases", [])
        if not batch:
            break

        # Filter to releases whose tags include a dubstep-adjacent term
        dubstep_terms = {
            "dubstep", "brostep", "riddim", "future bass", "wave dubstep",
            "melodic dubstep", "tearout", "halftime", "bass music",
        }
        for r in batch:
            release_tags = {t["name"].lower() for t in r.get("tags", [])}
            rg_tags = {t["name"].lower() for t in r.get("release-group", {}).get("tags", [])}
            all_tags = release_tags | rg_tags
            if all_tags & dubstep_terms:
                releases.append(r)

        offset += per_page
        time.sleep(1.1)

        if offset >= data.get("release-count", 0):
            break

    return releases[:limit]


def mb_search_recording(artist: str, title: str) -> dict | None:
    """Search MB for a single recording by artist + title."""
    query = f'recording:"{title}" AND artist:"{artist}"'
    try:
        data = mb_get(
            "recording/",
            query=query,
            limit=1,
            inc="releases+release-groups+isrcs",
        )
        recordings = data.get("recordings", [])
        return recordings[0] if recordings else None
    except requests.HTTPError as exc:
        print(f"    [mb] HTTP error searching '{artist} — {title}': {exc}")
        return None
    except Exception as exc:
        print(f"    [mb] Error searching '{artist} — {title}': {exc}")
        return None


# ── Last.fm helpers ───────────────────────────────────────────────────────────

def lastfm_get(method: str, **params) -> dict:
    resp = requests.get(
        LASTFM_BASE,
        params={"method": method, "api_key": LASTFM_API_KEY, "format": "json", **params},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_lastfm_recent_tracks(tag: str, limit: int = 50) -> list[dict]:
    """
    Fetch top tracks for a Last.fm tag, then filter by MB release date.
    We use a small limit so only currently-active tracks surface —
    all-time classics won't appear in the top 50 of niche tags as often.
    """
    try:
        data = lastfm_get("tag.gettoptracks", tag=tag, limit=limit)
        return data.get("tracks", {}).get("track", [])
    except Exception as exc:
        print(f"  [lastfm] Error fetching tracks for '{tag}': {exc}")
        return []


def fetch_lastfm_url(artist: str, title: str) -> str | None:
    """Look up a track on Last.fm and return its URL."""
    try:
        data = lastfm_get("track.getInfo", artist=artist, track=title)
        return data.get("track", {}).get("url")
    except Exception:
        return None


# ── Date helpers ──────────────────────────────────────────────────────────────

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
        # Year-only: if it's the current year, treat as potentially recent
        # (MB often only has year for very new releases not yet fully catalogued)
        year = int(d)
        if year == date.today().year:
            return date.today()  # conservative: assume today so it passes the window
        return date(year, 1, 1)
    return None


def is_recent(release_date_str: str | None, window_days: int = RELEASE_WINDOW_DAYS) -> bool:
    if not release_date_str:
        return False
    rd = parse_date(release_date_str)
    if not rd:
        return False
    return (date.today() - rd).days <= window_days


# ── Main pipeline ─────────────────────────────────────────────────────────────

def build_playlist() -> list[dict]:
    since = date.today() - timedelta(days=RELEASE_WINDOW_DAYS)
    print(f"[config] Searching for releases since {since}\n")

    seen_release_ids: set[str] = set()
    seen_dedup_keys: set[str] = set()
    results: list[dict] = []

    # ── Source 1: MusicBrainz new releases by tag ─────────────────────────────
    releases_by_id: dict[str, dict] = {}

    for tag in MB_TAGS:
        print(f"[mb] Searching releases for tag: {tag}")
        releases = fetch_recent_releases_by_tag(tag, since)
        print(f"  → {len(releases)} releases found")
        for r in releases:
            rid = r.get("id")
            if rid and rid not in releases_by_id:
                releases_by_id[rid] = r

    # ── MB source 2: broad electronic search filtered by dubstep tags ────────
    print(f"[mb] Running broad electronic/date search for additional coverage")
    broad_releases = fetch_recent_releases_by_date(since)
    added = 0
    for r in broad_releases:
        rid = r.get("id")
        if rid and rid not in releases_by_id:
            releases_by_id[rid] = r
            added += 1
    print(f"  → {added} additional releases from broad search")

    print(f"\n[mb] {len(releases_by_id)} unique releases to process")

    for release in releases_by_id.values():
        rid           = release.get("id")
        rdate         = release.get("date", "")
        rg            = release.get("release-group", {})
        rtype         = rg.get("primary-type", "").lower()
        release_title = release.get("title", "")

        artist_credits = release.get("artist-credit", [])
        artist = "".join(
            (ac.get("name") or ac.get("artist", {}).get("name", "")) + ac.get("joinphrase", "")
            for ac in artist_credits if isinstance(ac, dict)
        ).strip()

        label = None
        for li in release.get("label-info", []):
            label = li.get("label", {}).get("name")
            if label:
                break

        if rtype not in RELEASE_TYPE_PRIORITY:
            print(f"  ✗ {artist} — {release_title} (type: {rtype or 'unknown'})")
            continue

        if not is_recent(rdate):
            print(f"  ✗ {artist} — {release_title} (date: {rdate}, outside window)")
            continue

        print(f"  ✓ {artist} — {release_title} ({rtype}, {rdate})")
        seen_release_ids.add(rid)

        tracks = fetch_recordings_for_release(rid)
        for track in tracks:
            rec   = track.get("recording", {})
            title = rec.get("title", track.get("title", ""))
            isrcs = rec.get("isrcs", [])
            isrc  = isrcs[0] if isrcs else None

            dedup_key = isrc if isrc else f"{artist.lower()}|{title.lower()}"
            if dedup_key in seen_dedup_keys:
                continue
            seen_dedup_keys.add(dedup_key)

            results.append({
                "title":             title,
                "artist":            artist,
                "release_type":      rtype,
                "release_title":     release_title,
                "isrc":              isrc,
                "mb_release_id":     rid,
                "mb_recording_id":   rec.get("id"),
                "release_date":      rdate,
                "label":             label,
                "lastfm_url":        fetch_lastfm_url(artist, title),
                "source":            "musicbrainz",
            })

    # ── Source 2: Last.fm weekly tag chart ────────────────────────────────────
    print(f"\n[lastfm] Fetching recent tracks for {len(LASTFM_TAGS)} tags (filtered by MB release date)")

    for tag in LASTFM_TAGS:
        print(f"  tag: {tag}")
        weekly_tracks = fetch_lastfm_recent_tracks(tag)
        print(f"  → {len(weekly_tracks)} tracks (will filter by release date via MB)")

        for t in weekly_tracks:
            artist = t.get("artist", {}).get("#text", "").strip()
            title  = t.get("name", "").strip()
            if not artist or not title:
                continue

            rough_key = f"{artist.lower()}|{title.lower()}"
            if rough_key in seen_dedup_keys:
                print(f"    ~ {artist} — {title} (already seen, skipping)")
                continue

            print(f"    [mb] {artist} — {title}")
            recording = mb_search_recording(artist, title)
            time.sleep(1.1)  # MB rate limit — always sleep after a search

            if not recording:
                print("      ✗ not found in MusicBrainz")
                continue

            # Extract release info — pick best release type, then most recent date
            releases = recording.get("releases", [])
            best_release = None
            best_priority = 99
            for r in releases:
                rg  = r.get("release-group", {})
                rt  = rg.get("primary-type", "").lower()
                pri = RELEASE_TYPE_PRIORITY.get(rt, 99)
                rd  = r.get("date", "")
                # Prefer better type; break ties by most recent date
                if pri < best_priority or (
                    pri == best_priority
                    and rd > (best_release.get("date", "") if best_release else "")
                ):
                    best_priority = pri
                    best_release  = r

            rdate         = best_release.get("date", "") if best_release else ""
            rtype         = best_release.get("release-group", {}).get("primary-type", "").lower() if best_release else ""
            release_title = best_release.get("title", "") if best_release else ""

            isrcs = recording.get("isrcs", [])
            isrc  = isrcs[0] if isrcs else None

            dedup_key = isrc if isrc else rough_key
            if dedup_key in seen_dedup_keys:
                print(f"      ~ duplicate by ISRC, skipping")
                continue

            if rtype not in RELEASE_TYPE_PRIORITY:
                print(f"      ✗ skipped (type: {rtype or 'unknown'}, {len(releases)} releases checked)")
                continue

            if not is_recent(rdate):
                print(f"      ✗ skipped (date: {rdate or 'unknown'}, outside window)")
                continue

            seen_dedup_keys.add(dedup_key)
            print(f"      ✓ added ({rtype}, {rdate})")

            results.append({
                "title":           title,
                "artist":          artist,
                "release_type":    rtype,
                "release_title":   release_title,
                "isrc":            isrc,
                "mb_release_id":   best_release.get("id") if best_release else None,
                "mb_recording_id": recording.get("id"),
                "release_date":    rdate,
                "label":           None,
                "lastfm_url":      t.get("url"),
                "source":          "lastfm_weekly",
            })

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
