"""Loopback protocol tests; no HomePod, household identity, or microphone use."""

import asyncio
import binascii
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from pyatv.auth.hap_tlv8 import TlvValue, read_tlv, write_tlv
from pyatv.support import opack
from srptools import SRPContext, SRPServerSession, constants

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import homepod_companion_probe as companion


def seed(key):
    return key.private_bytes(serialization.Encoding.Raw,
                             serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())


class FakeAccessory:
    def __init__(self):
        self.identity = b"test-accessory-identity"
        self.signing_key = Ed25519PrivateKey.generate()
        self.session_key = X25519PrivateKey.generate()
        self.public = companion.public_bytes(self.session_key.public_key())
        self.client_signing_key = Ed25519PrivateKey.generate()
        self.client_identity = b"test-controller-identity"

    def credentials(self):
        return companion.Credentials(self.identity,
            companion.public_bytes(self.signing_key.public_key()),
            self.client_identity, seed(self.client_signing_key))

    def m2(self, message):
        self.client_public = read_tlv(message["_pd"])[TlvValue.PublicKey]
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
        self.shared = self.session_key.exchange(
            X25519PublicKey.from_public_bytes(self.client_public))
        self.cipher = ChaCha20Poly1305(companion.derive_key(self.shared,
            "Pair-Verify-Encrypt-Salt", "Pair-Verify-Encrypt-Info"))
        signature = self.signing_key.sign(self.public + self.identity + self.client_public)
        inner = write_tlv({TlvValue.Identifier: self.identity,
                           TlvValue.Signature: signature})
        return {"_pd": write_tlv({TlvValue.SeqNo: b"\2",
            TlvValue.PublicKey: self.public,
            TlvValue.EncryptedData: self.cipher.encrypt(b"\0\0\0\0PV-Msg02", inner, None)})}

    def check_m3(self, message):
        outer = read_tlv(message["_pd"])
        if outer[TlvValue.SeqNo] != b"\3":
            raise AssertionError("Wrong client state")
        inner = read_tlv(self.cipher.decrypt(
            b"\0\0\0\0PV-Msg03", outer[TlvValue.EncryptedData], None))
        if inner[TlvValue.Identifier] != self.client_identity:
            return False
        self.client_signing_key.public_key().verify(inner[TlvValue.Signature],
            self.client_public + self.client_identity + self.public)
        return True


class PairVerifyTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeAccessory()
        self.client = companion.PairVerify()
        self.m2 = self.server.m2(self.client.m1())

    def test_pinned_mutual_authentication_and_distinct_direction_keys(self):
        evidence = self.client.receive_m2(self.m2)
        self.assertFalse(evidence["server_signature_verified"])
        self.assertNotIn(self.server.identity.decode(), json.dumps(evidence))
        self.assertTrue(self.server.check_m3(self.client.m3(self.server.credentials())))
        output_key, input_key = self.client.receive_m4({"_pd": b"\6\1\4"})
        self.assertNotEqual(output_key, input_key)
        self.assertEqual(output_key, companion.derive_key(self.server.shared, "", "ClientEncrypt-main"))

    def test_unpaired_identity_is_fresh_and_never_enables_encryption(self):
        self.client.receive_m2(self.m2)
        self.assertFalse(self.server.check_m3(self.client.unpaired_m3()))
        with self.assertRaisesRegex(companion.ProtocolError, "unexpectedly accepted"):
            self.client.receive_m4({"_pd": b"\6\1\4"})

    def test_identity_rejection_preserves_error_and_state(self):
        with self.assertRaises(companion.AuthenticationRejected) as caught:
            self.client.receive_m4({"_pd": b"\6\1\4\7\1\2"})
        self.assertEqual((caught.exception.code, caught.exception.state), (2, 4))

    def test_wrong_pinned_identity_is_rejected(self):
        self.client.receive_m2(self.m2)
        c = self.server.credentials()
        wrong = companion.Credentials(b"other", c.server_public_key,
                                       c.client_identifier, c.client_private_key)
        with self.assertRaisesRegex(companion.ProtocolError, "pinned"):
            self.client.m3(wrong)

    def test_wrong_pinned_public_key_is_rejected(self):
        self.client.receive_m2(self.m2)
        c = self.server.credentials()
        wrong = companion.Credentials(c.server_identifier, b"x" * 32,
                                       c.client_identifier, c.client_private_key)
        with self.assertRaises(InvalidSignature):
            self.client.m3(wrong)

    def test_tampered_m2_is_rejected(self):
        fields = read_tlv(self.m2["_pd"])
        encrypted = fields[TlvValue.EncryptedData]
        fields[TlvValue.EncryptedData] = encrypted[:-1] + bytes([encrypted[-1] ^ 1])
        with self.assertRaises(InvalidTag):
            self.client.receive_m2({"_pd": write_tlv(fields)})

    def test_invalid_tlv_and_states_are_rejected(self):
        for data in (b"\6", b"\6\2\2", b"\6\1\3", b"\6\1\2\7\2\1\2"):
            with self.subTest(data=data), self.assertRaises(companion.ProtocolError):
                companion.pairing_data({"_pd": data}, 2)

    def test_private_key_cannot_appear_in_repr(self):
        c = self.server.credentials()
        self.assertNotIn(c.client_private_key.hex(), repr(c))
        self.assertNotIn(str(c.client_private_key), repr(c))

    def test_pair_verifier_cannot_reuse_m3_nonce_or_upgrade_unpaired_trust(self):
        self.client.receive_m2(self.m2)
        self.client.unpaired_m3()
        with self.assertRaisesRegex(companion.ProtocolError, "already sent M3"):
            self.client.m3(self.server.credentials())
        self.assertFalse(self.client.server_signature_verified)
        with self.assertRaisesRegex(companion.ProtocolError, "already received M2"):
            self.client.receive_m2(self.m2)


class CredentialTests(unittest.TestCase):
    def test_explicit_credentials_file_roundtrip(self):
        server = FakeAccessory()
        original = server.credentials()
        values = {name: getattr(original, name).hex() for name in
            ("server_identifier", "server_public_key", "client_identifier", "client_private_key")}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text(json.dumps(values))
            parsed = companion.Credentials.from_file(path)
        self.assertEqual(parsed.client_identifier, original.client_identifier)
        self.assertEqual(parsed.client_private_key, original.client_private_key)

    def test_invalid_lengths_and_types_are_rejected(self):
        for args in ((b"", b"a"*32, b"b", b"c"*32),
                     (b"a", b"b"*31, b"c", b"d"*32),
                     (b"a", b"b"*32, b"c", b"d"*64)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                companion.Credentials(*args)


class DropInRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_probe_only_queries_state_and_cleans_up(self):
        calls = []
        class Stream:
            async def request(self, identifier, content, xid):
                calls.append((identifier, content, xid))
                if identifier == "_sessionStart":
                    # Exercise the external codec's sized-int subclass, as on
                    # the device, rather than only a plain Python int fake.
                    return opack.unpack(opack.pack({"_c": {"_sid": 1700}}))[0]
                if identifier == "_sessionStop":
                    return {"_c": {}}
                return {"_ec": 58822, "_ed": "RPErrorDomain", "_c": {}}
        result = await companion.probe_dropin_route(Stream())
        self.assertEqual(result["state_request"]["_ec"], 58822)
        self.assertTrue(result["required_homeid_omitted"])
        self.assertFalse(result["starts_call"])
        self.assertTrue(calls[1][0].endswith(".DropInStateRequest"))
        self.assertEqual(calls[1][1]["p"], {})
        self.assertNotIn("StartDropIn", json.dumps(calls))
        self.assertEqual(set(calls[-1][1]), {"_sid"})
        self.assertEqual(calls[-1][1]["_sid"] >> 32, 1700)
        self.assertEqual(companion.probe_exit_code({"dropin_route": result}), 3)

    async def test_state_timeout_still_stops_logical_session(self):
        stopped = []
        class Stream:
            async def request(self, identifier, content, xid):
                if identifier == "_sessionStart":
                    return {"_c": {"_sid": 23}}
                if identifier == "_sessionStop":
                    stopped.append(content)
                    return {"_c": {}}
                raise asyncio.TimeoutError()
        result = await companion.probe_dropin_route(Stream())
        self.assertEqual(result["error"]["type"], "TimeoutError")
        self.assertEqual(len(stopped), 1)
        self.assertIn("session_stop", result)


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = []
        self.tasks = []

    async def asyncTearDown(self):
        for server in self.servers:
            server.close()
            await server.wait_closed()
        if self.tasks:
            results = await asyncio.gather(*self.tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    raise result

    async def serve(self, handler):
        async def tracked(reader, writer):
            try:
                await handler(reader, writer)
            finally:
                writer.close()
                await writer.wait_closed()
        def start(reader, writer):
            self.tasks.append(asyncio.create_task(tracked(reader, writer)))
        server = await asyncio.start_server(start, "127.0.0.1", 0)
        self.servers.append(server)
        return server.sockets[0].getsockname()[1]

    async def test_fragmented_opack_frames(self):
        payload = opack.pack({"_pd": b"\6\1\2"})
        frame = b"\6" + len(payload).to_bytes(3, "big") + payload
        async def handler(reader, writer):
            for value in frame:
                writer.write(bytes([value]))
                await writer.drain()
        port = await self.serve(handler)
        client = await companion.CompanionStream.connect("127.0.0.1", port, "127.0.0.1")
        try:
            self.assertEqual(await client.receive(), (6, {"_pd": b"\6\1\2"}))
        finally:
            await client.close()

    async def test_oversize_rejected_before_reading_payload(self):
        async def handler(reader, writer):
            writer.write(b"\6" + (companion.MAX_FRAME_SIZE + 1).to_bytes(3, "big"))
            await writer.drain()
        port = await self.serve(handler)
        client = await companion.CompanionStream.connect("127.0.0.1", port, "127.0.0.1")
        try:
            with self.assertRaisesRegex(companion.ProtocolError, "size"):
                await client.receive()
        finally:
            await client.close()

    async def test_full_mutual_authentication_then_encrypted_system_info(self):
        accessory = FakeAccessory()
        async def handler(reader, writer):
            server = companion.CompanionStream(reader, writer)
            frame, m1 = await server.receive()
            self.assertEqual(frame, 5)
            await server.send(6, accessory.m2(m1))
            frame, m3 = await server.receive()
            self.assertEqual(frame, 6)
            self.assertTrue(accessory.check_m3(m3))
            await server.send(6, {"_pd": b"\6\1\4"})
            server.enable_encryption(
                companion.derive_key(accessory.shared, "", "ServerEncrypt-main"),
                companion.derive_key(accessory.shared, "", "ClientEncrypt-main"))
            _, request = await server.receive()
            self.assertEqual(request["_i"], "_systemInfo")
            await server.send(8, {"_t": 1, "_i": "unrelated", "_c": {}})
            await server.send(8, {"_t": 3, "_x": request["_x"], "_c": {
                "model": "AudioAccessory6,1", "_hkID": "private",
                "_stA": ["com.apple.dropin.setup"]}})
        port = await self.serve(handler)
        report = await companion.probe("127.0.0.1", "127.0.0.1", port,
                                       accessory.credentials())
        self.assertTrue(report["authentication_complete"])
        self.assertTrue(report["server_identity"]["server_signature_verified"])
        self.assertEqual(report["stage"], "complete")
        self.assertFalse(report["homepod_microphone_verified"])
        self.assertNotIn("private", json.dumps(report))

    async def test_unknown_identity_fails_at_m4_and_is_not_recording(self):
        accessory = FakeAccessory()
        async def handler(reader, writer):
            server = companion.CompanionStream(reader, writer)
            _, m1 = await server.receive()
            await server.send(6, accessory.m2(m1))
            _, m3 = await server.receive()
            self.assertFalse(accessory.check_m3(m3))
            await server.send(6, {"_pd": b"\6\1\4\7\1\2"})
        port = await self.serve(handler)
        report = await companion.probe("127.0.0.1", "127.0.0.1", port, test_unpaired=True)
        self.assertEqual(report["error"], {"type": "AuthenticationRejected",
                                         "tlv_error": 2, "tlv_state": 4})
        self.assertFalse(report["authentication_complete"])
        self.assertFalse(report["homepod_microphone_verified"])

    async def test_default_stops_after_m2_without_claiming_trust(self):
        accessory = FakeAccessory()
        async def handler(reader, writer):
            server = companion.CompanionStream(reader, writer)
            _, m1 = await server.receive()
            await server.send(6, accessory.m2(m1))
            self.assertEqual(await reader.read(), b"")
        port = await self.serve(handler)
        report = await companion.probe("127.0.0.1", "127.0.0.1", port)
        self.assertEqual(report["stage"], "pair_verify_m2_received")
        self.assertFalse(report["authentication_complete"])

    async def test_system_info_requires_authenticated_stream(self):
        client = companion.CompanionStream(None, None)
        with self.assertRaisesRegex(companion.ProtocolError, "Authentication"):
            await client.system_info()

    async def public_server(self, reader, writer, bad_proof=False):
        context = SRPContext("Pair-Setup", "public", prime=constants.PRIME_3072,
            generator=constants.PRIME_3072_GEN, hash_func=hashlib.sha512, bits_salt=128)
        username, verifier, salt = context.get_user_data_triplet()
        session = SRPServerSession(SRPContext(username, prime=constants.PRIME_3072,
            generator=constants.PRIME_3072_GEN, hash_func=hashlib.sha512), verifier)
        server = companion.CompanionStream(reader, writer)
        frame, message = await server.receive()
        self.assertEqual((frame, message["_pwTy"]), (3, 0))
        self.assertEqual(read_tlv(message["_pd"])[TlvValue.Flags], b"\x10")
        await server.send(4, {"_pd": write_tlv({TlvValue.SeqNo: b"\2",
            TlvValue.Salt: binascii.unhexlify(salt),
            TlvValue.PublicKey: binascii.unhexlify(session.public)})})
        frame, message = await server.receive()
        self.assertEqual(frame, 4)
        fields = companion.pairing_data(message, 3)
        session.process(binascii.hexlify(fields[TlvValue.PublicKey]).decode(), salt)
        self.assertTrue(session.verify_proof(binascii.hexlify(fields[TlvValue.Proof])))
        proof = binascii.unhexlify(session.key_proof_hash)
        if bad_proof:
            proof = proof[:-1] + bytes([proof[-1] ^ 1])
        await server.send(4, {"_pd": write_tlv({TlvValue.SeqNo: b"\4", TlvValue.Proof: proof})})
        if bad_proof:
            self.assertEqual(await reader.read(), b"")
            return
        shared = binascii.unhexlify(session.key)
        server.enable_encryption(companion.derive_key(shared, "", "ServerEncrypt-main"),
                                  companion.derive_key(shared, "", "ClientEncrypt-main"))
        # The next frame must be encrypted OPACK, never persistent pairing M5.
        frame, request = await server.receive()
        self.assertEqual((frame, request["_i"]), (8, "_systemInfo"))
        await server.send(8, {"_t": 3, "_x": request["_x"], "_c": {
            "model": "AudioAccessory6,1"}})

    async def test_public_transient_has_encryption_but_no_home_identity(self):
        port = await self.serve(self.public_server)
        report = await companion.probe("127.0.0.1", "127.0.0.1", port, public_transient=True)
        self.assertTrue(report["authentication_complete"])
        self.assertTrue(report["server_srp_proof_verified"])
        self.assertFalse(report["trusted_controller_identity"])
        self.assertFalse(report["persistent_pairing_created"])
        self.assertFalse(report["homepod_microphone_verified"])
        self.assertEqual(report["stage"], "public_guest_complete")

    async def test_public_wrong_server_proof_never_enables_encryption(self):
        async def handler(reader, writer):
            await self.public_server(reader, writer, bad_proof=True)
        port = await self.serve(handler)
        report = await companion.probe("127.0.0.1", "127.0.0.1", port, public_transient=True)
        self.assertFalse(report["authentication_complete"])
        self.assertEqual(report["error"]["message"], "Server SRP proof does not match")

    async def test_authentication_modes_cannot_be_combined(self):
        with self.assertRaises(ValueError):
            await companion.probe("127.0.0.1", "127.0.0.1", 1,
                                  test_unpaired=True, public_transient=True)


if __name__ == "__main__":
    unittest.main()
