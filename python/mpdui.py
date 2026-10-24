"""
MPD side of the physical controller (TFT + knob + buttons on the MCU).

- Watches MPD with the `idle` command and pushes what is playing to the MCU:
    trk(src, fmt, artist, title, album, year, no, of, dur, radio)
    st(state, elapsed, volume, repeat, random)
    clk(minutes since midnight)
- Turns knob and button events from the MCU into MPD commands:
    vol(clicks)   cmd(code)   hello()

MPD runs on the host, outside the App Lab container, so it is reached
through the container's default gateway (the host side of the Docker
network). MPD must listen on all interfaces: `bind_to_address "any"`.

Bridge callbacks never wait for MPD: events go to a queue and a worker
thread talks to MPD, so a slow or restarting MPD can't stall the Bridge.
"""

import os
import queue
import re
import socket
import struct
import threading
import time
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:          # very old Python
    ZoneInfo = None

TIMEZONE = "Europe/Warsaw"   # the container runs in UTC; the clock shows local time
VOLUME_STEP = 2              # percent per knob click (must match the sketch)
RADIO_FILE = "/app/radio.txt"
REFRESH_S = 5                # re-send the status this often while playing

# Button codes sent by the sketch (must match sketch.ino)
CMD_TOGGLE, CMD_NEXT, CMD_PREV = 1, 2, 3

# Field limits in bytes. One Bridge message must fit in 256 bytes.
LIMITS = {"src": 7, "fmt": 15, "artist": 44, "title": 64, "album": 52}

LOSSLESS = {"FLAC", "WAV", "AIFF", "AIF", "APE", "WV", "ALAC", "DSF", "DFF", "CD"}


def default_gateway():
    """IPv4 default gateway from /proc/net/route, or None."""
    try:
        with open("/proc/net/route") as f:
            next(f)
            for line in f:
                fields = line.split()
                if fields[1] == "00000000" and int(fields[3], 16) & 2:
                    return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except (OSError, StopIteration, ValueError, IndexError):
        pass
    return None


MPD_HOST = os.environ.get("MPD_HOST") or default_gateway() or "127.0.0.1"
MPD_PORT = int(os.environ.get("MPD_PORT", "6600"))


class Mpd:
    """Minimal MPD protocol client with one persistent, self-healing socket."""

    def __init__(self, host=MPD_HOST, port=MPD_PORT, timeout=5):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock = None
        self.rfile = None

    def _connect(self):
        self.close()
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.rfile = self.sock.makefile("r", encoding="utf-8", errors="replace", newline="\n")
        hello = self.rfile.readline()
        if not hello.startswith("OK MPD"):
            raise ConnectionError(f"unexpected greeting: {hello!r}")

    def close(self):
        for obj in (self.rfile, self.sock):
            try:
                if obj:
                    obj.close()
            except OSError:
                pass
        self.sock = self.rfile = None

    def _send(self, cmd):
        self.sock.sendall((cmd + "\n").encode())
        result = {}
        while True:
            line = self.rfile.readline()
            if not line:
                raise ConnectionError("connection closed")
            line = line.rstrip("\n")
            if line == "OK":
                return result
            if line.startswith("ACK"):
                raise RuntimeError(line)
            key, _, value = line.partition(": ")
            result.setdefault(key, value)     # first value wins (e.g. one "file:")

    def command(self, cmd):
        """Run one command; reconnect and retry once if the link dropped."""
        for attempt in (1, 2):
            try:
                if self.sock is None:
                    self._connect()
                return self._send(cmd)
            except (OSError, ConnectionError):
                self.close()
                if attempt == 2:
                    raise


def load_radio(path=RADIO_FILE):
    """radio.txt: one station per line, "Name | URL". Returns {url: name}."""
    stations = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "|" not in line:
                    continue
                name, url = (part.strip() for part in line.split("|", 1))
                stations[url] = name
    except OSError:
        pass
    return stations


def fit(text, limit):
    """Cut a string to at most `limit` UTF-8 bytes without splitting a character."""
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def describe_format(song, status):
    """Header text: "FLAC 16/44", "FLAC 24/96", "CD 16/44", "MP3 320"..."""
    path = song.get("file", "")
    name = path.rsplit("/", 1)[-1]
    if path.startswith(("http://", "https://")):
        codec = "FLAC" if "flac" in path.lower() else ""
    elif path.startswith("cdda://"):
        codec = "CD"
    else:
        codec = name.rsplit(".", 1)[-1].upper() if "." in name else ""

    rate, bits = 0, ""
    parts = status.get("audio", "").split(":")
    if len(parts) == 3:
        try:
            rate = int(parts[0])
        except ValueError:
            pass
        bits = parts[1]

    if codec in LOSSLESS and rate:
        khz = rate // 1000
        return f"{codec} {bits}/{khz}" if bits.isdigit() else f"{codec} {khz}k"
    bitrate = status.get("bitrate", "0")
    if bitrate not in ("", "0"):
        return f"{codec} {bitrate}".strip()
    return codec


def describe_track(song, status, radio):
    """Everything the trk message carries, as a tuple."""
    path = song.get("file", "")
    fmt = describe_format(song, status)
    if not path:
        return ("MPD", "", "", "", "", 0, 0, 0, 0, 0)

    if path.startswith(("http://", "https://")):
        station = radio.get(path) or song.get("Name") or "Radio"
        full = song.get("Title", "")
        artist, title = full.split(" - ", 1) if " - " in full else ("", full or station)
        return ("RADIO", fmt, artist, title, station, 0, 0, 0, 0, 1)

    artist = song.get("Artist") or song.get("AlbumArtist") or ""
    title = song.get("Title") or os.path.splitext(path.rsplit("/", 1)[-1])[0]
    album = song.get("Album", "")
    m = re.match(r"\d{4}", song.get("Date", ""))
    year = int(m.group()) if m else 0
    no, of = 0, 0
    track = song.get("Track", "")
    if track:
        a, _, b = track.partition("/")
        no = int(a) if a.isdigit() else 0
        of = int(b) if b.isdigit() else 0
    try:
        dur = int(float(song.get("duration") or song.get("Time") or 0))
    except ValueError:
        dur = 0
    src = "CD" if path.startswith("cdda://") else "NAS"
    return (src, fmt, artist, title, album, year, no, of, dur, 0)


def describe_status(status):
    state = {"play": 1, "pause": 2}.get(status.get("state"), 0)
    try:
        elapsed = int(float(status.get("elapsed", 0)))
    except ValueError:
        elapsed = 0
    try:
        volume = int(status.get("volume", -1))
    except ValueError:
        volume = -1
    return (state, elapsed, volume, int(status.get("repeat", 0) == "1"),
            int(status.get("random", 0) == "1"))


def local_minutes():
    if ZoneInfo:
        try:
            now = datetime.now(ZoneInfo(TIMEZONE))
            return now.hour * 60 + now.minute
        except Exception:
            pass
    now = time.localtime()
    return now.tm_hour * 60 + now.tm_min


class Controller:
    """Keeps the MCU screen in sync with MPD and runs the knob's commands."""

    def __init__(self, send):
        self.send = send             # send(name, *args), thread-safe
        self.radio = load_radio()
        self.mpd = Mpd()             # commands and status (shared, under lock)
        self.lock = threading.Lock()
        self.events = queue.Queue()
        self.last_trk = None
        self.last_minute = -1
        for target in (self._watch, self._work, self._tick):
            threading.Thread(target=target, daemon=True).start()

    # --- Bridge callbacks (MCU -> Python): only enqueue, never block ---
    def on_vol(self, clicks):
        self.events.put(("vol", int(clicks)))

    def on_cmd(self, code):
        self.events.put(("cmd", int(code)))

    def on_hello(self):
        self.events.put(("hello", 0))

    # --- State push ---
    def push(self, force=False):
        with self.lock:
            status = self.mpd.command("status")
            song = self.mpd.command("currentsong")
        trk = describe_track(song, status, self.radio)
        if force or trk != self.last_trk:
            src, fmt, artist, title, album, *nums = trk
            self.send("trk", fit(src, LIMITS["src"]), fit(fmt, LIMITS["fmt"]),
                      fit(artist, LIMITS["artist"]), fit(title, LIMITS["title"]),
                      fit(album, LIMITS["album"]), *nums)
            self.last_trk = trk
        self.send("st", *describe_status(status))
        return status

    def push_clock(self, force=False):
        minute = local_minutes()
        if force or minute != self.last_minute:
            self.send("clk", minute)
            self.last_minute = minute

    # --- Threads ---
    def _watch(self):
        """Waits in MPD's `idle` on its own connection; pushes on every change."""
        conn = Mpd(timeout=None)
        while True:
            try:
                self.push(force=self.last_trk is None)
                conn.command("idle player mixer options")
            except Exception as exc:
                print(f"[mpdui] watch: {exc}")
                conn.close()
                time.sleep(2)

    def _tick(self):
        """Clock every minute; status every few seconds while playing (keeps the
        elapsed time honest and picks up the audio format, which MPD only knows
        a moment after a track starts)."""
        last_refresh = 0.0
        while True:
            try:
                self.push_clock()
                if time.monotonic() - last_refresh >= REFRESH_S:
                    last_refresh = time.monotonic()
                    with self.lock:
                        playing = self.mpd.command("status").get("state") == "play"
                    if playing:
                        self.push()
            except Exception as exc:
                print(f"[mpdui] tick: {exc}")
            time.sleep(1)

    def _work(self):
        while True:
            kind, value = self.events.get()
            if kind == "vol":                # a fast turn: add up queued clicks
                while True:
                    try:
                        k, v = self.events.get_nowait()
                    except queue.Empty:
                        break
                    if k == "vol":
                        value += v
                    else:
                        self.events.put((k, v))
                        break
            try:
                if kind == "vol":
                    self._volume(value)
                elif kind == "cmd":
                    self._command(value)
                elif kind == "hello":
                    self.radio = load_radio()
                    self.push(force=True)
                    self.push_clock(force=True)
            except Exception as exc:     # keep the worker alive whatever happens
                print(f"[mpdui] MPD at {self.mpd.host}:{self.mpd.port}: {exc}")
                time.sleep(0.5)

    def _volume(self, clicks):
        with self.lock:
            volume = int(self.mpd.command("status").get("volume", "-1"))
            if volume < 0:
                return                       # no mixer configured in MPD
            self.mpd.command(f"setvol {max(0, min(100, volume + clicks * VOLUME_STEP))}")

    def _command(self, code):
        with self.lock:
            if code == CMD_TOGGLE:
                state = self.mpd.command("status").get("state")
                self.mpd.command("pause 1" if state == "play" else "play")
            elif code == CMD_NEXT:
                self.mpd.command("next")
            elif code == CMD_PREV:
                self.mpd.command("previous")
