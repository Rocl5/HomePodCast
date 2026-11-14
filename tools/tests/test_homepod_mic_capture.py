"""Synthetic offline tests only. They do NOT verify a HomePod microphone."""

import asyncio
import contextlib
import io
import json
from pathlib import Path
import plistlib
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import homepod_mic_capture as capture


PROFILE = capture.PROFILES["main-telephony"]
HOST = "127.0.0.1"


def rtp(samples=(1, -2), seq=1, timestamp=100, ssrc=42, flags=0x80, payload_type=100):
    return struct.pack("!BBHII", flags, payload_type, seq, timestamp, ssrc) + struct.pack(f"!{len(samples)}h", *samples)


def confirmed_stream(**overrides):
    stream = {"type": PROFILE.stream_type, "input": True, "audioFormat": PROFILE.audio_format}
    stream.update(overrides)
    return {"streams": [stream]}


class NegotiationTests(unittest.TestCase):
    def assert_code(self, code, function, *args):
        with self.assertRaises(capture.CaptureError) as raised:
            function(*args)
        self.assertEqual(raised.exception.code, code)

    def test_200_without_stream_does_not_mean_microphone(self):
        self.assert_code("no_input_stream", capture.validate_input_reply, 200, {}, PROFILE)

    def test_real_homepod_playback_reply_is_not_input(self):
        profile = capture.PROFILES["realtime-telephony"]
        self.assert_code("input_unconfirmed", capture.validate_input_reply, 200,
                         {"streams": [{"type": 96, "dataPort": 12345, "controlPort": 12346}]}, profile)

    def test_missing_or_false_input_direction_rejected(self):
        for value in (False, None, 1, "true"):
            with self.subTest(value=value):
                self.assert_code("input_unconfirmed", capture.validate_input_reply, 200,
                                 confirmed_stream(input=value), PROFILE)

    def test_format_must_be_explicitly_confirmed(self):
        self.assert_code("format_unconfirmed", capture.validate_input_reply, 200,
                         {"streams": [{"type": 100, "input": True}]}, PROFILE)

    def test_wrong_direction_type_or_codec_rejected(self):
        for code, fields in (("wrong_stream_type", {"type": 96}),
                             ("unsupported_format", {"ct": 2}),
                             ("unsupported_format", {"sr": 48000}),
                             ("unsupported_format", {"ch": 2}),
                             ("unsupported_encryption", {"shk": b"not a plaintext stream"})):
            with self.subTest(fields=fields):
                self.assert_code(code, capture.validate_input_reply, 200, confirmed_stream(**fields), PROFILE)

    def test_non_200_rejected(self):
        self.assert_code("input_setup_rejected", capture.validate_input_reply, 461, confirmed_stream(), PROFILE)

    def test_confirmed_synthetic_stream_accepted(self):
        stream = capture.validate_input_reply(200, confirmed_stream(), PROFILE)
        self.assertIs(stream["input"], True)

    def test_observation_can_inspect_unconfirmed_direction_only(self):
        body = {"streams": [{"type": 100}]}
        self.assertEqual(capture.input_confirmation(200, body, PROFILE, observe_only=True), "input_unconfirmed")
        self.assert_code("input_unconfirmed", capture.input_confirmation, 200, body, PROFILE)

    def test_observation_cannot_bypass_rejection_or_wrong_stream(self):
        for status, body, code in ((400, {}, "input_setup_rejected"),
                                   (200, {}, "no_input_stream"),
                                   (200, {"streams": [{"type": 96}]}, "wrong_stream_type")):
            with self.subTest(status=status, body=body):
                self.assert_code(code, capture.input_confirmation, status, body, PROFILE, True)

    def test_binary_and_xml_plists(self):
        for format_ in (plistlib.FMT_BINARY, plistlib.FMT_XML):
            encoded = plistlib.dumps(confirmed_stream(), fmt=format_)
            self.assertEqual(capture.decode_reply(encoded), confirmed_stream())

    def test_empty_reply_is_inconclusive(self):
        self.assertEqual(capture.decode_reply(b""), {})

    def test_malformed_reply_rejected(self):
        for body in (b"not a plist", plistlib.dumps([1])):
            self.assert_code("invalid_reply", capture.decode_reply, body)

    def test_input_descriptor_has_no_output_request(self):
        request = capture.input_descriptor(PROFILE, 12345, 12346, 7)
        self.assertIs(request["input"], True)
        self.assertEqual(request["audioType"], "telephony")
        self.assertEqual(request["audioFormat"], 0x10)
        self.assertNotIn("output", request)

    def test_busy_unknown_and_wrong_device_rejected(self):
        for info, code in (({"model": "MacBookPro18,2", "statusFlags": 0}, "wrong_device"),
                           ({"model": "AudioAccessory6,1"}, "receiver_busy"),
                           ({"model": "AudioAccessory6,1", "statusFlags": True}, "receiver_busy"),
                           ({"model": "AudioAccessory6,1", "statusFlags": 1 << 17}, "receiver_busy"),
                           ({"model": "AudioAccessory6,1", "statusFlags": 1 << 9}, "pairing_required")):
            with self.subTest(info=info):
                self.assert_code(code, capture.validate_device, info)

    def test_idle_homepod_preflight(self):
        capture.validate_device({"model": "AudioAccessory6,1", "statusFlags": 0x80404})


class PacketTests(unittest.TestCase):
    def test_big_endian_network_pcm_becomes_little_endian_wav(self):
        seq, timestamp, ssrc, pcm = capture.pcm_from_rtp(rtp(), 100)
        self.assertEqual((seq, timestamp, ssrc), (1, 100, 42))
        self.assertEqual(pcm, struct.pack("<hh", 1, -2))

    def test_csrc_extension_and_padding(self):
        header = struct.pack("!BBHII", 0xB1, 100, 1, 100, 42)
        csrc = struct.pack("!I", 99)
        extension = struct.pack("!HHI", 0xBEDE, 1, 0)
        packet = header + csrc + extension + struct.pack("!hh", 1, -2) + b"\x00\x02"
        self.assertEqual(capture.pcm_from_rtp(packet, 100)[3], struct.pack("<hh", 1, -2))

    def test_malformed_packets_rejected(self):
        packets = (b"", b"x" * 8, rtp(flags=0x40), rtp(payload_type=83),
                   rtp(flags=0x8F), rtp()[:-1], rtp(flags=0x90)[:14],
                   rtp(flags=0xA0)[:-1] + b"\x00", rtp(flags=0xA0)[:-1] + b"\xFF")
        for packet in packets:
            with self.subTest(packet=packet), self.assertRaises(ValueError):
                capture.pcm_from_rtp(packet, 100)

    def test_payload_type_96_remains_supported_when_requested(self):
        packet = rtp(payload_type=96)
        self.assertEqual(capture.pcm_from_rtp(packet)[3], struct.pack("<hh", 1, -2))
        with self.assertRaisesRegex(ValueError, "Different RTP payload"):
            capture.pcm_from_rtp(packet, 100)

    def test_observation_parses_odd_unknown_codec_payload(self):
        packet = rtp(payload_type=111) + b'\x01'
        self.assertEqual(capture.parse_rtp(packet)[0], 111)
        with self.assertRaises(ValueError):
            capture.pcm_from_rtp(packet, 111)

    def test_extension_past_packet_end_is_rejected(self):
        packet = rtp(flags=0x90)[:12] + struct.pack('!HH', 0xBEDE, 100)
        with self.assertRaisesRegex(ValueError, 'Truncated RTP extension data'):
            capture.parse_rtp(packet)

    def test_rtcp_cannot_be_counted_as_rtp_audio(self):
        for kind in (192, 200, 212, 223):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                capture.parse_rtp(rtp(payload_type=kind))


class ObservationTests(unittest.TestCase):
    def test_observer_counts_96_100_and_other_payload_types(self):
        observer = capture.RtpObserver(HOST)
        observer.enabled = True
        for kind in (96, 100, 111, 100 | 0x80):
            observer.datagram_received(rtp(payload_type=kind), (HOST, 12345))
        self.assertEqual(observer.packets, 4)
        self.assertEqual(observer.datagrams, 4)
        self.assertEqual(observer.payload_types, {'96': 1, '100': 2, '111': 1})
        self.assertEqual(observer.frames, 0)
        self.assertTrue(observer.first_packet.is_set())

    def test_disabled_and_foreign_datagrams_do_not_count_as_target(self):
        observer = capture.RtpObserver(HOST)
        observer.datagram_received(rtp(), (HOST, 12345))
        observer.enabled = True
        observer.datagram_received(rtp(), ('127.0.0.2', 12345))
        self.assertEqual((observer.packets, observer.datagrams, observer.byte_count), (0, 0, 0))
        self.assertEqual(observer.ignored, 1)
        self.assertFalse(observer.first_packet.is_set())

    def test_malformed_target_traffic_is_visible_without_becoming_audio(self):
        observer = capture.RtpObserver(HOST)
        observer.enabled = True
        packets = [b'not audio', rtp(payload_type=200), rtp(flags=0x8F)]
        for packet in packets:
            observer.datagram_received(packet, (HOST, 12345))
        self.assertEqual(observer.datagrams, 3)
        self.assertEqual(observer.byte_count, sum(map(len, packets)))
        self.assertEqual(observer.ignored, 3)
        self.assertEqual(observer.packets, 0)
        self.assertFalse(observer.first_packet.is_set())

    def test_timing_counts_only_valid_selected_peer_requests(self):
        timing = capture.TimingResponder(HOST)
        transport = SimpleNamespace(sendto=Mock())
        timing.connection_made(transport)
        request = b'\x80\xd2' + bytes(22) + b'12345678'
        timing.datagram_received(request, ('127.0.0.2', 12345))
        timing.datagram_received(b'\x80\xd2', (HOST, 12345))
        timing.datagram_received(bytes(32), (HOST, 12345))
        self.assertEqual((timing.requests, timing.replies), (0, 0))
        timing.datagram_received(request, (HOST, 12345))
        self.assertEqual((timing.requests, timing.replies), (1, 1))
        reply, peer = transport.sendto.call_args.args
        self.assertEqual(peer, (HOST, 12345))
        self.assertEqual(reply[8:16], request[24:32])
        self.assertEqual(len(reply), 32)


class FakeSession:
    host = HOST

    def __init__(self, error=None, packets=()):
        self.error = error
        self.packets = packets
        self.trace = []
        self.closed = False
        self.opened = False

    async def open_input(self, profile, recorder, observe_only=False):
        self.opened = True
        if self.error:
            raise self.error
        recorder.enabled = True
        for packet in self.packets:
            recorder.datagram_received(packet, (HOST, 12345))

    async def feedback(self):
        pass

    async def close(self):
        self.closed = True


class RecordingTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_successful_setup_is_still_torn_down(self):
        session = capture.AirPlayInputSession(HOST)
        session.rtsp = SimpleNamespace(
            exchange=AsyncMock(return_value=SimpleNamespace(code=200, body=b"malformed")),
            teardown=AsyncMock(return_value=SimpleNamespace(code=200)),
        )
        with self.assertRaises(capture.CaptureError):
            await session.exchange("SETUP", {"timingProtocol": "NTP"})
        await session.close()
        session.rtsp.teardown.assert_awaited_once_with("0")

    async def test_rejected_session_does_not_send_teardown(self):
        session = capture.AirPlayInputSession(HOST)
        session.rtsp = SimpleNamespace(
            exchange=AsyncMock(return_value=SimpleNamespace(code=453, body=b"")),
            teardown=AsyncMock(),
        )
        status, _ = await session.exchange("SETUP", {"timingProtocol": "NTP"})
        self.assertEqual(status, 453)
        await session.close()
        session.rtsp.teardown.assert_not_awaited()

    async def test_observation_never_creates_or_confirms_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            unused = Path(directory) / "must-not-exist.wav"
            session = FakeSession(packets=(rtp(),))
            report = await capture.capture(session, PROFILE, unused, 0.001, observe_only=True)
            self.assertEqual(report["result"], "packets_observed_unverified")
            self.assertEqual(report["frames"], 0)
            self.assertFalse(report["recording_created"])
            self.assertFalse(report["homepod_microphone_verified"])
            self.assertFalse(unused.exists())

    async def test_synthetic_audio_is_saved_and_still_marked_unverified(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.wav"
            session = FakeSession(packets=(rtp(), rtp(seq=2, timestamp=102)))
            report = await capture.capture(session, PROFILE, path, 0.001)
            self.assertEqual(report["result"], "audio_received_unverified")
            self.assertFalse(report["homepod_microphone_verified"])
            self.assertEqual(report["frames"], 4)
            self.assertEqual(report["data_datagrams"], 2)
            self.assertEqual(report["payload_types"], {'100': 2})
            with wave.open(str(path), "rb") as recording:
                self.assertEqual((recording.getnchannels(), recording.getsampwidth(), recording.getframerate()),
                                 (1, 2, 16000))
                self.assertEqual(recording.readframes(4), struct.pack("<hhhh", 1, -2, 1, -2))
            self.assertTrue(session.closed)

    async def test_realtime_profile_records_only_its_payload_type(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'realtime.wav'
            session = FakeSession(packets=(rtp(payload_type=96),))
            profile = capture.PROFILES['realtime-telephony']
            report = await capture.capture(session, profile, path, 0.001)
            self.assertEqual(report['payload_types'], {'96': 1})
            self.assertEqual(report['frames'], 2)

    async def test_wrong_payload_type_cannot_create_wav(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'absent.wav'
            session = FakeSession(packets=(rtp(payload_type=96),))
            report = await capture.capture(session, PROFILE, path, 0.001, first_packet_timeout=0.001)
            self.assertEqual(report['error']['code'], 'no_audio')
            self.assertEqual(report['data_datagrams'], 1)
            self.assertEqual(report['ignored_datagrams'], 1)
            self.assertFalse(path.exists())

    async def test_observation_report_contains_timing_positive_control(self):
        session = FakeSession(packets=(rtp(),))
        session.timing = SimpleNamespace(requests=3, replies=3)
        report = await capture.capture(session, PROFILE, None, 0.001, observe_only=True)
        self.assertEqual((report['timing_requests'], report['timing_replies']), (3, 3))
        self.assertFalse(report['homepod_microphone_verified'])

    async def test_no_audio_creates_no_wav(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "absent.wav"
            session = FakeSession()
            report = await capture.capture(session, PROFILE, path, 0.001, first_packet_timeout=0.001)
            self.assertEqual(report["error"]["code"], "no_audio")
            self.assertFalse(path.exists())
            self.assertTrue(session.closed)

    async def test_failed_negotiation_creates_no_wav_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "absent.wav"
            session = FakeSession(error=capture.CaptureError("no_input_stream", "not allocated"))
            report = await capture.capture(session, PROFILE, path, 0.001)
            self.assertEqual(report["error"]["code"], "no_input_stream")
            self.assertFalse(path.exists())
            self.assertTrue(session.closed)

    async def test_existing_output_is_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "existing.wav"
            path.write_bytes(b"keep this")
            session = FakeSession()
            with self.assertRaises(capture.CaptureError):
                await capture.capture(session, PROFILE, path, 0.001)
            self.assertEqual(path.read_bytes(), b"keep this")
            self.assertFalse(session.opened)

    async def test_foreign_source_and_disabled_capture_cannot_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "absent.wav"
            recorder = capture.PcmRecorder(HOST, path, PROFILE)
            recorder.datagram_received(rtp(), (HOST, 1))
            recorder.enabled = True
            recorder.datagram_received(rtp(), ("127.0.0.2", 1))
            recorder.close()
            self.assertFalse(path.exists())

    async def test_sequence_and_timestamp_wrap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrap.wav"
            session = FakeSession(packets=(rtp(seq=65535, timestamp=0xFFFFFFFE), rtp(seq=0, timestamp=0)))
            report = await capture.capture(session, PROFILE, path, 0.001)
            self.assertEqual(report["packets"], 2)
            self.assertIsNone(report["error"])

    async def test_duplicates_and_late_packets_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.wav"
            session = FakeSession(packets=(rtp(), rtp(), rtp(seq=0, timestamp=98), rtp(seq=2, timestamp=102)))
            report = await capture.capture(session, PROFILE, path, 0.001)
            self.assertEqual(report["packets"], 2)

    async def test_packet_loss_is_partial_not_fabricated_silence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partial.wav"
            session = FakeSession(packets=(rtp(), rtp(seq=3, timestamp=104)))
            report = await capture.capture(session, PROFILE, path, 0.001)
            self.assertEqual(report["error"]["code"], "rtp_discontinuity")
            self.assertTrue(report["partial_recording"])
            with wave.open(str(path), "rb") as recording:
                self.assertEqual(recording.getnframes(), 2)

    async def test_cancellation_finalizes_wav_and_closes_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cancelled.wav"
            session = FakeSession(packets=(rtp(),))
            task = asyncio.create_task(capture.capture(session, PROFILE, path, 10))
            await asyncio.sleep(0.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(session.closed)
            with wave.open(str(path), "rb") as recording:
                self.assertEqual(recording.getnframes(), 2)


class CliTests(unittest.TestCase):
    def test_invalid_duration(self):
        for value in ("nan", "inf", "0", "-1", "61"):
            with self.subTest(value=value), self.assertRaises(Exception):
                capture.duration(value)

    def test_invalid_arguments_do_not_connect(self):
        for extra in (("--port", "0"), ("--seconds", "nan")):
            with tempfile.TemporaryDirectory() as directory:
                args = ["--host", HOST, "--profile", "main-speech", "--output", str(Path(directory) / "x.wav"), *extra]
                with patch.object(capture, "AirPlayInputSession") as backend:
                    with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                        capture.main(args)
                    backend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
