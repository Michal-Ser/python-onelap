"""Minimal FIT container validation (header only, no record parsing)."""

import struct

from .exceptions import NotFitError


def validate_fit(data):
    """Return ``data`` unchanged if it looks like a complete FIT file.

    Checks: more than 14 bytes, header size byte is 12 or 14, bytes 8-11 are
    ``.FIT`` and the file is at least as long as the header declares
    (header + data + 2-byte CRC).
    """
    if not isinstance(data, (bytes, bytearray)):
        raise NotFitError("FIT data must be bytes")
    if len(data) <= 14:
        raise NotFitError("file too short to be FIT (%d bytes)" % len(data))
    header_size = data[0]
    if header_size not in (12, 14):
        raise NotFitError("bad FIT header size byte: %d" % header_size)
    if bytes(data[8:12]) != b".FIT":
        raise NotFitError("missing .FIT signature")
    data_size = struct.unpack_from("<I", data, 4)[0]
    if len(data) < header_size + data_size + 2:
        raise NotFitError("FIT file is truncated")
    return bytes(data)
