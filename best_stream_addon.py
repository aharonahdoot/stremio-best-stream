#!/usr/bin/env python3
""""Best Stream" - a local Stremio addon that picks the stream that will actually play,
and tells you why the one you were watching didn't.

Every time Stremio asks for streams (opening an episode, or going back to the list
because playback is stuck) the addon:
  1. Diagnoses: is the stream you were just watching delivering data? Is the internet up?
     Result goes in a 🩺 entry at the top of the list.
  2. Ranks: Torrentio's results, re-scored by LIVE tracker seeders (BEP-15 scrape) instead
     of Torrentio's stale cached counts.
  3. Proves: test-downloads 2 MB from the middle of the top candidates through Stremio's
     own torrent engine. The fastest real deliverer becomes ★ BEST.

It must run on the same machine as Stremio (it talks to Stremio's local streaming server
at 127.0.0.1:11470 and measures YOUR network).

Install in Stremio: Addons -> paste  http://127.0.0.1:7171/manifest.json
"""
import concurrent.futures as cf
import html
import json
import math
import os
import random
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("BEST_STREAM_PORT", 7171))
UPSTREAM = os.environ.get("BEST_STREAM_UPSTREAM",
                          "https://torrentio.strem.fun/qualityfilter=threed,cam,other,scr")
SERVER = os.environ.get("STREMIO_SERVER", "http://127.0.0.1:11470")
TRACKERS = [
    ("tracker.opentrackr.org", 1337),
    ("open.stealth.si", 80),
    ("tracker.torrent.eu.org", 451),
    ("exodus.desync.com", 6969),
    ("explodie.org", 6969),
    ("tracker.theoks.net", 6969),
    ("tracker-udp.gbitt.info", 80),
]
SCRAPE_TIMEOUT = 3.0
CACHE_TTL = 600
RACE_K = int(os.environ.get("BEST_STREAM_RACE", 4))  # 0 disables the delivery test
RACE_BYTES = 2_000_000
RACE_SECS = 8.0
SAMPLE_SECS = 4.0  # how long to watch the current stream for incoming bytes
# Well-seeded public test torrents (Blender open movies), used only to tell
# "this torrent is dead" apart from "no torrent works on this network".
REFERENCE_TORRENTS = ["dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c",  # Big Buck Bunny
                      "08ada5a7a6183aae1e09d831df6748d566095a10"]  # Sintel
RES_WEIGHT = {"2160p": 0.7, "4k": 0.7, "1080p": 1.0, "720p": 0.85, "480p": 0.55}

MANIFEST = {
    "id": "local.beststream",
    "version": "1.1.0",
    "name": "Best Stream (live)",
    "description": "Picks the stream that will actually play (live-tested), and diagnoses stuck streams.",
    "resources": ["stream"],
    "types": ["movie", "series"],
    "idPrefixes": ["tt"],
    "catalogs": [],
}

_last_diagnosis = {"time": None, "lines": ["No diagnosis yet. Open an episode in Stremio."]}


def log(msg):
    sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    sys.stderr.flush()


# ================================================================ network diagnosis
# Rule: only blame the network when independent checks AGREE. Being wrong here
# ("your Wi-Fi is down" when it isn't) is worse than saying nothing.

def _tcp(host, port, timeout=2.5):
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def _dns(name):
    try:
        socket.getaddrinfo(name, 443)
        return True
    except OSError:
        return False


def _captive():
    """'ok' = real internet; 'portal' = something intercepted the request (login page);
    None = request failed / inconclusive."""
    try:
        req = urllib.request.Request("http://captive.apple.com/hotspot-detect.html",
                                     headers={"User-Agent": "CaptiveNetworkSupport"})
        with urllib.request.urlopen(req, timeout=3) as r:
            body = r.read(4096).decode("utf-8", "replace")
        return "ok" if "Success" in body else "portal"
    except Exception:
        return None


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return ""


def _mac_link():
    """Wi-Fi facts from sources that don't lie. (Not `networksetup -getairportnetwork`:
    on recent macOS it says 'not associated' even while connected.)"""
    if sys.platform != "darwin":
        return None
    ports = _run(["networksetup", "-listallhardwareports"])
    m = re.search(r"Hardware Port: Wi-Fi\s*\nDevice: (\S+)", ports)
    wifi = m.group(1) if m else None
    route = _run(["route", "-n", "get", "default"])
    m = re.search(r"interface: (\S+)", route)
    default_if = m.group(1) if m else None
    info = {"wifi_dev": wifi, "default_if": default_if}
    if wifi:
        ifc = _run(["ifconfig", wifi])
        info["wifi_active"] = "status: active" in ifc
        info["wifi_has_ip"] = bool(re.search(r"inet (?!169\.254)\d+\.\d+\.\d+\.\d+", ifc))
        power = _run(["networksetup", "-getairportpower", wifi])
        info["wifi_power"] = "On" if ": On" in power else "Off" if ": Off" in power else None
    return info


def network_check():
    with cf.ThreadPoolExecutor(8) as ex:
        tcp = {h: ex.submit(_tcp, h, 443) for h in ("1.1.1.1", "8.8.8.8", "9.9.9.9")}
        dns = {n: ex.submit(_dns, n) for n in ("apple.com", "google.com", "cloudflare.com")}
        cap = ex.submit(_captive)
        link = ex.submit(_mac_link)
        return {"tcp": {h: f.result() for h, f in tcp.items()},
                "dns": {n: f.result() for n, f in dns.items()},
                "captive": cap.result(), "link": link.result()}


def classify_network(ev):
    """-> (problem_or_None, [evidence lines]). Claims a problem only when checks agree."""
    tcp_ok = [h for h, ok in ev["tcp"].items() if ok]
    link = ev.get("link") or {}
    on_wifi = link.get("wifi_dev") and link.get("default_if") == link.get("wifi_dev")
    evidence = [f"internet servers reachable: {len(tcp_ok)}/{len(ev['tcp'])}",
                f"captive check: {ev['captive'] or 'failed'}",
                f"DNS: {sum(ev['dns'].values())}/{len(ev['dns'])} names resolved"]
    if link:
        evidence.append(f"default route via: {link.get('default_if') or 'none'}; "
                        f"Wi-Fi power {link.get('wifi_power')}, "
                        f"link {'active' if link.get('wifi_active') else 'inactive'}, "
                        f"{'has' if link.get('wifi_has_ip') else 'no'} IP")

    offline = not tcp_ok and ev["captive"] is None  # 4 independent endpoints, all unreachable
    if offline:
        if link and not link.get("default_if"):
            if link.get("wifi_power") == "Off":
                return "Wi-Fi is turned off.", evidence
            if link.get("wifi_dev") and not link.get("wifi_active"):
                return "Wi-Fi isn't connected to any network.", evidence
            return "This computer has no network connection.", evidence
        if on_wifi and link.get("wifi_active"):
            return ("Connected to Wi-Fi, but the internet isn't reachable through it "
                    "(router or internet provider problem)."), evidence
        return "No internet connection.", evidence
    if tcp_ok and ev["captive"] == "portal":
        where = "This Wi-Fi" if on_wifi else "This network"
        return f"{where} wants you to sign in first (a login page is intercepting traffic).", evidence
    # captive "ok" means captive.apple.com resolved and loaded, which proves DNS works.
    # Some networks filter individual domains, so never judge DNS on one name.
    if tcp_ok and ev["captive"] is None and not any(ev["dns"].values()):
        return "Internet is up but DNS isn't working (names can't be looked up).", evidence
    return None, evidence  # healthy, or inconclusive -> say nothing


# ================================================================ Stremio server helpers
def _server(path, body=None, timeout=10, headers=None):
    req = urllib.request.Request(SERVER + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json", **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


def _server_json(path, timeout=5):
    with _server(path, timeout=timeout) as r:
        return json.load(r)


def selection_files(t):
    """Map piece-range selections to file indexes. Piece length isn't reported, so infer
    it: the power of two for which every selection starts on some file's first piece."""
    files, sels = t.get("files") or [], t.get("selections") or []
    for L in (2 ** k for k in range(14, 27)):
        starts = {f["offset"] // L: i for i, f in enumerate(files)}
        if sels and all(s["from"] in starts for s in sels):
            return [(starts[s["from"]], s) for s in sels]
    return []


def playing_file(t):
    """The file the player is reading: selection whose read head advanced furthest."""
    mapped = selection_files(t)
    if mapped:
        return max(enumerate(mapped), key=lambda e: (e[1][1]["readFrom"] - e[1][1]["from"], e[0]))[1][0]
    flagged = [i for i, f in enumerate(t.get("files") or []) if f.get("__cacheEvents")]
    return flagged[-1] if flagged else None


# ================================================================ current-stream diagnosis
def torrent_reasons(t, competing):
    wires = t.get("wires") or []
    seeders = sum(1 for w in wires if w.get("isSeeder"))
    sending = sum(1 for w in wires if w.get("downSpeed", 0) > 0)
    found = sum(s.get("numFoundUniq", 0) for s in t.get("sources", []))
    peers, tries = t.get("peers", 0), t.get("connectionTries", 0)
    r = []
    if peers == 0:
        r.append("not connected to a single peer")
    elif seeders == 0:
        r.append(f"connected to {peers} peer(s), none of them a seeder")
    if peers and sending == 0:
        r.append("none of the connected peers is sending anything")
    if tries > 50 and peers < 5:
        r.append(f"{tries} connection attempts, only {peers} worked")
    if found < 15:
        r.append(f"only {found} peers exist for this torrent at all")
    if competing > 1:
        r.append(f"{competing} episodes from this torrent are downloading at once, splitting the little bandwidth")
    if len(t.get("files") or []) > 1:
        r.append(f"it's a {len(t['files'])}-file pack; few peers hold your episode")
    if t.get("opts", {}).get("dht") is False:
        r.append("Stremio has DHT off for it, so it can only find peers through trackers")
    return r


def current_streams(exclude=()):
    """Streams the player is pulling right now, each with a measured verdict."""
    try:
        s1 = _server_json("/stats.json")
    except Exception:
        return None  # streaming server not running
    active = {h: t for h, t in s1.items() if t.get("selections") and h not in exclude}
    if not active:
        return []
    time.sleep(SAMPLE_SECS)
    try:
        s2 = _server_json("/stats.json")
    except Exception:
        return None
    out = []
    for h, t in active.items():
        t2 = s2.get(h)
        if not t2:
            continue
        idx = playing_file(t2)
        name, prog = t2.get("name"), None
        if idx is not None:
            try:
                fs = _server_json(f"/{h}/{idx}/stats.json")
                name, prog = fs.get("streamName", name), fs.get("streamProgress")
                t2["opts"] = fs.get("opts", {})
            except Exception:
                pass
        if prog is not None and prog >= 0.999:
            continue  # fully downloaded; nothing can be stuck
        delta = t2.get("downloaded", 0) - t.get("downloaded", 0)
        out.append({
            "hash": h, "name": name, "progress": prog, "bytes": delta,
            "stalled": delta == 0,
            "reasons": torrent_reasons(t2, len({i for i, _ in selection_files(t2)})),
        })
    return out


# ================================================================ tracker scrape (BEP 15)
_cache = {}  # infohash -> (time, seeders, leechers, trackers_answered)


def _scrape_one(host, port, hashes):
    out, s = {}, None
    try:
        addr = (socket.gethostbyname(host), port)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(SCRAPE_TIMEOUT)
        tid = random.getrandbits(32)
        s.sendto(struct.pack(">QII", 0x41727101980, 0, tid), addr)
        action, rtid, conn = struct.unpack(">IIQ", s.recv(2048)[:16])
        if action != 0 or rtid != tid:
            return out
        for i in range(0, len(hashes), 70):
            chunk = hashes[i:i + 70]
            tid = random.getrandbits(32)
            s.sendto(struct.pack(">QII", conn, 2, tid) + b"".join(bytes.fromhex(h) for h in chunk), addr)
            data = s.recv(8 + 12 * len(chunk) + 64)
            action, rtid = struct.unpack(">II", data[:8])
            if action != 2 or rtid != tid:
                continue
            for j, h in enumerate(chunk):
                off = 8 + 12 * j
                if off + 12 <= len(data):
                    seeders, _done, leechers = struct.unpack(">III", data[off:off + 12])
                    out[h] = (seeders, leechers)
    except Exception:
        pass
    finally:
        if s:
            s.close()
    return out


def scrape(hashes):
    now = time.time()
    need = [h for h in set(hashes) if not (h in _cache and now - _cache[h][0] < CACHE_TTL)]
    if need:
        merged = {h: [0, 0, 0] for h in need}
        with cf.ThreadPoolExecutor(len(TRACKERS)) as ex:
            for res in ex.map(lambda t: _scrape_one(t[0], t[1], need), TRACKERS):
                for h, (se, le) in res.items():
                    m = merged[h]
                    m[0], m[1], m[2] = max(m[0], se), max(m[1], le), m[2] + 1
        for h, (se, le, n) in merged.items():
            if n:
                _cache[h] = (now, se, le, n)
    return {h: _cache[h][1:] for h in hashes if h in _cache}


# ================================================================ delivery test (race)
_race_cache = {}  # (hash, idx) -> (time, result)


def race_one(h, idx):
    """Pull RACE_BYTES from the MIDDLE of the file through Stremio's engine. The start may
    be cached from an earlier attempt (making a dead torrent look alive); the middle isn't."""
    key = (h, idx)
    if key in _race_cache and time.time() - _race_cache[key][0] < CACHE_TTL:
        return _race_cache[key][1]
    t0, got = time.time(), 0
    try:
        files = json.load(_server(f"/{h}/create", {"torrent": {"infoHash": h}}, timeout=RACE_SECS)).get("files") or []
        if idx is None and files:
            idx = max(range(len(files)), key=lambda k: files[k]["length"])
        length = files[idx]["length"] if files and idx is not None and idx < len(files) else 0
        start = (length // 2) if length > 4 * RACE_BYTES else 0
        left = RACE_SECS - (time.time() - t0)
        if left > 0.5:
            r = _server(f"/{h}/{idx}", headers={"Range": f"bytes={start}-{start + RACE_BYTES - 1}"}, timeout=left)
            while time.time() - t0 < RACE_SECS:
                chunk = r.read(65536)
                if not chunk:
                    break
                got += len(chunk)
    except Exception:
        pass
    res = {"bytes": got, "secs": time.time() - t0}
    _race_cache[key] = (time.time(), res)
    return res


def drop_engine(h):
    try:
        _server(f"/{h}/remove", timeout=3).read()
    except Exception:
        pass


def race(cands, keep_winner=True):
    """cands: [(hash, fileIdx)]. Frees engines it created, except the winner. Never
    touches an engine that already existed (it may be what the user is watching)."""
    try:
        preexisting = set(_server_json("/stats.json", timeout=3).keys())
    except Exception:
        return {}, None
    with cf.ThreadPoolExecutor(max(1, len(cands))) as ex:
        results = dict(zip(cands, ex.map(lambda c: race_one(*c), cands)))
    done = [c for c, r in results.items() if r["bytes"] >= RACE_BYTES]
    winner = min(done, key=lambda c: results[c]["secs"]) if done else None
    for c in cands:
        if (c != winner or not keep_winner) and c[0] not in preexisting:
            drop_engine(c[0])
    return results, winner


def torrents_work_at_all():
    """Only called when nothing delivered. Tests well-seeded reference torrents."""
    results, winner = race([(h, None) for h in REFERENCE_TORRENTS], keep_winner=False)
    return winner is not None, results


# ================================================================ scoring
SIZE_RE = re.compile(r"💾\s*([\d.]+)\s*(GB|MB)")
SEED_RE = re.compile(r"👤\s*(\d+)")
FLAG_RE = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}")


def parse(stream):
    title = stream.get("title", "")
    lines = title.split("\n")
    stats_idx = next((i for i, l in enumerate(lines) if "👤" in l or "💾" in l), len(lines))
    m = SIZE_RE.search(title)
    sm = SEED_RE.search(title)
    return {
        "pack": stats_idx >= 2,  # torrent name + file-inside-torrent line => multi-file torrent
        "size_gb": (float(m.group(1)) / (1 if m.group(2) == "GB" else 1024)) if m else None,
        "res": (stream.get("name", "").split("\n") + [""])[1].strip().lower(),
        "listed_seeders": int(sm.group(1)) if sm else None,
        "foreign": bool(FLAG_RE.search(title)) or bool(re.search(r"[Ѐ-ӿ]", lines[0])),
    }


def score(info, seeders, leechers):
    s = math.log2(1 + seeders + 0.25 * leechers)
    s *= RES_WEIGHT.get(info["res"], 0.6)
    if info["pack"]:
        s *= 0.6
    if info["foreign"]:
        s *= 0.15
    if info["size_gb"]:
        s *= 1 / (1 + max(0.0, info["size_gb"] - 1.5) / 4)
    return s


def health(seeders):
    return "🟢" if seeders >= 10 else "🟡" if seeders >= 3 else "🔴"


# ================================================================ request handling
def fetch_upstream(typ, vid):
    req = urllib.request.Request(f"{UPSTREAM}/stream/{typ}/{vid}.json", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r).get("streams", [])


def diag_entry(headline, lines):
    return {
        "name": "🩺 Diagnosis",
        "title": "\n".join([headline] + lines),
        "externalUrl": f"http://127.0.0.1:{PORT}/status",
        "behaviorHints": {"notWebReady": True},
    }


def build(typ, vid):
    t0 = time.time()
    with cf.ThreadPoolExecutor(4) as ex:
        f_net = ex.submit(network_check)
        f_cur = ex.submit(current_streams)
        f_up = ex.submit(fetch_upstream, typ, vid)
        net_problem, net_evidence = classify_network(f_net.result())
        current = f_cur.result()
        try:
            upstream = f_up.result()
        except Exception as e:
            upstream, up_err = [], str(e)
        else:
            up_err = None

    diag_lines, headline = [], None
    if net_problem:
        headline = f"❌ {net_problem}"
        diag_lines = ["That's why nothing is loading. Torrents can't fix this."] + net_evidence
    stalled = [c for c in (current or []) if c["stalled"]]
    if not headline and stalled:
        c = stalled[0]
        pct = f" at {c['progress'] * 100:.1f}%" if c["progress"] is not None else ""
        headline = f"❌ The stream you were watching is stuck{pct}: 0 bytes in {SAMPLE_SECS:.0f}s"
        diag_lines = [f"{c['name']}", "✓ Your internet is fine; this torrent is the problem:"] + \
                     [f"• {r}" for r in c["reasons"]] + ["→ Pick ★ BEST below (tested just now)."]
    elif not headline and current:
        c = current[0]
        diag_lines = [f"✓ Your current stream is downloading (+{c['bytes'] / 1e6:.1f} MB in {SAMPLE_SECS:.0f}s)."]
    if current is None and not net_problem:
        headline = headline or "⚠️ Stremio's streaming server isn't responding"
        diag_lines = diag_lines or ["Torrents can't play until it's running (Settings → Streaming)."]

    rows = []
    stalled_hashes = {c["hash"] for c in stalled}
    if upstream and not net_problem:
        live = scrape([s["infoHash"] for s in upstream if s.get("infoHash")])
        for s in upstream:
            h = s.get("infoHash")
            if not h:
                continue
            info = parse(s)
            if h in live:
                se, le, _ = live[h]
                sc, tag = score(info, se, le), f"{health(se)} {se} seeders live · {le} leechers"
            else:
                se = None
                sc, tag = score(info, (info["listed_seeders"] or 0) * 0.3, 0) * 0.5, "⚪ live count unavailable"
            if h in stalled_hashes:
                sc, tag = -1, "❌ stuck for you right now · " + tag
            rows.append([sc, se, info, s, tag, None])
        rows.sort(key=lambda r: r[0], reverse=True)

        if RACE_K and current is not None:
            top = [r for r in rows if r[3]["infoHash"] not in stalled_hashes][:RACE_K]
            results, _ = race([(r[3]["infoHash"], r[3].get("fileIdx")) for r in top])
            for r in top:
                r[5] = results.get((r[3]["infoHash"], r[3].get("fileIdx")))
            ok = lambda r: r[5] and r[5]["bytes"] >= RACE_BYTES
            top.sort(key=lambda r: (0 if ok(r) else 1, r[5]["secs"] if ok(r) else 0))
            rest = [r for r in rows if r not in top]
            rows = top + rest
            if top and not any(ok(r) for r in top) and not net_problem:
                works, _ = torrents_work_at_all()
                if not works:
                    headline = ("❌ No torrent downloads on this network right now, "
                                "not even well-known test torrents")
                    diag_lines = ["✓ Regular internet works, so something is blocking torrent traffic "
                                  "(VPN, firewall, router, or internet provider).",
                                  "Streams that don't use torrents (e.g. a debrid service) would still work."]
                else:
                    headline = headline or f"⚠️ None of the top {len(top)} streams delivered data in a live test"
                    diag_lines = diag_lines or ["✓ Torrents work on your network (test torrent downloaded fine).",
                                                "These particular torrents are dead for you right now. "
                                                "Try lower ones, or a debrid service."]

    out = []
    if headline:
        out.append(diag_entry(headline, diag_lines))
    for i, (sc, se, info, s, tag, proof) in enumerate(rows):
        best = i == 0 and proof is not None and proof["bytes"] >= RACE_BYTES
        note = tag + (" · season pack" if info["pack"] else "")
        if proof:
            note = (f"✅ tested: started in {proof['secs']:.1f}s · " if proof["bytes"] >= RACE_BYTES else
                    f"❌ tested: {proof['bytes'] // 1024} KB in {RACE_SECS:.0f}s (won't play well) · ") + note
        new = dict(s)
        new["name"] = ("★ BEST\n" if best else "Live\n") + (info["res"] or "?")
        new["title"] = "\n".join([note] + s.get("title", "").split("\n"))
        bh = dict(s.get("behaviorHints") or {})
        if best:
            bh["bingeGroup"] = "beststream|best"  # auto-play-next keeps choosing our best pick
        new["behaviorHints"] = bh
        out.append(new)
    if up_err and not net_problem:
        out.insert(0, diag_entry("⚠️ Couldn't reach Torrentio", [up_err]))

    _last_diagnosis.update(time=time.strftime("%H:%M:%S"), vid=vid,
                           lines=[headline or "✓ No problems found"] + diag_lines + ["", "Network checks:"] + net_evidence)
    log(f"{vid}: {len(out)} entries in {time.time() - t0:.1f}s; {headline or 'no problems'}")
    return out


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self._send(200, {})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/manifest.json"):
            return self._send(200, MANIFEST)
        if path == "/status":
            d = _last_diagnosis
            body = "<br>".join(html.escape(l) for l in d["lines"])
            page = (f"<!doctype html><meta charset=utf-8><title>Best Stream diagnosis</title>"
                    f"<body style='font:16px system-ui;max-width:720px;margin:40px auto;padding:0 16px'>"
                    f"<h2>Diagnosis</h2><p style='color:#888'>{d.get('time') or ''} {html.escape(d.get('vid') or '')}</p>"
                    f"<p>{body}</p>")
            return self._send(200, page.encode(), "text/html; charset=utf-8")
        m = re.match(r"^/stream/(movie|series)/([^/]+)\.json$", path)
        if not m:
            return self._send(404, {"err": "not found"})
        try:
            streams = build(m.group(1), urllib.request.unquote(m.group(2)))
        except Exception as e:
            log(f"error: {e!r}")
            streams = [diag_entry("⚠️ Best Stream hit an error", [repr(e)])]
        self._send(200, {"streams": streams})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--rank":  # CLI: --rank series/tt123:3:5
        typ, vid = sys.argv[2].split("/", 1)
        for s in build(typ, vid)[:8]:
            print(s["name"].replace("\n", " "), "|", s["title"].replace("\n", " / ")[:220])
        sys.exit(0)
    print(f"Best Stream addon on http://127.0.0.1:{PORT}/manifest.json  (upstream: {UPSTREAM})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
