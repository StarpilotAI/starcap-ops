#!/usr/bin/env python3
"""Generate camrig's app icon: two stereo lenses over a slider, matching the UI."""
from PIL import Image, ImageDraw

S = 256
BG, RING, GLASS, ACCENT, TRACK = (22, 27, 34), (60, 70, 82), (12, 16, 21), (63, 182, 216), (42, 50, 61)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# rounded body
d.rounded_rectangle([8, 8, S - 8, S - 8], radius=52, fill=BG)

# two lenses side by side = the stereo pair
for cx in (88, 168):
    d.ellipse([cx - 44, 58, cx + 44, 146], fill=RING)
    d.ellipse([cx - 35, 67, cx + 35, 137], fill=GLASS)
    d.ellipse([cx - 24, 76, cx + 24, 124], outline=ACCENT, width=6)
    d.ellipse([cx - 13, 84, cx + 5, 102], fill=(232, 245, 250, 190))  # catchlight

# slider underneath = the control panel
y = 190
d.rounded_rectangle([40, y - 5, S - 40, y + 5], radius=5, fill=TRACK)
d.ellipse([150, y - 17, 184, y + 17], fill=ACCENT)

import os
img.save(os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png"))
print("wrote icon.png (256px)")
