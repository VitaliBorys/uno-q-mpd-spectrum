"""
MPD spectrum analyzer and controller for the Arduino UNO Q.

Reads raw PCM from an MPD FIFO output and runs an FFT. Each frame one lv
message (31 bytes) goes to the MCU: 16 bands for the TFT spectrum, the
left/right VU needle positions, and 13 bands for the LED matrix. The sketch
does all the drawing and the motion (bars, peaks, needles).
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
N_LO = 4096                  # a longer FFT (~93 ms) for the TFT's bass bands

# --- Analyzer --------------------------------------------------------------
COLS = 13                    # LED matrix bands (the matrix is 8 x 13)
FPS = 50                     # analyses per second
# Sensitivity (the level shown as an empty bar) is in the Settings menu:
# -36 .. -60 dB, closer to 0 = less sensitive. Stored in settings.json.
# Bar fall, peak hold and brightness of the matrix are in sketch.ino (MAT_*).

PROFILE = False              # print per-stage timings to the Python Shell every 5 s

# --- TFT -------------------------------------------------------------------
TFT_BANDS = 32               # spectrum bars on the TFT, 31 Hz .. 16 kHz
TFT_LO_HZ = 250              # bars below this come from the N_LO FFT
VU_REF_DBFS = -18.0          # level that reads 0 VU (a common digital reference)

# --- Setup -----------------------------------------------------------------
# Non-blocking read so the loop keeps running while MPD is paused or stopped.
fd = os.open(FIFO, os.O_RDONLY | os.O_NONBLOCK)
pending = b""
buf = np.zeros(N_LO, dtype=np.float32)       # the newest N_LO mono samples
window = np.hanning(N).astype(np.float32)
norm = window.sum() / 2      # scale so a full-scale sine reads 0 dB
window_lo = np.hanning(N_LO).astype(np.float32)
norm_lo = window_lo.sum() / 2

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

# All 13 band peaks in one call: the bins of every band one after another,
# and np.maximum.reduceat takes the max of each band's run.
band_flat = np.concatenate(bands)
band_offs = np.cumsum([0] + [len(b) for b in bands[:-1]])

# The TFT's 32 bands. At 31 Hz a band is ~7 Hz wide, narrower than one bin
# of the 2048-point FFT (21.5 Hz): several bars would show the same bin. So
# the bass bars use a 4096-point FFT (10.8 Hz bins), and a band still
# narrower than a bin reads the spectrum at its centre, interpolated
# between the two nearest bins, so neighbouring bars differ smoothly.
freqs_lo = np.fft.rfftfreq(N_LO, 1 / RATE)
tft_edges = np.geomspace(31, 16000, TFT_BANDS + 1)
# Precomputed so a frame is a few numpy calls, not one per band: both
# spectra go into one array (the N_LO one first); a wide band takes the max
# over its bins (reduceat, as for the matrix), a narrow one interpolates
# between two bins: a * (1 - w) + b * w.
_wide_rows, _wide_bands, _narrow = [], [], []
for b, (lo, hi) in enumerate(zip(tft_edges[:-1], tft_edges[1:])):
    centre = np.sqrt(lo * hi)
    use_lo = centre < TFT_LO_HZ
    f, base = (freqs_lo, 0) if use_lo else (freqs, len(freqs_lo))
    idx = np.where((f >= lo) & (f < hi))[0]
    if len(idx) >= 2:
        _wide_bands.append(b)
        _wide_rows.append(idx + base)
    else:
        i = int(np.clip(np.searchsorted(f, centre) - 1, 0, len(f) - 2))
        w = float(np.clip((centre - f[i]) / (f[i + 1] - f[i]), 0, 1))
        _narrow.append((b, i + base, w))
tft_wide_flat = np.concatenate(_wide_rows)
tft_wide_offs = np.cumsum([0] + [len(x) for x in _wide_rows[:-1]])
tft_wide_bands = np.array(_wide_bands)
tft_narrow_bands = np.array([b for b, _, _ in _narrow], dtype=int)
tft_narrow_i = np.array([i for _, i, _ in _narrow], dtype=int)
tft_narrow_w = np.array([w for _, _, w in _narrow], dtype=np.float32)

spec_lo = None               # the bass FFT, refreshed every other frame
lo_frame = False
last_data = 0.0              # time of the last audio received
rms_lr = np.zeros(2)         # left/right RMS of the latest audio, 0..1
last_levels = None           # last lv message, to skip repeated silence
last_sent = 0.0              # when it went out
# The sketch drops the bars if no lv came for 300 ms. A steady tone gives the
# same bytes frame after frame: still send them this often.
LV_REPEAT_S = 0.1

# Bridge.notify is called from several threads (this loop, mpdui): one at a time.
bridge_lock = threading.Lock()


def send(name, *args):
    with bridge_lock:
        Bridge.notify(name, *args)

prof = {"read": 0.0, "fft": 0.0, "bridge": 0.0}
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

    pending = pending[-N_LO * 4 * 2:]        # only the freshest audio matters
    usable = len(pending) - len(pending) % 4  # whole stereo 16-bit frames only
    if usable == 0:
        return

    data = np.frombuffer(pending[:usable], dtype="<i2").astype(np.float32)
    pending = pending[usable:]
    mono = (data[0::2] + data[1::2]) / 65536.0  # L+R mix, scaled to -1..1
    recent = data[-N * 2:] / 32768.0            # last ~46 ms, interleaved L R
    rms_lr = np.sqrt(np.mean(recent.reshape(-1, 2) ** 2, axis=0))

    if len(mono) >= N_LO:
        buf = mono[-N_LO:]
    else:
        buf = np.concatenate((buf[len(mono):], mono))
    last_data = time.monotonic()


def spectrum():
    """Matrix and TFT band levels, 0..1 (the sensitivity setting .. 0 dB)."""
    floor = controller.floor_db()
    global spec_lo, lo_frame
    spec = np.abs(np.fft.rfft(buf[-N:] * window)) / norm
    # The bass FFT spans ~93 ms: every other frame is plenty, and it is the
    # bigger of the two.
    lo_frame = not lo_frame
    if lo_frame or spec_lo is None:
        spec_lo = np.abs(np.fft.rfft(buf * window_lo)) / norm_lo
    db = 20 * np.log10(np.maximum.reduceat(spec[band_flat], band_offs) + 1e-9)
    both = np.concatenate((spec_lo, spec))
    tft = np.empty(TFT_BANDS, dtype=np.float32)
    tft[tft_wide_bands] = np.maximum.reduceat(both[tft_wide_flat], tft_wide_offs)
    a, b = both[tft_narrow_i], both[tft_narrow_i + 1]
    tft[tft_narrow_bands] = a + (b - a) * tft_narrow_w
    db_tft = 20 * np.log10(tft + 1e-9)
    return (np.clip((db - floor) / -floor, 0, 1),
            np.clip((db_tft - floor) / -floor, 0, 1))


def vu_position(rms):
    """Needle position 0..1 on the sketch's scale: -20..0 VU takes the first
    65 % of the swing, 0..+3 VU the rest (like a real VU meter, roughly)."""
    vu = 20 * np.log10(rms + 1e-9) - VU_REF_DBFS
    return np.where(vu <= 0, (vu + 20) / 20 * 0.65, 0.65 + vu / 3 * 0.35).clip(0, 1)


def loop():
    global last_levels, last_sent, prof_frames, prof_sends
    start = time.monotonic()

    read_audio()
    t_read = time.monotonic()

    if t_read - last_data > 0.5:
        levels = np.zeros(TFT_BANDS + 2 + COLS)       # silence: everything falls
    else:
        mx, tft = spectrum()
        levels = np.concatenate((tft, vu_position(rms_lr), mx))
    t_fft = time.monotonic()

    lv = (levels * 255).astype(np.uint8).tobytes()
    # Changed levels go out at once; unchanged ones every LV_REPEAT_S, so the
    # sketch knows they still hold. Silence (all zero) is sent once.
    if lv != last_levels or (any(lv) and t_fft - last_sent >= LV_REPEAT_S):
        # notify = fire-and-forget: no waiting for the MCU to reply
        send("lv", lv)
        last_levels = lv
        last_sent = t_fft
        prof_sends += 1
    t_bridge = time.monotonic()

    if PROFILE:
        prof["read"] += t_read - start
        prof["fft"] += t_fft - t_read
        prof["bridge"] += t_bridge - t_fft
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
Bridge.provide("set", controller.on_set)

App.run(user_loop=loop)
