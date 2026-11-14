"""Inspect AirPlay metadata; this tool does not capture a HomePod microphone.

Uses only GET /info and OPTIONS. No pairing, SETUP, RECORD, audio, or volume
commands. An advertised capability is a research lead, not capture support.
"""

import argparse
import datetime
import ipaddress
import json
import math
from pathlib import Path
import plistlib
import socket
import sys
from xml.parsers.expat import ExpatError


MAX_HEADER = 64 * 1024
MAX_BODY = 1024 * 1024
USER_AGENT = "AirPlay/550.10"


class ProbeError(Exception):
    """A failed or malformed control reply, never evidence of no microphone."""


def read_response(connection):
    """Read a bounded HTTP/RTSP reply, including fragmented plist bodies."""
    data = bytearray()
    while b"\r\n\r\n" not in data:
        block = connection.recv(4096)
        if not block:
            raise ProbeError("Connection closed before response headers")
        data.extend(block)
        marker = data.find(b"\r\n\r\n")
        if (marker < 0 and len(data) > MAX_HEADER) or marker > MAX_HEADER:
            raise ProbeError("Response headers exceed limit")

    head, body = bytes(data).split(b"\r\n\r\n", 1)
    try:
        lines = head.decode("ascii").split("\r\n")
        protocol, status, *_ = lines[0].split(" ")
        if protocol not in ("HTTP/1.0", "HTTP/1.1", "RTSP/1.0"):
            raise ValueError("Unsupported response protocol")
        status = int(status)
        if not 100 <= status <= 599:
            raise ValueError("Invalid response status")
        headers = {}
        for line in lines[1:]:
            key, value = line.split(":", 1)
            key = key.strip().lower()
            if not key or key in headers:
                raise ValueError("Missing or duplicate header name")
            headers[key] = value.strip()
        if "transfer-encoding" in headers:
            raise ValueError("Transfer encoding is unsupported")
        length = int(headers.get("content-length", "0"))
        if not 0 <= length <= MAX_BODY:
            raise ValueError("Response body length is outside the limit")
        if "cseq" in headers and headers["cseq"] != "1":
            raise ValueError("Unexpected CSeq")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProbeError(f"Invalid response headers: {exc}") from exc

    while len(body) < length:
        block = connection.recv(min(16384, length - len(body)))
        if not block:
            raise ProbeError("Connection closed before the complete response body")
        body += block
    return {"status": status, "headers": headers, "body": body[:length]}


def request(host, port, timeout, method, path):
    # Keep this allowlist even if future callers want to try session commands.
    if (method, path) not in (("GET", "/info"), ("OPTIONS", "*")):
        raise ProbeError("Only GET /info and OPTIONS * are allowed")
    host = str(ipaddress.ip_address(host))
    wire = (
        f"{method} {path} RTSP/1.0\r\nCSeq: 1\r\n"
        f"User-Agent: {USER_AGENT}\r\n"
        "Accept: application/x-apple-binary-plist\r\n"
        "Content-Length: 0\r\n\r\n"
    ).encode("ascii")
    with socket.create_connection((host, port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        connection.sendall(wire)
        return read_response(connection)


def parse_info(body):
    if len(body) > MAX_BODY:
        raise ProbeError("Device info exceeds limit")
    try:
        info = plistlib.loads(body)
    except (ValueError, TypeError, OverflowError, RecursionError, ExpatError) as exc:
        raise ProbeError("Device info is not a valid property list") from exc
    if not isinstance(info, dict):
        raise ProbeError("Device info must be a property-list dictionary")
    return info


def feature_bits(value):
    if isinstance(value, str):
        words = [word.strip() for word in value.split(",")]
        if len(words) == 2:
            low, high = (int(word, 16) for word in words)
            if not all(0 <= word <= 0xFFFFFFFF for word in (low, high)):
                raise ValueError("Feature word outside 32-bit range")
            value = low | (high << 32)
        elif len(words) == 1:
            value = int(words[0], 16 if words[0].lower().startswith("0x") else 10)
        else:
            raise ValueError("Expected one feature integer or two hex words")
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**64:
        raise ValueError("Features are not an unsigned 64-bit integer")
    return [bit for bit in range(64) if value & (1 << bit)]


def input_hint_paths(value, path="", depth=0):
    """Keep key paths only, never unknown values that may contain credentials."""
    if depth > 16:
        return []
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                continue
            child_path = f"{path}.{key}" if path else key
            normalized = key.lower().replace("_", "").replace("-", "")
            if any(term in normalized for term in ("audioinput", "microphone", "micinput")):
                found.append(child_path)
            found.extend(input_hint_paths(child, child_path, depth + 1))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(input_hint_paths(child, f"{path}[{index}]", depth + 1))
    return found


def make_report(info=None, responses=None, error=None, access="offline"):
    info = info if info is not None else {}
    identity = {}
    for source, target in (("model", "model"), ("osBuildVersion", "os_build"),
                           ("sourceVersion", "airplay_version")):
        value = info.get(source)
        if isinstance(value, str):
            identity[target] = value[:256]
    features = {"set_bits": [], "parse_error": None}
    if "features" in info:
        try:
            features["set_bits"] = feature_bits(info["features"])
        except ValueError as exc:
            features["parse_error"] = str(exc)
    return {
        "schema_version": 1,
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "device_info_access": access,
        "device": identity,
        "features": features,
        "input_hint_paths": input_hint_paths(info),
        "requests": responses or [],
        "error": error,
        "assessment": {
            "homepod_microphone_capture": "unverified",
            "this_tool_records_audio": False,
            "explanation": (
                "Metadata, input-related keys, RECORD in OPTIONS, or an authentication "
                "failure do not establish or rule out a usable HomePod microphone uplink. "
                "No authenticated microphone session or audio samples were obtained."
            ),
        },
    }


def probe(host, port=7000, timeout=5.0):
    # Resolve no names and scan no subnets: contact only the user's specified IP.
    ipaddress.ip_address(host)
    responses = []
    errors = []
    info = None
    access = "unavailable"
    for method, path in (("GET", "/info"), ("OPTIONS", "*")):
        try:
            reply = request(host, port, timeout, method, path)
            item = {"method": method, "path": path, "status": reply["status"]}
            if method == "OPTIONS":
                item["public_methods"] = [
                    part.strip() for part in reply["headers"].get("public", "").split(",")
                    if part.strip()
                ]
            responses.append(item)
            if method == "GET":
                if reply["status"] in (401, 403, 470):
                    access = "authentication_required"
                elif 200 <= reply["status"] < 300:
                    info = parse_info(reply["body"])
                    access = "received"
                else:
                    access = f"status_{reply['status']}"
        except (OSError, ProbeError) as exc:
            # Report the category, not OS messages that can include private addresses.
            errors.append(f"{method} {path}: {type(exc).__name__}")
    return make_report(info, responses, "; ".join(errors) or None, access)


def positive_timeout(value):
    value = float(value)
    if not math.isfinite(value) or not 0 < value <= 30:
        raise argparse.ArgumentTypeError("timeout must be greater than 0 and at most 30 seconds")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--host", type=ipaddress.ip_address, help="HomePod IP; no auto-discovery")
    source.add_argument("--info-file", type=Path, help="inspect an existing binary/XML /info plist offline")
    parser.add_argument("--port", type=int, default=7000, help="AirPlay control port, default 7000")
    parser.add_argument("--timeout", type=positive_timeout, default=5.0)
    parser.add_argument("--output", type=Path, help="write the selected metadata report as UTF-8 JSON")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        if args.info_file:
            with args.info_file.open("rb") as source_file:
                info = parse_info(source_file.read(MAX_BODY + 1))
            report = make_report(info)
        else:
            report = probe(str(args.host), args.port, args.timeout)
        output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            # Avoid accidentally overwriting a prior result (or the source plist).
            with args.output.open("x", encoding="utf-8") as output_file:
                output_file.write(output)
        else:
            print(output, end="")
        return 0 if report["device_info_access"] in ("offline", "received") and not report["error"] else 2
    except (OSError, ProbeError) as exc:
        print(f"Probe failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
