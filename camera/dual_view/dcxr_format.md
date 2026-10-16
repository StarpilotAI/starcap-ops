# .dcxr format (from Youssef Mahran, Slack 2026-10-08; source of truth: In-the-wild-gripper, branch HIFI-ORBSLAM,
# scripts/record/decxin.py + scripts/imu_strip.py)

Container: b"DCXR0001", then per frame: u64 LE host_ns (CLOCK_MONOTONIC) + u32 LE jpeg_len + the camera's own MJPEG,
never re-encoded. A truncated last record is dropped by the reader. Sidecar record_stats.json: frame count, capture
rate, overflow (frames the recorder dropped).

168 frame, 4000x1200:  cols 0-159 IMU strip | 160-2079 RIGHT lens | 2080-3999 LEFT lens (cam0, reference)
The halves are SWAPPED and the seam is at 2080, not 2000. 175 deg KB4 fisheyes, ~60.2 mm baseline.

IMU strip: see ../camfw/lib/imu_strip.py. ICM-42688-P, 8192 LSB/g, 32.8 LSB/(deg/s), ~600 Hz at 60 fps.
Measured on 168 (2026-10-07): IMU stamps ~5.5 ms LATE vs images -> decxin.py prep --imu-time-offset-ms -5.5.
SLAM takes: MANUAL exposure (5 ms); auto exposure lets camera and IMU drift apart.

decxin.py prep --raw <take> --out <asl> --scale 0.5 --img-format pgm --gray --no-rectify --imu-time-offset-ms -5.5
  -> EuRoC/ASL: mav0/cam0 (left), mav0/cam1 (right), mav0/imu0/data.csv (t_ns, gyro rad/s, accel m/s^2)
