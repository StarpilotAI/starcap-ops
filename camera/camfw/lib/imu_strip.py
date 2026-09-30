"""DECXIN/Nori stereo camera — embedded IMU strip decoder (numpy, 60 fps capable).

Format (reverse-engineered, confirmed by the vendor's Nori 3D Camera guide and
TimeStamp_Data_Decode demo code):

  The top-left of every frame carries a bit-strip of 8x8-pixel cells:
  cols 0-1 / 18-19 = white alignment bars, cols 2-17 = 16 data bits per row.
  12 groups x 16 bytes, one group = 8 rows (2 bytes/row). Bytes are encoded
  LSB-FIRST in pixel order; multi-byte fields are MSB-first.

    group 0      header: protocol/device table (8 B) +
                 frame exposure START u32 + exposure END u32  (microseconds)
    groups 1-11  IMU sample: timestamp u32 us + ax,ay,az,gx,gy,gz int16

  Scales (ICM-42688-P as configured by the camera firmware):
    accel AFS_4G       -> 8192 LSB / g
    gyro  GFS_1000DPS  -> 32.8 LSB / (deg/s)

  Sample rate scales with the video mode: ~300 Hz at 30 fps, ~600 Hz at 60 fps.
  IMU<->frame sync (vendor spec): < 30 us; the last sample in each frame lands
  at the frame's exposure-end timestamp.
"""
import ctypes
import os

import numpy as np

CELL = 8
THR = 90
NROWS = 96                      # 12 groups x 8 rows
SAMPLE_ROW0 = 8                 # group 1 starts at cell-row 8
ACCEL_LSB_PER_G = 8192.0
GYRO_LSB_PER_DPS = 32.8
G = 9.81

_REV8 = np.array([int(f"{i:08b}"[::-1], 2) for i in range(256)], dtype=np.uint16)
_WEIGHTS = (1 << np.arange(15, -1, -1)).astype(np.uint16)


def _load_native():
    """vision/libimuwords.so does the cell-mean bit extraction in C.

    Measured 0.36ms of every frame in numpy (~3% of a core at 60fps) plus the
    GIL for its duration; the C version is the same predicate with no
    allocation. Optional on purpose — a board where it was never built keeps
    using the numpy path below, which stays the reference implementation.
    """
    try:
        so = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "vision", "libimuwords.so")
        if not os.path.exists(so):
            return None
        lib = ctypes.CDLL(so)
        lib.imu_words.argtypes = [
            np.ctypeslib.ndpointer(dtype=np.uint8, flags="C_CONTIGUOUS"),
            ctypes.c_int,
            np.ctypeslib.ndpointer(dtype=np.uint16, flags="C_CONTIGUOUS")]
        lib.imu_words.restype = ctypes.c_int
        return lib
    except Exception as e:                       # never break capture over this
        print(f"[imu] native strip decode unavailable ({e}) — using numpy", flush=True)
        return None


_LIB = _load_native()


class StripDecoder:
    """Stateful decoder: feed NV12 luma planes, get (exposure_window, new_samples).
    Deduplicates across frames via the 32-bit sample timestamps."""

    @staticmethod
    def rows_needed():
        """Pixel rows the strip occupies — all decode() ever reads.

        Callers holding a full frame use this to hand over just the strip region
        instead of the whole thing; see app.py's _on_imu for why that is worth
        3 ms a frame.
        """
        return NROWS * CELL

    def __init__(self, frame_width):
        self.width = frame_width
        self._last_ts = None
        self.total_samples = 0
        self.bad_frames = 0
        self.exp_window = None       # (start_us, end_us)

    def _words(self, data, stride):
        """All 96 row-words from the strip region. C when available, else numpy —
        both produce identical words (see imu_words.c on why the integer-sum
        threshold is exactly numpy's float mean comparison)."""
        need = NROWS * CELL * stride
        if len(data) < need:
            return None
        if _LIB is not None:
            src = np.frombuffer(data, dtype=np.uint8, count=need)
            out = np.empty(NROWS, dtype=np.uint16)
            if _LIB.imu_words(src, stride, out) == 0:
                return out
            # fall through to numpy if C refused the geometry
        arr = np.frombuffer(data, dtype=np.uint8, count=need).reshape(NROWS * CELL, stride)
        # data cells: cols 2..17 -> px 16..143; inner 6x6 of each 8x8 cell
        region = arr[:, 16:144].reshape(NROWS, CELL, 16, CELL)
        cellmean = region[:, 1:7, :, 1:7].mean(axis=(1, 3))
        bits = (cellmean > THR).astype(np.uint16)            # (96, 16) MSB..LSB col order
        raw = (bits * _WEIGHTS).sum(axis=1).astype(np.uint16)
        return ((_REV8[(raw >> 8) & 0xFF] << 8) | _REV8[raw & 0xFF]).astype(np.uint16)

    def decode(self, data, stride):
        """-> (exposure(start_us,end_us) | None, [(ts_us, accel m/s^2, gyro dps)]) or
        None if the frame's strip is unreadable."""
        w = self._words(data, stride)
        if w is None:
            self.bad_frames += 1
            return None
        w = w.astype(np.int64)

        # header (group 0): rows 0-7. row0 high-nibble must be protocol 1
        exposure = None
        if (int(w[0]) >> 12) == 1:
            exposure = (int((w[4] << 16) | w[5]), int((w[6] << 16) | w[7]))
            self.exp_window = exposure

        # sample groups: 11 blocks of 8 rows from row 8
        blocks = w[SAMPLE_ROW0:SAMPLE_ROW0 + 11 * 8].reshape(11, 8)
        ts = (blocks[:, 0] << 16) | blocks[:, 1]
        ts_hi = blocks[:, 0]
        med = int(np.median(ts_hi))
        ok = np.abs(((ts_hi - med + 32768) % 65536) - 32768) <= 16   # bit-error filter
        if ok.sum() < 5:
            self.bad_frames += 1
            return None

        sens = blocks[:, 2:8].astype(np.int64)
        sens = np.where(sens >= 32768, sens - 65536, sens)           # int16
        out = []
        for i in np.nonzero(ok)[0]:
            t = int(ts[i])
            if self._last_ts is not None:
                d = (t - self._last_ts) & 0xFFFFFFFF
                if d == 0 or d > 0x80000000:
                    continue                                          # overlap/dup
            self._last_ts = t
            a = (sens[i, 0:3] / ACCEL_LSB_PER_G * G).tolist()
            g = (sens[i, 3:6] / GYRO_LSB_PER_DPS).tolist()
            out.append((t, a, g))
        self.total_samples += len(out)
        return exposure, out

    def live_status(self):
        return {"total_samples": self.total_samples, "bad_frames": self.bad_frames,
                "exposure_us": (self.exp_window[1] - self.exp_window[0]) & 0xFFFFFFFF
                               if self.exp_window else None}
