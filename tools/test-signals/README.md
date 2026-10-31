# Test signals

Tracks for checking what the analyzer shows against known input.

```sh
cd ~/ArduinoApps/spectrum
python3 tools/test-signals/make.py      # ~1 min, writes /mnt/rips/test-signals
mpc update --wait rips
```

Then USB menu → "Test signals" (an album replaces the queue). Turn the
volume down first: tones at −12 dBFS and noise are loud.

`check.py` runs `python/main.py`'s analysis on the same tracks without the
board and prints what the bars and needles get (needs numpy; the sketch's
motion is not simulated). Expected values below are at the default
sensitivity (−48 dB: 3 dB per segment).

| Track | Should show |
|---|---|
| 01 Tones at the labels | 31 Hz, 63 … 16 kHz, 3 s each at −12 dBFS: the bar above that label at 12 of 16 segments. Neighbours light lower (the FFT window's skirt), most in the bass: 31 Hz → bars 0/1/2 at 12/10/6 |
| 02 Levels | 1 kHz from 0 to −48 dBFS in 6 dB steps: bar 17 at 16, 14, 12 … 2, 0 segments |
| 03 Sweep | 20 Hz → 20 kHz in 30 s (3 s per octave): one bar at 11–12 segments walking left to right |
| 04 VU | 1 kHz at −20, −10, −5, 0, +3 VU (0 VU = −18 dBFS RMS): needles step there |
| 05 Left right both | left needle only, right only, both (−3 VU). Bars at 8, 8, 10: they show L+R, so one channel is 6 dB lower |
| 06 Sync beeps | 100 ms beep every second: bar 17 flashes (~140 ms) with each beep. A flash before the beep = the analyzer is ahead of the DAC |
| 07 White noise | bars rise to the right: 0–1 segments at 31 Hz, ~3 at 16 kHz |
| 08 Pink noise | bars fall to the right: ~5 at 31 Hz, ~1 at 16 kHz |

The noise tracks show how bars are measured: a bar is the loudest FFT bin
in its band, not the band's total. Wide treble bands have many bins but each
holds little of pink noise (equal energy per octave, like most music).
