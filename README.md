# Best Stream for Stremio

A Stremio addon that picks the torrent stream that will **actually play**, and tells you why a stream is stuck.

Stremio's stream list ranks torrents by seeder counts that were cached when the torrent was indexed. Those numbers are often stale: a torrent can show 👤 9 and deliver nothing. Best Stream checks every option live, test-downloads the top candidates through your own Stremio, and puts the one that delivered fastest at the top as **★ BEST**.

```
🩺 Diagnosis  ❌ The stream you were watching is stuck at 1.3%: 0 bytes in 4s
              ✓ Your internet is fine; this torrent is the problem:
              • connected to 3 peer(s), none of them a seeder
              • it's a 64-file pack; few peers hold your episode
★ BEST 1080p  ✅ tested: started in 1.2s · 🟢 10 seeders live · 12 leechers
Live 720p     ✅ tested: started in 3.9s · 🟢 23 seeders live · 5 leechers
Live 1080p    ❌ tested: 0 KB in 8s (won't play well) · 🟢 11 seeders live
```

## What it does

Every time you open an episode or movie (or go back to the list because playback is stuck):

1. **Live seeders.** It asks the public trackers how many seeders and leechers each torrent has *right now*, instead of trusting the cached count.
2. **Delivery test.** It downloads 2 MB from the middle of the file for the top 4 candidates through Stremio's own torrent engine. The middle is used because the start may already be cached from an earlier attempt, which would make a dead torrent look alive. The fastest one that delivers becomes **★ BEST** and stays loaded so it starts quickly. ★ BEST picks share a binge group, so auto-play-next-episode keeps choosing them.
3. **Diagnosis.** If the stream you were just watching isn't receiving data, a 🩺 entry at the top says so, with the reasons it can measure (no seeders, unreachable peers, a season pack nobody seeds, etc.). It also checks your connection, but only blames your network when several independent checks agree (offline, Wi-Fi off, a hotel login page, broken DNS, or torrents blocked entirely). Click the 🩺 entry for the full report.

Results come from [Torrentio](https://torrentio.strem.fun). You don't need Torrentio installed, but if it is, you'll see both lists.

## Requirements

- **Stremio desktop** (Windows, macOS or Linux) with its streaming server running (the default).
- **Python 3.8+**. No packages to install.

It must run on the **same computer as Stremio**, because it uses Stremio's local streaming server to test downloads on your network. It doesn't work with Stremio on smart TVs or phones.

## Install

```sh
git clone https://github.com/aharonahdoot/stremio-best-stream.git
cd stremio-best-stream
python3 best_stream_addon.py        # on Windows: python best_stream_addon.py
```

Then in Stremio: **Addons** → paste `http://127.0.0.1:7171/manifest.json` into the search box → **Install**.

Keep it running while you use Stremio. To start it automatically, add that command to your login items / Task Scheduler / systemd user service.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `BEST_STREAM_PORT` | `7171` | Port the addon listens on |
| `BEST_STREAM_UPSTREAM` | `https://torrentio.strem.fun/qualityfilter=threed,cam,other,scr` | Torrentio config URL (without `/manifest.json`). Use your own from torrentio.strem.fun/configure for different providers, languages or filters |
| `BEST_STREAM_RACE` | `4` | How many top candidates to test-download (`0` turns the test off) |
| `STREMIO_SERVER` | `http://127.0.0.1:11470` | Stremio streaming server |

Try it from the command line: `python3 best_stream_addon.py --rank series/tt0903747:1:1`

## Good to know

- **The list takes ~10-20 s to fill in**: a few seconds for the tracker check (longer for popular titles with hundreds of torrents) and up to 8 s for the delivery test. That's the price of knowing instead of guessing.
- **Each list load downloads about 8 MB** (2 MB × 4 candidates). Torrents it tested and didn't pick are removed from Stremio again. It never touches a torrent you're already playing.
- **Torrent health changes by the minute.** A torrent that was dead an hour ago can work now, so nothing is cached for more than 10 minutes.
- The Wi-Fi-specific messages ("Wi-Fi is turned off") are macOS only. Other systems get the generic "No internet connection".
- Using torrents can expose your IP address to other peers, and what you can legally stream depends on where you live. This addon doesn't change either of those.

## License

MIT
