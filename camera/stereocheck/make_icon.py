#!/usr/bin/python3 -s
"""Draw icon.png: two eyes, both on the same red row, with a green tick."""
import os
import cv2
import numpy as np

S = 256
img = np.zeros((S, S, 4), np.uint8)
cv2.rectangle(img, (14, 58), (242, 198), (60, 50, 40, 255), -1)             # camera body
for cx in (78, 178):                                                        # the two lenses
    cv2.circle(img, (cx, 128), 44, (30, 30, 30, 255), -1)
    cv2.circle(img, (cx, 128), 44, (200, 200, 200, 255), 4)
    cv2.circle(img, (cx, 128), 12, (0, 220, 255, 255), 4)                   # yellow ring, same row
cv2.line(img, (10, 128), (246, 128), (40, 40, 230, 255), 5)                 # red row line
cv2.circle(img, (205, 205), 42, (60, 170, 60, 255), -1)                     # green tick badge
cv2.polylines(img, [np.array([(186, 206), (200, 220), (226, 190)])], False, (255, 255, 255, 255), 9, cv2.LINE_AA)
cv2.imwrite(os.path.join(os.path.dirname(os.path.realpath(__file__)), "icon.png"), img)
