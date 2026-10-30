"""
MPD side of the physical controller (TFT + knob + buttons on the MCU).

- Watches MPD with the `idle` command and pushes what is playing to the MCU:
    trk(src, fmt, artist, title, album, year, no, of, dur, radio)
    st(state, elapsed, volume, repeat, random, song)
    clk(minutes since midnight)
- Turns knob and button events from the MCU into MPD commands:
    vol(clicks)   cmd(code)   hello()
- Serves the menu one page at a time:
    get(list, ctx, start)         -> page(list, ctx, start, total) + item(list, ctx, i, text)...
    sel(list, ctx, index, action) -> play it (action 0) or add it to the queue (1)

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
CD_CACHE_S = 5               # how long a probe of the disc stays valid

# Button codes sent by the sketch (must match sketch.ino)
CMD_TOGGLE, CMD_NEXT, CMD_PREV = 1, 2, 3

# Menu lists (must match the sketch's enum)
L_QUEUE, L_ARTISTS, L_ARTIST_ALBUMS, L_ALBUMS, L_RADIO, L_CD = 1, 2, 3, 4, 5, 6
PAGE = 8                     # rows on the screen
ITEM_LIMIT = 64              # bytes per menu row
CURRENT = "\x10"             # glyph 16 in the sketch's font: a small play triangle

# Field limits in bytes. One Bridge message must fit in 256 bytes.
LIMITS = {"src": 7, "fmt": 15, "artist": 44, "title": 64, "album": 52}

STREAM_CODECS = {"MP3", "OGG", "OGA", "OPUS", "AAC", "M4A"}
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
        """Send a command, return the response as a list of (key, value)."""
        self.sock.sendall((cmd + "\n").encode())
        pairs = []
        while True:
            line = self.rfile.readline()
            if not line:
                raise ConnectionError("connection closed")
            line = line.rstrip("\n")
            if line == "OK":
                return pairs
            if line.startswith("ACK"):
                raise RuntimeError(line)
            key, _, value = line.partition(": ")
            pairs.append((key, value))

    def lines(self, cmd):
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

    def command(self, cmd):
        """Run one command; the response as a dict (first value of each key)."""
        result = {}
        for key, value in self.lines(cmd):
            result.setdefault(key, value)
        return result

    def batch(self, cmds):
        """Several commands in one round trip (command_list_begin/end)."""
        if cmds:
            self.lines("command_list_begin\n" + "\n".join(cmds) + "\ncommand_list_end")


def quote(text):
    """An MPD protocol argument: in double quotes, with \\ and \" escaped."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def records(pairs, start_key="file"):
    """Split a flat MPD response into dicts, one per song."""
    out, cur = [], None
    for key, value in pairs:
        if key == start_key:
            cur = {key: value}
            out.append(cur)
        elif key in ("directory", "playlist"):
            cur = None
        elif cur is not None:
            cur.setdefault(key, value)
    return out


def number(text):
    """Leading integer of "3", "3/12", "1997-05-01"; 0 if none."""
    m = re.match(r"\d+", text or "")
    return int(m.group()) if m else 0


class Library:
    """Artists and albums from MPD's database, grouped by AlbumArtist
    (falling back to Artist), sorted case-insensitively. Reloaded when MPD
    reports a database update."""

    def __init__(self):
        self.artists = []          # names
        self.by_artist = {}        # name -> [album, ...] sorted by year
        self.albums = []           # every album, sorted by name

    def load(self, mpd):
        groups = {}
        for song in records(mpd.lines("listallinfo")):
            path = song["file"]
            artist = song.get("AlbumArtist") or song.get("Artist") or "Unknown artist"
            folder = path.rsplit("/", 2)[-2] if "/" in path else "Unknown album"
            album = song.get("Album") or folder      # untagged files: group by folder
            key = (artist, album)
            entry = groups.setdefault(key, {"name": album, "artist": artist, "year": 0, "files": []})
            year = number(song.get("Date"))
            if year and (not entry["year"] or year < entry["year"]):
                entry["year"] = year
            entry["files"].append((number(song.get("Disc")), number(song.get("Track")), path))
        for entry in groups.values():
            entry["files"] = [f for _, _, f in sorted(entry["files"])]
        fold = str.casefold
        self.albums = sorted(groups.values(), key=lambda a: (fold(a["name"]), fold(a["artist"])))
        self.by_artist = {}
        for a in groups.values():
            self.by_artist.setdefault(a["artist"], []).append(a)
        for albums in self.by_artist.values():
            albums.sort(key=lambda a: (a["year"] or 9999, fold(a["name"])))
        self.artists = sorted(self.by_artist, key=fold)
        print(f"[mpdui] library: {len(self.artists)} artists, {len(self.albums)} albums")


def load_radio(path=RADIO_FILE):
    """radio.txt: one station per line, "Name | URL". Returns {url: name}
    in file order."""
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


def cd_uri(n):
    """MPD's URI of audio CD track n (1-based)."""
    return f"cdda:///{n}"


def cd_track(path):
    """Track number of a cdda:/// URI, 0 for anything else."""
    return number(path[8:]) if path.startswith("cdda:///") else 0


def extension(path):
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].upper() if "." in name else ""


def describe_format(song, status, codec=None):
    """Header text: "FLAC 16/44", "FLAC 24/96", "CD 16/44", "MP3 320"...
    `codec` overrides the guess from the file name (tracks of a CUE sheet)."""
    path = song.get("file", "")
    if codec is not None:
        pass
    elif path.startswith(("http://", "https://")):
        # Streams: a hint in the URL (".../flacm", "stream.ogg", "x.mp3"), if any
        url = path.lower().split("?", 1)[0]
        ext = extension(url)
        codec = ("FLAC" if "flac" in url else ext if ext in STREAM_CODECS else "")
    elif path.startswith("cdda://"):
        codec = "CD"
    else:
        codec = extension(path)

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
        return f"{codec} {bitrate}" if codec else f"{bitrate} kbps"
    if bits.isdigit() and rate:                  # codec unknown: at least 16/44
        return f"{bits}/{rate // 1000}"
    return codec


def describe_track(song, status, radio, codec=None):
    """Everything the trk message carries, as a tuple."""
    path = song.get("file", "")
    fmt = describe_format(song, status, codec)
    if not path:
        return ("MPD", "", "", "", "", 0, 0, 0, 0, 0)

    if path.startswith(("http://", "https://")):
        station = radio.get(path) or song.get("Name") or "Radio"
        full = song.get("Title", "")
        artist, title = full.split(" - ", 1) if " - " in full else ("", full or station)
        return ("RADIO", fmt, artist, title, station, 0, 0, 0, 0, 1)

    artist = song.get("Artist") or song.get("AlbumArtist") or ""
    cd = cd_track(path)
    title = (song.get("Title") or (f"Track {cd}" if cd else
                                   os.path.splitext(path.rsplit("/", 1)[-1])[0]))
    album = song.get("Album") or ("Audio CD" if cd else "")
    m = re.match(r"\d{4}", song.get("Date", ""))
    year = int(m.group()) if m else 0
    no, of = 0, 0
    track = song.get("Track", "")
    if track:
        a, _, b = track.partition("/")
        no = int(a) if a.isdigit() else 0
        of = int(b) if b.isdigit() else 0
    no = no or cd
    try:
        dur = int(float(song.get("duration") or song.get("Time") or 0))
    except ValueError:
        dur = 0
    # music_directory holds nas/ (the QNAP over NFS) and rips/ (the USB stick)
    src = ("CD" if path.startswith("cdda://") else
           "USB" if path.startswith("rips/") else "NAS")
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
    song = int(status["song"]) if status.get("song", "").isdigit() else -1
    return (state, elapsed, volume, int(status.get("repeat", 0) == "1"),
            int(status.get("random", 0) == "1"), song)


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
        self.library = Library()
        self.cue_codecs = {}         # CUE sheet path -> codec of its audio file
        self.cd_count = 0            # audio tracks on the disc (see _cd_tracks)
        self.cd_checked = -CD_CACHE_S
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

    def on_get(self, lst, ctx, start):
        self.events.put(("get", (int(lst), int(ctx), int(start))))

    def on_sel(self, lst, ctx, index, action):
        self.events.put(("sel", (int(lst), int(ctx), int(index), int(action))))

    # --- State push ---
    def push(self, force=False):
        with self.lock:
            status = self.mpd.command("status")
            song = self.mpd.command("currentsong")
            codec = self._cue_codec(song.get("file", ""))
        trk = describe_track(song, status, self.radio, codec)
        if force or trk != self.last_trk:
            src, fmt, artist, title, album, *nums = trk
            self.send("trk", fit(src, LIMITS["src"]), fit(fmt, LIMITS["fmt"]),
                      fit(artist, LIMITS["artist"]), fit(title, LIMITS["title"]),
                      fit(album, LIMITS["album"]), *nums)
            self.last_trk = trk
        self.send("st", *describe_status(status))
        return status

    def _cue_codec(self, path):
        """Tracks of a CUE sheet look like "Album/album.cue/track0003": no
        extension to go by. The audio file sits next to the sheet, usually
        with the same name; look it up once per folder. Call under the lock."""
        if ".cue/" not in path.lower():
            return None
        cue = path[:path.lower().index(".cue/") + 4]
        if cue not in self.cue_codecs:
            folder, _, sheet = cue.rpartition("/")
            stem = sheet[:-4].casefold()
            skip = ("", "CUE", "LOG", "TXT", "JPG", "PNG", "M3U", "M3U8", "TOC", "NFO")
            files = [v for k, v in self.mpd.lines(f"lsinfo {quote(folder)}") if k == "file"]
            audio = [f for f in files if extension(f) not in skip]
            if not audio:
                # MPD 0.24 hides the files a CUE sheet refers to (e.g. whipper's
                # per-track FLACs next to its .cue); listfiles still shows them.
                files = [v for k, v in self.mpd.lines(f"listfiles {quote(folder)}") if k == "file"]
                audio = [f for f in files if extension(f) not in skip]
            same = [f for f in audio if f.rsplit("/", 1)[-1].casefold().startswith(stem + ".")]
            self.cue_codecs[cue] = extension((same or audio or [""])[0])
        return self.cue_codecs[cue]

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
                changed = [v for k, v in conn.lines("idle player mixer options database")
                           if k == "changed"]
                if "database" in changed:
                    self.events.put(("reload", 0))
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
        self.events.put(("reload", 0))
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
                elif kind == "reload":
                    with self.lock:
                        self.library.load(self.mpd)
                elif kind == "get":
                    self._page(*value)
                elif kind == "sel":
                    self._select(*value)
            except Exception as exc:     # keep the worker alive whatever happens
                print(f"[mpdui] MPD at {self.mpd.host}:{self.mpd.port}: {exc}")
                time.sleep(0.5)

    def _volume(self, clicks):
        with self.lock:
            volume = int(self.mpd.command("status").get("volume", "-1"))
            if volume < 0:
                return                       # no mixer configured in MPD
            self.mpd.command(f"setvol {max(0, min(100, volume + clicks * VOLUME_STEP))}")

    # --- Menu ---
    def _rows(self, lst, ctx):
        """All rows of a list, as text."""
        lib = self.library
        if lst == L_QUEUE:
            with self.lock:
                songs = records(self.mpd.lines("playlistinfo"))
                current = self.mpd.command("status").get("song", "")
            rows = []
            for i, song in enumerate(songs):
                path = song["file"]
                if path.startswith(("http://", "https://")):
                    text = self.radio.get(path) or song.get("Name") or path
                else:
                    artist, title = song.get("Artist", ""), song.get("Title", "")
                    cd = cd_track(path)
                    text = (f"{artist} - {title}" if artist and title
                            else title or (f"CD track {cd}" if cd else
                                           os.path.splitext(path.rsplit("/", 1)[-1])[0]))
                rows.append((CURRENT if str(i) == current else " ") + text)
            return rows
        if lst == L_ARTISTS:
            return lib.artists
        if lst == L_ARTIST_ALBUMS:
            albums = lib.by_artist.get(lib.artists[ctx], []) if ctx < len(lib.artists) else []
            return [a["name"] for a in albums]
        if lst == L_ALBUMS:
            return [a["name"] for a in lib.albums]
        if lst == L_RADIO:
            with self.lock:
                playing = self.mpd.command("currentsong").get("file", "")
            return [(CURRENT if url == playing else " ") + name for url, name in self.radio.items()]
        if lst == L_CD:
            count = self._cd_tracks()
            if not count:
                return []                    # the sketch shows "No disc"
            with self.lock:
                playing = self.mpd.command("currentsong").get("file", "")
            return [" Play disc"] + [(CURRENT if playing == cd_uri(n) else " ") + f"Track {n}"
                                     for n in range(1, count + 1)]
        return []

    def _cd_tracks(self):
        """Audio tracks on the disc, 0 if there is none. MPD can play
        cdda:///N but can't list a disc, so probe: `lsinfo cdda:///N` fails
        with "No such file" for a track that exists (no tags) and with
        "Error ... on track N" past the last one. Kept for a few seconds so
        paging through the list doesn't probe again."""
        if time.monotonic() - self.cd_checked < CD_CACHE_S:
            return self.cd_count
        with self.lock:
            def exists(n):
                try:
                    self.mpd.lines(f"lsinfo {quote(cd_uri(n))}")
                    return True
                except RuntimeError as exc:
                    if "on track" in str(exc):
                        return False
                    if "[50@" in str(exc):
                        return True
                    raise                    # no disc, drive not ready
            try:
                if not exists(1):
                    count = 0
                else:
                    lo, hi = 1, 2            # lo exists; grow hi until it doesn't
                    while hi <= 99 and exists(hi):
                        lo, hi = hi, hi * 2
                    hi = min(hi, 100)
                    while hi - lo > 1:       # last existing track in (lo, hi)
                        mid = (lo + hi) // 2
                        lo, hi = (mid, hi) if exists(mid) else (lo, mid)
                    count = lo
            except RuntimeError as exc:
                print(f"[mpdui] cd: {exc}")
                count = 0
        self.cd_count, self.cd_checked = count, time.monotonic()
        return count

    def _page(self, lst, ctx, start):
        rows = self._rows(lst, ctx)
        self.send("page", lst, ctx, start, len(rows))
        for i in range(start, min(start + PAGE, len(rows))):
            self.send("item", lst, ctx, i, fit(rows[i], ITEM_LIMIT))

    def _files(self, lst, ctx, index):
        """Files (or stream URLs) behind a menu row."""
        lib = self.library
        if lst == L_ARTISTS and index < len(lib.artists):
            return [f for a in lib.by_artist[lib.artists[index]] for f in a["files"]]
        if lst == L_ARTIST_ALBUMS and ctx < len(lib.artists):
            albums = lib.by_artist.get(lib.artists[ctx], [])
            return albums[index]["files"] if index < len(albums) else []
        if lst == L_ALBUMS and index < len(lib.albums):
            return lib.albums[index]["files"]
        if lst == L_RADIO:
            urls = list(self.radio)
            return [urls[index]] if index < len(urls) else []
        return []

    def _select(self, lst, ctx, index, action):
        if lst == L_CD:
            count = self._cd_tracks()
            if not count or index > count:
                return
            whole = [f"add {quote(cd_uri(n))}" for n in range(1, count + 1)]
            with self.lock:
                if action == 0:              # the whole disc, from this track
                    self.mpd.batch(["clear"] + whole + [f"play {max(0, index - 1)}"])
                elif index == 0:
                    self.mpd.batch(whole)
                else:
                    self.mpd.command(f"add {quote(cd_uri(index))}")
            return
        with self.lock:
            if lst == L_QUEUE:
                if action == 0:
                    self.mpd.command(f"play {index}")
                return
            files = self._files(lst, ctx, index)
            if not files:
                return
            adds = [f"add {quote(f)}" for f in files]
            if action == 0:                  # replace the queue and play
                self.mpd.batch(["clear"] + adds + ["play 0"])
            else:                            # add to the end, keep playing
                self.mpd.batch(adds)

    def _command(self, code):
        with self.lock:
            if code == CMD_TOGGLE:
                state = self.mpd.command("status").get("state")
                self.mpd.command("pause 1" if state == "play" else "play")
            elif code == CMD_NEXT:
                self.mpd.command("next")
            elif code == CMD_PREV:
                self.mpd.command("previous")
