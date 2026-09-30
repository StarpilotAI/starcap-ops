#!/usr/bin/env python3
"""
Hear مزمار عبد السلام on THIS laptop's speakers, without touching a recorder.

The buzzer is a passive piezo driven by lgpio software PWM at 50% duty — i.e. a
plain square wave. So a 50%-duty square rendered to a WAV is a faithful preview
of what the board will actually sound like, and you can tune the melody in
seconds instead of stopping `stereocam` on a real board for every attempt.

    ./preview_mozmar.py                  # play it
    ./preview_mozmar.py --tempo 240      # slower (base note in ms; default 200)
    ./preview_mozmar.py --notes          # print the frames, numbered, and exit
    ./preview_mozmar.py --wav out.wav    # just write the file

The frames themselves live in starcapfinder.py (MOZMAR) — this only renders
them, so the preview can never drift from what the boards play.
"""
import argparse
import array
import math
import os
import shutil
import subprocess
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RATE = 22050
AMP = 0.22            # a real piezo is louder and nastier; keep the preview civil


def render(frames, rate=RATE):
    """(freq_hz, ms) frames -> 16-bit mono PCM, 50% duty square, freq 0 = silence."""
    buf = array.array("h")
    for freq, ms in frames:
        n = int(rate * ms / 1000.0)
        if not freq:
            buf.extend([0] * n)
            continue
        period = rate / float(freq)
        lvl = int(AMP * 32767)
        # A 2 ms fade at each edge. The buzzer's own switching clicks are real,
        # but a hard DC step into laptop speakers is just unpleasant.
        fade = min(int(rate * 0.002), n // 2) or 1
        for i in range(n):
            s = lvl if (i % period) < (period / 2) else -lvl
            if i < fade:
                s = int(s * i / fade)
            elif i > n - fade:
                s = int(s * max(0, n - i) / fade)
            buf.append(s)
    return buf


def write_wav(path, pcm, rate=RATE):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def play(path):
    for cmd in (["paplay", path], ["aplay", "-q", path], ["pw-play", path],
                ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path]):
        if shutil.which(cmd[0]):
            return subprocess.run(cmd).returncode == 0
    print("no audio player found (tried paplay/aplay/pw-play/ffplay)", file=sys.stderr)
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tempo", type=int, help="base note length in ms (default 200)")
    ap.add_argument("--wav", help="write a WAV here instead of playing it")
    ap.add_argument("--notes", action="store_true", help="list the frames and exit")
    a = ap.parse_args()

    if a.tempo:                      # MOZMAR is built from _Q at import time
        os.environ["MOZMAR_TEMPO_MS"] = str(a.tempo)
    import starcapfinder as F

    if a.notes:
        names = {v: k for k, v in
                 (("D5", F.D5), ("Eb5", F.EB5), ("F#5", F.F5S), ("G5", F.G5),
                  ("A5", F.A5), ("Bb5", F.BB5), ("C6", F.C6), ("D6", F.D6))}
        for i, (f, ms) in enumerate(F.MOZMAR, 1):
            print(f"{i:3}. {names.get(f, 'rest' if not f else str(f)):>4}  "
                  f"{f or 0:>5} Hz  {ms:>4} ms")
        print(f"\n{len(F.MOZMAR)} frames, {F.song_ms()/1000:.2f} s total")
        return

    pcm = render(F.MOZMAR)
    print(f"مزمار عبد السلام — {len(F.MOZMAR)} frames, {F.song_ms()/1000:.2f} s "
          f"(base note {F._Q} ms)")
    if a.wav:
        write_wav(a.wav, pcm)
        print("wrote", a.wav)
        return
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as t:
        write_wav(t.name, pcm)
    try:
        play(t.name)
    finally:
        os.unlink(t.name)


if __name__ == "__main__":
    main()
