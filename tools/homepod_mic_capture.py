"""Experimental authenticated HomePod input-stream negotiation and PCM recording.

These are protocol hypotheses, NOT verified HomePod microphone profiles. A 200
response alone never enables capture. No PC microphone or loopback API is used.
"""

import argparse
import asyncio
from dataclasses import dataclass
import ipaddress
import json
import logging
from pathlib import Path
import plistlib
import struct
import sys
import time
import uuid
import wave


class CaptureError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class InputProfile:
    stream_type: int
    audio_type: str
    sample_rate: int = 16000
    channels: int = 1
    audio_format: int = 0x10  # AirPlay PCM 16 kHz, mono, signed 16-bit.
    payload_type: int = 96


# MainAudio + input + telephony/speechRecognition is documented for CarPlay.
# The type-96 variant tests whether HomePod exposes input on its realtime path.
# Neither assumption is established for a stock HomePod. Do not auto-cycle them.
PROFILES = {
    "main-telephony": InputProfile(100, "telephony", payload_type=100),
    "main-speech": InputProfile(100, "speechRecognition", payload_type=100),
    "realtime-telephony": InputProfile(96, "telephony"),
}


def validate_device(info):
    if info.get("model") != "AudioAccessory6,1":
        raise CaptureError("wrong_device", "This experiment targets a second-generation HomePod only")
    flags = info.get("statusFlags")
    if isinstance(flags, bool) or not isinstance(flags, int) or flags & (1 << 17):
        raise CaptureError("receiver_busy", "Receiver busy or status unknown; it will not be taken over")
    if flags & ((1 << 9) | (1 << 3)):
        raise CaptureError("pairing_required", "Receiver requires a persistent pairing; no PIN attempts made")


def input_descriptor(profile, data_port, control_port, connection_id):
    return {
        "type": profile.stream_type,
        "audioType": profile.audio_type,
        "input": True,
        "audioFormat": profile.audio_format,
        "ct": 1,
        "sr": profile.sample_rate,
        "ch": profile.channels,
        "ss": 16,
        "spf": profile.sample_rate // 100,
        "dataPort": data_port,
        "controlPort": control_port,
        "streamConnectionID": connection_id,
    }


def decode_reply(body):
    """Decode by payload, not the peer's header spelling/casing."""
    if not body:
        return {}
    try:
        value = plistlib.loads(body.encode() if isinstance(body, str) else body)
    except Exception as exc:
        raise CaptureError("invalid_reply", "SETUP returned an invalid property list") from exc
    if not isinstance(value, dict):
        raise CaptureError("invalid_reply", "SETUP reply must be a dictionary")
    return value


def validate_input_reply(status, body, profile):
    if status != 200:
        raise CaptureError("input_setup_rejected", f"Input SETUP returned status {status}")
    streams = body.get("streams")
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], dict):
        raise CaptureError("no_input_stream", "SETUP did not return exactly one audio stream")
    stream = streams[0]
    if stream.get("type") != profile.stream_type:
        raise CaptureError("wrong_stream_type", "Receiver did not confirm the requested stream type")
    if stream.get("input") is not True:
        raise CaptureError("input_unconfirmed", "Receiver did not explicitly confirm input=true; capture stopped")
    if stream.get("audioFormat") != profile.audio_format:
        raise CaptureError("format_unconfirmed", "Receiver did not explicitly confirm the requested PCM format")
    for field, expected in (("sr", profile.sample_rate), ("ch", profile.channels), ("ss", 16), ("ct", 1)):
        if field in stream and stream[field] != expected:
            raise CaptureError("unsupported_format", f"Receiver returned an unsupported {field}")
    if any(stream.get(key) for key in ("shk", "ekey", "eiv", "et", "encryptionType")):
        raise CaptureError("unsupported_encryption", "Encrypted audio is not decoded by this prototype")
    return stream


def input_confirmation(status, body, profile, observe_only=False):
    try:
        validate_input_reply(status, body, profile)
        return None
    except CaptureError as exc:
        observable = exc.code in ("input_unconfirmed", "format_unconfirmed", "unsupported_format", "unsupported_encryption")
        if not observe_only or not observable:
            raise
        return exc.code


def parse_rtp(packet):
    """Parse candidate RTP without assuming a codec."""
    if len(packet) < 12 or packet[0] >> 6 != 2:
        raise ValueError("Not an RTP v2 audio packet")
    # RFC 5761 reserves this overlapping range for RTCP when multiplexed.
    # A control packet is not candidate audio, even if its length looks valid.
    if 192 <= packet[1] <= 223:
        raise ValueError("RTCP or ambiguous control packet")
    offset = 12 + (packet[0] & 15) * 4
    if len(packet) < offset:
        raise ValueError("Truncated CSRC list")
    if packet[0] & 0x10:
        if len(packet) < offset + 4:
            raise ValueError("Truncated RTP extension")
        words = struct.unpack_from("!H", packet, offset + 2)[0]
        offset += 4 + words * 4
    if offset > len(packet):
        raise ValueError("Truncated RTP extension data")
    end = len(packet)
    if packet[0] & 0x20:
        padding = packet[-1]
        if padding == 0 or padding > end - offset:
            raise ValueError("Invalid RTP padding")
        end -= padding
    payload = packet[offset:end]
    if not payload:
        raise ValueError("Missing RTP payload")
    sequence, timestamp, ssrc = struct.unpack_from("!HII", packet, 2)
    return packet[1] & 0x7F, sequence, timestamp, ssrc, payload


def pcm_from_rtp(packet, expected_payload_type=96):
    """Validate RTP v2 and the negotiated payload type; return L16 as WAV LE."""
    payload_type, sequence, timestamp, ssrc, payload = parse_rtp(packet)
    if payload_type != expected_payload_type:
        raise ValueError("Different RTP payload type")
    if not payload or len(payload) % 2:
        raise ValueError("Missing or incomplete 16-bit PCM")
    pcm = bytearray(len(payload))
    pcm[0::2], pcm[1::2] = payload[1::2], payload[0::2]
    return sequence, timestamp, ssrc, bytes(pcm)


class PcmRecorder(asyncio.DatagramProtocol):
    """Accept only the selected peer; never create an empty pretend recording."""

    def __init__(self, host, path, profile):
        self.host = ipaddress.ip_address(host)
        self.path = Path(path)
        self.profile = profile
        self.file = None
        self.writer = None
        self.enabled = False
        self.first_packet = asyncio.Event()
        self.failure = None
        self.packets = 0
        self.frames = 0
        self.ignored = 0
        self.sequence = None
        self.timestamp = None
        self.last_frames = None
        self.ssrc = None
        self.datagrams = 0
        self.byte_count = 0
        self.payload_types = {}

    def datagram_received(self, packet, peer):
        if not self.enabled or self.failure:
            return
        if ipaddress.ip_address(peer[0]) != self.host:
            self.ignored += 1
            return
        self.datagrams += 1
        self.byte_count += len(packet)
        try:
            seq, timestamp, ssrc, pcm = pcm_from_rtp(packet, self.profile.payload_type)
        except ValueError:
            self.ignored += 1
            return
        try:
            if self.sequence is not None:
                advance = (seq - self.sequence) & 0xFFFF
                if advance == 0 or advance >= 0x8000:
                    self.ignored += 1
                    return
                if advance != 1 or ssrc != self.ssrc:
                    raise CaptureError("rtp_discontinuity", "Packet loss or stream change; no fabricated silence inserted")
                if timestamp != ((self.timestamp + self.last_frames) & 0xFFFFFFFF):
                    raise CaptureError("rtp_discontinuity", "RTP timestamp discontinuity")
            if len(pcm) % (2 * self.profile.channels):
                raise CaptureError("invalid_pcm", "Incomplete PCM frame")
            if self.writer is None:
                self.file = self.path.open("xb")
                self.writer = wave.open(self.file, "wb")
                self.writer.setnchannels(self.profile.channels)
                self.writer.setsampwidth(2)
                self.writer.setframerate(self.profile.sample_rate)
            self.writer.writeframesraw(pcm)
            self.last_frames = len(pcm) // (2 * self.profile.channels)
            self.sequence, self.timestamp, self.ssrc = seq, timestamp, ssrc
            self.frames += self.last_frames
            self.packets += 1
            key = str(self.profile.payload_type)
            self.payload_types[key] = self.payload_types.get(key, 0) + 1
        except (OSError, CaptureError, wave.Error) as exc:
            self.failure = exc
            self.enabled = False
        finally:
            self.first_packet.set()

    def close(self):
        self.enabled = False
        try:
            if self.writer:
                self.writer.close()
        finally:
            if self.file:
                self.file.close()


class RtpObserver(asyncio.DatagramProtocol):
    """Count candidate RTP only. Never decode or retain unknown audio payloads."""

    def __init__(self, host):
        self.host = ipaddress.ip_address(host)
        self.enabled = False
        self.failure = None
        self.first_packet = asyncio.Event()
        self.packets = 0
        self.frames = 0
        self.datagrams = 0
        self.byte_count = 0
        self.payload_types = {}
        self.ignored = 0

    def datagram_received(self, packet, peer):
        if not self.enabled:
            return
        if ipaddress.ip_address(peer[0]) != self.host:
            self.ignored += 1
            return
        self.datagrams += 1
        self.byte_count += len(packet)
        try:
            payload_type, *_ = parse_rtp(packet)
        except ValueError:
            self.ignored += 1
            return
        self.packets += 1
        key = str(payload_type)
        self.payload_types[key] = self.payload_types.get(key, 0) + 1
        self.first_packet.set()

    def close(self):
        self.enabled = False


class TimingResponder(asyncio.DatagramProtocol):
    def __init__(self, host):
        self.host = ipaddress.ip_address(host)
        self.requests = 0
        self.replies = 0

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, peer):
        if ipaddress.ip_address(peer[0]) != self.host or len(data) < 32 or data[1] & 127 != 0x52:
            return
        self.requests += 1
        stamp = int((time.time() + 2208988800) * 2**32) & (2**64 - 1)
        reply = bytes([data[0], 0xD3]) + struct.pack("!HI", 7, 0)
        self.transport.sendto(reply + data[24:32] + struct.pack("!QQ", stamp, stamp), peer)
        self.replies += 1


class AirPlayInputSession:
    """Experimental backend isolated from the existing app and its settings."""

    def __init__(self, host, port=7000):
        self.host = str(ipaddress.IPv4Address(host))
        self.port = port
        self.connection = None
        self.rtsp = None
        self.transports = []
        self.session_started = False
        self.trace = []
        self.timing = None

    async def exchange(self, method, body=None):
        reply = await asyncio.wait_for(self.rtsp.exchange(method, body=body, allow_error=True), 8)
        self.trace.append({"request": method, "status": reply.code})
        # Even an undecodable successful SETUP may have reserved the speaker.
        # Remember ownership before parsing so failure still tears our session down.
        if method == "SETUP" and isinstance(body, dict) and "streams" not in body and reply.code == 200:
            self.session_started = True
        return reply.code, decode_reply(reply.body) if reply.body else {}

    async def open_input(self, profile, recorder, observe_only=False):
        # Optional dependency: offline parser/recorder tests need only stdlib.
        from pyatv.auth.hap_pairing import TRANSIENT_CREDENTIALS
        from pyatv.auth.hap_channel import setup_channel
        from pyatv.protocols.airplay.auth import verify_connection
        from pyatv.protocols.airplay.channels import EventChannel
        from pyatv.support.http import http_connect
        from pyatv.support.rtsp import RtspSession

        self.connection = await asyncio.wait_for(http_connect(self.host, self.port), 5)
        self.rtsp = RtspSession(self.connection)
        info = await asyncio.wait_for(self.rtsp.info(), 5)
        validate_device(info)
        verifier = await asyncio.wait_for(verify_connection(TRANSIENT_CREDENTIALS, self.connection), 10)
        encrypted_info = await asyncio.wait_for(self.rtsp.info(), 5)
        validate_device(encrypted_info)  # Recheck ownership before session SETUP.
        self.trace.append({"request": "transient_authentication", "success": True})

        loop = asyncio.get_running_loop()
        endpoints = []
        self.timing = TimingResponder(self.host)
        for protocol in (self.timing, recorder, asyncio.DatagramProtocol()):
            transport, _ = await loop.create_datagram_endpoint(
                lambda protocol=protocol: protocol, local_addr=(self.connection.local_ip, 0)
            )
            self.transports.append(transport)
            endpoints.append(transport.get_extra_info("sockname")[1])
        timing_port, data_port, control_port = endpoints
        status, session = await self.exchange("SETUP", {
            "deviceID": "02:48:50:43:41:53", "macAddress": "02:48:50:43:41:53",
            "sessionUUID": str(uuid.uuid4()).upper(), "timingPort": timing_port,
            "timingProtocol": "NTP", "isMultiSelectAirPlay": True,
            "groupContainsGroupLeader": False, "senderSupportsRelay": False,
            "statsCollectionEnabled": False, "name": "HomePodCast microphone research",
            "model": "HomePodCast", "osName": "Windows", "sourceVersion": "550.10",
        })
        if status != 200:
            raise CaptureError("session_rejected", f"Session SETUP returned status {status}")
        self.session_started = True
        event_port = session.get("eventPort")
        if not isinstance(event_port, int) or not 1 <= event_port <= 65535:
            raise CaptureError("missing_event_channel", "Session did not return a valid event port")
        transport, _ = await asyncio.wait_for(setup_channel(
            EventChannel, verifier, self.host, event_port, "Events-Salt",
            "Events-Read-Encryption-Key", "Events-Write-Encryption-Key",
        ), 5)
        self.transports.append(transport)
        status, body = await self.exchange("SETUP", {
            "streams": [input_descriptor(profile, data_port, control_port, self.rtsp.session_id)]
        })
        # Only selected direction/format fields are retained; never session keys.
        streams = body.get("streams")
        self.trace.append({
            "input_reply_keys": sorted(body),
            "stream_fields": [{key: item[key] for key in ("type", "input", "audioFormat", "ct", "sr", "ch", "ss")
                               if key in item and isinstance(item[key], (int, bool, str))}
                              for item in streams if isinstance(item, dict)] if isinstance(streams, list) else [],
        })
        unconfirmed = input_confirmation(status, body, profile, observe_only)
        if unconfirmed:
            self.trace.append({"observation_only": True, "input_validation_error": unconfirmed})
        status, _ = await self.exchange("RECORD")
        if status != 200:
            raise CaptureError("record_rejected", f"RECORD returned status {status}")
        recorder.enabled = True

    async def feedback(self):
        reply = await asyncio.wait_for(self.rtsp.feedback(allow_error=True), 5)
        if reply.code != 200:
            raise CaptureError("feedback_failed", f"Feedback returned status {reply.code}")

    async def close(self):
        try:
            if self.session_started and self.rtsp:
                try:
                    reply = await asyncio.wait_for(self.rtsp.teardown("0"), 3)
                    self.trace.append({"request": "TEARDOWN", "status": reply.code})
                except Exception:
                    self.trace.append({"request": "TEARDOWN", "success": False})
        finally:
            for transport in self.transports:
                transport.close()
            if self.connection:
                self.connection.close()


async def capture(session, profile, path, seconds, first_packet_timeout=5, observe_only=False):
    path = Path(path) if path is not None else None
    if not observe_only and (path is None or path.exists()):
        raise CaptureError("output_exists", "Output file already exists")
    recorder = RtpObserver(session.host) if observe_only else PcmRecorder(session.host, path, profile)
    report = {"result": "unverified", "profile": None, "packets": 0, "frames": 0,
              "homepod_microphone_verified": False, "error": None}
    heartbeat = None

    async def keep_alive():
        try:
            while True:
                await asyncio.sleep(2)
                await session.feedback()
        except Exception as exc:
            recorder.failure = exc
            recorder.first_packet.set()

    try:
        await session.open_input(profile, recorder, observe_only=observe_only)
        heartbeat = asyncio.create_task(keep_alive())
        try:
            await asyncio.wait_for(recorder.first_packet.wait(), first_packet_timeout)
        except TimeoutError as exc:
            raise CaptureError("no_audio", "No valid input audio received; no empty WAV is created") from exc
        deadline = asyncio.get_running_loop().time() + seconds
        while not recorder.failure and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(min(0.25, max(0, deadline - asyncio.get_running_loop().time())))
        if recorder.failure:
            raise recorder.failure
        report["result"] = "packets_observed_unverified" if observe_only else "audio_received_unverified"
    except Exception as exc:
        report["result"] = "failed"
        report["error"] = {"code": getattr(exc, "code", type(exc).__name__), "message": str(exc)}
    finally:
        if heartbeat:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        try:
            recorder.close()
        finally:
            await session.close()
        report.update(packets=recorder.packets, frames=recorder.frames, trace=session.trace,
                      observation_only=observe_only,
                      recording_created=bool(not observe_only and recorder.packets),
                      partial_recording=bool(not observe_only and recorder.packets and report["result"] == "failed"))
        report.update(data_datagrams=recorder.datagrams, data_bytes=recorder.byte_count,
                      payload_types=recorder.payload_types, ignored_datagrams=recorder.ignored)
        timing = getattr(session, "timing", None)
        report.update(timing_requests=timing.requests if timing else 0,
                      timing_replies=timing.replies if timing else 0)
    return report


def duration(value):
    result = float(value)
    if not 0 < result <= 60:
        raise argparse.ArgumentTypeError("duration must be greater than 0 and at most 60 seconds")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, type=ipaddress.IPv4Address)
    parser.add_argument("--port", type=int, default=7000)
    parser.add_argument("--profile", required=True, choices=PROFILES)
    parser.add_argument("--output", type=Path, help="exclusive WAV path; only created after valid input")
    parser.add_argument("--observe-only", action="store_true", help="count candidate RTP despite missing input confirmation; never save audio")
    parser.add_argument("--report", type=Path, help="exclusive JSON diagnostic path")
    parser.add_argument("--seconds", type=duration, default=10)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    if bool(args.output) == args.observe_only:
        parser.error("recording requires --output; observation requires --observe-only without --output")
    if (args.output and args.output.exists()) or (args.report and args.report.exists()):
        parser.error("output/report already exists; choose new file paths")
    if args.report and args.output and args.output.resolve() == args.report.resolve():
        parser.error("WAV and JSON paths must differ")
    # pyatv's DEBUG logs include authentication material. Never enable them here.
    logging.getLogger("pyatv").setLevel(logging.WARNING)
    report = asyncio.run(capture(AirPlayInputSession(str(args.host), args.port),
                                 PROFILES[args.profile], args.output, args.seconds,
                                 observe_only=args.observe_only))
    report["profile"] = args.profile
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    print(text, end="")
    if args.report:
        with args.report.open("x", encoding="utf-8") as report_file:
            report_file.write(text)
    return 0 if report["result"] in ("audio_received_unverified", "packets_observed_unverified") else 2


if __name__ == "__main__":
    sys.exit(main())
