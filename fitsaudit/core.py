"""Strict FITS file auditor.

Validates a FITS file HDU by HDU before archival:

* header cards are 80-byte ASCII, terminated by an ``END`` card and the
  header is padded with ASCII spaces to a multiple of 2880 bytes;
* the file consists of one primary HDU (``SIMPLE = T``) followed by at
  most 15 ``IMAGE`` extensions (16 HDUs in total);
* the data-section length is determined consistently by ``BITPIX``,
  ``NAXIS``, the ``NAXISn`` axis lengths, ``PCOUNT`` and ``GCOUNT`` and
  the data padding (to 2880 bytes) must contain only zero bytes;
* ``DATASUM`` / ``CHECKSUM`` cards, when present, must match the values
  computed from the file bytes (FITS checksum convention, compatible
  with CFITSIO and astropy).

Any header-field conflict, out-of-bounds data, trailing bytes or
checksum mismatch rejects the whole file.  The audit deterministically
reports the earliest failing HDU, a stable reason code and a locatable
byte offset.
"""

from __future__ import annotations

import re
import struct

BLOCK_SIZE = 2880
CARD_SIZE = 80
MAX_FILE_BYTES = 16 * 1024 * 1024  # 16 MiB
MAX_HDUS = 16  # 1 primary HDU + at most 15 IMAGE extensions
MAX_NAXIS = 999
UINT32_MAX = 0xFFFFFFFF
VALID_BITPIX = frozenset((8, 16, 32, 64, -32, -64))

# HDU types
TYPE_PRIMARY = "PRIMARY"
TYPE_IMAGE = "IMAGE"

# DATASUM / CHECKSUM verdicts
VERDICT_VALID = "VALID"
VERDICT_ABSENT = "ABSENT"

# Overall conclusions
ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"


class Reason:
    """Stable machine-readable rejection reason codes."""

    EMPTY_FILE = "EMPTY_FILE"
    TRUNCATED_HEADER = "TRUNCATED_HEADER"
    INVALID_CARD = "INVALID_CARD"
    NONSPACE_HEADER_PADDING = "NONSPACE_HEADER_PADDING"
    MISSING_KEYWORD = "MISSING_KEYWORD"
    DUPLICATE_KEYWORD = "DUPLICATE_KEYWORD"
    INVALID_KEYWORD_VALUE = "INVALID_KEYWORD_VALUE"
    KEYWORD_CONFLICT = "KEYWORD_CONFLICT"
    UNSUPPORTED_EXTENSION = "UNSUPPORTED_EXTENSION"
    UNSUPPORTED_FEATURE = "UNSUPPORTED_FEATURE"
    TRUNCATED_DATA = "TRUNCATED_DATA"
    NONZERO_DATA_PADDING = "NONZERO_DATA_PADDING"
    DATASUM_MISMATCH = "DATASUM_MISMATCH"
    CHECKSUM_MISMATCH = "CHECKSUM_MISMATCH"
    TOO_MANY_HDUS = "TOO_MANY_HDUS"
    TRAILING_BYTES = "TRAILING_BYTES"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    # Materialization-only reason (never produced by audit_bytes).
    INSUFFICIENT_HEADER_SPACE = "INSUFFICIENT_HEADER_SPACE"


class AuditFailure(Exception):
    """The earliest audit failure; aborts the audit of the whole file."""

    __slots__ = ("hdu", "reason", "offset", "message", "details")

    def __init__(self, hdu, reason, offset, message, details=None):
        super().__init__(message)
        self.hdu = hdu
        self.reason = reason
        self.offset = offset
        self.message = message
        self.details = details or {}

    def as_dict(self):
        out = {
            "hdu": self.hdu,
            "reason": self.reason,
            "offset": self.offset,
            "message": self.message,
        }
        if self.details:
            out["details"] = self.details
        return out


# ---------------------------------------------------------------------------
# FITS checksum convention (1's-complement sums and the ASCII encoding),
# cross-validated against astropy.io.fits.
# ---------------------------------------------------------------------------


def sum32_be(buf, seed=0):
    """1's-complement sum of *buf* taken as big-endian uint32 words.

    A trailing partial word is zero-padded, which does not change the
    sum.  The result is folded into 32 bits (all-ones is "negative
    zero", as required by the FITS checksum convention).
    """
    pad = (-len(buf)) % 4
    if pad:
        buf = buf + b"\x00" * pad
    total = seed
    step = 1 << 18  # 256 KiB per unpack keeps memory bounded
    for off in range(0, len(buf), step):
        chunk = buf[off : off + step]
        total += sum(struct.unpack(">%dI" % (len(chunk) // 4), chunk))
        total = (total & UINT32_MAX) + (total >> 32)
    while total > UINT32_MAX:
        total = (total & UINT32_MAX) + (total >> 32)
    return total


# ASCII punctuation excluded from encoded CHECKSUM strings.
_ENCODE_EXCLUDE = (0x3A, 0x3B, 0x3C, 0x3D, 0x3E, 0x3F, 0x40,
                   0x5B, 0x5C, 0x5D, 0x5E, 0x5F, 0x60)


def _encode_byte(byte):
    quotient = byte // 4 + 0x30  # ASCII '0'
    remainder = byte % 4
    ch = [quotient + remainder, quotient, quotient, quotient]
    check = True
    while check:
        check = False
        for x in _ENCODE_EXCLUDE:
            for j in (0, 2):
                if ch[j] == x or ch[j + 1] == x:
                    ch[j] += 1
                    ch[j + 1] -= 1
                    check = True
    return ch


def char_encode(value):
    """Encode a 32-bit checksum as the canonical 16-character string."""
    value &= UINT32_MAX
    asc = [0] * 16
    for i in range(4):
        byte = (value >> ((3 - i) * 8)) & 0xFF
        ch = _encode_byte(byte)
        for j in range(4):
            asc[4 * j + i] = ch[j]
    # The 16 characters are rotated one position to the right.
    return "".join(chr(asc[(i + 15) % 16]) for i in range(16))


def char_decode(text):
    """Decode a 16-character CHECKSUM string; ``None`` if not decodable."""
    if len(text) != 16:
        return None
    try:
        raw = [ord(c) for c in text]
    except TypeError:  # pragma: no cover - defensive
        return None
    asc = [raw[(i + 1) % 16] for i in range(16)]  # inverse rotation
    out = 0
    for i in range(4):
        byte = asc[i] + asc[4 + i] + asc[8 + i] + asc[12 + i] - 4 * 0x30
        if byte < 0 or byte > 255:
            return None
        out = (out << 8) | byte
    return out


# ---------------------------------------------------------------------------
# Header card parsing.
# ---------------------------------------------------------------------------

_KEYWORD_RE = re.compile(rb"[A-Z0-9_\-]+\Z")
_AXIS_RE = re.compile(r"NAXIS(\d+)\Z")
_INT_RE = re.compile(rb"[+-]?\d+\Z")

# Keywords that must be unique and carry a value; everything else
# (COMMENT, HISTORY, observatory keywords, ...) is passed through.
_STRUCTURAL = frozenset(
    ("SIMPLE", "XTENSION", "BITPIX", "NAXIS", "PCOUNT", "GCOUNT",
     "DATASUM", "CHECKSUM", "GROUPS")
)


class Card:
    """One 80-byte header card."""

    __slots__ = ("offset", "keyword", "has_value", "image")

    def __init__(self, offset, keyword, has_value, image):
        self.offset = offset
        self.keyword = keyword
        self.has_value = has_value
        self.image = image


def _fail_card_ascii(hdu, offset, byte):
    raise AuditFailure(
        hdu, Reason.INVALID_CARD, offset,
        "non-ASCII byte 0x%02X in header card at offset %d "
        "(cards must be 80-byte ASCII)" % (byte, offset),
        {"byte": "0x%02X" % byte},
    )


def _parse_keyword(hdu, image, offset):
    raw = image[:8]
    if raw == b" " * 8:
        return ""
    stripped = raw.rstrip(b" ")
    if raw[0:1] == b" " or raw != stripped.ljust(8, b" ") \
            or not _KEYWORD_RE.match(stripped):
        raise AuditFailure(
            hdu, Reason.INVALID_CARD, offset,
            "malformed keyword field %r in card at offset %d" % (raw, offset),
        )
    return stripped.decode("ascii")


def _read_header(data, start, hdu):
    """Read one header; returns (cards, end_card_offset, content_end).

    ``content_end`` is the offset just past the END card; the padded
    header end is derived from it by the caller.  Blank card slots
    following the END card may additionally hold ``DATASUM`` /
    ``CHECKSUM`` cards materialized in place of header padding; such
    cards are returned like ordinary cards and validated as part of the
    HDU.
    """
    cards = []
    pos = start
    while True:
        if pos + CARD_SIZE > len(data):
            raise AuditFailure(
                hdu, Reason.TRUNCATED_HEADER, start,
                "header of HDU %d starting at offset %d is not terminated "
                "by an END card before end of file" % (hdu, start),
                {"headerOffset": start, "fileSize": len(data)},
            )
        image = data[pos : pos + CARD_SIZE]
        lo = min(image)
        hi = max(image)
        if lo < 0x20 or hi > 0x7E:
            for i, b in enumerate(image):
                if b < 0x20 or b > 0x7E:
                    _fail_card_ascii(hdu, pos + i, b)
        keyword = _parse_keyword(hdu, image, pos)
        has_value = False
        if image[8:9] == b"=":
            if image[9:10] != b" ":
                raise AuditFailure(
                    hdu, Reason.INVALID_CARD, pos + 8,
                    "value indicator at offset %d must be '= '" % (pos + 8),
                )
            has_value = True
        if keyword == "END":
            if has_value or image[8:] != b" " * 72:
                raise AuditFailure(
                    hdu, Reason.INVALID_CARD, pos,
                    "END card at offset %d must be blank after the keyword"
                    % pos,
                )
            end_offset = pos
            content_end = pos + CARD_SIZE
            break
        cards.append(Card(pos, keyword, has_value, image))
        pos += CARD_SIZE

    # The FITS standard ends header content at the END card; the rest of
    # the last 2880-byte block is space padding.  A checksum materialized
    # by this service is allowed to occupy what would otherwise be a
    # blank card slot there (it never shifts the END card or any HDU
    # boundary).  Recognize those slots here so they are verified like
    # any other DATASUM/CHECKSUM card; any other non-space content is
    # left untouched here and reported as bad padding by the caller,
    # exactly as before.
    for slot in range(content_end,
                      start + _round_up(content_end - start, BLOCK_SIZE),
                      CARD_SIZE):
        if slot + CARD_SIZE > len(data):
            break
        image = data[slot : slot + CARD_SIZE]
        if image == b" " * CARD_SIZE:
            continue
        if image[:8] not in (b"DATASUM ", b"CHECKSUM"):
            continue
        # Only a well-formed printable-ASCII value card ('= ' in
        # columns 9-10) is recognized as a materialized checksum card;
        # anything else that merely starts with these letters stays
        # padding and is reported as such by the caller, preserving the
        # historic audit verdict byte for byte.
        if image[8:10] != b"= " or min(image) < 0x20 or max(image) > 0x7E:
            continue
        keyword = image[:8].rstrip(b" ").decode("ascii")
        cards.append(Card(slot, keyword, True, image))
    return cards, end_offset, content_end


def _parse_integer(card, hdu, keyword):
    field = card.image[10:]
    if b"'" in field:
        raise AuditFailure(
            hdu, Reason.INVALID_KEYWORD_VALUE, card.offset,
            "keyword %s at offset %d must have an integer value, not a "
            "string" % (keyword, card.offset),
        )
    token = field.split(b"/", 1)[0].strip()
    if not _INT_RE.match(token):
        raise AuditFailure(
            hdu, Reason.INVALID_KEYWORD_VALUE, card.offset,
            "keyword %s at offset %d has a non-integer value"
            % (keyword, card.offset),
        )
    return int(token)


def _parse_logical(card, hdu, keyword):
    token = card.image[10:].split(b"/", 1)[0].strip()
    if token == b"T":
        return True
    if token == b"F":
        return False
    raise AuditFailure(
        hdu, Reason.INVALID_KEYWORD_VALUE, card.offset,
        "keyword %s at offset %d must have logical value T or F"
        % (keyword, card.offset),
    )


def _parse_string(card, hdu, keyword):
    """Parse a quoted string value; returns (text, quote_start, quote_end).

    The quote positions are card-relative indices of the opening and
    closing quote characters.
    """
    field = card.image[10:]
    i = 0
    while i < len(field) and field[i : i + 1] == b" ":
        i += 1
    if i >= len(field) or field[i : i + 1] != b"'":
        raise AuditFailure(
            hdu, Reason.INVALID_KEYWORD_VALUE, card.offset,
            "keyword %s at offset %d must have a quoted string value"
            % (keyword, card.offset),
        )
    out = bytearray()
    j = i + 1
    while True:
        if j >= len(field):
            raise AuditFailure(
                hdu, Reason.INVALID_KEYWORD_VALUE, card.offset,
                "unterminated string value of keyword %s at offset %d"
                % (keyword, card.offset),
            )
        c = field[j]
        if c == 0x27:  # single quote
            if j + 1 < len(field) and field[j + 1] == 0x27:
                out.append(0x27)
                j += 2
                continue
            break
        out.append(c)
        j += 1
    rest = field[j + 1 :].strip()
    if rest and not rest.startswith(b"/"):
        raise AuditFailure(
            hdu, Reason.INVALID_KEYWORD_VALUE, card.offset,
            "garbage after string value of keyword %s at offset %d"
            % (keyword, card.offset),
        )
    return out.decode("ascii"), 10 + i, 10 + j


# ---------------------------------------------------------------------------
# Per-HDU structural validation.
# ---------------------------------------------------------------------------


def _require_value_card(card, hdu):
    if not card.has_value:
        raise AuditFailure(
            hdu, Reason.INVALID_CARD, card.offset,
            "keyword %s at offset %d must be a value card ('= ' in "
            "columns 9-10)" % (card.keyword, card.offset),
        )


def _validate_structure(cards, hdu, end_offset):
    """Validate mandatory keywords; returns a dict of structural values."""
    seen = {}
    axes = {}
    for card in cards:
        kw = card.keyword
        if not kw or kw in ("COMMENT", "HISTORY"):
            continue
        axis = _AXIS_RE.match(kw)
        if axis is not None:
            digits = axis.group(1)
            if str(int(digits)) != digits:
                raise AuditFailure(
                    hdu, Reason.INVALID_CARD, card.offset,
                    "non-canonical axis keyword %s at offset %d"
                    % (kw, card.offset),
                )
            n = int(digits)
            _require_value_card(card, hdu)
            if n in axes:
                raise AuditFailure(
                    hdu, Reason.DUPLICATE_KEYWORD, card.offset,
                    "duplicate keyword NAXIS%d at offset %d (first at "
                    "offset %d)" % (n, card.offset, axes[n].offset),
                    {"keyword": "NAXIS%d" % n},
                )
            axes[n] = card
            continue
        if kw in _STRUCTURAL:
            _require_value_card(card, hdu)
            if kw in seen:
                raise AuditFailure(
                    hdu, Reason.DUPLICATE_KEYWORD, card.offset,
                    "duplicate keyword %s at offset %d (first at offset %d)"
                    % (kw, card.offset, seen[kw].offset),
                    {"keyword": kw},
                )
            seen[kw] = card

    def missing(keyword):
        raise AuditFailure(
            hdu, Reason.MISSING_KEYWORD, end_offset,
            "mandatory keyword %s is missing from header of HDU %d "
            "(END card at offset %d)" % (keyword, hdu, end_offset),
            {"keyword": keyword},
        )

    # First card: SIMPLE = T (primary) or XTENSION = 'IMAGE' (extension).
    if hdu == 0:
        if not cards or cards[0].keyword != "SIMPLE":
            raise AuditFailure(
                hdu, Reason.MISSING_KEYWORD, cards[0].offset if cards else end_offset,
                "first card of the primary HDU must be SIMPLE = T",
                {"keyword": "SIMPLE"},
            )
        if _parse_logical(cards[0], hdu, "SIMPLE") is not True:
            raise AuditFailure(
                hdu, Reason.INVALID_KEYWORD_VALUE, cards[0].offset,
                "SIMPLE must be T for a standard FITS file",
            )
        if "XTENSION" in seen:
            raise AuditFailure(
                hdu, Reason.KEYWORD_CONFLICT, seen["XTENSION"].offset,
                "XTENSION must not appear in the primary HDU",
                {"keyword": "XTENSION"},
            )
        hdu_type = TYPE_PRIMARY
    else:
        if not cards or cards[0].keyword != "XTENSION":
            raise AuditFailure(
                hdu, Reason.MISSING_KEYWORD, cards[0].offset if cards else end_offset,
                "first card of extension HDU %d must be \"XTENSION= "
                "'IMAGE   '\"" % hdu,
                {"keyword": "XTENSION"},
            )
        xtension, _, _ = _parse_string(cards[0], hdu, "XTENSION")
        if xtension.rstrip() != "IMAGE":
            raise AuditFailure(
                hdu, Reason.UNSUPPORTED_EXTENSION, cards[0].offset,
                "unsupported extension type %r in HDU %d: only IMAGE "
                "extensions are archivable" % (xtension.rstrip(), hdu),
                {"extension": xtension.rstrip()},
            )
        if "SIMPLE" in seen:
            raise AuditFailure(
                hdu, Reason.KEYWORD_CONFLICT, seen["SIMPLE"].offset,
                "SIMPLE must not appear in an extension HDU",
                {"keyword": "SIMPLE"},
            )
        hdu_type = TYPE_IMAGE

    if "BITPIX" not in seen:
        missing("BITPIX")
    bitpix = _parse_integer(seen["BITPIX"], hdu, "BITPIX")
    if bitpix not in VALID_BITPIX:
        raise AuditFailure(
            hdu, Reason.INVALID_KEYWORD_VALUE, seen["BITPIX"].offset,
            "BITPIX must be one of 8, 16, 32, 64, -32, -64 (got %d)" % bitpix,
            {"keyword": "BITPIX", "value": bitpix},
        )

    if "NAXIS" not in seen:
        missing("NAXIS")
    naxis = _parse_integer(seen["NAXIS"], hdu, "NAXIS")
    if naxis < 0 or naxis > MAX_NAXIS:
        raise AuditFailure(
            hdu, Reason.INVALID_KEYWORD_VALUE, seen["NAXIS"].offset,
            "NAXIS must be between 0 and %d (got %d)" % (MAX_NAXIS, naxis),
            {"keyword": "NAXIS", "value": naxis},
        )

    for n in sorted(axes):
        if n > naxis:
            raise AuditFailure(
                hdu, Reason.KEYWORD_CONFLICT, axes[n].offset,
                "NAXIS%d present at offset %d but NAXIS is %d"
                % (n, axes[n].offset, naxis),
                {"keyword": "NAXIS%d" % n, "naxis": naxis},
            )
    axis_lengths = []
    for n in range(1, naxis + 1):
        if n not in axes:
            missing("NAXIS%d" % n)
        length = _parse_integer(axes[n], hdu, "NAXIS%d" % n)
        if length < 0:
            raise AuditFailure(
                hdu, Reason.INVALID_KEYWORD_VALUE, axes[n].offset,
                "NAXIS%d must be non-negative (got %d)" % (n, length),
                {"keyword": "NAXIS%d" % n, "value": length},
            )
        axis_lengths.append(length)

    if "GROUPS" in seen and _parse_logical(seen["GROUPS"], hdu, "GROUPS"):
        raise AuditFailure(
            hdu, Reason.UNSUPPORTED_FEATURE, seen["GROUPS"].offset,
            "random groups (GROUPS = T) are not archivable",
        )

    if hdu > 0:
        if "PCOUNT" not in seen:
            missing("PCOUNT")
        if "GCOUNT" not in seen:
            missing("GCOUNT")
    pcount = (_parse_integer(seen["PCOUNT"], hdu, "PCOUNT")
              if "PCOUNT" in seen else 0)
    gcount = (_parse_integer(seen["GCOUNT"], hdu, "GCOUNT")
              if "GCOUNT" in seen else 1)
    if pcount < 0:
        raise AuditFailure(
            hdu, Reason.INVALID_KEYWORD_VALUE, seen["PCOUNT"].offset,
            "PCOUNT must be non-negative (got %d)" % pcount,
            {"keyword": "PCOUNT", "value": pcount},
        )
    if gcount < 1:
        raise AuditFailure(
            hdu, Reason.INVALID_KEYWORD_VALUE, seen["GCOUNT"].offset,
            "GCOUNT must be at least 1 (got %d)" % gcount,
            {"keyword": "GCOUNT", "value": gcount},
        )

    return {
        "type": hdu_type,
        "bitpix": bitpix,
        "naxis": naxis,
        "axes": axis_lengths,
        "pcount": pcount,
        "gcount": gcount,
        "datasum_card": seen.get("DATASUM"),
        "checksum_card": seen.get("CHECKSUM"),
    }


# ---------------------------------------------------------------------------
# DATASUM / CHECKSUM verification.
# ---------------------------------------------------------------------------


def _datasum_value(card, hdu):
    """Parse the DATASUM value (integer card or astropy's string form)."""
    field = card.image[10:]
    if b"'" in field:
        text, _, _ = _parse_string(card, hdu, "DATASUM")
        token = text.strip().encode("ascii")
    else:
        token = field.split(b"/", 1)[0].strip()
    if not _INT_RE.match(token):
        raise AuditFailure(
            hdu, Reason.INVALID_KEYWORD_VALUE, card.offset,
            "DATASUM at offset %d is not an integer" % card.offset,
        )
    return int(token)


def _verify_datasum(card, data_section, hdu):
    stored = _datasum_value(card, hdu)
    computed = sum32_be(data_section)
    if stored != computed:
        raise AuditFailure(
            hdu, Reason.DATASUM_MISMATCH, card.offset,
            "DATASUM mismatch in HDU %d: stored %d, computed %d from %d "
            "data bytes" % (hdu, stored, computed, len(data_section)),
            {"stored": stored, "computed": computed},
        )
    return {"verdict": VERDICT_VALID, "stored": stored, "computed": computed}


def _verify_checksum(card, header_bytes, header_base, data_section, hdu):
    text, q0, q1 = _parse_string(card, hdu, "CHECKSUM")
    if len(text) != 16 or q1 - q0 != 17:
        raise AuditFailure(
            hdu, Reason.CHECKSUM_MISMATCH, card.offset,
            "CHECKSUM value in HDU %d is not a 16-character string"
            % hdu,
            {"stored": text},
        )
    # The checksum is computed with the value field set to 16 ASCII
    # zeros; the data section contributes its DATASUM as the seed.
    start = card.offset - header_base + q0 + 1
    blanked = bytearray(header_bytes)
    blanked[start : start + 16] = b"0" * 16
    datasum = sum32_be(data_section)
    total = sum32_be(bytes(blanked), datasum)
    expected = char_encode(~total & UINT32_MAX)
    if text != expected:
        raise AuditFailure(
            hdu, Reason.CHECKSUM_MISMATCH, card.offset,
            "CHECKSUM mismatch in HDU %d: stored %r, computed %r"
            % (hdu, text, expected),
            {"stored": text, "computed": expected},
        )
    return {"verdict": VERDICT_VALID, "stored": text, "computed": expected}


# ---------------------------------------------------------------------------
# Top-level audit.
# ---------------------------------------------------------------------------


def _round_up(n, multiple):
    return n if n % multiple == 0 else n + multiple - n % multiple


def _audit_hdu(data, start, hdu):
    """Audit one HDU; returns (report_dict, offset_of_next_hdu)."""
    cards, end_offset, content_end = _read_header(data, start, hdu)

    header_len = _round_up(content_end - start, BLOCK_SIZE)
    header_end = start + header_len
    if header_end > len(data):
        raise AuditFailure(
            hdu, Reason.TRUNCATED_HEADER, start,
            "header of HDU %d is truncated: padding to %d bytes is "
            "missing (file has %d bytes)" % (hdu, header_end, len(data)),
            {"headerOffset": start, "fileSize": len(data)},
        )
    padding = data[content_end:header_end]
    # Card slots after END that carry materialized DATASUM/CHECKSUM
    # cards are legitimate occupants of the header padding; every other
    # non-space byte still rejects the HDU.
    occupied = [
        (c.offset - content_end, CARD_SIZE)
        for c in cards if c.offset >= content_end
    ]
    for i, b in enumerate(padding):
        if b == 0x20 or any(lo <= i < lo + length for lo, length in occupied):
            continue
        raise AuditFailure(
            hdu, Reason.NONSPACE_HEADER_PADDING, content_end + i,
            "non-space byte 0x%02X in header padding at offset %d "
            "(header padding must be ASCII spaces)"
            % (b, content_end + i),
            {"byte": "0x%02X" % b},
        )

    structure = _validate_structure(cards, hdu, end_offset)

    elements = 1
    for length in structure["axes"]:
        elements *= length
    if structure["naxis"] == 0:
        elements = 0
    data_bytes = (
        (abs(structure["bitpix"]) // 8)
        * structure["gcount"]
        * (structure["pcount"] + elements)
    )
    data_start = header_end
    data_end = data_start + data_bytes
    padded_end = data_start + _round_up(data_bytes, BLOCK_SIZE)
    if padded_end > len(data):
        raise AuditFailure(
            hdu, Reason.TRUNCATED_DATA, data_start,
            "data section of HDU %d needs %d bytes (plus %d bytes of "
            "padding) at offset %d, but only %d bytes remain in the file"
            % (hdu, data_bytes, padded_end - data_end, data_start,
               len(data) - data_start),
            {
                "dataOffset": data_start,
                "declaredDataBytes": data_bytes,
                "requiredEndOffset": padded_end,
                "fileSize": len(data),
            },
        )
    data_padding = data[data_end:padded_end]
    if data_padding.strip(b"\x00"):
        bad = next(i for i, b in enumerate(data_padding) if b != 0)
        raise AuditFailure(
            hdu, Reason.NONZERO_DATA_PADDING, data_end + bad,
            "non-zero byte 0x%02X hidden in data padding at offset %d "
            "(data padding must be zero)" % (data_padding[bad], data_end + bad),
            {"byte": "0x%02X" % data_padding[bad]},
        )

    data_section = data[data_start:data_end]
    datasum_card = structure["datasum_card"]
    if datasum_card is None:
        datasum = {"verdict": VERDICT_ABSENT}
    else:
        datasum = _verify_datasum(datasum_card, data_section, hdu)
    checksum_card = structure["checksum_card"]
    if checksum_card is None:
        checksum = {"verdict": VERDICT_ABSENT}
    else:
        checksum = _verify_checksum(
            checksum_card, data[start:header_end], start, data_section, hdu)

    report = {
        "index": hdu,
        "type": structure["type"],
        "range": {"start": start, "end": padded_end},
        "header": {
            "offset": start,
            "bytes": header_len,
            "cards": len(cards) + 1,  # including the END card
        },
        "data": {
            "offset": data_start,
            "bytes": data_bytes,
            "paddedBytes": padded_end - data_start,
        },
        "datasum": datasum,
        "checksum": checksum,
    }
    return report, padded_end


def audit_bytes(data):
    """Audit a whole FITS file; returns the report as a plain dict.

    The function never raises: every outcome is reported through the
    ``conclusion``/``failure`` fields so identical input bytes always
    produce an identical report.
    """
    hdus = []
    failure = None
    try:
        if len(data) == 0:
            raise AuditFailure(
                0, Reason.EMPTY_FILE, 0,
                "the file is empty; a FITS file needs at least one "
                "primary HDU",
            )
        if len(data) > MAX_FILE_BYTES:
            raise AuditFailure(
                0, Reason.FILE_TOO_LARGE, MAX_FILE_BYTES,
                "the file exceeds the %d-byte audit limit" % MAX_FILE_BYTES,
                {"fileSize": len(data), "maxFileBytes": MAX_FILE_BYTES},
            )
        pos = 0
        index = 0
        while pos < len(data):
            remaining = len(data) - pos
            if remaining < BLOCK_SIZE:
                raise AuditFailure(
                    index, Reason.TRAILING_BYTES, pos,
                    "%d trailing byte(s) at offset %d cannot form a "
                    "complete FITS block" % (remaining, pos),
                    {"trailingBytes": remaining},
                )
            if index >= MAX_HDUS:
                raise AuditFailure(
                    index, Reason.TOO_MANY_HDUS, pos,
                    "HDU %d at offset %d exceeds the limit of one primary "
                    "HDU plus 15 IMAGE extensions" % (index, pos),
                    {"maxHdus": MAX_HDUS},
                )
            report, pos = _audit_hdu(data, pos, index)
            hdus.append(report)
            index += 1
    except AuditFailure as exc:
        failure = exc.as_dict()

    return {
        "conclusion": ACCEPTED if failure is None else REJECTED,
        "fileSize": len(data),
        "hduCount": len(hdus),
        "limits": {"maxFileBytes": MAX_FILE_BYTES, "maxHdus": MAX_HDUS},
        "hdus": hdus,
        "failure": failure,
    }


# ---------------------------------------------------------------------------
# Checksum materialization.
# ---------------------------------------------------------------------------


class MaterializationFailure(Exception):
    """Raised when no materialized file can be produced.

    ``report`` is an audit-shaped report dict (``conclusion`` /
    ``failure`` / ``hdus`` ...): either the unchanged rejection report of
    the mandatory full audit that ran first, or a synthesized report
    carrying the ``INSUFFICIENT_HEADER_SPACE`` failure.
    """

    __slots__ = ("report",)

    def __init__(self, report):
        super().__init__(report.get("failure", {}).get("message", ""))
        self.report = report


def _hdu_layout(data, start, hdu):
    """Re-derive the audited layout of one HDU for materialization.

    The full audit has already passed, so none of the parsing calls
    below can fail.  Returns the structure dict together with every
    offset needed to place checksum cards.
    """
    cards, end_offset, content_end = _read_header(data, start, hdu)
    header_end = start + _round_up(content_end - start, BLOCK_SIZE)
    structure = _validate_structure(cards, hdu, end_offset)
    elements = 1
    for length in structure["axes"]:
        elements *= length
    if structure["naxis"] == 0:
        elements = 0
    data_bytes = (
        (abs(structure["bitpix"]) // 8)
        * structure["gcount"]
        * (structure["pcount"] + elements)
    )
    data_start = header_end
    data_end = data_start + data_bytes
    return {
        "start": start,
        "cards": cards,
        "end_offset": end_offset,
        "content_end": content_end,
        "header_end": header_end,
        "data_start": data_start,
        "data_end": data_end,
        "structure": structure,
    }


def _datasum_card_image(value):
    """80-byte integer DATASUM card (standard integer-value form)."""
    return (b"DATASUM = %20d" % value).ljust(CARD_SIZE, b" ")


def _checksum_card_image(value):
    """80-byte CHECKSUM card carrying the 16-character encoded value."""
    return (b"CHECKSUM= '" + value.encode("ascii") + b"'").ljust(
        CARD_SIZE, b" ")


def materialize_checksums(data):
    """Return *data* with missing DATASUM/CHECKSUM cards filled in.

    A full audit runs first; any structural, boundary or existing
    checksum rejection aborts materialization (no output file).  For
    every accepted HDU the missing cards are placed into blank card
    slots *after* the END card within the existing header padding, so
    the scientific payload, the END card and every HDU boundary stay
    byte-for-byte in place; the affected checksums are recomputed per
    the FITS convention.  If any HDU lacks enough blank slots the whole
    request fails with ``INSUFFICIENT_HEADER_SPACE`` (no partial
    result).  An input whose HDUs already carry two valid checksum
    cards is returned unchanged, byte for byte.
    """
    report = audit_bytes(data)
    if report["conclusion"] != ACCEPTED:
        raise MaterializationFailure(report)

    layouts = []
    pos = 0
    index = 0
    while pos < len(data):
        layout = _hdu_layout(data, pos, index)
        layouts.append(layout)
        pos = layout["data_start"] + _round_up(
            layout["data_end"] - layout["data_start"], BLOCK_SIZE)
        index += 1

    # Determine the needs of every HDU before touching a single byte:
    # insufficient space anywhere rejects the complete request.  Slots
    # after END already occupied by materialized DATASUM/CHECKSUM cards
    # are not available.
    needs = []
    for index, layout in enumerate(layouts):
        structure = layout["structure"]
        blank_slots = [
            off for off in range(layout["content_end"], layout["header_end"],
                                 CARD_SIZE)
            if data[off : off + CARD_SIZE] == b" " * CARD_SIZE
        ]
        required = (0 if structure["datasum_card"] is not None else 1) \
            + (0 if structure["checksum_card"] is not None else 1)
        if required > len(blank_slots):
            failure = {
                "hdu": index,
                "reason": Reason.INSUFFICIENT_HEADER_SPACE,
                "offset": layout["end_offset"],
                "message": (
                    "HDU %d has %d blank card slot(s) after the END card at "
                    "offset %d but needs %d slot(s) for the missing "
                    "DATASUM/CHECKSUM cards"
                    % (index, len(blank_slots), layout["end_offset"],
                       required)),
                "details": {
                    "endOffset": layout["end_offset"],
                    "availableSlots": len(blank_slots),
                    "requiredSlots": required,
                },
            }
            blocked = dict(report)
            blocked["conclusion"] = REJECTED
            blocked["failure"] = failure
            raise MaterializationFailure(blocked)
        needs.append(required)

    if not any(needs):
        # Every HDU already carries two valid checksum cards: the
        # accepted input must come back byte-for-byte unchanged.
        return data

    out = bytearray(data)
    for index, layout in enumerate(layouts):
        structure = layout["structure"]
        data_section = bytes(out[layout["data_start"]:layout["data_end"]])
        datasum = sum32_be(data_section)

        def first_blank_slot():
            for off in range(layout["content_end"], layout["header_end"],
                             CARD_SIZE):
                if out[off : off + CARD_SIZE] == b" " * CARD_SIZE:
                    return off
            raise AssertionError("blank slot counted during planning "
                                 "vanished")  # pragma: no cover

        # Add the missing DATASUM card (if any) into the first blank slot
        # after END.  Adding cards to the header can invalidate a
        # pre-existing CHECKSUM, which is recomputed below.
        if structure["datasum_card"] is None:
            slot = first_blank_slot()
            out[slot : slot + CARD_SIZE] = _datasum_card_image(datasum)

        checksum_card = structure["checksum_card"]
        if checksum_card is None:
            # Occupy the next blank slot: a zero placeholder while the
            # header sum is taken, then the encoded final value.
            target = first_blank_slot() + 11  # after "CHECKSUM= '"
            out[target - 11 : target - 11 + CARD_SIZE] = \
                _checksum_card_image("0" * 16)
        else:
            # Recomputing an affected value never moves the card: only
            # its 16-character value field is rewritten in place.
            _, q0, _ = _parse_string(checksum_card, index, "CHECKSUM")
            target = checksum_card.offset + q0 + 1

        header = bytearray(out[layout["start"]:layout["header_end"]])
        header[target - layout["start"]
               : target - layout["start"] + 16] = b"0" * 16
        total = sum32_be(bytes(header), datasum)
        encoded = char_encode(~total & UINT32_MAX).encode("ascii")
        out[target : target + 16] = encoded
    return bytes(out)
