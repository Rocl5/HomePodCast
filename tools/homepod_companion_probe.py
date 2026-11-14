"""Bounded Companion authentication diagnostics, not a microphone recorder.

Uses pyatv's OPACK/TLV8 codecs and cryptography's authenticated primitives.
Never creates a persistent pairing, starts playback, or starts a DropIn call.
An unpaired identity test is explicit, single-attempt, and never trusts the
server identity learned from an unauthenticated handshake.
"""

import argparse
import asyncio
import binascii
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import secrets
from uuid import uuid4

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey, X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pyatv.auth.hap_tlv8 import TlvValue, read_tlv, write_tlv
from pyatv.auth.hap_srp import SRPAuthHandler
from pyatv.core import mdns
from pyatv.support import opack
from homepod_dropin_messages import (
    Envelope, RAPPORT_SERVICE_TYPE, RAPPORT_STATE_REQUEST_ID, STATE_REQUEST_PAYLOAD_NAME,
)


MAX_FRAME_SIZE = 1024 * 1024
COMPANION_SERVICE = "_companion-link._tcp.local"


class ProtocolError(ValueError):
    """Unexpected or unsafe protocol response."""


class AuthenticationRejected(ProtocolError):
    def __init__(self, code, state):
        self.code = code
        self.state = state
        super().__init__(f"HomePod rejected identity: TLV error {code}, state {state}")


def derive_key(shared, salt, info):
    return HKDF(algorithm=hashes.SHA512(), length=32,
                salt=salt.encode(), info=info.encode()).derive(shared)


def public_bytes(key):
    return key.public_bytes(serialization.Encoding.Raw,
                            serialization.PublicFormat.Raw)


def pairing_data(message, expected_state):
    data = message.get("_pd")
    if not isinstance(data, bytes):
        raise ProtocolError("Missing binary pairing data")
    # Reject truncated TLV before passing it to the external codec.
    offset = 0
    while offset < len(data):
        if offset + 2 > len(data) or offset + 2 + data[offset + 1] > len(data):
            raise ProtocolError("Truncated pairing TLV")
        offset += 2 + data[offset + 1]
    result = read_tlv(data)
    state = result.get(TlvValue.SeqNo)
    if state != bytes([expected_state]):
        raise ProtocolError("Unexpected pairing state")
    if TlvValue.Error in result:
        value = result[TlvValue.Error]
        if len(value) != 1:
            raise ProtocolError("Malformed pairing error")
        raise AuthenticationRejected(value[0], expected_state)
    return result


@dataclass(frozen=True, repr=False)
class Credentials:
    """Explicitly supplied existing identity; no keychain/profile searching."""

    server_identifier: bytes
    server_public_key: bytes
    client_identifier: bytes
    client_private_key: bytes = field(repr=False)

    def __post_init__(self):
        for value in (self.server_identifier, self.client_identifier):
            if not isinstance(value, bytes) or not 1 <= len(value) <= 255:
                raise ValueError("Identity must contain 1 to 255 bytes")
        for value in (self.server_public_key, self.client_private_key):
            if not isinstance(value, bytes) or len(value) != 32:
                raise ValueError("Ed25519 key must contain 32 bytes")

    @classmethod
    def from_file(cls, path):
        raw = Path(path).read_bytes()
        if len(raw) > 16384:
            raise ValueError("Credentials file is too large")
        values = json.loads(raw)
        names = ("server_identifier", "server_public_key",
                 "client_identifier", "client_private_key")
        if not isinstance(values, dict) or set(values) != set(names):
            raise ValueError("Credentials require exactly four hex fields")
        try:
            decoded = [bytes.fromhex(values[name]) for name in names]
        except (ValueError, TypeError) as exc:
            raise ValueError("Credentials fields must be hex strings") from exc
        return cls(*decoded)


class PairVerify:
    """Fresh X25519 exchange with an existing pinned Ed25519 identity."""

    def __init__(self):
        self._private = X25519PrivateKey.generate()
        self._public = public_bytes(self._private.public_key())
        self._shared = None
        self._pair_cipher = None
        self._server_public = None
        self._server_identifier = None
        self._server_signature = None
        self._m3_sent = False
        self.server_signature_verified = False

    def m1(self):
        return {"_auTy": 4, "_pd": write_tlv({
            TlvValue.SeqNo: b"\x01", TlvValue.PublicKey: self._public})}

    def receive_m2(self, message):
        if self._pair_cipher is not None:
            raise ProtocolError("This verifier already received M2")
        values = pairing_data(message, 2)
        server_public = values.get(TlvValue.PublicKey)
        encrypted = values.get(TlvValue.EncryptedData)
        if not isinstance(server_public, bytes) or len(server_public) != 32:
            raise ProtocolError("Invalid server X25519 public key")
        if not isinstance(encrypted, bytes) or len(encrypted) < 16:
            raise ProtocolError("Missing encrypted server identity")
        self._shared = self._private.exchange(
            X25519PublicKey.from_public_bytes(server_public))
        self._pair_cipher = ChaCha20Poly1305(derive_key(
            self._shared, "Pair-Verify-Encrypt-Salt", "Pair-Verify-Encrypt-Info"))
        inner = read_tlv(self._pair_cipher.decrypt(
            b"\0\0\0\0PV-Msg02", encrypted, None))
        identifier = inner.get(TlvValue.Identifier)
        signature = inner.get(TlvValue.Signature)
        if not isinstance(identifier, bytes) or not 1 <= len(identifier) <= 255:
            raise ProtocolError("Invalid server identity")
        if not isinstance(signature, bytes) or len(signature) != 64:
            raise ProtocolError("Invalid server identity signature")
        self._server_public = server_public
        self._server_identifier = identifier
        self._server_signature = signature
        return {"identity_fingerprint": hashlib.sha256(identifier).hexdigest(),
                "server_signature_verified": False}

    def _m3(self, identifier, private_key):
        if self._pair_cipher is None:
            raise ProtocolError("M2 must be received before M3")
        if self._m3_sent:
            raise ProtocolError("This verifier already sent M3")
        self._m3_sent = True
        signature = private_key.sign(self._public + identifier + self._server_public)
        inner = write_tlv({TlvValue.Identifier: identifier,
                           TlvValue.Signature: signature})
        return {"_auTy": 4, "_pd": write_tlv({TlvValue.SeqNo: b"\x03",
                TlvValue.EncryptedData: self._pair_cipher.encrypt(
                    b"\0\0\0\0PV-Msg03", inner, None)})}

    def m3(self, credentials):
        if self._server_identifier != credentials.server_identifier:
            raise ProtocolError("Server identity differs from pinned identity")
        Ed25519PublicKey.from_public_bytes(credentials.server_public_key).verify(
            self._server_signature,
            self._server_public + self._server_identifier + self._public)
        message = self._m3(credentials.client_identifier,
                          Ed25519PrivateKey.from_private_bytes(credentials.client_private_key))
        self.server_signature_verified = True
        return message

    def unpaired_m3(self):
        # A fresh identity is expected to fail. This is not pair-setup and does
        # not add a home user, derive an owner's key, or enable authenticated IO.
        return self._m3(str(uuid4()).upper().encode(), Ed25519PrivateKey.generate())

    def receive_m4(self, message):
        pairing_data(message, 4)
        if not self.server_signature_verified:
            raise ProtocolError("Unpaired identity unexpectedly accepted; stopping")
        return (derive_key(self._shared, "", "ClientEncrypt-main"),
                derive_key(self._shared, "", "ServerEncrypt-main"))


async def public_transient_authenticate(stream):
    """Public guest session, not a home-member identity or persistent pairing.

    The public mode is evidenced by RPConnection's serverPairSetup path. Stop
    at M4: never send persistent pairing's M5/M6 credential exchange. Verify
    the received server proof before enabling the encrypted Companion stream.
    """
    srp = SRPAuthHandler()
    srp.initialize()
    await stream.send(3, {"_pwTy": 0, "_pd": write_tlv({
        TlvValue.Method: b"\0", TlvValue.SeqNo: b"\1", TlvValue.Flags: b"\x10"})})
    frame, message = await stream.receive()
    if frame != 4:
        raise ProtocolError("Expected public pair-setup response")
    values = pairing_data(message, 2)
    salt, server_public = values.get(TlvValue.Salt), values.get(TlvValue.PublicKey)
    if not isinstance(salt, bytes) or len(salt) != 16:
        raise ProtocolError("Invalid SRP salt")
    if not isinstance(server_public, bytes) or len(server_public) != 384:
        raise ProtocolError("Invalid SRP-3072 public key")
    srp.step1("public")
    public, proof = srp.step2(server_public, salt)
    await stream.send(4, {"_pwTy": 0, "_pd": write_tlv({
        TlvValue.SeqNo: b"\3", TlvValue.PublicKey: public, TlvValue.Proof: proof})})
    frame, message = await stream.receive()
    if frame != 4:
        raise ProtocolError("Expected public pair-setup proof response")
    values = pairing_data(message, 4)
    server_proof = values.get(TlvValue.Proof)
    # pyatv's transient helper omits this check; a locally computed proof is
    # not evidence that the peer actually supplied the correct server proof.
    if not isinstance(server_proof, bytes) or len(server_proof) != 64:
        raise ProtocolError("Invalid server SRP proof")
    if not srp._session.verify_proof(binascii.hexlify(server_proof)):
        raise ProtocolError("Server SRP proof does not match")
    shared = binascii.unhexlify(srp.shared_key)
    stream.enable_encryption(derive_key(shared, "", "ClientEncrypt-main"),
                             derive_key(shared, "", "ServerEncrypt-main"))


class CompanionStream:
    """Size-bounded serial OPACK transport with header-authenticated encryption."""

    def __init__(self, reader, writer, timeout=5):
        self.reader, self.writer, self.timeout = reader, writer, timeout
        self._out_cipher = self._in_cipher = None
        self._out_counter = self._in_counter = 0

    @classmethod
    async def connect(cls, host, port, local_ip, timeout=5):
        reader, writer = await asyncio.wait_for(asyncio.open_connection(
            host, port, local_addr=(local_ip, 0)), timeout)
        return cls(reader, writer, timeout)

    def enable_encryption(self, output_key, input_key):
        if self._out_cipher is not None:
            raise ProtocolError("Encryption is already enabled")
        self._out_cipher = ChaCha20Poly1305(output_key)
        self._in_cipher = ChaCha20Poly1305(input_key)

    async def send(self, frame_type, message):
        data = opack.pack(message)
        length = len(data) + (16 if self._out_cipher is not None else 0)
        if length > MAX_FRAME_SIZE:
            raise ProtocolError("Frame exceeds size limit")
        header = bytes([frame_type]) + length.to_bytes(3, "big")
        if self._out_cipher is not None:
            data = self._out_cipher.encrypt(
                self._out_counter.to_bytes(12, "little"), data, header)
            self._out_counter += 1
        self.writer.write(header + data)
        await asyncio.wait_for(self.writer.drain(), self.timeout)

    async def receive(self):
        header = await asyncio.wait_for(self.reader.readexactly(4), self.timeout)
        length = int.from_bytes(header[1:], "big")
        if not 1 <= length <= MAX_FRAME_SIZE:
            raise ProtocolError("Invalid frame size")
        payload = await asyncio.wait_for(self.reader.readexactly(length), self.timeout)
        if self._in_cipher is not None:
            if header[0] != 8:
                raise ProtocolError("Expected encrypted OPACK frame")
            payload = self._in_cipher.decrypt(
                self._in_counter.to_bytes(12, "little"), payload, header)
            self._in_counter += 1
        message, remainder = opack.unpack(payload)
        if remainder or not isinstance(message, dict):
            raise ProtocolError("Expected exactly one OPACK dictionary")
        return header[0], message

    async def exchange_auth(self, frame_type, message):
        await self.send(frame_type, message)
        actual, response = await self.receive()
        if actual != 6:
            raise ProtocolError("Expected pair-verify response frame")
        return response

    async def request(self, identifier, content, xid=1):
        if self._out_cipher is None:
            raise ProtocolError("Authentication is required")
        await self.send(8, {"_i": identifier, "_t": 2, "_x": xid, "_c": content})
        # Events can precede the response, but cannot extend the whole deadline.
        async def wait_response():
            for _ in range(32):
                _, response = await self.receive()
                if response.get("_t") == 3 and response.get("_x") == xid:
                    return response
            raise ProtocolError("Too many messages before response")
        return await asyncio.wait_for(wait_response(), self.timeout)

    async def system_info(self):
        response = await self.request("_systemInfo", {})
        if "_em" in response or response.get("_ec", 0):
            raise ProtocolError("System-info request was rejected")
        content = response.get("_c")
        if not isinstance(content, dict):
            raise ProtocolError("Invalid system-info content")
        return {"model": content.get("model"),
                "service_types": content.get("_stA", []),
                "homekit_identity_present": "_hkID" in content,
                "homekit_user_identity_present": "_hkUID" in content}

    async def close(self):
        self.writer.close()
        try:
            await asyncio.wait_for(self.writer.wait_closed(), self.timeout)
        except (OSError, asyncio.TimeoutError):
            pass


async def discover(host, local_ip, timeout=5):
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: mdns.UnicastDnsSdClientProtocol([COMPANION_SERVICE], host, timeout),
        local_addr=(local_ip, 0), remote_addr=(host, 5353))
    try:
        result = await protocol.get_response()
    finally:
        transport.close()
    services = [s for s in result.services if s.type == COMPANION_SERVICE
                and str(s.address) == host]
    if len(services) != 1 or not 1 <= services[0].port <= 65535:
        raise ProtocolError("No unique Companion service advertised by target")
    service = services[0]
    model = service.properties.get("rpmd", "")
    if model not in ("AudioAccessory6,1", "AudioAccessory5,1", "AudioAccessory1,1"):
        raise ProtocolError("Target does not advertise a recognized HomePod model")
    return {"port": service.port, "model": model,
            "pairing_flags": service.properties.get("rpfl"),
            "rapport_version": service.properties.get("rpvr")}


def response_summary(response):
    result = {"response_keys": sorted(response),
              "content_keys": sorted(response.get("_c", {}))
              if isinstance(response.get("_c"), dict) else []}
    for name in ("_ec", "_ed"):
        value = response.get(name)
        if isinstance(value, (str, int)):
            result[name] = value
    return result


async def probe_dropin_route(stream):
    """Routing-only test: deliberately omit homeid; never send StartDropIn.

    No home UUID is invented or inferred from an AirPlay group UUID. A missing
    homeid state request cannot prove that a valid DropIn request is authorized.
    Close the logical Rapport session even if the state request fails.
    """
    result = {"request": RAPPORT_STATE_REQUEST_ID, "required_homeid_omitted": True,
              "starts_call": False}
    # Keep the local half below the signed-32 boundary. Physical 27.2 cleanup
    # succeeded with these IDs; some full-range IDs yielded Session not found.
    local_sid = secrets.randbits(31) or 1
    session_id = None
    try:
        response = await stream.request("_sessionStart",
            {"_srvT": RAPPORT_SERVICE_TYPE, "_sid": local_sid}, xid=2)
        result["session_start"] = response_summary(response)
        content = response.get("_c")
        remote_sid = content.get("_sid") if isinstance(content, dict) else None
        # pyatv preserves integer wire widths using int subclasses.
        if (response.get("_ec") or isinstance(remote_sid, bool)
                or not isinstance(remote_sid, int) or not 1 <= remote_sid <= 0xffffffff):
            raise ProtocolError("Logical Rapport session did not start")
        session_id = (remote_sid << 32) | local_sid
        envelope = Envelope.create(STATE_REQUEST_PAYLOAD_NAME, {}).to_dictionary()
        response = await stream.request(RAPPORT_STATE_REQUEST_ID, envelope, xid=3)
        result["state_request"] = response_summary(response)
    except Exception as exc:
        result["error"] = {"type": type(exc).__name__}
        if isinstance(exc, ProtocolError):
            result["error"]["message"] = str(exc)
    finally:
        if session_id is not None:
            try:
                # The stop payload contains the combined session ID only.
                response = await stream.request("_sessionStop", {"_sid": session_id}, xid=4)
                result["session_stop"] = response_summary(response)
            except Exception as exc:
                result["session_stop_error"] = {"type": type(exc).__name__}
    return result


async def probe(host, local_ip, port=None, credentials=None, test_unpaired=False,
                timeout=5, public_transient=False, check_dropin_route=False):
    if sum((credentials is not None, test_unpaired, public_transient)) > 1:
        raise ValueError("Authentication modes are mutually exclusive")
    if check_dropin_route and not (public_transient or credentials is not None):
        raise ValueError("DropIn routing probe requires an encrypted session")
    report = {"timestamp": datetime.now(timezone.utc).isoformat(), "host": host,
              "authentication_complete": False, "homepod_microphone_verified": False,
              "trusted_controller_identity": False,
              "persistent_pairing_created": False, "stage": "discovery"}
    stream = None
    try:
        if port is None:
            report["discovery"] = await discover(host, local_ip, timeout)
            port = report["discovery"]["port"]
        report["port"] = port
        report["stage"] = "connect"
        stream = await CompanionStream.connect(host, port, local_ip, timeout)
        if public_transient:
            report["stage"] = "public_transient_srp"
            report["authentication_method"] = "public_transient_srp"
            await public_transient_authenticate(stream)
            report["authentication_complete"] = True
            report["server_srp_proof_verified"] = True
            report["stage"] = "public_system_info"
            report["system_info"] = await stream.system_info()
            if check_dropin_route:
                report["stage"] = "public_dropin_route"
                report["dropin_route"] = await probe_dropin_route(stream)
            report["stage"] = "public_guest_complete"
            return report
        verify = PairVerify()
        report["authentication_method"] = "pinned_identity_pair_verify"
        report["stage"] = "pair_verify_m1"
        response = await stream.exchange_auth(5, verify.m1())
        report["server_identity"] = verify.receive_m2(response)
        report["stage"] = "pair_verify_m2_received"
        if credentials is None and not test_unpaired:
            report["required_next"] = "existing trusted controller identity"
            return report
        report["stage"] = "pair_verify_m3"
        message = verify.m3(credentials) if credentials else verify.unpaired_m3()
        report["unpaired_identity_test"] = credentials is None
        response = await stream.exchange_auth(6, message)
        keys = verify.receive_m4(response)
        stream.enable_encryption(*keys)
        report["authentication_complete"] = True
        report["trusted_controller_identity"] = True
        report["server_identity"]["server_signature_verified"] = True
        report["stage"] = "authenticated_system_info"
        report["system_info"] = await stream.system_info()
        if check_dropin_route:
            report["dropin_route"] = await probe_dropin_route(stream)
        report["stage"] = "complete"
    except AuthenticationRejected as exc:
        report["error"] = {"type": "AuthenticationRejected", "tlv_error": exc.code,
                           "tlv_state": exc.state}
    except Exception as exc:
        # Some library exceptions can contain binary payloads. Only our own
        # controlled protocol errors are copied verbatim into the public report.
        report["error"] = {"type": type(exc).__name__}
        if isinstance(exc, ProtocolError):
            report["error"]["message"] = str(exc)
    finally:
        if stream is not None:
            await stream.close()
    return report


def probe_exit_code(report):
    if "error" in report:
        return 2
    route = report.get("dropin_route")
    if route is not None:
        if "error" in route or "session_stop_error" in route:
            return 2
        if route.get("session_stop", {}).get("_ec"):
            return 2
        # Missing homeid means this is never evidence of usable DropIn access.
        return 3
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--local-ip", required=True)
    parser.add_argument("--port", type=int)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--credentials-file", type=Path)
    parser.add_argument("--test-unpaired-identity", action="store_true")
    parser.add_argument("--public-transient", action="store_true",
                        help="read-only system info using a nonpersistent public guest session")
    parser.add_argument("--probe-dropin-route", action="store_true",
                        help="probe a logical session and invalid state request; never start a call")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    for address in (args.host, args.local_ip):
        if ipaddress.ip_address(address).version != 4:
            parser.error("This tool currently requires IPv4 addresses")
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("Port must be 1 to 65535")
    if not 0 < args.timeout <= 30:
        parser.error("Timeout must be greater than zero and at most 30 seconds")
    if sum((bool(args.credentials_file), args.test_unpaired_identity, args.public_transient)) > 1:
        parser.error("Authentication modes are mutually exclusive")
    if args.probe_dropin_route and not (args.credentials_file or args.public_transient):
        parser.error("Routing probe requires --public-transient or --credentials-file")
    # Reserve output before networking; never overwrite an earlier result.
    with args.report.open("x", encoding="utf-8") as output:
        credentials = Credentials.from_file(args.credentials_file) if args.credentials_file else None
        result = asyncio.run(probe(args.host, args.local_ip, args.port, credentials,
                                   args.test_unpaired_identity, args.timeout, args.public_transient,
                                   args.probe_dropin_route))
        text = json.dumps(result, indent=2, ensure_ascii=True)
        output.write(text + "\n")
        print(text)
    return probe_exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
