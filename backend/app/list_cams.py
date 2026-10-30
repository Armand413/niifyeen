"""Sonde les webcams locales (Windows / DirectShow).

Usage : python -m backend.app.list_cams
"""
import cv2


def main():
    for i in range(0, 6):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
        ok, _ = cap.read()
        cap.release()
        print(i, "OK" if ok else "NO")


if __name__ == "__main__":
    main()
