#!/usr/bin/env python3
"""
FM radio for MPD: an RTL-SDR stick as one more internet-radio-like stream.

Runs on the board itself (not in the App Lab container), as the systemd
service fm-radio.service. Plain Python, no numpy.

    RTL-SDR -> rtl_fm (FM demodulation, MPX at 171 kHz)
                 |-> redsea -> RDS: station name (PS), radiotext (RT)
                 '-> sox: 15 kHz low-pass, 50 us de-emphasis, 48 kHz
                       -> HTTP audio/L16 -> MPD

HTTP on port 8001:
    /fm              the audio stream (MPD plays http://localhost:8001/fm)
    /tune?f=107.5    switch to 107.5 MHz (the stream goes on)
    /status          JSON: frequency, RDS (station, artist, title), scan progress
    /stations        JSON: the station list
    /scan            look for stations (~1 min); the list is saved to
                     ~/.config/fm-radio/stations.txt, "107.5 | Radio ZET" per
                     line: edit the names there, a new scan keeps them

The tuner only runs while someone (MPD) is listening to /fm. Mono for now.
"""

import collections
import json
import os
import re
import select
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = 8001
BIND = "0.0.0.0"             # the App Lab container asks /status over the docker gateway
GAIN = 30                    # tuner gain, dB (rtl_test -t lists the steps)
MPX_RATE = 171000            # rtl_fm output: the whole FM baseband, as redsea wants it
AUDIO_RATE = 48000
DEFAULT_MHZ = 107.5
IDLE_STOP_S = 10             # tuner off this long after the last listener left
RETRY_S = 5                  # a listener without audio this long: start the tuner again
CONFIG = os.path.expanduser("~/.config/fm-radio")
STATE = os.path.join(CONFIG, "state.json")
STATIONS = os.path.join(CONFIG, "stations.txt")
CHUNK = 4096                 # bytes per read
BUFFER_S = 2.0               # audio kept for a listener that falls behind

# Scan: a sweep of the band, then RDS on every peak for the station's name.
SCAN_BAND = "87.5M:108M:100k"
SCAN_SWEEP_S = 10            # rtl_power integration
SCAN_ABOVE_DB = 6            # a peak this much above the band's median is a station
SCAN_MAX = 30
SCAN_RDS_S = 6               # how long to wait for a station's name

AUDIO_BYTES_S = AUDIO_RATE * 2
MIME = f"audio/L16;rate={AUDIO_RATE};channels=1"   # raw PCM, MPD's pcm decoder

# Radiotext often rotates "now playing", "next" and a slogan. Only the first
# is the song. RadioText+ (tags), when a station sends it, says it exactly.
NOW_PREFIX = re.compile(r"^\s*(teraz gramy|gramy|teraz|now playing|now|on air|słuchasz)\s*[:\-]\s*", re.I)
NEXT_PREFIX = re.compile(r"^\s*(zagramy|za chwilę|za chwile|next|coming up|potem|następnie)\b", re.I)


def log(*a):
    print("[fm-radio]", *a, flush=True)


def song_from_radiotext(text):
    """(artist, title) if the radiotext names the song now on air, else None."""
    if not text or NEXT_PREFIX.match(text):
        return None
    m = NOW_PREFIX.match(text)
    body = text[m.end():] if m else text
    if " - " in body:
        artist, title = body.split(" - ", 1)
        return artist.strip(), title.strip()
    return ("", body.strip()) if m else None


def load_stations():
    """[(mhz, name)] from stations.txt, in file order."""
    out = []
    try:
        with open(STATIONS, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                mhz, _, name = line.partition("|")
                try:
                    out.append((round(float(mhz), 2), name.strip()))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def save_stations(stations):
    os.makedirs(CONFIG, exist_ok=True)
    tmp = STATIONS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("# FM stations: MHz | name. Found by the scan; edit the names freely.\n")
        for mhz, name in stations:
            f.write(f"{mhz:.1f} | {name}\n")
    os.replace(tmp, STATIONS)


class Radio:
    def __init__(self):
        self.lock = threading.Lock()
        self.mhz = self._load()
        self.procs = []
        self.gen = 0                 # pipeline generation: old threads stop on change
        self.audio = collections.deque()
        self.audio_bytes = 0
        self.audio_cv = threading.Condition()
        self.listeners = 0
        self.last_listener = time.monotonic()
        self.rds = {}
        self.song = ("", "")         # artist, title on air (RT+ or radiotext)
        self.scan = None             # progress while scanning
        self.redsea = shutil.which("redsea")
        if not self.redsea:
            log("redsea not found: no RDS")
        threading.Thread(target=self._idle_watch, daemon=True).start()

    # --- state -----------------------------------------------------------
    def _load(self):
        try:
            with open(STATE) as f:
                return float(json.load(f)["mhz"])
        except (OSError, ValueError, KeyError):
            return DEFAULT_MHZ

    def _save(self):
        os.makedirs(CONFIG, exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"mhz": self.mhz}, f)
        os.replace(tmp, STATE)

    def status(self):
        with self.lock:
            return {"mhz": self.mhz, "running": bool(self.procs),
                    "listeners": self.listeners,
                    "ps": self.rds.get("ps", "").strip(),
                    "rt": self.rds.get("radiotext", "").strip(),
                    "artist": self.song[0], "title": self.song[1],
                    "pi": self.rds.get("pi", ""), "scan": self.scan}

    # --- the pipeline ----------------------------------------------------
    def start(self):
        with self.lock:
            if self.procs or self.scan:
                return
            self.gen += 1
            gen = self.gen
            self.rds, self.song = {}, ("", "")
            rtl = subprocess.Popen(
                ["rtl_fm", "-M", "fm", "-l", "0", "-A", "std", "-s", str(MPX_RATE),
                 "-g", str(GAIN), "-f", f"{self.mhz}M", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            # De-emphasis 50 us = a first-order low-pass at 3183 Hz; above 15 kHz
            # there's the 19 kHz pilot, stereo and RDS, not sound. The output is
            # big-endian, as audio/L16 is.
            sox = subprocess.Popen(
                ["sox", "-q", "-t", "raw", "-r", str(MPX_RATE), "-e", "signed", "-b", "16",
                 "-c", "1", "-", "-t", "raw", "-r", str(AUDIO_RATE), "-e", "signed",
                 "-b", "16", "-B", "-c", "1", "-", "sinc", "-15k", "lowpass", "-1", "3183"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            self.procs = [rtl, sox]
            red = None
            if self.redsea:
                red = subprocess.Popen([self.redsea, "-r", str(MPX_RATE)],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL)
                self.procs.append(red)
            log(f"tuner on, {self.mhz} MHz")
        threading.Thread(target=self._pump, args=(gen, rtl, sox, red), daemon=True).start()
        threading.Thread(target=self._stderr, args=(rtl,), daemon=True).start()
        threading.Thread(target=self._audio, args=(gen, sox), daemon=True).start()
        if red:
            threading.Thread(target=self._rds, args=(gen, red), daemon=True).start()

    def stop(self):
        with self.lock:
            procs, self.procs = self.procs, []
            self.gen += 1
            self.rds, self.song = {}, ("", "")
        for p in procs:
            try:
                if p.stdin:
                    p.stdin.close()
            except OSError:
                pass
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
        if procs:
            log("tuner off")

    def tune(self, mhz):
        with self.lock:
            self.mhz = mhz
            running = bool(self.procs)
        self._save()
        log(f"tune {mhz} MHz")
        if running:            # rtl_fm can't retune: restart it, the stream goes on
            self.stop()
            self.start()

    def _pump(self, gen, rtl, sox, red):
        """rtl_fm's MPX to sox (sound) and redsea (RDS)."""
        while gen == self.gen:
            data = rtl.stdout.read(CHUNK)
            if not data:
                break
            for p in (sox, red):
                if p is None:
                    continue
                try:
                    p.stdin.write(data)
                    p.stdin.flush()
                except (BrokenPipeError, ValueError, OSError):
                    pass
        if gen == self.gen:
            # It died (stick unplugged or busy): clean up, so a listener starts
            # it again instead of waiting for audio that won't come.
            log(f"rtl_fm ended, exit code {rtl.wait()}")
            threading.Thread(target=self.stop, daemon=True).start()

    def _stderr(self, rtl):
        """rtl_fm's messages into our log (tuner, sample rate, errors)."""
        for line in rtl.stderr:
            line = line.decode(errors="replace").strip()
            if line:
                log("rtl_fm:", line)

    def _audio(self, gen, sox):
        """sox's 48 kHz audio into the buffer the HTTP listener reads."""
        limit = int(BUFFER_S * AUDIO_BYTES_S)
        while gen == self.gen:
            data = sox.stdout.read(CHUNK)
            if not data:
                break
            with self.audio_cv:
                self.audio.append(data)
                self.audio_bytes += len(data)
                while self.audio_bytes > limit:          # a slow listener: drop the oldest
                    self.audio_bytes -= len(self.audio.popleft())
                self.audio_cv.notify_all()

    def _rds(self, gen, red):
        """redsea prints one JSON object per RDS group: keep the latest fields,
        and the song on air from RadioText+ or the radiotext."""
        for line in red.stdout:
            if gen != self.gen:
                break
            try:
                g = json.loads(line)
            except ValueError:
                continue
            song = None
            tags = (g.get("radiotext_plus") or {}).get("tags") or []
            if tags:
                t = {x.get("content-type"): x.get("data", "") for x in tags}
                if t.get("item.title"):
                    song = (t.get("item.artist", "").strip(), t["item.title"].strip())
            elif "radiotext" in g:
                song = song_from_radiotext(g["radiotext"])
            with self.lock:
                if gen != self.gen:
                    break
                for k in ("pi", "ps", "radiotext", "prog_type"):
                    if k in g:
                        if k == "ps" and g[k] != self.rds.get(k):
                            log(f"station: {g[k].strip()}")
                        self.rds[k] = g[k]
                if song and song != self.song:
                    self.song = song
                    log(f"on air: {' - '.join(s for s in song if s)}")

    def _idle_watch(self):
        while True:
            time.sleep(1)
            with self.lock:
                idle = (self.procs and self.listeners == 0
                        and time.monotonic() - self.last_listener > IDLE_STOP_S)
            if idle:
                self.stop()

    # --- scan ------------------------------------------------------------
    def start_scan(self):
        with self.lock:
            if self.scan:
                return False
            self.scan = {"phase": "sweep", "step": 0, "of": 0, "mhz": 0}
        threading.Thread(target=self._scan, daemon=True).start()
        return True

    def _scan(self):
        self.stop()                  # one program at a time can use the stick
        try:
            peaks = self._sweep()
            log(f"scan: {len(peaks)} peaks")
            found = []               # (mhz, name, pi, power)
            for i, (mhz, power) in enumerate(peaks):
                with self.lock:
                    self.scan = {"phase": "rds", "step": i + 1, "of": len(peaks), "mhz": mhz}
                ps, pi = self._listen_rds(mhz)
                log(f"scan: {mhz} MHz {power:+.1f} dB  {ps or '-'}")
                if ps or power >= SCAN_ABOVE_DB * 2:     # no RDS: keep only strong ones
                    found.append((mhz, ps, pi, power))
            best = {}                # the same station (PI) on two frequencies: the stronger
            for mhz, ps, pi, power in found:
                key = pi or mhz
                if key not in best or power > best[key][3]:
                    best[key] = (mhz, ps, pi, power)
            old = dict(load_stations())                  # names edited by hand stay
            stations = sorted((mhz, old.get(mhz) or ps or f"{mhz:.1f} MHz")
                              for mhz, ps, _, _ in best.values())
            if stations:
                save_stations(stations)
            log(f"scan: {len(stations)} stations saved")
        except Exception as exc:
            log(f"scan failed: {exc}")
        finally:
            with self.lock:
                self.scan = None
                listening = self.listeners > 0
            if listening:
                self.start()

    def _sweep(self):
        """[(mhz, dB above the median)], strongest first, at most SCAN_MAX."""
        with tempfile.NamedTemporaryFile(suffix=".csv") as f:
            subprocess.run(["rtl_power", "-f", SCAN_BAND, "-g", str(GAIN), "-i", str(SCAN_SWEEP_S),
                            "-1", f.name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=SCAN_SWEEP_S + 30, check=True)
            points = []
            for row in open(f.name):
                r = row.split(",")
                if len(r) < 7:
                    continue
                f0, step = float(r[2]), float(r[4])
                points += [((f0 + i * step) / 1e6, float(v)) for i, v in enumerate(r[6:])]
        if not points:
            return []
        floor = statistics.median(v for _, v in points)
        peaks = []
        for mhz, v in sorted(points, key=lambda p: -p[1]):
            if v - floor < SCAN_ABOVE_DB or len(peaks) >= SCAN_MAX:
                break
            mhz = round(mhz, 1)
            if all(abs(mhz - p) > 0.25 for p, _ in peaks):
                peaks.append((mhz, v - floor))
        return peaks

    def _listen_rds(self, mhz):
        """(station name, PI) heard on mhz within SCAN_RDS_S, or ("", "")."""
        if not self.redsea:
            return "", ""
        rtl = subprocess.Popen(["rtl_fm", "-M", "fm", "-l", "0", "-A", "std", "-s", str(MPX_RATE),
                                "-g", str(GAIN), "-f", f"{mhz}M", "-"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        red = subprocess.Popen([self.redsea, "-r", str(MPX_RATE)], stdin=rtl.stdout,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        rtl.stdout.close()
        ps, pi = "", ""
        end = time.monotonic() + SCAN_RDS_S
        buf = b""
        try:
            while time.monotonic() < end and not ps:
                ready, _, _ = select.select([red.stdout], [], [], 0.5)
                if not ready:
                    continue
                chunk = os.read(red.stdout.fileno(), 4096)
                if not chunk:
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    try:
                        g = json.loads(line)
                    except ValueError:
                        continue
                    pi = g.get("pi", pi)
                    if g.get("ps", "").strip():
                        ps = g["ps"].strip()
        finally:
            for p in (red, rtl):
                p.terminate()
            for p in (red, rtl):
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
        return ps, pi

    # --- listeners -------------------------------------------------------
    def listen(self, write):
        """Send audio to one listener until it hangs up."""
        with self.lock:
            self.listeners += 1
        self.start()
        with self.audio_cv:                 # start from now, not from old audio
            self.audio.clear()
            self.audio_bytes = 0
        last_audio = time.monotonic()
        try:
            while True:
                with self.audio_cv:
                    while not self.audio:
                        self.audio_cv.wait(1)
                        if time.monotonic() - last_audio > RETRY_S:
                            break
                    data = b"".join(self.audio)
                    self.audio.clear()
                    self.audio_bytes = 0
                if data:
                    last_audio = time.monotonic()
                    write(data)
                else:
                    self.start()            # the tuner died or the stick came back
                    last_audio = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with self.lock:
                self.listeners -= 1
                self.last_listener = time.monotonic()


radio = Radio()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"     # no length, the stream ends when we close

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/fm":
            self.send_response(200)
            self.send_header("Content-Type", MIME)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("icy-name", "FM radio")
            self.end_headers()
            log("listener connected")
            radio.listen(lambda b: (self.wfile.write(b), self.wfile.flush()))
            log("listener left")
        elif url.path == "/tune":
            try:
                mhz = round(float(parse_qs(url.query)["f"][0]), 2)
                if not 64 <= mhz <= 108:
                    raise ValueError
            except (KeyError, ValueError):
                self._json({"error": "f=MHz, 64..108"}, 400)
                return
            radio.tune(mhz)
            self._json(radio.status())
        elif url.path == "/status":
            self._json(radio.status())
        elif url.path == "/stations":
            self._json([{"mhz": m, "name": n} for m, n in load_stations()])
        elif url.path == "/scan":
            started = radio.start_scan()
            self._json({"started": started, **radio.status()})
        else:
            self._json({"error": "not found"}, 404)


def main():
    for tool in ("rtl_fm", "rtl_power", "sox"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} not found: sudo apt install rtl-sdr sox")
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    server.daemon_threads = True
    log(f"listening on {BIND}:{PORT}, {radio.mhz} MHz")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        radio.stop()


if __name__ == "__main__":
    main()
