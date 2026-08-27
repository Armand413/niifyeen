import cv2

for i in range(0, 6):
    cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
    ok, _ = cap.read()
    cap.release()
    print(i, "OK" if ok else "NO")