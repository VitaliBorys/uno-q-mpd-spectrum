# MPD Spectrum Analyzer for Arduino UNO Q

A 13-band audio spectrum analyzer for the built-in 8×13 LED matrix of the
[Arduino UNO Q](https://www.arduino.cc/product-uno-q), driven by whatever
[MPD](https://www.musicpd.org) is playing — CDs, FLAC/WAV files, CUE images
or internet radio.

- 13 log-spaced bands from 40 Hz to 16 kHz (bass on the left)
- Bars with fast attack and smooth fall
- Peak dots that hold for ~0.5 s, then drop
- 25 fps, a few percent of one CPU core
- Never affects playback: if the analyzer stops, MPD just keeps playing

## How it works

```
MPD ──► main audio output (DAC) ──► amplifier
 │
 └──► FIFO output (44.1 kHz / 16-bit / stereo)
         │
         ▼
   python/main.py  (Linux side, App Lab container)
   reads PCM → FFT → 13 bands → 104-byte frame
         │  Bridge.notify("draw", frame)
         ▼
   sketch/sketch.ino  (STM32 MCU)
   matrix.draw(frame) → LED matrix
```

MPD writes a copy of the decoded audio into a named pipe. Its format is fixed
in `mpd.conf`, so every source (hi-res FLAC, MP3 radio, mono) arrives in the
same 44.1 kHz / 16-bit / stereo format, while the main output still gets the
original stream. The volume mixer only affects the main output, so the
analyzer looks the same at any volume.

The App Lab Python app runs inside a Docker container that only sees the app
folder (mounted at `/app`), so the FIFO is placed inside the app folder.

## Requirements

- Arduino UNO Q with Arduino App Lab
- MPD running on the board (`sudo apt install mpd mpc`)

## Setup

### 1. Get the app onto the board

Clone this repository into the App Lab apps folder on the board:

```sh
cd ~/ArduinoApps
git clone https://github.com/VitaliBorys/uno-q-mpd-spectrum.git spectrum
```

### 2. Create the FIFO

```sh
mkdir -p ~/ArduinoApps/spectrum/viz
mkfifo ~/ArduinoApps/spectrum/viz/mpd.fifo
chmod 666 ~/ArduinoApps/spectrum/viz/mpd.fifo
```

MPD runs as its own user, so it needs to be able to traverse into the folder.
The default permissions of `/home/arduino` (`drwxr-xr-x`) allow this. Also
check that the MPD service does not set `ProtectHome=`:

```sh
systemctl cat mpd | grep -i protect
```

### 3. Add the FIFO output to MPD

Append to `/etc/mpd.conf`:

```
audio_output {
    type    "fifo"
    name    "visualizer"
    path    "/home/arduino/ArduinoApps/spectrum/viz/mpd.fifo"
    format  "44100:16:2"
}
```

Then restart MPD and check that both outputs are enabled:

```sh
sudo systemctl restart mpd
mpc outputs
```

Quick test while music is playing (should be roughly 350–400 KB):

```sh
timeout 2 cat ~/ArduinoApps/spectrum/viz/mpd.fifo > /tmp/fifo_test.raw
ls -l /tmp/fifo_test.raw
```

### 4. Run

Open the `spectrum` app in Arduino App Lab and press **Run**. The first start
installs numpy and takes a minute or two.

## Tuning

All settings are constants at the top of `python/main.py`:

| Setting | Default | Effect |
|---|---|---|
| `FLOOR_DB` | `-48.0` | Sensitivity. Closer to 0 (e.g. `-42`) → lower bars; further (e.g. `-54`) → taller bars |
| `FPS` | `25` | Frame rate |
| `BAR_FALL` | `0.5` | How fast bars drop, rows per frame |
| `PEAK_HOLD` | `12` | Frames a peak dot stays before falling |
| `PEAK_FALL` | `0.15` | How fast peak dots drop, rows per frame |
| `BAR_BRIGHT` | `7` | Bar brightness, 0..7 |
| `PEAK_BRIGHT` | `4` | Peak dot brightness, 0..7 |

## Limitations

- Only audio played through MPD is shown. Bluetooth audio going straight to a
  DAC, or players like Spotify Connect / AirPlay that output to ALSA directly,
  bypass the FIFO.
- The FIFO is fed slightly ahead of what you hear because of the DAC's output
  buffer. In practice the offset is small.

## Project layout

```
python/main.py           audio capture, FFT, frame rendering
python/requirements.txt  Python dependencies (numpy)
sketch/sketch.ino        Bridge receiver, LED matrix output
```

## License

MIT
