import asyncio
import unittest
from types import SimpleNamespace

from backend.app.video_stream import mjpeg_frames


class VideoStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_image_is_not_repeated(self):
        cam = SimpleNamespace(running=True, latest_jpeg=b"first")
        stream = mjpeg_frames(cam, interval=.005)
        self.assertIn(b"first", await anext(stream))
        next_frame = asyncio.create_task(anext(stream))
        try:
            await asyncio.sleep(.03)
            self.assertFalse(next_frame.done())
            cam.latest_jpeg = b"second"
            self.assertIn(b"second", await asyncio.wait_for(next_frame, 1))
        finally:
            next_frame.cancel()
            await stream.aclose()

    async def test_idle_stream_can_be_cancelled(self):
        cam = SimpleNamespace(running=True, latest_jpeg=None)
        stream = mjpeg_frames(cam, interval=.005)
        task = asyncio.create_task(anext(stream))
        await asyncio.sleep(.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await stream.aclose()

    async def test_stopped_camera_ends_stream(self):
        cam = SimpleNamespace(running=False, latest_jpeg=b"old")
        with self.assertRaises(StopAsyncIteration):
            await anext(mjpeg_frames(cam))
