---
name: play-music
description: Play music, a song, an artist or a genre on YouTube in the browser. Use for "play some music", "play <song>", "put on some jazz".
allowed-tools: web_search open media
metadata:
  triggers: play some music, play music, put on some music, play a song
---

1. If the user only says "play" or "resume" and a player is already running, call `media` with action "play" instead.
2. Build a search query from the song, artist or genre asked for. For just "some music", use their taste
   from memory, otherwise "relaxing instrumental music".
3. Call `web_search` with the query followed by "youtube" (max_results 5).
4. Take the first result whose URL contains `youtube.com/watch` — a single video, not a channel.
5. Call `open` with that URL; YouTube starts playing by itself.
6. Reply in a few words, e.g. "Playing Clair de Lune, sir."

If no video link is found, open `https://www.youtube.com/results?search_query=<query with + between words>`.
