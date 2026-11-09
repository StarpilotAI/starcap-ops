#!/usr/bin/env python3
"""Draw icon.png: an isometric cube with an ArUco marker on each face, under a magnifier."""
import os
import cv2
import numpy as np

S = 512                                     # drawn at 2x, downsampled for smooth edges
img = np.zeros((S, S, 4), np.uint8)
d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)

c = (232, 262)                              # cube centre (front vertex)
r = 170
ang = lambda a: (c[0] + r * np.cos(np.radians(a)), c[1] - r * np.sin(np.radians(a)))
top, ul, ur, bl, br, bot = ang(90), ang(150), ang(30), ang(210), ang(330), ang(270)
faces = [  # (quad in marker corner order TL,TR,BR,BL, marker id, shade)
    ([top, ur, c, ul], 7, 1.00),           # top
    ([ul, c, bot, bl], 3, 0.82),           # left
    ([c, ur, br, bot], 12, 0.66),          # right
]
for quad, mid, shade in faces:
    m = cv2.aruco.generateImageMarker(d, mid, 240, borderBits=1)
    m = cv2.copyMakeBorder(m, 40, 40, 40, 40, cv2.BORDER_CONSTANT, value=255)
    n = m.shape[0]
    H = cv2.getPerspectiveTransform(np.float32([[0, 0], [n, 0], [n, n], [0, n]]),
                                    np.float32(quad))
    face = cv2.warpPerspective(m, H, (S, S), flags=cv2.INTER_AREA)
    mask = cv2.warpPerspective(np.full((n, n), 255, np.uint8), H, (S, S))
    rgb = (face.astype(np.float32) * shade).astype(np.uint8)
    for ch in range(3):
        img[..., ch] = np.where(mask > 0, rgb, img[..., ch])
    img[..., 3] = np.maximum(img[..., 3], mask)
for a, b in [(top, ur), (ur, br), (br, bot), (bot, bl), (bl, ul), (ul, top),
             (c, ul), (c, ur), (c, bot)]:
    cv2.line(img, tuple(map(int, a)), tuple(map(int, b)), (40, 40, 40, 255), 7, cv2.LINE_AA)

# magnifier, bottom right, with one red "bad cell" in the lens
g = (372, 372)
cv2.line(img, (g[0] + 62, g[1] + 62), (g[0] + 118, g[1] + 118), (50, 50, 50, 255), 30, cv2.LINE_AA)
cv2.circle(img, g, 86, (245, 245, 245, 255), -1, cv2.LINE_AA)
cv2.rectangle(img, (g[0] - 48, g[1] - 48), (g[0] + 48, g[1] + 48), (20, 20, 20, 255), -1)
cv2.rectangle(img, (g[0] - 22, g[1] - 22), (g[0] + 22, g[1] + 22), (40, 40, 235, 255), -1)
cv2.circle(img, g, 86, (40, 150, 255, 255), 14, cv2.LINE_AA)

out = cv2.resize(img, (256, 256), interpolation=cv2.INTER_AREA)
cv2.imwrite(os.path.join(os.path.dirname(os.path.realpath(__file__)), "icon.png"), out)
