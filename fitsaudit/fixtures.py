"""Deterministic FITS file builders used by the unit tests and by the
``verify`` one-shot service to produce smoke-test fixtures.

The builders reuse the auditor's own checksum routines, and the
committed reference file ``tests/data/valid_astropy.fits`` (produced by
astropy) guards against a self-consistent but non-standard encoder.
"""

from __future__ import annotations

import struct

from .core import (
    BLOCK_SIZE,
    CARD_SIZE,
    char_encode,
    sum32_be,
    UINT32_MAX,
)


def card(keyword, value=None, comment=None):
    """Build one 80-byte card.  ``value`` may be bool/int/str or None
    (comment-style card)."""
    if value is None:
        text = "%-8s" % keyword
    elif isinstance(value, bool):
        text = "%-8s= %20s" % (keyword, "T" if value else "F")
    elif isinstance(value, int):
        text = "%-8s= %20d" % (keyword, value)
    elif isinstance(value, str):
        text = "%-8s= '%-8s'" % (keyword, value)
    else:  # pragma: no cover - defensive
        raise TypeError(value)
    if comment:
        text += " / " + comment
    raw = text.encode("ascii")
    if len(raw) > CARD_SIZE:  # pragma: no cover - defensive
        raise ValueError("card too long: %r" % text)
    return raw.ljust(CARD_SIZE, b" ")


END_CARD = b"END".ljust(CARD_SIZE, b" ")


def _pad(block, fill):
    return block + fill * ((-len(block)) % BLOCK_SIZE)


def header_block(cards):
    return _pad(b"".join(cards) + END_CARD, b" ")


def data_block(data):
    return _pad(data, b"\x00")


def _finalize_checksums(cards, data, datasum_as_string=False):
    """Append DATASUM/CHECKSUM cards with correct values to *cards*."""
    datasum = sum32_be(data)
    if datasum_as_string:
        cards.append(card("DATASUM", str(datasum)))
    else:
        cards.append(card("DATASUM", datasum))
    cards.append(card("CHECKSUM", "0" * 16))
    zeroed = header_block(cards)
    total = sum32_be(zeroed, datasum)
    cards[-1] = card("CHECKSUM", char_encode(~total & UINT32_MAX))
    return cards


def primary_hdu(data=b"", bitpix=8, axes=(), checksums=False,
                extra_cards=()):
    """Build a primary HDU.  ``axes`` defaults to match ``data``."""
    cards = [card("SIMPLE", True), card("BITPIX", bitpix),
             card("NAXIS", len(axes))]
    cards += [card("NAXIS%d" % (i + 1), n) for i, n in enumerate(axes)]
    cards += list(extra_cards)
    if checksums:
        _finalize_checksums(cards, data)
    return header_block(cards) + data_block(data)


def image_hdu(data=b"", bitpix=8, axes=(), checksums=True,
              datasum_as_string=False, extra_cards=()):
    """Build an IMAGE extension HDU."""
    element_bytes = abs(bitpix) // 8
    elements = 1
    for n in axes:
        elements *= n
    expected = elements * element_bytes
    if len(data) != expected:  # pragma: no cover - defensive
        raise ValueError("data length %d != %d from axes" % (len(data), expected))
    cards = [card("XTENSION", "IMAGE"), card("BITPIX", bitpix),
             card("NAXIS", len(axes))]
    cards += [card("NAXIS%d" % (i + 1), n) for i, n in enumerate(axes)]
    cards += [card("PCOUNT", 0), card("GCOUNT", 1)]
    cards += list(extra_cards)
    if checksums:
        _finalize_checksums(cards, data, datasum_as_string)
    return header_block(cards) + data_block(data)


def build_valid_file(n_extensions=2, checksums=True):
    """A valid file: primary HDU (no data) + ``n_extensions`` IMAGE HDUs."""
    parts = [primary_hdu(checksums=checksums)]
    for i in range(n_extensions):
        vals = struct.pack(
            ">%dh" % (6 * (i + 2)),
            *range(-100, 6 * (i + 2) - 100),
        )
        parts.append(image_hdu(vals, bitpix=16, axes=(6, i + 2),
                               checksums=checksums))
    return b"".join(parts)


def build_max_hdus_file():
    """A valid file with the maximum 16 HDUs (primary + 15 extensions)."""
    parts = [primary_hdu(checksums=True)]
    for _ in range(15):
        parts.append(image_hdu(b"\x01\x02\x03\x04", bitpix=8, axes=(4,),
                               checksums=True))
    return b"".join(parts)


def find_card(blob, keyword, occurrence=0):
    """Return the absolute offset of the ``occurrence``-th card named
    ``keyword`` in *blob* (scanning 80-byte card boundaries)."""
    key = keyword.encode("ascii").ljust(8, b" ")
    hits = 0
    for off in range(0, len(blob) - CARD_SIZE + 1, CARD_SIZE):
        if blob[off : off + 8] == key:
            if hits == occurrence:
                return off
            hits += 1
    raise ValueError("card %r not found" % keyword)


def corrupt_datasum(blob, occurrence=0):
    """Return a copy of *blob* with the stored DATASUM value bumped by 1.

    Handles both the integer form and astropy's quoted-string form while
    preserving the card layout, so the result stays well-formed but
    wrong.
    """
    off = find_card(blob, "DATASUM", occurrence)
    image = blob[off : off + CARD_SIZE]
    field = image[10:]
    if b"'" in field:
        q0 = field.index(b"'")
        q1 = field.index(b"'", q0 + 1)
        digits = field[q0 + 1 : q1].strip()
        value = int(digits) + 1
        replacement = (b"'%-" + str(q1 - q0 - 1).encode("ascii") + b"s'")
        new_field = field[:q0] + replacement % str(value).encode("ascii") \
            + field[q1 + 1 :]
    else:
        token = field.split(b"/", 1)[0]
        value = int(token.strip()) + 1
        new_token = b"%20d" % value
        new_field = new_token + field[len(token):]
    new_image = (image[:10] + new_field)[:CARD_SIZE].ljust(CARD_SIZE, b" ")
    return blob[:off] + new_image + blob[off + CARD_SIZE :]


def corrupt_checksum(blob, occurrence=0):
    """Return a copy of *blob* with one CHECKSUM character altered."""
    off = find_card(blob, "CHECKSUM", occurrence)
    image = bytearray(blob[off : off + CARD_SIZE])
    q0 = image.index(b"'") + 1
    image[q0] = 0x41 if image[q0] != 0x41 else 0x42  # 'A' <-> 'B'
    return blob[:off] + bytes(image) + blob[off + CARD_SIZE :]


def corrupt_data_byte(blob, occurrence=1):
    """Flip one byte inside the data section of HDU ``occurrence``.

    Only valid for files built by :func:`build_valid_file` (primary HDU
    has no data, so HDU 1's data starts at 2 * 2880).
    """
    off = 2 * BLOCK_SIZE
    flipped = bytes([blob[off] ^ 0xFF])
    return blob[:off] + flipped + blob[off + 1 :]
