# Competition check (gh search repos, 2026-10-29)

Queries: "live stream translation", "realtime translation whisper", "live subtitles whisper",
"real-time dubbing", "stream translator", "live translate subtitles", "whisper livestream",
"iptv translate", "simultaneous interpretation", "live translation iptv", "iptv whisper".

| Stars | Repo | What it is | Overlap with babel-tv |
|---:|---|---|---|
| 756 | yuxino/Mimi | System-audio / mic → live translated subtitles (desktop) | Captions whatever plays; no channel directory, not stream-native |
| 267 | fortypercnt/stream-translator | streamlink + whisper → translated text in terminal | Closest ancestor; no player overlay, no directory, English-only output (whisper translate) |
| 217 | ionic-bond/stream-translator-gpt | Fork of above + VAD + GPT/Gemini translation | Cloud LLM by default; no player, no channel picker |
| 126 | azuse/youtube-streaming-translator-python | YouTube live → translation | YouTube-only, idle |
| 87 | byteresearchcla/RealSI | Benchmark for simultaneous interpretation | Not a tool |
| 85 | JonathanFly/faster-whisper-livestream-translator | faster-whisper livestream translation, dual subs | Closest in stack; no directory, OBS-oriented |
| 37 | letr1n1ty/Gemive | Chrome extension, Gemini live subtitles | Cloud |
| 22 | wxkingstar/TransEcho | System-audio simultaneous interpretation | Cloud APIs |
| 0–4 | SpeechHub, Real_Time_Dubbing, live-translate-realtime-dubbing, korean-to-english-live-stream-translation, … | Real-time dubbing | Essentially no traction |
| — | "iptv translate", "iptv whisper", "live translation iptv" | | **Zero results** |

## Takeaways
- Live stream → translated subtitles exists, tops out under 1k stars (Mimi 756).
- Nobody pairs a world TV directory (iptv-org, 142k stars) with local translation:
  every iptv+translate query returned nothing.
- Dubbing repos are all 0–4 stars, consistent with the 2026-10-28 note.
- Most tools translate into English or Chinese. Indic targets (Telugu, Hindi) as the
  default are unoccupied.
