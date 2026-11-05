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
    /status          JSON: frequency, running, RDS name and text

The tuner only runs while someone (MPD) is listening to /fm. Mono for now.
"""

import collections
import json
import os
import shutil
import subprocess
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
STATE = os.path.expanduser("~/.config/fm-radio/state.json")
CHUNK = 4096                 # bytes per read
BUFFER_S = 2.0               # audio kept for a listener that falls behind

AUDIO_BYTES_S = AUDIO_RATE * 2
MIME = f"audio/L16;rate={AUDIO_RATE};channels=1"   # raw PCM, MPD's pcm decoder


def log(*a):
    print("[fm-radio]", *a, flush=True)


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
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"mhz": self.mhz}, f)
        os.replace(tmp, STATE)

    def status(self):
        with self.lock:
            return {"mhz": self.mhz, "running": bool(self.procs),
                    "listeners": self.listeners,
                    "ps": self.rds.get("ps", ""), "rt": self.rds.get("radiotext", ""),
                    "pi": self.rds.get("pi", "")}

    # --- the pipeline ----------------------------------------------------
    def start(self):
        with self.lock:
            if self.procs:
                return
            self.gen += 1
            gen = self.gen
            self.rds = {}
            rtl = subprocess.Popen(
                ["rtl_fm", "-M", "fm", "-l", "0", "-A", "std", "-s", str(MPX_RATE),
                 "-g", str(GAIN), "-f", f"{self.mhz}M", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
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
                                       stderr=subprocess.DEVNULL, text=False)
                self.procs.append(red)
            log(f"tuner on, {self.mhz} MHz")
        threading.Thread(target=self._pump, args=(gen, rtl, sox, red), daemon=True).start()
        threading.Thread(target=self._audio, args=(gen, sox), daemon=True).start()
        if red:
            threading.Thread(target=self._rds, args=(gen, red), daemon=True).start()

    def stop(self):
        with self.lock:
            procs, self.procs = self.procs, []
            self.gen += 1
            self.rds = {}
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
            log("rtl_fm ended")

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
        """redsea prints one JSON object per RDS group: keep the latest fields."""
        for line in red.stdout:
            if gen != self.gen:
                break
            try:
                g = json.loads(line)
            except ValueError:
                continue
            with self.lock:
                for k in ("pi", "ps", "radiotext", "prog_type"):
                    if k in g:
                        if k in ("ps", "radiotext") and g[k] != self.rds.get(k):
                            log(f"{k}: {g[k]}")
                        self.rds[k] = g[k]

    def _idle_watch(self):
        while True:
            time.sleep(1)
            with self.lock:
                idle = (self.procs and self.listeners == 0
                        and time.monotonic() - self.last_listener > IDLE_STOP_S)
            if idle:
                self.stop()

    # --- listeners -------------------------------------------------------
    def listen(self, write):
        """Send audio to one listener until it hangs up."""
        with self.lock:
            self.listeners += 1
        self.start()
        with self.audio_cv:                 # start from now, not from old audio
            self.audio.clear()
            self.audio_bytes = 0
        try:
            while True:
                with self.audio_cv:
                    while not self.audio:
                        self.audio_cv.wait(1)
                    data = b"".join(self.audio)
                    self.audio.clear()
                    self.audio_bytes = 0
                write(data)
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
            self.send_header("icy-name", f"FM {radio.mhz:.1f}")
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
        else:
            self._json({"error": "not found"}, 404)


def main():
    for tool in ("rtl_fm", "sox"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} not found: sudo apt install rtl-sdr sox")
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    server.daemon_threads = True
    log(f"listening on {BIND}:{PORT}, {radio.mhz} MHz")
    try:
        server.serve_forever()
    finally:
        radio.stop()


if __name__ == "__main__":
    main()
