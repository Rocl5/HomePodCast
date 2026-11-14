"""Constructed offline schema fixtures, not successful HomePod recordings."""

from pathlib import Path
from datetime import datetime, timezone
import plistlib
import sys
import unittest
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import homepod_dropin_messages as messages


HOME = "E43827D2-32E1-4F23-86A4-B10F1B2C3D4E"
MID = "0B3FBB54-5812-4A80-A507-0F37735E8E9A"


class StartRequestTests(unittest.TestCase):
    def test_user_initiated_uses_recovered_keys(self):
        self.assertEqual(messages.StartRequest(HOME).to_dictionary(),
                         {"homeid": HOME, "sessiontype": 1})

    def test_sound_analysis_value_is_distinct(self):
        request = messages.StartRequest(HOME, messages.SessionType.SOUND_ANALYSIS)
        self.assertEqual(request.to_dictionary()["sessiontype"], 0)

    def test_lowercase_uuid_is_canonicalized(self):
        self.assertEqual(messages.StartRequest(HOME.lower()).to_dictionary()["homeid"], HOME)

    def test_uuid_instance_is_supported(self):
        self.assertEqual(messages.StartRequest(UUID(HOME)).home_identifier, UUID(HOME))

    def test_dictionary_roundtrip(self):
        request = messages.StartRequest.from_dictionary({"homeid": HOME, "sessiontype": 1})
        self.assertIs(request.session_type, messages.SessionType.USER_INITIATED)

    def test_rejects_invalid_session_types(self):
        for value in (True, False, 2, -1, "1", 1.0, None):
            with self.subTest(value=value), self.assertRaises(messages.MessageError):
                messages.StartRequest(HOME, value)

    def test_rejects_invalid_home_identifiers(self):
        for value in ("192.168.1.18", "", 12, None, b"uuid"):
            with self.subTest(value=value), self.assertRaises(messages.MessageError):
                messages.StartRequest(value)

    def test_missing_fields_fail(self):
        for value in ({}, {"homeid": HOME}, {"sessiontype": 1}):
            with self.subTest(value=value), self.assertRaises(messages.MessageError):
                messages.StartRequest.from_dictionary(value)

    def test_non_dictionary_fails(self):
        with self.assertRaises(messages.MessageError):
            messages.StartRequest.from_dictionary([("homeid", HOME)])

    def test_additional_fields_do_not_replace_required_fields(self):
        request = messages.StartRequest.from_dictionary(
            {"homeid": HOME, "sessiontype": 1, "future": 42})
        self.assertEqual(request.to_dictionary(), {"homeid": HOME, "sessiontype": 1})


class EnvelopeTests(unittest.TestCase):
    def test_recovered_envelope_keys(self):
        envelope = messages.Envelope(MID, "explicit-fixture-name", {"homeid": HOME})
        self.assertEqual(envelope.to_dictionary(),
                         {"mid": MID, "pid": "explicit-fixture-name", "p": {"homeid": HOME}})

    def test_dictionary_roundtrip(self):
        fixture = {"mid": MID, "pid": "fixture", "p": {"sessiontype": 1}}
        self.assertEqual(messages.Envelope.from_dictionary(fixture).to_dictionary(), fixture)

    def test_nested_dictionary_is_not_serialized_as_json_text(self):
        envelope = messages.Envelope.create("fixture", {"sessiontype": 1})
        self.assertIsInstance(envelope.to_dictionary()["p"], dict)

    def test_binary_plist_fixture_roundtrip(self):
        # Plist is only an offline fixture container, not the Rapport encoding.
        fixture = messages.Envelope(MID, "fixture", {"opaque": b"archive"}).to_dictionary()
        decoded = plistlib.loads(plistlib.dumps(fixture, fmt=plistlib.FMT_BINARY))
        self.assertEqual(messages.Envelope.from_dictionary(decoded).to_dictionary(), fixture)

    def test_create_generates_distinct_message_ids(self):
        first = messages.Envelope.create("fixture", {})
        second = messages.Envelope.create("fixture", {})
        self.assertNotEqual(first.identifier, second.identifier)

    def test_missing_fields_fail(self):
        fixture = {"mid": MID, "pid": "fixture", "p": {}}
        for field in fixture:
            value = {key: item for key, item in fixture.items() if key != field}
            with self.subTest(field=field), self.assertRaises(messages.MessageError):
                messages.Envelope.from_dictionary(value)

    def test_invalid_message_id_fails(self):
        with self.assertRaises(messages.MessageError):
            messages.Envelope("not-a-uuid", "fixture", {})

    def test_invalid_payload_name_fails(self):
        for name in ("", None, 7, b"fixture"):
            with self.subTest(name=name), self.assertRaises(messages.MessageError):
                messages.Envelope(MID, name, {})

    def test_payload_must_be_dictionary(self):
        for payload in (None, [], b"{}", "{}", {1: "value"}):
            with self.subTest(payload=payload), self.assertRaises(messages.MessageError):
                messages.Envelope(MID, "fixture", payload)

    def test_outer_dictionary_must_have_string_keys(self):
        with self.assertRaises(messages.MessageError):
            messages.Envelope.from_dictionary({1: "value"})

    def test_top_level_payload_copy(self):
        payload = {"value": 1}
        envelope = messages.Envelope(MID, "fixture", payload)
        payload["value"] = 2
        exported = envelope.to_dictionary()
        exported["p"]["value"] = 3
        self.assertEqual(envelope.payload["value"], 1)


class StartResponseTests(unittest.TestCase):
    def fixture(self):
        return {"sessionid": MID, "homeid": HOME, "devicePseudonym": "pseud:fixture",
                "devicePseudonymExpiration": datetime(2026, 11, 14, tzinfo=timezone.utc)}

    def test_recovered_response_fields(self):
        response = messages.StartResponse.from_dictionary(self.fixture())
        self.assertEqual(response.session_identifier, UUID(MID))
        self.assertEqual(response.home_identifier, UUID(HOME))
        self.assertIsNone(response.error_archive)

    def test_error_archive_stays_opaque(self):
        fixture = self.fixture()
        fixture["error"] = b"untrusted opaque archive"
        response = messages.StartResponse.from_dictionary(fixture)
        self.assertEqual(response.error_archive, fixture["error"])

    def test_missing_response_fields_fail(self):
        fixture = self.fixture()
        for field in fixture:
            value = {key: item for key, item in fixture.items() if key != field}
            with self.subTest(field=field), self.assertRaises(messages.MessageError):
                messages.StartResponse.from_dictionary(value)

    def test_rejects_unverified_date_encodings(self):
        for value in ("2026-11-14", 0, 123.5, None):
            fixture = self.fixture()
            fixture["devicePseudonymExpiration"] = value
            with self.subTest(value=value), self.assertRaises(messages.MessageError):
                messages.StartResponse.from_dictionary(fixture)

    def test_rejects_unverified_error_encodings(self):
        fixture = self.fixture()
        fixture["error"] = {"description": "not an archive"}
        with self.assertRaises(messages.MessageError):
            messages.StartResponse.from_dictionary(fixture)

    def test_response_uuid_is_validated(self):
        for field in ("sessionid", "homeid"):
            fixture = self.fixture()
            fixture[field] = "not-a-uuid"
            with self.subTest(field=field), self.assertRaises(messages.MessageError):
                messages.StartResponse.from_dictionary(fixture)

    def test_response_pseudonym_is_validated(self):
        fixture = self.fixture()
        fixture["devicePseudonym"] = 123
        with self.assertRaises(messages.MessageError):
            messages.StartResponse.from_dictionary(fixture)


if __name__ == "__main__":
    unittest.main()
