#!/usr/bin/env python3
"""
Test signals for checking the analyzer: tones at the spectrum's labels,
level steps, a sweep, VU reference levels, left/right, sync beeps, noise.

Plain Python (no numpy), so it runs on the board as is:

    python3 tools/test-signals/make.py            # -> /mnt/rips/test-signals
    python3 tools/test-signals/make.py /some/dir

Writes FLAC with tags if `flac` is installed (it comes with whipper), WAV
otherwise. Then `mpc update --wait rips` and play "Test signals" from the
USB menu. What each track should show: README.md next to this script.
"""

import array
import math
import os
import random
import shutil
import subprocess
import sys
import wave

RATE = 44100
FADE = 0.02                  # s, fade in/out of every tone (no clicks)

# The spectrum's labels (32 bars) and the bar each one is under.
LABELS = [(31, 0), (63, 3), (125, 7), (250, 10), (500, 14),
          (1000, 17), (2000, 21), (4000, 24), (8000, 28), (16000, 31)]


def db(x):
    return 10 ** (x / 20)


# A track is a list of segments; a segment is (seconds, left, right, note),
# where left/right are functions of t (time within the segment) -> -1..1, or
# None for silence. `note` says what the screen should show (for check.py).

def tone(freq, dbfs, secs):
    a = db(dbfs)
    def f(t):
        env = min(1.0, t / FADE, (secs - t) / FADE)
        return a * env * math.sin(2 * math.pi * freq * t)
    return f


def sweep(f0, f1, dbfs, secs):
    # Exponential (log) sweep: equal time per octave.
    a, k = db(dbfs), math.log(f1 / f0)
    def f(t):
        env = min(1.0, t / FADE, (secs - t) / FADE)
        return a * env * math.sin(2 * math.pi * f0 * secs / k * (math.exp(t / secs * k) - 1))
    return f


def beeps(freq, dbfs, length, period):
    a = db(dbfs)
    def f(t):
        p = t % period
        if p >= length:
            return 0.0
        env = min(1.0, p / 0.002, (length - p) / 0.002)
        return a * env * math.sin(2 * math.pi * freq * t)
    return f


def noise(kind, rms_dbfs, secs, seed):
    """White or pink noise at a given RMS level, generated once (stateful)."""
    rnd = random.Random(seed)
    n = int(secs * RATE)
    out = array.array("d", bytes(8 * n))
    b = [0.0] * 7
    for i in range(n):
        w = rnd.gauss(0, 1)
        if kind == "pink":   # Paul Kellet's filter: -3 dB per octave
            b[0] = 0.99886 * b[0] + w * 0.0555179
            b[1] = 0.99332 * b[1] + w * 0.0750759
            b[2] = 0.96900 * b[2] + w * 0.1538520
            b[3] = 0.86650 * b[3] + w * 0.3104856
            b[4] = 0.55000 * b[4] + w * 0.5329522
            b[5] = -0.7616 * b[5] - w * 0.0168980
            out[i] = b[0] + b[1] + b[2] + b[3] + b[4] + b[5] + b[6] + w * 0.5362
            b[6] = w * 0.115926
        else:
            out[i] = w
    rms = math.sqrt(sum(x * x for x in out) / n)
    g = db(rms_dbfs) / rms
    for i in range(n):
        out[i] *= g
    def f(t):
        return out[min(int(t * RATE), n - 1)]
    return f


def both(f):
    return f, f


TRACKS = [
    ("01", "Tones at the labels", [
        (3.0, *both(tone(hz, -12, 3.0)),
         f"{hz} Hz -12 dBFS: bar {bar} ('{lbl}') at 12 of 16, neighbours lower")
        for hz, bar in LABELS
        for lbl in [f"{hz // 1000}k" if hz >= 1000 else str(hz)]
    ]),
    ("02", "Levels 1 kHz 0 to -48 dB", [
        (3.0, *both(tone(1000, -6 * i, 3.0)),
         f"1 kHz {-6 * i} dBFS: bar 17 at {16 - 2 * i} segments (sensitivity -48)")
        for i in range(9)
    ]),
    ("03", "Sweep 20 Hz to 20 kHz", [
        (30.0, *both(sweep(20, 20000, -12, 30.0)),
         "one bar at 12 segments moving left to right, ~3 s per octave"),
    ]),
    ("04", "VU -20 -10 -5 0 +3", [
        (4.0, *both(tone(1000, vu - 18 + 3.01, 4.0)),
         f"needles at {vu:+d} VU")
        for vu in (-20, -10, -5, 0, 3)
    ]),
    ("05", "Left right both", [
        (3.0, tone(1000, -18, 3.0), None, "left needle at -3 VU, right at rest; bar 17 at 8 (L+R mix: -6 dB)"),
        (3.0, None, tone(1000, -18, 3.0), "right needle at -3 VU, left at rest; bar 17 at 8"),
        (3.0, *both(tone(1000, -18, 3.0)), "both needles at -3 VU; bar 17 at 10"),
    ]),
    ("06", "Sync beeps", [
        (20.0, *both(beeps(1000, -6, 0.1, 1.0)),
         "bar 17 flashes once a second: flash and beep should coincide"),
    ]),
    ("07", "White noise", [
        (10.0, *both(noise("white", -20, 10.0, 1)), "white noise -20 dBFS RMS"),
    ]),
    ("08", "Pink noise", [
        (10.0, *both(noise("pink", -20, 10.0, 2)), "pink noise -20 dBFS RMS"),
    ]),
]


def render(segments):
    """The track as interleaved 16-bit stereo samples."""
    out = array.array("h")
    for secs, left, right, _ in segments:
        n = int(secs * RATE)
        for i in range(n):
            t = i / RATE
            for f in (left, right):
                v = f(t) if f else 0.0
                out.append(max(-32767, min(32767, round(v * 32767))))
    return out


def main():
    dest = sys.argv[1] if len(sys.argv) > 1 else "/mnt/rips/test-signals"
    os.makedirs(dest, exist_ok=True)
    have_flac = shutil.which("flac") is not None
    for num, title, segments in TRACKS:
        name = f"{num} {title}"
        path = os.path.join(dest, name + ".wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(render(segments).tobytes())
        if have_flac:
            subprocess.run(["flac", "-s", "-f", "--delete-input-file",
                            "-T", "ARTIST=Analyzer", "-T", "ALBUM=Test signals",
                            "-T", f"TITLE={title}", "-T", f"TRACKNUMBER={int(num)}",
                            path], check=True)
        print(name)
    print(f"done: {dest}")


if __name__ == "__main__":
    main()
