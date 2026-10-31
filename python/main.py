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
FPS = 25                     # analyses per second
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

# Pad every band to the same length (repeating its last bin, which does not
# change the max) so all 13 band peaks come out of one numpy call.
maxlen = max(len(b) for b in bands)
band_idx = np.array([np.pad(b, (0, maxlen - len(b)), mode="edge") for b in bands])

# The TFT's 32 bands. At 31 Hz a band is ~7 Hz wide, narrower than one bin
# of the 2048-point FFT (21.5 Hz): several bars would show the same bin. So
# the bass bars use a 4096-point FFT (10.8 Hz bins), and a band still
# narrower than a bin reads the spectrum at its centre, interpolated
# between the two nearest bins, so neighbouring bars differ smoothly.
freqs_lo = np.fft.rfftfreq(N_LO, 1 / RATE)
tft_edges = np.geomspace(31, 16000, TFT_BANDS + 1)
tft_plan = []                # per band: (use the N_LO FFT, bin indexes or None, centre Hz)
for lo, hi in zip(tft_edges[:-1], tft_edges[1:]):
    use_lo = np.sqrt(lo * hi) < TFT_LO_HZ
    f = freqs_lo if use_lo else freqs
    idx = np.where((f >= lo) & (f < hi))[0]
    tft_plan.append((use_lo, idx if len(idx) >= 2 else None, np.sqrt(lo * hi)))

last_data = 0.0              # time of the last audio received
rms_lr = np.zeros(2)         # left/right RMS of the latest audio, 0..1
last_levels = None           # last lv message, to skip repeated silence

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
    spec = np.abs(np.fft.rfft(buf[-N:] * window)) / norm
    spec_lo = np.abs(np.fft.rfft(buf * window_lo)) / norm_lo
    db = 20 * np.log10(spec[band_idx].max(axis=1) + 1e-9)
    tft = np.empty(TFT_BANDS)
    for b, (use_lo, idx, centre) in enumerate(tft_plan):
        sp, f = (spec_lo, freqs_lo) if use_lo else (spec, freqs)
        tft[b] = sp[idx].max() if idx is not None else np.interp(centre, f, sp)
    db_tft = 20 * np.log10(tft + 1e-9)
    return (np.clip((db - floor) / -floor, 0, 1),
            np.clip((db_tft - floor) / -floor, 0, 1))


def vu_position(rms):
    """Needle position 0..1 on the sketch's scale: -20..0 VU takes the first
    65 % of the swing, 0..+3 VU the rest (like a real VU meter, roughly)."""
    vu = 20 * np.log10(rms + 1e-9) - VU_REF_DBFS
    return np.where(vu <= 0, (vu + 20) / 20 * 0.65, 0.65 + vu / 3 * 0.35).clip(0, 1)


def loop():
    global last_levels, prof_frames, prof_sends
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
    if lv != last_levels:                    # silence is sent once, not 25 times a second
        # notify = fire-and-forget: no waiting for the MCU to reply
        send("lv", lv)
        last_levels = lv
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
