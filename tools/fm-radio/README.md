# FM radio (RTL-SDR)

An RTL-SDR stick (tested: RTL-SDR Blog V3, R820T2) as one more source for
MPD. A small service on the board tunes the stick and serves the sound as an
HTTP stream, so MPD treats it like internet radio: the knob, pause, the
analyzer and the VU meters all work.

```
RTL-SDR -> rtl_fm (FM demodulation, MPX at 171 kHz)
             |-> redsea -> RDS: station name, radiotext
             '-> stereo.py: left/right from the 19 kHz pilot (numpy)
                   -> sox: 15 kHz low-pass, 50 us de-emphasis, 48 kHz
                        -> http://localhost:8001/fm (audio/L16) -> MPD
```

The tuner runs only while MPD listens to the stream, and stops 10 s after
it disconnects. Stereo when the station sends the pilot and it is strong
enough, else mono ("ST" next to the frequency on the screen). Without
numpy (`sudo apt install python3-numpy`) always mono.

## Setup

```sh
sudo apt install rtl-sdr sox python3-numpy
sudo usermod -aG plugdev arduino     # USB access to the stick; log in again
rtl_test -t                          # should find the R820T tuner
```

RDS needs redsea (not in Debian), built from source:

```sh
sudo apt install git meson g++ libliquid-dev libsndfile1-dev nlohmann-json3-dev
git clone https://github.com/windytan/redsea.git ~/redsea
cd ~/redsea && meson setup build && meson compile -C build && sudo meson install -C build
```

Without redsea the radio plays, just without station names.

The service:

```sh
sudo cp ~/ArduinoApps/spectrum/tools/fm-radio/fm-radio.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fm-radio
journalctl -u fm-radio -f            # its log
```

## Use

From the remote: Menu → **FM** (greyed out while the stick is unplugged).
The first time, pick **Scan for stations** (~1 min: a sweep of the band,
then a few seconds on every peak for its RDS name). The list is saved in
`~/.config/fm-radio/stations.txt` (`107.5 | Radio ZET` per line): rename
or delete stations there; a new scan keeps the names you gave.

On the Now playing screen: the station's RDS name, and the song on air
from RadioText+ or the radiotext ("GRAMY: Artist - Title"; "next" and
slogans are skipped).

By hand:

```sh
curl "localhost:8001/tune?f=107.5"   # MHz
mpc clear; mpc add http://localhost:8001/fm; mpc play
curl localhost:8001/status           # frequency, RDS, scan progress
curl localhost:8001/stations
curl localhost:8001/scan             # stops the sound for the scan
```

## Notes

- `rtl_fm` takes ~60-70 % of one core.
- Don't let `rtl_fm` resample to 48 kHz itself (`-r`) from a rate that
  isn't a whole multiple: its resampler whistles. sox does it here.
- Walkie-talkies (narrow FM, e.g. PMR446 channel 1) by hand, with the
  service stopped (`sudo systemctl stop fm-radio`: one program at a time
  can use the stick). Here `-r 48k` gave pure silence, so sox resamples:
  `rtl_fm -M fm -f 446.00625M -s 16k -l 50 -E pad -g 40 - | sox -t raw -r 16k
  -e signed -b 16 -c 1 - -t raw -r 48k - vol 3 | aplay -D plughw:CARD=QCC5125
  -r 48000 -f S16_LE -t raw -c 1` (`-l` squelch, `-E pad` zeros while closed).
- While MPD is paused it keeps the connection, so the tuner keeps running.
