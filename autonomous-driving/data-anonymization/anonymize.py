"""Minimal image anonymization PoC: face (YuNet) + license plate (open-image-models YOLOv9) -> blur.
Usage: python anonymize.py <in_img>... ; outputs *_anon.jpg and timing.
"""
import sys, time
import cv2
import numpy as np
from open_image_models import LicensePlateDetector

FACE_MODEL = "yunet.onnx"


def blur(img, x1, y1, x2, y2, pad=0.15):
    h, w = img.shape[:2]
    dx, dy = int((x2 - x1) * pad), int((y2 - y1) * pad)
    x1, y1 = max(0, x1 - dx), max(0, y1 - dy)
    x2, y2 = min(w, x2 + dx), min(h, y2 + dy)
    if x2 <= x1 or y2 <= y1:
        return
    roi = img[y1:y2, x1:x2]
    k = max(15, ((x2 - x1) // 3) | 1)
    img[y1:y2, x1:x2] = cv2.GaussianBlur(roi, (k, k), 0)


def main(paths):
    face = cv2.FaceDetectorYN.create(FACE_MODEL, "", (320, 320), score_threshold=0.6)
    plate = LicensePlateDetector(detection_model="yolo-v9-t-384-license-plate-end2end")
    for p in paths:
        img = cv2.imread(p)
        if img is None:
            print(f"skip {p}: cannot read")
            continue
        h, w = img.shape[:2]
        t0 = time.perf_counter()
        face.setInputSize((w, h))
        _, faces = face.detect(img)
        faces = [] if faces is None else faces
        t1 = time.perf_counter()
        plates = plate.predict(img)
        t2 = time.perf_counter()
        for f in faces:
            x, y, fw, fh = map(int, f[:4])
            blur(img, x, y, x + fw, y + fh)
        for d in plates:
            b = d.bounding_box
            blur(img, b.x1, b.y1, b.x2, b.y2, pad=0.05)
        t3 = time.perf_counter()
        out = p.rsplit(".", 1)[0] + "_anon.jpg"
        cv2.imwrite(out, img)
        print(f"{p} {w}x{h}: faces={len(faces)} plates={len(plates)} "
              f"face={1000*(t1-t0):.0f}ms plate={1000*(t2-t1):.0f}ms blur={1000*(t3-t2):.0f}ms -> {out}")


if __name__ == "__main__":
    main(sys.argv[1:])
