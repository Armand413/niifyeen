"""Bounded, cancellable MJPEG delivery independent of video analysis."""
import asyncio


async def mjpeg_frames(camera, interval=1 / 20):
    previous = None
    while camera.running:
        frame = camera.latest_jpeg
        if frame is not None and frame is not previous:
            previous = frame
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                   + frame + b"\r\n")
        await asyncio.sleep(interval)
