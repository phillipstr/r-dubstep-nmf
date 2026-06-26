# r/dubstep New Music Friday — Source of Truth

Automatically discovers new dubstep releases every Friday and commits a playlist JSON to this repo.
Both the Apple Music and Spotify playlist managers consume `playlists/latest.json`.

## How it works

```
Every Friday @ 10:00 UTC
        ↓
Last.fm tag charts (dubstep / brostep / riddim / future bass dubstep)
        ↓
MusicBrainz lookup per track → ISRC + release date + label
        ↓
Filter: released in the last 8 days
        ↓
playlists/YYYY-MM-DD.json  +  playlists/latest.json
        ↓
committed back to this repo by GitHub Actions
```

## Output format

```json
[
  {
    "title": "Track Name",
    "artist": "Artist Name",
    "isrc": "USRC12345678",
    "mb_id": "...",
    "release_date": "2026-06-27",
    "label": "Never Say Die",
    "tags": ["dubstep"],
    "lastfm_url": "https://www.last.fm/music/..."
  }
]
```

**ISRC** is the universal identifier — use it to look up the track on both Apple Music and Spotify:

- **Apple Music:** `GET /v1/catalog/us/songs?filter[isrc]={ISRC}`
- **Spotify:** `GET /v1/search?q=isrc:{ISRC}&type=track`

## Setup

### 1. Get a Last.fm API key
Go to https://www.last.fm/api/account/create — it's free and instant.

### 2. Add it as a GitHub secret
`Settings → Secrets and variables → Actions → New repository secret`
- Name: `LASTFM_API_KEY`
- Value: your key

### 3. Update the MusicBrainz User-Agent in `discover.py`
Replace `YOUR_ORG/YOUR_REPO` with your actual GitHub repo path.
MusicBrainz requires a real contact in the User-Agent header.

### 4. Trigger manually to test
`Actions → New Dubstep Friday → Run workflow`

## Running locally

```bash
pip install -r requirements.txt
export LASTFM_API_KEY=your_key_here
python discover.py
```

## Consuming the playlist

### Apple Music (MusicKit JS / REST API)
```python
import requests, json

tracks = json.load(open("playlists/latest.json"))
for track in tracks:
    if track["isrc"]:
        # Apple Music API lookup by ISRC
        r = requests.get(
            f"https://api.music.apple.com/v1/catalog/us/songs",
            params={"filter[isrc]": track["isrc"]},
            headers={"Authorization": f"Bearer {APPLE_DEVELOPER_TOKEN}",
                     "Music-User-Token": MUSIC_USER_TOKEN},
        )
        am_id = r.json()["data"][0]["id"]
        # add am_id to your playlist...
```

### Spotify (no Premium needed for playlist management via API)
```python
for track in tracks:
    if track["isrc"]:
        r = requests.get(
            "https://api.spotify.com/v1/search",
            params={"q": f'isrc:{track["isrc"]}', "type": "track"},
            headers={"Authorization": f"Bearer {SPOTIFY_TOKEN}"},
        )
        sp_id = r.json()["tracks"]["items"][0]["id"]
        # add sp_id to your playlist...
```

## Adjusting the tags

Edit the `TAGS` list in `discover.py` to add or remove Last.fm tags:
```python
TAGS = ["dubstep", "brostep", "riddim", "future bass dubstep"]
```

Check https://www.last.fm/tag/ to find tags with good coverage.
