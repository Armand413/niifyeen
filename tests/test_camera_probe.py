import base64
import socket
import unittest
from unittest import mock

import cv2
import numpy as np

import tests  # noqa: F401
from backend.app import camera_probe as probe
from tests.test_camera_server import Served


def discovery_reply(ip="192.168.1.64", types="dn:NetworkVideoTransmitter tds:Device",
                    scopes="onvif://www.onvif.org/name/Cam%20Hall onvif://www.onvif.org/hardware/DS-2CD2",
                    xaddrs=None):
    xaddrs = xaddrs if xaddrs is not None else f"http://{ip}/onvif/device_service http://[fe80::1]/onvif/device_service"
    return (f'<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
            f'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"><s:Body><d:ProbeMatches><d:ProbeMatch>'
            f'<d:Types>{types}</d:Types><d:Scopes>{scopes}</d:Scopes><d:XAddrs>{xaddrs}</d:XAddrs>'
            f'</d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>').encode()


class ParseDiscoveryTests(unittest.TestCase):
    def test_a_camera_reply_is_understood(self):
        d = probe.parse_discovery_response(discovery_reply())
        self.assertEqual((d["address"], d["name"], d["hardware"]), ("192.168.1.64", "Cam Hall", "DS-2CD2"))
        self.assertEqual(d["suggested_source"], "rtsp://192.168.1.64:554/")
        self.assertEqual(d["xaddrs"][0], "http://192.168.1.64/onvif/device_service")

    def test_other_devices_are_ignored(self):
        self.assertIsNone(probe.parse_discovery_response(discovery_reply(types="wsdp:Device", scopes="printer")))

    def test_replies_without_an_address_are_ignored(self):
        self.assertIsNone(probe.parse_discovery_response(discovery_reply(xaddrs="")))
        self.assertIsNone(probe.parse_discovery_response(discovery_reply(xaddrs="ftp://x/y javascript:1")))

    def test_hostile_xml_is_refused_before_parsing(self):
        bomb = (b'<?xml version="1.0"?><!DOCTYPE lol [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
                b'<r><XAddrs>&b;</XAddrs></r>')
        self.assertIsNone(probe.parse_discovery_response(bomb))
        self.assertIsNone(probe.parse_discovery_response(b'<!doctype x><a/>'))
        self.assertIsNone(probe.parse_discovery_response(b"\x00\x01 pas du xml"))
        self.assertIsNone(probe.parse_discovery_response(b"x" * 70000))

    def test_the_probe_message_asks_for_video_transmitters(self):
        msg = probe.build_probe().decode()
        self.assertIn("NetworkVideoTransmitter", msg)
        self.assertIn("discovery/Probe", msg)
        self.assertNotEqual(probe.build_probe(), probe.build_probe())          # identifiant de message unique


class FakeSocket:
    def __init__(self, replies):
        self.replies, self.sent, self.closed, self.options = list(replies), [], False, []

    def setsockopt(self, *a): self.options.append(a)
    def settimeout(self, t): pass
    def sendto(self, data, addr): self.sent.append((data, addr))
    def close(self): self.closed = True

    def recvfrom(self, n):
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply, ("192.168.1.64", 3702)
        raise socket.timeout()


class DiscoverTests(unittest.TestCase):
    def run_discover(self, replies):
        fake = FakeSocket(replies)
        found = probe.discover_onvif(timeout_sec=0.3, sock_factory=lambda *a: fake)
        return fake, found

    def test_devices_are_found_and_deduplicated(self):
        fake, found = self.run_discover([discovery_reply("192.168.1.64"), discovery_reply("192.168.1.64"),
                                         discovery_reply("192.168.1.70"), b"bruit", discovery_reply(types="x", scopes="y")])
        self.assertEqual(sorted(d["address"] for d in found), ["192.168.1.64", "192.168.1.70"])
        self.assertEqual(fake.sent[0][1], probe.WS_DISCOVERY_ADDR)
        self.assertTrue(fake.closed)

    def test_nothing_on_the_network(self):
        fake, found = self.run_discover([])
        self.assertEqual(found, [])
        self.assertTrue(fake.closed)

    def test_a_network_error_returns_what_was_found_and_closes_the_socket(self):
        fake, found = self.run_discover([discovery_reply("192.168.1.64"), OSError("réseau coupé")])
        self.assertEqual([d["address"] for d in found], ["192.168.1.64"])
        self.assertTrue(fake.closed)

    def test_a_socket_that_cannot_send_does_not_raise(self):
        class Broken(FakeSocket):
            def sendto(self, data, addr): raise OSError("pas de réseau")
        fake = Broken([])
        self.assertEqual(probe.discover_onvif(timeout_sec=0.2, sock_factory=lambda *a: fake), [])
        self.assertTrue(fake.closed)


class ProbeSourceTests(unittest.TestCase):
    def serve(self, token=""):
        s = Served(token=token)
        self.addCleanup(s.close)
        return s

    def test_a_working_stream_is_measured_and_previewed(self):
        s = self.serve()
        r = probe.probe_source(f"http://127.0.0.1:{s.port}/video", "http", timeout_sec=5, sample_sec=0.5)
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["width"], r["height"], r["kind"]), (320, 240, "http"))
        self.assertGreater(r["fps"], 3)
        self.assertGreaterEqual(r["first_frame_ms"], 0)
        header, data = r["preview"].split(",", 1)
        self.assertEqual(header, "data:image/jpeg;base64")
        img = cv2.imdecode(np.frombuffer(base64.b64decode(data), dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(img.shape[:2], (240, 320))

    def test_wrong_token(self):
        s = self.serve(token="bon")
        r = probe.probe_source(f"http://127.0.0.1:{s.port}/video?token=mauvais", "http", timeout_sec=3)
        self.assertFalse(r["ok"])
        self.assertIn("jeton", r["error"])

    def test_nothing_listening(self):
        sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
        r = probe.probe_source(f"http://127.0.0.1:{port}/video", "http", timeout_sec=3)
        self.assertEqual(r["ok"], False)
        self.assertIn("pare-feu", r["error"])

    def test_error_messages_depend_on_the_kind_of_source(self):
        with mock.patch.object(probe, "open_capture", return_value=None):
            self.assertIn("Webcam", probe.probe_source(0, "webcam")["error"])
            self.assertIn("RTSP" if False else "mot de passe", probe.probe_source("rtsp://h/s", "rtsp")["error"])
            self.assertIn("pare-feu", probe.probe_source("http://h/s", "http")["error"])

    def test_a_connection_without_images(self):
        cap = mock.MagicMock()
        cap.read.return_value = (False, None)
        with mock.patch.object(probe, "open_capture", return_value=cap):
            r = probe.probe_source("http://h/s", "http")
        self.assertFalse(r["ok"])
        self.assertIn("aucune image", r["error"])
        cap.release.assert_called()

    def test_the_capture_is_always_released_even_on_errors(self):
        cap = mock.MagicMock()
        cap.read.side_effect = RuntimeError("boum")
        with mock.patch.object(probe, "open_capture", return_value=cap):
            r = probe.probe_source("http://h/s", "http")
        self.assertFalse(r["ok"])
        cap.release.assert_called()

    def test_preview_is_reduced(self):
        url = probe.preview_data_url(np.zeros((1080, 1920, 3), dtype=np.uint8))
        img = cv2.imdecode(np.frombuffer(base64.b64decode(url.split(",", 1)[1]), dtype=np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(img.shape[1], probe.PREVIEW_MAX_WIDTH)

    def test_open_capture_falls_back_when_opencv_has_no_timeout_parameters(self):
        calls = []
        class Cap:
            def __init__(self, *a):
                calls.append(a)
                if len(a) == 3:
                    raise TypeError("pas de paramètres")
            def isOpened(self): return True
            def release(self): pass
        with mock.patch.object(probe.cv2, "VideoCapture", Cap):
            self.assertIsNotNone(probe.open_capture("http://h/s", 1))
        self.assertEqual([len(c) for c in calls], [3, 2])


class ListWebcamsTests(unittest.TestCase):
    def test_used_indexes_are_not_reopened(self):
        opened = []
        def fake_open(index, timeout_sec=0):
            opened.append(index)
            cap = mock.MagicMock()
            cap.read.return_value = (True, np.zeros((480, 640, 3), dtype=np.uint8)) if index in (0, 2) else (False, None)
            return cap if index in (0, 2, 3) else None
        with mock.patch.object(probe, "open_capture", fake_open):
            found = probe.list_webcams(skip=[0], max_index=5)
        self.assertEqual(found, [{"index": 2, "width": 640, "height": 480}])      # 3 : ouverte mais sans image
        self.assertNotIn(0, opened)


if __name__ == "__main__":
    unittest.main()
