"""
MPD spectrum analyzer and controller for the Arduino UNO Q.

Reads raw PCM from an MPD FIFO output and runs an FFT. Each frame:
- the LED matrix gets a 104-byte grayscale frame (13 log-spaced bands);
- the TFT gets 16 bands plus left/right levels for its spectrum, VU meters
  and magic eye (the sketch does the drawing and the needle physics).
mpdui.py keeps the TFT's "now playing" screen in sync with MPD and turns
the knob and buttons into MPD commands.
"""

import os
import threading
import time

import numpy as np
from arduino.app_utils import App, Bridge

from mpdui import Controller

# --- Input -----------------------------------------------------------------
FIFO = "/app/viz/mpd.fifo"   # MPD FIFO; the app folder is mounted at /app
RATE = 44100                 # FIFO format must match mpd.conf: 44100:16:2
N = 2048                     # FFT window size (~46 ms of audio)

# --- Display ---------------------------------------------------------------
ROWS, COLS = 8, 13           # UNO Q LED matrix
FPS = 25                     # frames per second
FLOOR_DB = -48.0             # level shown as an empty bar (closer to 0 = less sensitive)

BAR_FALL = 0.5               # bar fall speed, rows per frame
PEAK_HOLD = 12               # frames a peak dot stays put (~0.5 s at 25 fps)
PEAK_FALL = 0.15             # peak dot fall speed, rows per frame
BAR_BRIGHT = 7               # bar brightness (0..7)
PEAK_BRIGHT = 4              # peak dot brightness (0..7)

PROFILE = False              # print per-stage timings to the Python Shell every 5 s

# --- TFT -------------------------------------------------------------------
TFT_BANDS = 16               # spectrum bars on the TFT, 31 Hz .. 16 kHz
VU_REF_DBFS = -18.0          # level that reads 0 VU (a common digital reference)

# --- Setup -----------------------------------------------------------------
# Non-blocking read so the loop keeps running while MPD is paused or stopped.
fd = os.open(FIFO, os.O_RDONLY | os.O_NONBLOCK)
pending = b""
buf = np.zeros(N, dtype=np.float32)
window = np.hanning(N).astype(np.float32)
norm = window.sum() / 2      # scale so a full-scale sine reads 0 dB

# 13 log-spaced bands from 40 Hz to 16 kHz.
edges = np.geomspace(40, 16000, COLS + 1)
freqs = np.fft.rfftfreq(N, 1 / RATE)
bands = []
for lo, hi in zip(edges[:-1], edges[1:]):
    idx = np.where((freqs >= lo) & (freqs < hi))[0]
    if len(idx) == 0:
        # Low bands can be narrower than one FFT bin: use the nearest bin.
        idx = np.array([np.argmin(np.abs(freqs - (lo + hi) / 2))])
    bands.append(idx)

# Pad every band to the same length (repeating its last bin, which does not
# change the max) so all 13 band peaks come out of one numpy call.
maxlen = max(len(b) for b in bands)
band_idx = np.array([np.pad(b, (0, maxlen - len(b)), mode="edge") for b in bands])

# Same thing for the TFT's 16 bands.
tft_edges = np.geomspace(31, 16000, TFT_BANDS + 1)
tft_bands = []
for lo, hi in zip(tft_edges[:-1], tft_edges[1:]):
    idx = np.where((freqs >= lo) & (freqs < hi))[0]
    if len(idx) == 0:
        idx = np.array([np.argmin(np.abs(freqs - (lo + hi) / 2))])
    tft_bands.append(idx)
tft_maxlen = max(len(b) for b in tft_bands)
tft_band_idx = np.array([np.pad(b, (0, tft_maxlen - len(b)), mode="edge") for b in tft_bands])

# Row numbers as a column vector, broadcast against per-column heights.
ROW = np.arange(ROWS)[:, None]

levels = np.zeros(COLS)      # current bar heights (0..ROWS, float)
peaks = np.zeros(COLS)       # current peak dot heights
peak_age = np.zeros(COLS)    # frames since each peak was last pushed up
last_data = 0.0              # time of the last audio received
last_frame = None            # last frame sent, to skip duplicates
rms_lr = np.zeros(2)         # left/right RMS of the latest audio, 0..1
last_levels = None           # last lv message, to skip repeated silence

# Bridge.notify is called from several threads (this loop, mpdui): one at a time.
bridge_lock = threading.Lock()


def send(name, *args):
    with bridge_lock:
        Bridge.notify(name, *args)

prof = {"read": 0.0, "fft": 0.0, "render": 0.0, "bridge": 0.0}
prof_frames = 0
prof_sends = 0
prof_start = time.monotonic()


def report():
    """Print average time per frame for each stage, then reset counters."""
    global prof_frames, prof_sends, prof_start
    elapsed = time.monotonic() - prof_start
    busy = sum(prof.values())
    parts = "  ".join(f"{k} {v / prof_frames * 1000:.2f}" for k, v in prof.items())
    print(f"[profile] {prof_frames / elapsed:.1f} fps, {prof_sends} frames sent | "
          f"ms/frame: {parts} | busy {busy / elapsed * 100:.1f}% of one core")
    for k in prof:
        prof[k] = 0.0
    prof_frames = prof_sends = 0
    prof_start = time.monotonic()


def read_audio():
    """Drain the FIFO, keep the most recent N mono samples in `buf` and the
    left/right RMS of the newest audio in `rms_lr`."""
    global pending, buf, last_data, rms_lr
    while True:
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            break
        if not chunk:        # no writer: MPD is paused or stopped
            break
        pending += chunk

    pending = pending[-N * 4 * 2:]           # only the freshest audio matters
    usable = len(pending) - len(pending) % 4  # whole stereo 16-bit frames only
    if usable == 0:
        return

    data = np.frombuffer(pending[:usable], dtype="<i2").astype(np.float32)
    pending = pending[usable:]
    mono = (data[0::2] + data[1::2]) / 65536.0  # L+R mix, scaled to -1..1
    recent = data[-N * 2:] / 32768.0            # last ~46 ms, interleaved L R
    rms_lr = np.sqrt(np.mean(recent.reshape(-1, 2) ** 2, axis=0))

    if len(mono) >= N:
        buf = mono[-N:]
    else:
        buf = np.concatenate((buf[len(mono):], mono))
    last_data = time.monotonic()


def spectrum():
    """Matrix band heights (0..ROWS) and TFT band levels (0..1)."""
    spec = np.abs(np.fft.rfft(buf * window)) / norm
    db = 20 * np.log10(spec[band_idx].max(axis=1) + 1e-9)
    db_tft = 20 * np.log10(spec[tft_band_idx].max(axis=1) + 1e-9)
    return (np.clip((db - FLOOR_DB) / -FLOOR_DB * ROWS, 0, ROWS),
            np.clip((db_tft - FLOOR_DB) / -FLOOR_DB, 0, 1))


def vu_position(rms):
    """Needle position 0..1 on the sketch's scale: -20..0 VU takes the first
    65 % of the swing, 0..+3 VU the rest (like a real VU meter, roughly)."""
    vu = 20 * np.log10(rms + 1e-9) - VU_REF_DBFS
    return np.where(vu <= 0, (vu + 20) / 20 * 0.65, 0.65 + vu / 3 * 0.35).clip(0, 1)


def render():
    """Build a 104-byte frame (brightness 0..7), row-major, top row first."""
    h = np.rint(levels).astype(np.int8)
    p = np.rint(peaks).astype(np.int8)
    # Bar: every row from the bottom up to height h.
    frame = np.where(ROW >= ROWS - h, BAR_BRIGHT, 0)
    # Peak dot: a single row at height p, only when it is above the bar.
    frame = np.where((ROW == ROWS - p) & (p > h), PEAK_BRIGHT, frame)
    return frame.astype(np.uint8).tobytes()


def loop():
    global levels, peaks, peak_age, last_frame, last_levels, prof_frames, prof_sends
    start = time.monotonic()

    read_audio()
    t_read = time.monotonic()

    if t_read - last_data > 0.5:
        target = np.zeros(COLS)              # silence: let everything fall
        tft_levels = np.zeros(TFT_BANDS + 2)
    else:
        target, tft = spectrum()
        tft_levels = np.concatenate((tft, vu_position(rms_lr)))
    t_fft = time.monotonic()

    # Bars: jump up instantly, fall smoothly.
    levels = np.maximum(target, levels - BAR_FALL)

    # Peaks: pushed up by the bar, held for PEAK_HOLD frames, then fall.
    rising = levels >= peaks
    peaks = np.where(rising, levels, peaks)
    peak_age = np.where(rising, 0, peak_age + 1)
    falling = (~rising) & (peak_age > PEAK_HOLD)
    peaks = np.where(falling, np.maximum(peaks - PEAK_FALL, 0), peaks)

    frame = render()
    t_render = time.monotonic()

    if frame != last_frame:
        # notify = fire-and-forget: no waiting for the MCU to reply (~16 ms saved)
        send("draw", frame)
        last_frame = frame
        prof_sends += 1

    lv = (tft_levels * 255).astype(np.uint8).tobytes()
    if lv != last_levels:                    # silence is sent once, not 25 times a second
        send("lv", lv)
        last_levels = lv
    t_bridge = time.monotonic()

    if PROFILE:
        prof["read"] += t_read - start
        prof["fft"] += t_fft - t_read
        prof["render"] += t_render - t_fft
        prof["bridge"] += t_bridge - t_render
        prof_frames += 1
        if t_bridge - prof_start >= 5:
            report()

    time.sleep(max(0.0, 1 / FPS - (time.monotonic() - start)))


# Knob and buttons: the sketch notifies, the controller talks to MPD.
controller = Controller(send)
Bridge.provide("vol", controller.on_vol)
Bridge.provide("cmd", controller.on_cmd)
Bridge.provide("hello", controller.on_hello)
Bridge.provide("get", controller.on_get)
Bridge.provide("sel", controller.on_sel)

App.run(user_loop=loop)
