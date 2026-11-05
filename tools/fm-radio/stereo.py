"""
FM stereo decoder: the MPX signal from rtl_fm in, left/right out.

What a station sends (after FM demodulation), at 171 kHz here:

    MPX = (L+R)  +  pilot sin(wt)  +  (L-R) sin(2wt)  +  RDS at 57 kHz
          0-15 kHz   19 kHz           23-53 kHz

The 38 kHz carrier itself isn't sent: it is rebuilt from the 19 kHz pilot.
A complex band-pass filter around 19 kHz turns the pilot into a rotating
phasor z = A e^(j theta); z squared turns at twice the speed, so its
imaginary part, divided by |z|^2, is the 38 kHz carrier in step with the
station's. Multiplying the MPX by it brings L-R down to 0-15 kHz.

    L = (MPX + 2 MPX carrier) / 2,  R = (MPX - 2 MPX carrier) / 2

(Sent here twice as loud, L = MPX + side: the level mono had, L = R = MPX.)

Everything above 15 kHz (pilot, leftovers at 38/57 kHz) is removed after
this by sox's low-pass, so it isn't filtered here: the sum is linear.

No pilot (a mono station, or too weak to trust): L = R = MPX.
"""

import numpy as np

PILOT_HZ = 19000
TAPS = 255                   # pilot filter: ~700 Hz wide at 171 kHz
PILOT_MIN = 0.02             # pilot amplitude (of full scale) below which: mono
STEREO_RAMP = 0.05           # per block: stereo fades in over ~0.5 s, mono at once


class StereoDecoder:
    def __init__(self, rate):
        n = np.arange(TAPS) - (TAPS - 1) / 2
        # Low-pass prototype (windowed sinc, +-350 Hz), shifted up to 19 kHz:
        # passes only +19 kHz, so the output is the pilot as a phasor.
        cutoff = 350 / rate
        lp = 2 * cutoff * np.sinc(2 * cutoff * n) * np.blackman(TAPS)
        lp /= lp.sum()
        self.h = (lp * np.exp(2j * np.pi * PILOT_HZ / rate * n)).astype(np.complex64)
        self.H = {}                  # the filter's spectrum, per FFT size
        self.hist = np.zeros(TAPS - 1, dtype=np.float32)   # the filter's memory
        self.blend = 0.0             # 0 mono .. 1 stereo, follows the pilot
        self.pilot = 0.0             # last pilot amplitude, for /status

    def process(self, mpx_bytes):
        """int16 MPX bytes -> interleaved int16 L R bytes (same rate)."""
        x = np.frombuffer(mpx_bytes, dtype="<i2").astype(np.float32) / 32768
        if len(x) == 0:
            return b""
        buf = np.concatenate((self.hist, x))
        self.hist = buf[-(TAPS - 1):]
        # The filter by FFT (overlap-save): np.convolve with 255 complex taps
        # took ~40 % of a core on the board, this a fraction of it.
        nfft = 1 << (len(buf) - 1).bit_length()
        if nfft not in self.H:
            self.H[nfft] = np.fft.fft(self.h, nfft).astype(np.complex64)
        z = np.fft.ifft(np.fft.fft(buf, nfft) * self.H[nfft])[TAPS - 1:len(buf)]   # pilot phasor
        mpx = buf[(TAPS - 1) // 2:(TAPS - 1) // 2 + len(x)]  # same delay as the filter
        power = np.abs(z) ** 2
        self.pilot = float(2 * np.sqrt(np.mean(power)))     # the pilot's amplitude
        if self.pilot > PILOT_MIN:
            self.blend = min(1.0, self.blend + STEREO_RAMP)
        else:
            self.blend = 0.0

        if self.blend < 0.01:
            left = right = mpx
        else:
            # sin(2 theta) from z^2. The pilot is sin(theta), whose phasor is
            # e^(j(theta - pi/2)) / 2: squared, e^(j(2 theta - pi)) / 4, so
            # the carrier is minus the imaginary part.
            carrier = -(z * z).imag / np.maximum(power, 1e-12)
            side = 2 * mpx * carrier * self.blend
            left, right = mpx + side, mpx - side
        out = np.empty(2 * len(x), dtype="<i2")
        out[0::2] = np.clip(left * 32768, -32768, 32767)
        out[1::2] = np.clip(right * 32768, -32768, 32767)
        return out.tobytes()
