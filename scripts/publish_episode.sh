#!/usr/bin/env bash
# Publish (or update) an episode on Voxa Shows, from a folder with episode.json and any of
# video.mp4, audio.mp3, cover.jpg / cover.png. Needs the owner API key in VOXA_KEY.
#   VOXA_KEY=vx_... scripts/publish_episode.sh ai-explained-02 path/to/folder [https://voxavocals.com]
set -euo pipefail
slug=$1 dir=$2 site=${3:-https://voxavocals.com}
: "${VOXA_KEY:?set VOXA_KEY to the owner API key}"
put() { curl -fsS -X PUT -H "Authorization: Bearer $VOXA_KEY" "$@"; echo; }
put -H 'Content-Type: application/json' --data-binary @"$dir/episode.json" "$site/api/episodes/$slug"
for f in video.mp4 audio.mp3 cover.jpg cover.png; do
  [ -f "$dir/$f" ] && put --data-binary @"$dir/$f" "$site/api/episodes/$slug/$f"
done
echo "Live: $site/shows/$slug"
