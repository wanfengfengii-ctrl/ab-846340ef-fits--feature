"""Unit tests for checksum materialization (fitsaudit.core)."""

import struct
import unittest

from fitsaudit import fixtures
from fitsaudit.core import (
    ACCEPTED,
    REJECTED,
    MaterializationFailure,
    audit_bytes,
    char_encode,
    materialize_checksums,
    sum32_be,
    UINT32_MAX,
)
from fitsaudit.fixtures import (
    card,
    data_block,
    header_block,
    image_hdu,
    primary_hdu,
)


def _verdicts(report):
    return [(h["datasum"]["verdict"], h["checksum"]["verdict"])
            for h in report["hdus"]]


class MaterializeSuccessTests(unittest.TestCase):
    def test_missing_cards_are_filled(self):
        early = fixtures.build_valid_file(2, checksums=False)
        before = audit_bytes(early)
        self.assertEqual(before["conclusion"], ACCEPTED)
        self.assertEqual(_verdicts(before),
                         [("ABSENT", "ABSENT")] * 3)

        completed = materialize_checksums(early)
        after = audit_bytes(completed)
        self.assertEqual(after["conclusion"], ACCEPTED)
        self.assertEqual(_verdicts(after), [("VALID", "VALID")] * 3)

    def test_size_and_hdu_boundaries_unchanged(self):
        early = fixtures.build_valid_file(2, checksums=False)
        completed = materialize_checksums(early)
        self.assertEqual(len(completed), len(early))
        before = audit_bytes(early)
        after = audit_bytes(completed)
        for b, a in zip(before["hdus"], after["hdus"]):
            self.assertEqual(b["range"], a["range"])
            self.assertEqual(b["header"]["offset"], a["header"]["offset"])
            self.assertEqual(b["header"]["bytes"], a["header"]["bytes"])
            self.assertEqual(b["data"], a["data"])

    def test_scientific_payload_byte_identical(self):
        early = fixtures.build_valid_file(2, checksums=False)
        completed = materialize_checksums(early)
        for h in audit_bytes(early)["hdus"]:
            start = h["data"]["offset"]
            end = start + h["data"]["paddedBytes"]
            self.assertEqual(completed[start:end], early[start:end])
        # Only header padding (spaces in the input) differs.
        for i, (a, b) in enumerate(zip(early, completed)):
            if a != b:
                self.assertEqual(a, 0x20, i)

    def test_cards_land_directly_after_end(self):
        completed = materialize_checksums(primary_hdu(checksums=False))
        end_off = fixtures.find_card(completed, "END")
        self.assertEqual(completed[end_off + 80:end_off + 88], b"DATASUM ")
        self.assertEqual(completed[end_off + 160:end_off + 168],
                         b"CHECKSUM")

    def test_datasum_value_matches_data(self):
        data = struct.pack(">12h", *range(-6, 6))
        early = primary_hdu(data, bitpix=16, axes=(12,), checksums=False)
        completed = materialize_checksums(early)
        report = audit_bytes(completed)
        hdu = report["hdus"][0]
        self.assertEqual(hdu["datasum"]["computed"], sum32_be(data))
        self.assertEqual(hdu["datasum"]["stored"], sum32_be(data))

    def test_exactly_two_free_slots_fit(self):
        # 33 content cards + END occupy 34 cards, leaving exactly 2 slots.
        cards = [card("SIMPLE", True), card("BITPIX", 8), card("NAXIS", 0)]
        cards += [card("COMMENT", None) for _ in range(30)]
        early = header_block(cards)
        completed = materialize_checksums(early)
        self.assertEqual(len(completed), len(early))
        self.assertEqual(audit_bytes(completed)["conclusion"], ACCEPTED)

    def test_multiblock_header_uses_slots_after_end(self):
        cards = [card("SIMPLE", True), card("BITPIX", 8), card("NAXIS", 0)]
        cards += [card("COMMENT", None) for _ in range(40)]  # 2 header blocks
        early = header_block(cards)
        self.assertEqual(len(early), 5760)
        completed = materialize_checksums(early)
        end_off = fixtures.find_card(completed, "END")
        self.assertEqual(completed[end_off + 80:end_off + 88], b"DATASUM ")
        self.assertEqual(completed[end_off + 160:end_off + 168],
                         b"CHECKSUM")
        self.assertEqual(len(completed), len(early))
        self.assertEqual(audit_bytes(completed)["conclusion"], ACCEPTED)

    def test_mixed_hdus_only_missing_ones_change(self):
        data = struct.pack(">6h", *range(6))
        early = primary_hdu(checksums=True) + image_hdu(
            data, bitpix=16, axes=(6, 1), checksums=False)
        completed = materialize_checksums(early)
        # The already-complete primary HDU is byte-for-byte identical.
        self.assertEqual(completed[:2880], early[:2880])
        report = audit_bytes(completed)
        self.assertEqual(report["conclusion"], ACCEPTED)
        self.assertEqual(_verdicts(report),
                         [("VALID", "VALID"), ("VALID", "VALID")])

    def test_datasum_only_extension_keeps_card_adds_checksum(self):
        data = struct.pack(">6h", *range(6))
        datasum = sum32_be(data)
        cards = [card("XTENSION", "IMAGE"), card("BITPIX", 16),
                 card("NAXIS", 1), card("NAXIS1", 6), card("PCOUNT", 0),
                 card("GCOUNT", 1), card("DATASUM", datasum)]
        early = primary_hdu(checksums=True) + header_block(cards) \
            + data_block(data)
        self.assertEqual(audit_bytes(early)["conclusion"], ACCEPTED)
        completed = materialize_checksums(early)
        # Existing DATASUM card is preserved in place.
        old_off = fixtures.find_card(early, "DATASUM", occurrence=1)
        self.assertEqual(completed[old_off:old_off + 80],
                         early[old_off:old_off + 80])
        # The new CHECKSUM sits right after END.
        end_off = fixtures.find_card(completed, "END", occurrence=1)
        self.assertEqual(completed[end_off + 80:end_off + 88], b"CHECKSUM")
        self.assertEqual(audit_bytes(completed)["conclusion"], ACCEPTED)

    def test_checksum_only_extension_recomputed_in_place(self):
        data = struct.pack(">6h", *range(6))
        datasum = sum32_be(data)
        cards = [card("XTENSION", "IMAGE"), card("BITPIX", 16),
                 card("NAXIS", 1), card("NAXIS1", 6), card("PCOUNT", 0),
                 card("GCOUNT", 1), card("CHECKSUM", "0" * 16)]
        # Make the CHECKSUM valid for the header without a DATASUM card.
        total = sum32_be(header_block(cards), datasum)
        cards[-1] = card("CHECKSUM", char_encode(~total & UINT32_MAX))
        early = primary_hdu(checksums=True) + header_block(cards) \
            + data_block(data)
        self.assertEqual(audit_bytes(early)["conclusion"], ACCEPTED)
        old_off = fixtures.find_card(early, "CHECKSUM", occurrence=1)

        completed = materialize_checksums(early)
        # CHECKSUM card slot does not move; only its value is recomputed.
        new_off = fixtures.find_card(completed, "CHECKSUM", occurrence=1)
        self.assertEqual(old_off, new_off)
        self.assertNotEqual(completed[old_off + 11:old_off + 27],
                            early[old_off + 11:old_off + 27])
        # DATASUM fills the slot after END.
        end_off = fixtures.find_card(completed, "END", occurrence=1)
        self.assertEqual(completed[end_off + 80:end_off + 88], b"DATASUM ")
        self.assertEqual(audit_bytes(completed)["conclusion"], ACCEPTED)


class IdempotencyTests(unittest.TestCase):
    def test_fully_checksummed_input_returned_unchanged(self):
        for blob in (fixtures.build_valid_file(2),
                     fixtures.build_max_hdus_file(),
                     open("tests/data/valid_astropy.fits", "rb").read()):
            self.assertEqual(materialize_checksums(blob), blob)

    def test_repeated_materialization_is_fixed_point(self):
        early = fixtures.build_valid_file(2, checksums=False)
        once = materialize_checksums(early)
        twice = materialize_checksums(once)
        self.assertEqual(once, twice)
        self.assertEqual(
            audit_bytes(once)["conclusion"], ACCEPTED)

    def test_idempotent_retry_after_partial_client_failure(self):
        # Simulating a client retry: posting the completed file again
        # must produce exactly the same bytes.
        early = fixtures.build_valid_file(1, checksums=False)
        first = materialize_checksums(early)
        for _ in range(3):
            first = materialize_checksums(first)
        self.assertEqual(first, materialize_checksums(early))


class InsufficientSpaceTests(unittest.TestCase):
    def _tight_primary(self, comments):
        cards = [card("SIMPLE", True), card("BITPIX", 8), card("NAXIS", 0)]
        cards += [card("COMMENT", None) for _ in range(comments)]
        return header_block(cards)

    def test_one_free_slot_reports_insufficient_space(self):
        # 34 content cards + END = 35 cards -> 1 blank slot, needs 2.
        early = self._tight_primary(31)
        with self.assertRaises(MaterializationFailure) as ctx:
            materialize_checksums(early)
        failure = ctx.exception.report["failure"]
        self.assertEqual(failure["hdu"], 0)
        self.assertEqual(failure["reason"],
                         "INSUFFICIENT_HEADER_SPACE")
        self.assertEqual(failure["offset"], 34 * 80)  # END card offset
        self.assertEqual(failure["details"]["endOffset"], 34 * 80)
        self.assertEqual(failure["details"]["availableSlots"], 1)
        self.assertEqual(failure["details"]["requiredSlots"], 2)

    def test_zero_free_slots_reports_insufficient_space(self):
        early = self._tight_primary(32)  # header fills the block exactly
        with self.assertRaises(MaterializationFailure) as ctx:
            materialize_checksums(early)
        failure = ctx.exception.report["failure"]
        self.assertEqual(failure["reason"],
                         "INSUFFICIENT_HEADER_SPACE")
        self.assertEqual(failure["details"]["availableSlots"], 0)
        self.assertEqual(failure["details"]["requiredSlots"], 2)

    def test_insufficient_space_in_later_hdu_fails_whole_request(self):
        good = primary_hdu(checksums=False)  # HDU 0: plenty of space
        middle = image_hdu(b"\x01\x02", bitpix=8, axes=(2,),
                           checksums=False)
        # HDU 2: no room at all (5 structural + 30 comments + END fill
        # the 36 cards of one 2880-byte block exactly).
        tight = header_block(
            [card("XTENSION", "IMAGE"), card("BITPIX", 8), card("NAXIS", 0),
             card("PCOUNT", 0), card("GCOUNT", 1)]
            + [card("COMMENT", None) for _ in range(30)])
        with self.assertRaises(MaterializationFailure) as ctx:
            materialize_checksums(good + middle + tight)
        report = ctx.exception.report
        self.assertEqual(report["conclusion"], REJECTED)
        failure = report["failure"]
        self.assertEqual(failure["hdu"], 2)
        self.assertEqual(failure["reason"],
                         "INSUFFICIENT_HEADER_SPACE")
        # HDU 2 starts at 2880 + 2*2880; END is 35 cards in.
        self.assertEqual(failure["offset"], 2880 + 2 * 2880 + 35 * 80)

    def test_insufficient_space_report_is_audit_shaped(self):
        early = self._tight_primary(31)
        with self.assertRaises(MaterializationFailure) as ctx:
            materialize_checksums(early)
        report = ctx.exception.report
        self.assertEqual(set(report),
                         {"conclusion", "fileSize", "hduCount", "limits",
                          "hdus", "failure"})
        self.assertEqual(report["hduCount"], 1)


class AuditRejectionTests(unittest.TestCase):
    def _assert_blocked(self, blob, reason, hdu=None):
        with self.assertRaises(MaterializationFailure) as ctx:
            materialize_checksums(blob)
        report = ctx.exception.report
        self.assertEqual(report["conclusion"], REJECTED)
        self.assertEqual(report["failure"]["reason"], reason)
        if hdu is not None:
            self.assertEqual(report["failure"]["hdu"], hdu)
        # The report is exactly what the audit endpoint would return.
        self.assertEqual(report, audit_bytes(blob))

    def test_truncated_file_blocked(self):
        self._assert_blocked(fixtures.build_valid_file(1)[:6000],
                             "TRUNCATED_DATA", hdu=1)

    def test_trailing_bytes_blocked(self):
        self._assert_blocked(primary_hdu() + b"\x00" * 100,
                             "TRAILING_BYTES")

    def test_corrupt_datasum_blocked(self):
        blob = fixtures.corrupt_datasum(fixtures.build_valid_file(1),
                                        occurrence=1)
        self._assert_blocked(blob, "DATASUM_MISMATCH", hdu=1)

    def test_corrupt_checksum_blocked(self):
        blob = fixtures.corrupt_checksum(fixtures.build_valid_file(1),
                                         occurrence=1)
        self._assert_blocked(blob, "CHECKSUM_MISMATCH", hdu=1)

    def test_empty_file_blocked(self):
        self._assert_blocked(b"", "EMPTY_FILE")

    def test_nonspace_padding_blocked(self):
        # A non-space byte in padding that is not itself a checksum card
        # must keep the ordinary audit rejection (no file produced).
        cards = [card("SIMPLE", True), card("BITPIX", 8), card("NAXIS", 0)]
        early = bytearray(header_block(cards))
        early[500] = 0x00
        self._assert_blocked(bytes(early), "NONSPACE_HEADER_PADDING")

    def test_lookalike_keyword_in_padding_is_not_a_card(self):
        # Slot starts with the DATASUM keyword but is not a value card:
        # it stays ordinary (bad) padding rather than becoming a card.
        cards = [card("SIMPLE", True), card("BITPIX", 8), card("NAXIS", 0)]
        early = bytearray(header_block(cards))
        early[480:560] = b"DATASUM  not a value card".ljust(80)
        self._assert_blocked(bytes(early), "NONSPACE_HEADER_PADDING")

    def test_well_formed_padding_checksum_card_is_verified(self):
        # A correctly shaped DATASUM card placed after END is a real
        # card and a wrong value rejects via DATASUM_MISMATCH.
        cards = [card("SIMPLE", True), card("BITPIX", 8), card("NAXIS", 0)]
        early = bytearray(header_block(cards))
        early[480:560] = card("DATASUM", 12345)
        self._assert_blocked(bytes(early), "DATASUM_MISMATCH")


if __name__ == "__main__":
    unittest.main()
