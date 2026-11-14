"""Offline DropIn message dictionaries recovered during firmware research.

This is NOT a microphone backend or an authenticated Rapport/IDS client. These
functions neither open sockets nor serialize an OPACK frame. Constants and keys
were checked against AudioAccessory5,1 27.2 (24K122); this is a different model
from the physical second-generation HomePod. See docs/homepod-dropin-research.md.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from uuid import UUID, uuid4


RAPPORT_SERVICE_TYPE = "com.apple.dropin.setup"
RAPPORT_REQUEST_ID = "com.apple.dropin.setup.request."
RAPPORT_EVENT_ID = "com.apple.dropin.setup.event."
IDS_SERVICE = "com.apple.private.alloy.dropin.setup"


class MessageError(ValueError):
    """The dictionary does not satisfy the recovered message schema."""


class SessionType(IntEnum):
    SOUND_ANALYSIS = 0
    USER_INITIATED = 1


def _uuid(value, field):
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise MessageError(f"{field} must be a UUID string")
    try:
        return UUID(value)
    except (ValueError, AttributeError) as exc:
        raise MessageError(f"{field} must be a UUID string") from exc


def _dictionary(value):
    if not isinstance(value, Mapping):
        raise MessageError("Expected a message dictionary")
    if any(not isinstance(key, str) for key in value):
        raise MessageError("Message dictionary keys must be strings")
    return value


@dataclass(frozen=True)
class StartRequest:
    home_identifier: UUID
    session_type: SessionType = SessionType.USER_INITIATED

    def __post_init__(self):
        object.__setattr__(self, "home_identifier",
                           _uuid(self.home_identifier, "homeid"))
        if isinstance(self.session_type, bool) or not isinstance(self.session_type, int):
            raise MessageError("sessiontype must be integer 0 or 1")
        try:
            value = SessionType(self.session_type)
        except ValueError as exc:
            raise MessageError("Unknown sessiontype") from exc
        object.__setattr__(self, "session_type", value)

    def to_dictionary(self):
        return {"homeid": str(self.home_identifier).upper(),
                "sessiontype": int(self.session_type)}

    @classmethod
    def from_dictionary(cls, value):
        value = _dictionary(value)
        if "homeid" not in value or "sessiontype" not in value:
            raise MessageError("Start request requires homeid and sessiontype")
        return cls(value["homeid"], value["sessiontype"])


@dataclass(frozen=True)
class Envelope:
    identifier: UUID
    payload_name: str
    payload: dict

    def __post_init__(self):
        object.__setattr__(self, "identifier", _uuid(self.identifier, "mid"))
        if not isinstance(self.payload_name, str) or not self.payload_name:
            raise MessageError("pid must be a nonempty payload-name string")
        object.__setattr__(self, "payload", dict(_dictionary(self.payload)))

    def to_dictionary(self):
        return {"mid": str(self.identifier).upper(),
                "pid": self.payload_name, "p": dict(self.payload)}

    @classmethod
    def create(cls, payload_name, payload):
        # The payload name is supplied explicitly: no unverified class-name or
        # Rapport framing convention is inferred from Swift reflection names.
        return cls(uuid4(), payload_name, payload)

    @classmethod
    def from_dictionary(cls, value):
        value = _dictionary(value)
        if not {"mid", "pid", "p"}.issubset(value):
            raise MessageError("Envelope requires mid, pid and p")
        return cls(value["mid"], value["pid"], value["p"])


@dataclass(frozen=True)
class StartResponse:
    session_identifier: UUID
    home_identifier: UUID
    device_pseudonym: str
    device_pseudonym_expiration: datetime
    error_archive: bytes | None = None

    @classmethod
    def from_dictionary(cls, value):
        value = _dictionary(value)
        required = {"sessionid", "homeid", "devicePseudonym", "devicePseudonymExpiration"}
        if not required.issubset(value):
            raise MessageError("Start response is missing required fields")
        pseudonym = value["devicePseudonym"]
        if not isinstance(pseudonym, str):
            raise MessageError("devicePseudonym must be a string")
        expiration = value["devicePseudonymExpiration"]
        if not isinstance(expiration, datetime):
            raise MessageError("devicePseudonymExpiration must be a decoded Foundation date")
        error = value.get("error")
        if error is not None and not isinstance(error, bytes):
            raise MessageError("error must be opaque archived-error bytes")
        # Never deserialize an NSKeyedArchiver object or treat this control
        # response as evidence of an active microphone or received media.
        return cls(_uuid(value["sessionid"], "sessionid"),
                   _uuid(value["homeid"], "homeid"), pseudonym, expiration, error)
