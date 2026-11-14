"""Offline and loopback-only tests; never contact a HomePod or play audio."""

import contextlib
import io
import json
from pathlib import Path
import plistlib
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import homepod_mic_probe as mic


def response(body=b"", status="200 OK", extra=b""):
    return (f"RTSP/1.0 {status}\r\nCSeq: 1\r\nContent-Length: {len(body)}\r\n".encode()
            + extra + b"\r\n" + body)


class FragmentedSocket:
    def __init__(self, chunks):
        self.chunks = iter(chunks)

    def recv(self, size):
        return next(self.chunks, b"")


class ResponseTests(unittest.TestCase):
    def test_fragmented_binary_plist(self):
        body = plistlib.dumps({"model": "test"}, fmt=plistlib.FMT_BINARY)
        wire = response(body)
        parsed = mic.read_response(FragmentedSocket([wire[:13], wire[13:70], wire[70:]]))
        self.assertEqual(mic.parse_info(parsed["body"]), {"model": "test"})

    def test_truncated_headers(self):
        with self.assertRaises(mic.ProbeError):
            mic.read_response(FragmentedSocket([b"RTSP/1.0 200 OK\r\n"]))

    def test_truncated_body(self):
        with self.assertRaises(mic.ProbeError):
            mic.read_response(FragmentedSocket([response(b"body")[:-1]]))

    def test_invalid_lengths(self):
        for length in ("-1", "no", str(mic.MAX_BODY + 1)):
            with self.subTest(length=length), self.assertRaises(mic.ProbeError):
                wire = f"RTSP/1.0 200 OK\r\nContent-Length: {length}\r\n\r\n".encode()
                mic.read_response(FragmentedSocket([wire]))

    def test_duplicate_length(self):
        with self.assertRaises(mic.ProbeError):
            mic.read_response(FragmentedSocket([response(extra=b"content-length: 0\r\n")]))

    def test_chunked_response_rejected(self):
        with self.assertRaises(mic.ProbeError):
            mic.read_response(FragmentedSocket([response(extra=b"Transfer-Encoding: chunked\r\n")]))

    def test_unexpected_cseq(self):
        with self.assertRaises(mic.ProbeError):
            mic.read_response(FragmentedSocket([response().replace(b"CSeq: 1", b"CSeq: 8")]))

    def test_oversized_headers(self):
        for ending in (b"", b"\r\n\r\n"):
            with self.subTest(ending=ending), self.assertRaises(mic.ProbeError):
                mic.read_response(FragmentedSocket([b"X" * (mic.MAX_HEADER + 1) + ending]))

    def test_invalid_plist(self):
        for body in (b"not a plist", b'<?xml version="1.0"?><plist><dict>',
                     plistlib.dumps(["not a dictionary"])):
            with self.subTest(body=body), self.assertRaises(mic.ProbeError):
                mic.parse_info(body)


class AssessmentTests(unittest.TestCase):
    def test_split_feature_words_are_low_then_high(self):
        self.assertEqual(mic.feature_bits("0x200,0x200"), [9, 41])

    def test_integer_and_hex_features(self):
        for value in (512, "512", "0x200"):
            self.assertEqual(mic.feature_bits(value), [9])

    def test_invalid_features(self):
        for value in (-1, True, 2**64, "0x100000000,0", "wrong", "1,2,3", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                mic.feature_bits(value)

    def test_input_hints_do_not_prove_capture(self):
        info = {"audioInputFormats": [1], "nested": [{"microphone": True}]}
        report = mic.make_report(info)
        self.assertEqual(report["input_hint_paths"], ["audioInputFormats", "nested[0].microphone"])
        self.assertEqual(report["assessment"]["homepod_microphone_capture"], "unverified")
        self.assertFalse(report["assessment"]["this_tool_records_audio"])

    def test_missing_input_hints_do_not_prove_no_microphone(self):
        report = mic.make_report({"features": 512})
        self.assertEqual(report["input_hint_paths"], [])
        self.assertEqual(report["assessment"]["homepod_microphone_capture"], "unverified")

    def test_report_omits_private_values(self):
        report = mic.make_report({"model": "test", "name": "PRIVATE_NAME", "deviceID": "PRIVATE_ID",
                                  "pk": b"PRIVATE_KEY", "microphone": "PRIVATE_VALUE"})
        encoded = json.dumps(report)
        self.assertNotIn("PRIVATE_", encoded)
        self.assertEqual(report["device"], {"model": "test"})

    def test_unknown_feature_encoding_is_reported(self):
        report = mic.make_report({"features": b"unknown"})
        self.assertIsNotNone(report["features"]["parse_error"])


class ProbeTests(unittest.TestCase):
    def test_mutating_requests_rejected_before_connect(self):
        with patch.object(mic.socket, "create_connection") as connect:
            for method in ("SETUP", "RECORD", "POST", "SET_PARAMETER"):
                with self.subTest(method=method), self.assertRaises(mic.ProbeError):
                    mic.request("127.0.0.1", 7000, 1, method, "/info")
            connect.assert_not_called()

    def test_auth_failure_is_inconclusive(self):
        replies = [{"status": 401, "headers": {}, "body": b""},
                   {"status": 200, "headers": {"public": "SETUP, RECORD"}, "body": b""}]
        with patch.object(mic, "request", side_effect=replies):
            report = mic.probe("127.0.0.1")
        self.assertEqual(report["device_info_access"], "authentication_required")
        self.assertEqual(report["assessment"]["homepod_microphone_capture"], "unverified")

    def test_connection_failures_produce_report(self):
        with patch.object(mic, "request", side_effect=TimeoutError("private address")):
            report = mic.probe("127.0.0.1")
        self.assertEqual(report["device_info_access"], "unavailable")
        self.assertNotIn("private address", report["error"])

    def test_loopback_receiver_observes_only_read_only_requests(self):
        received = []
        failures = []
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(2)
        listener.settimeout(3)
        port = listener.getsockname()[1]

        def serve():
            try:
                for index in range(2):
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(3)
                        wire = b""
                        while b"\r\n\r\n" not in wire:
                            block = connection.recv(4096)
                            if not block:
                                raise AssertionError("Incomplete request")
                            wire += block
                        received.append(wire.split(b"\r\n", 1)[0])
                        body = plistlib.dumps({"model": "TEST_ONLY"}, fmt=plistlib.FMT_BINARY) if index == 0 else b""
                        connection.sendall(response(body, extra=b"Public: SETUP, RECORD\r\n"))
            except Exception as exc:
                failures.append(exc)
            finally:
                listener.close()

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        try:
            report = mic.probe("127.0.0.1", port, 2)
        finally:
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(received, [b"GET /info RTSP/1.0", b"OPTIONS * RTSP/1.0"])
        self.assertEqual(report["device"]["model"], "TEST_ONLY")
        self.assertEqual(report["assessment"]["homepod_microphone_capture"], "unverified")


class CliTests(unittest.TestCase):
    def test_offline_input_and_output_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            source = path / "info.plist"
            output = path / "report.json"
            source.write_bytes(plistlib.dumps({"model": "TEST_ONLY", "features": 512}))
            with patch.object(mic.socket, "create_connection") as connect:
                self.assertEqual(mic.main(["--info-file", str(source), "--output", str(output)]), 0)
                connect.assert_not_called()
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["features"]["set_bits"], [9])

    def test_existing_output_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "info.plist"
            original = plistlib.dumps({"model": "test"})
            path.write_bytes(original)
            with contextlib.redirect_stderr(io.StringIO()):
                result = mic.main(["--info-file", str(path), "--output", str(path)])
            self.assertEqual(result, 2)
            self.assertEqual(path.read_bytes(), original)

    def test_invalid_parameters_do_not_connect(self):
        for args in (["--host", "host\r\nBad: x"], ["--host", "127.0.0.1", "--port", "0"],
                     ["--host", "127.0.0.1", "--timeout", "nan"],
                     ["--host", "127.0.0.1", "--timeout", "inf"]):
            with self.subTest(args=args), patch.object(mic.socket, "create_connection") as connect:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    mic.main(args)
                connect.assert_not_called()

    def test_authentication_report_exits_nonzero(self):
        with patch.object(mic, "probe", return_value=mic.make_report(access="authentication_required")):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                result = mic.main(["--host", "127.0.0.1"])
        self.assertEqual(result, 2)
        self.assertEqual(json.loads(output.getvalue())["device_info_access"], "authentication_required")


if __name__ == "__main__":
    unittest.main()
