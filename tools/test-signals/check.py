#!/usr/bin/env python3
"""
Runs python/main.py's analyzer on the test signals without the board: the
audio goes in 20 ms at a time (as at 50 fps), and for the middle of every
segment it prints what the TFT bars, the VU needles and the matrix get.

    python3 tools/test-signals/check.py [sensitivity index 0..8, default 4]

Needs numpy. Only the analysis is simulated, not the sketch's motion
(fall, peaks, needle ballistics).
"""

import os
import sys
import types

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "python"))
import make  # noqa: E402

SENS = int(sys.argv[1]) if len(sys.argv) > 1 else 4

# main.py talks to the Bridge and opens the FIFO at import: stub both.
app_utils = types.ModuleType("arduino.app_utils")
app_utils.App = types.SimpleNamespace(run=lambda **k: None)
app_utils.Bridge = types.SimpleNamespace(notify=lambda *a: None, provide=lambda *a: None)
sys.modules["arduino"] = types.ModuleType("arduino")
sys.modules["arduino.app_utils"] = app_utils
mpdui = types.ModuleType("mpdui")


class Controller:
    def __init__(self, send):
        pass

    def floor_db(self):
        return -(36.0 + 3 * SENS)

    def __getattr__(self, name):
        return lambda *a: None


mpdui.Controller = Controller
sys.modules["mpdui"] = mpdui
_open = os.open
os.open = lambda *a, **k: -1
import main  # noqa: E402
os.open = _open

FRAME = main.RATE // main.FPS
SEGS = 16


def run(track):
    """Feed one track through the analyzer; yield (segment, frames) with
    every frame's (tft levels, vu positions, matrix levels)."""
    samples = np.array(make.render(track), dtype=np.float32).reshape(-1, 2)
    main.buf = np.zeros(main.N_LO, dtype=np.float32)
    main.spec_lo = None
    pos = 0
    for seg in track:
        n = int(seg[0] * main.RATE)
        frames = []
        for start in range(pos, pos + n - FRAME + 1, FRAME):
            chunk = samples[start:start + FRAME] / 32768.0
            mono = (chunk[:, 0] + chunk[:, 1]) / 2
            main.buf = np.concatenate((main.buf[len(mono):], mono))
            recent = samples[max(0, start + FRAME - main.N):start + FRAME] / 32768.0
            rms = np.sqrt(np.mean(recent ** 2, axis=0))
            mx, tft = main.spectrum()
            frames.append((tft, main.vu_position(rms), mx))
        pos += n
        yield seg, frames


def bars(levels, top=6):
    """The lit bars as 'bar:segments', loudest first."""
    s = np.round(levels * SEGS).astype(int)
    lit = [b for b in np.argsort(-levels) if s[b] > 0][:top]
    more = int((s > 0).sum()) - len(lit)
    return " ".join(f"{b}:{s[b]}" for b in lit) + (f" (+{more} more)" if more > 0 else "")


def vu_text(p):
    # Back from needle position to VU (the sketch's scale).
    return "/".join(f"{(x / 0.65 * 20 - 20) if x <= 0.65 else (x - 0.65) / 0.35 * 3:+.1f}" for x in p)


print(f"sensitivity index {SENS}: floor {-(36 + 3 * SENS)} dB, "
      f"{(36 + 3 * SENS) / SEGS:.2f} dB per segment\n")
for num, title, track in make.TRACKS:
    print(f"== {num} {title}")
    for seg, frames in run(track):
        secs, _, _, note = seg
        if secs > 5:
            # A long segment: sample it every 2.5 s.
            picks = range(len(frames) // 12, len(frames), max(1, len(frames) // 12))
        else:
            picks = [len(frames) // 2]
        print(f"  expect: {note}")
        if num == "06":
            # Beeps: the loudest and quietest frame of every second.
            for k in range(0, len(frames) - main.FPS + 1, main.FPS * 4):
                sec = [f[0][17] for f in frames[k:k + main.FPS]]
                lit = sum(1 for x in sec if x > 0)
                print(f"    second {k // main.FPS:2d}: bar 17 max {max(sec) * SEGS:.0f}, "
                      f"min {min(sec) * SEGS:.0f}, lit in {lit} of {main.FPS} frames")
            continue
        for i in picks:
            tft, vu, mx = frames[i]
            if len(picks) > 1:
                print(f"    t={i / main.FPS:5.1f}s  ", end="")
            else:
                print("    ", end="")
            print(f"TFT {bars(tft)} | VU {vu_text(vu)} | matrix {bars(mx, 3)}")
        if num in ("07", "08"):
            avg = np.mean([f[0] for f in frames], axis=0) * SEGS
            print("    average TFT segments per bar: " + " ".join(f"{x:.0f}" for x in avg))
    print()
