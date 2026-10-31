# Hardware notes

Things found out the hard way on this setup, so they don't get lost.

## The board (Arduino UNO Q)

- `sudo poweroff` stops Linux, but the board restarts by itself about a
  minute later. Shut down, wait ~20 s (SSH drops, LEDs off), then pull the
  power within the minute.
- Pulling the power right after writing files can leave them empty (it
  happened to `.git/objects` and `sketch.ino` after a `git pull`). Run
  `sync` after updates.
- After a reboot the network can come up late: App Lab then fails to find
  libraries (`DebugLog@latest not found`). Run again.
- Restart the app with **Run**, not `docker restart`: the Bridge keeps
  routing the MCU's messages to the old Python connection.

## The kernel (7.0, Arduino's build)

- Not built in: `sr_mod` / `cdrom` (CD drives), ISO9660 and UDF (data CDs,
  DVDs), NTFS3. Built in: USB storage, SCSI disks, ext4, vfat, exfat.
- The CD drivers are built out of tree by `tools/cdrom-modules/build.sh`.
  Reading the files of a data CD without ISO9660: `7z l /dev/sr0`.

## Display module (2.4" ST7789 + EC11 + K0, "2.4 TFT + EC11" V1.2)

- Wiring and wire colours: README.
- **No TE (tearing effect) pin** on the header; the panel's flex is soldered
  under the panel to the module's front side, nothing to reach from the
  back. So writes can't be timed to the panel's refresh and big or moving
  areas can tear. A bare panel with TE on its flex (e.g. Raystar 2.4"
  ST7789, TE on pin 13 of 20) would fix it.
- The panel refreshes column by column, **right to left** in our rotation
  (found by trying both strip orders in the menu): `STRIPS_RTL`.
- Refresh rate: only the default 60 Hz works. FRCTRL2 = 0x00 (119 Hz) left
  the screen black with two stripes.
- SPI: Zephyr's driver runs without DMA, an interrupt per word: 300–500 KB/s
  whatever the clock. Writing SPI2's registers directly: 1.1 MB/s at the
  same 10 MHz, 3.3 MB/s at 40 MHz; 40 MHz is clean over ~20 cm jumper wires
  (`tools/spi-bench`). The library asks for 16 MHz and gets 10 (divider 16).

## DAC (ZK-QCC: QCC5125 + PCM5102A, USB + Bluetooth)

- Over USB it is playback only (no recording), S24_3LE 8–96 kHz, USB full
  speed; FLAC 24/48 reaches it bit-perfect.
- It has an output relay. With **no phone connected over Bluetooth**, the
  relay clicks off when the USB stream stops (MPD pause/stop) and on again
  when it starts. With a phone connected (blue LED on steadily) the output
  stays on and pauses are silent. Possible fix: `always_on "yes"` in MPD's
  ALSA output (keeps sending silence) – not tried yet: check that Bluetooth
  can still take over the sound with it.
- It shows up in `lsusb` as `0a12:4007` / `0a12:4010` "Mpow HC5 Headset"
  (a CSR chip with a hub inside).

## USB hub and power

- Everything hangs off one powered hub (UGREEN 160 W charger).
- **Plugging the CD drive in drops the other devices for a moment** (inrush
  current): the USB stick disconnects and comes back as `sdb`, the DAC
  re-enumerates; unplugging is mostly fine. Keep the drive connected, or
  only replug it with nothing playing. A stick dropped while mounted needs
  `sudo umount /mnt/rips` + `sudo fsck.ext4 -f /dev/sdX1`.
- Amplifier: AIYIMA TPA3116 2.1 (DC 12–24 V, 6800 µF 35 V), on 12 V now.
  An ASUS ADP-90YD B (19 V 4.74 A, centre +, likely 5.5×2.5 mm) would give
  it more headroom.

## Storage

- USB stick for CD rips: GPT partition named `rips`, ext4 label `RIPS`,
  mounted at `/mnt/rips` by fstab with `x-systemd.automount` and a 60 s idle
  timeout. Python finds it by `PARTNAME=rips` in `/sys/class/block/*/uevent`.
- MPD's `music_directory` is `/srv/music` with `nas -> /mnt/music` (NFS from
  the QNAP) and `rips -> /mnt/rips`.
- MPD 0.24 hides audio files that a `.cue` next to them refers to (whipper's
  rips): the code finds their codec with `listfiles`.
- Transcend StoreJet 1 TB (Samsung Spinpoint M8, 2012): the QNAP backup,
  healthy SMART, keep it off the board.

## CD drives

- HL-DT-ST GSA-T50N (the replacement from Asato): reads fine, read offset
  **+667** confirmed by AccurateRip (in `~/.config/whipper/whipper.conf`).
- HL-DT-ST GT70N (from the ASUS K75VJ laptop): works too.
- MPD plays `cdda:///N` but can't list a disc; `lsinfo cdda:///N` answers
  "No such file" for a track that exists and "Error … on track N" past the
  last one. A bare `cdda://` in the queue made the drive eject the disc.
- Spinning up takes 10–20 s after inserting a disc; switching tracks
  1–3 s.
