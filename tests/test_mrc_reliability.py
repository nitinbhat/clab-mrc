from dataclasses import replace
import random
import sys
import unittest
from pathlib import Path

# Add src/ to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mrc_reliability import CcState, PSN_MASK, Receiver, Requestor, Sack, WindowError


class SackCodecTests(unittest.TestCase):
    # Hand-transcribed fields in network order from Tables 7-12 and 7-14.
    VECTOR = bytes.fromhex(
        "0020 fffe 12345678 9abc def0 00010203 0002 ffc0 "
        "8000000000000005 11221234 b5abcdef")

    def test_fixed_vector_including_signed_offsets_and_cc_bitfields(self):
        sack = Sack(
            ack_psn_offset=-2, entropy=0x12345678, spdcid=0x9ABC, dpdcid=0xDEF0,
            cack_psn=0x010203, sack_offset=-64, sack_bitmap=0x8000000000000005,
            cc_state=CcState(0x1122, 0x1234, True, 0x35, 0xABCDEF), m=1, mpr=2)
        self.assertEqual(sack.encode(), self.VECTOR)
        self.assertEqual(Sack.decode(self.VECTOR), sack)
        self.assertEqual(sack.ack_psn, 0x010201)
        self.assertEqual(sack.sack_base, 0x0101C3)

    def test_probe_id_is_unsigned_not_a_packet_ack(self):
        sack = replace(Sack.decode(self.VECTOR), pr=True, ack_psn_offset=0xFFFF, m=2)
        encoded = sack.encode()
        self.assertEqual(encoded[:4], bytes.fromhex("0042 ffff"))
        self.assertEqual(Sack.decode(encoded), sack)
        with self.assertRaisesRegex(ValueError, "identifier"):
            _ = sack.ack_psn

    def test_reserved_bits_are_ignored_and_zeroed_on_encode(self):
        encoded = bytearray(self.VECTOR)
        encoded[0] = 0xFF
        encoded[1] |= 0x9D
        encoded[12] = 0xFF
        encoded[16] |= 0x0F
        encoded[30] |= 0x80
        self.assertEqual(Sack.decode(encoded).encode(), self.VECTOR)

    def test_malformed_headers_and_unsupported_cc_type(self):
        for size in (0, 28, 35, 37):
            with self.subTest(size=size), self.assertRaisesRegex(ValueError, "36 bytes"):
                Sack.decode(bytes(size))
        bad_type = bytearray(self.VECTOR)
        bad_type[16] = 0x10
        with self.assertRaisesRegex(ValueError, "CC_TYPE"):
            Sack.decode(bad_type)
        bad_mark = bytearray(self.VECTOR)
        bad_mark[1] = 0x60
        with self.assertRaisesRegex(ValueError, "reserved"):
            Sack.decode(bad_mark)

    def test_unrepresentable_fields_are_not_truncated(self):
        original = Sack.decode(self.VECTOR)
        for changes in ({"ack_psn_offset": 32768}, {"sack_offset": -32769},
                        {"cack_psn": 1 << 24}, {"sack_bitmap": 1 << 64},
                        {"mpr": 256}, {"m": 3}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(original, **changes)
        with self.assertRaises(ValueError):
            CcState(ooo_count=1 << 15)
        with self.assertRaises(ValueError):
            CcState(rcv_cwnd_pen=128)


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.receiver = Receiver(100, 0x123456, 0xABCDEF)

    def test_out_of_order_bitmap_and_negative_trigger_offset_after_gap_fill(self):
        first = self.receiver.receive(102, nominal_bytes=257, entropy=0x1234,
                                      tx_timestamp=0xABCD, m=1).sack
        self.assertIsNotNone(first)
        self.assertEqual(first.cack_psn, 99)
        self.assertEqual(first.ack_psn_offset, 3)
        self.assertEqual(first.sack_bitmap, 0b1001)
        self.assertEqual(first.spdcid, 0x3456)
        self.assertEqual(first.dpdcid, 0xCDEF)
        self.assertEqual(first.entropy, 0x1234)
        self.assertEqual(first.m, 1)
        self.assertEqual(first.cc_state, CcState(0xABCD, 1, False, 0, 2))
        self.receiver.receive(100, nominal_bytes=256)
        filled = self.receiver.receive(101, nominal_bytes=256).sack
        self.assertEqual(filled.cack_psn, 102)
        self.assertEqual(filled.ack_psn_offset, -1)
        self.assertEqual(filled.ack_psn, 101)
        self.assertEqual(filled.cc_state.ooo_count, 0)
        self.assertEqual(filled.cc_state.rcvd_bytes, 4)

    def test_duplicate_does_not_double_count_or_advance_state(self):
        self.receiver.receive(101, nominal_bytes=256)
        duplicate = self.receiver.receive(101, nominal_bytes=256)
        self.assertFalse(duplicate.is_new)
        self.assertEqual(duplicate.sack.cc_state.ooo_count, 1)
        self.assertEqual(self.receiver.rx_rcvd_bytes, 256)
        self.receiver.receive(100, nominal_bytes=256)
        old = self.receiver.receive(100, nominal_bytes=256)
        self.assertFalse(old.is_new)
        self.assertEqual(old.sack.cack_psn, 101)
        self.assertEqual(old.sack.cc_state.rcvd_bytes, 2)

    def test_forward_window_includes_boundary_and_rejects_next_without_mutation(self):
        with self.assertRaises(WindowError):
            self.receiver.receive(228, nominal_bytes=1)
        self.assertEqual(self.receiver.max_rcv_psn, 99)
        self.assertEqual(self.receiver.rx_rcvd_bytes, 0)
        self.assertTrue(self.receiver.receive(227, nominal_bytes=1).is_new)

    def test_old_duplicate_window_and_invalid_metadata(self):
        with self.assertRaisesRegex(WindowError, "predates"):
            self.receiver.receive(99, nominal_bytes=1)
        self.receiver.receive(100, nominal_bytes=1)
        with self.assertRaises(WindowError):
            self.receiver.receive((100 - 128) & PSN_MASK, nominal_bytes=1)
        with self.assertRaises(ValueError):
            self.receiver.receive(101, nominal_bytes=1, tx_timestamp=65536)
        self.assertEqual(self.receiver.cack_psn, 100)
        self.assertEqual(self.receiver.rx_rcvd_bytes, 1)

    def test_wraparound_uses_serial_order_not_integer_order(self):
        receiver = Receiver(0xFFFFFE, 1, 2)
        receiver.receive(0, nominal_bytes=256)
        receiver.receive(0xFFFFFE, nominal_bytes=256)
        result = receiver.receive(0xFFFFFF, nominal_bytes=256)
        self.assertEqual(receiver.cack_psn, 0)
        self.assertEqual(receiver.max_rcv_psn, 0)
        self.assertEqual(result.sack.ack_psn_offset, -1)
        self.assertEqual(result.sack.ack_psn, 0xFFFFFF)

    def test_bitmap_rotation_and_lower_arrival_from_section_7_5_2_2(self):
        receiver = Receiver(406, 1, 2, window=512)
        # The spec example assumes previous feedback has rotated above PSN 673.
        for _ in range(7):
            receiver.receive(800, nominal_bytes=1)
        for psn in (673, 704, 705):
            receiver.receive(psn, nominal_bytes=1, emit_sack=False)
        sack = receiver.receive(680, nominal_bytes=1).sack
        self.assertEqual(sack.cack_psn, 405)
        self.assertEqual(sack.sack_offset, 268)
        self.assertEqual(sack.sack_base, 673)
        self.assertEqual(sack.sack_bitmap, (1 << 0) | (1 << 7) | (1 << 31) | (1 << 32))
        self.assertEqual(sack.cc_state.ooo_count, 5)
        self.assertEqual(receiver.lowest_unsacked_psn, 737)
        next_sack = receiver.receive(800, nominal_bytes=1).sack
        self.assertEqual(next_sack.sack_base, 736)
        self.assertEqual(next_sack.sack_bitmap, 0)
        self.assertEqual(next_sack.ack_psn, 800)

    def test_fixed_mpr_is_unpopulated_and_received_byte_counter_wraps(self):
        receiver = Receiver(0, 1, 2)
        receiver.receive(0, nominal_bytes=0xFFFFFF00)
        sack = receiver.receive(1, nominal_bytes=256).sack
        self.assertEqual(receiver.rx_rcvd_bytes, 0)
        self.assertEqual(sack.cc_state.rcvd_bytes, 0)
        self.assertEqual(sack.mpr, 0)


class RequestorTests(unittest.TestCase):
    def setUp(self):
        self.requestor = Requestor(0, 1, 2)
        self.receiver = Receiver(0, 1, 2)

    def test_selective_feedback_does_not_treat_zero_bits_as_loss(self):
        for _ in range(4):
            self.requestor.send()
        sack = self.receiver.receive(2, nominal_bytes=256).sack
        self.assertEqual(self.requestor.apply_sack(Sack.decode(sack.encode())), (2,))
        self.assertEqual(self.requestor.outstanding, (0, 1, 3))
        self.assertEqual(self.requestor.apply_sack(sack), ())

    def test_cumulative_ack_after_lost_feedback(self):
        for _ in range(4):
            self.requestor.send()
        for psn in range(4):
            sack = self.receiver.receive(psn, nominal_bytes=256).sack
        self.assertEqual(self.requestor.apply_sack(sack), (0, 1, 2, 3))
        self.assertEqual(self.requestor.outstanding, ())

    def test_window_is_psn_span_not_count_of_unacked_packets(self):
        for _ in range(128):
            self.requestor.send()
        sack = self.receiver.receive(127, nominal_bytes=256).sack
        self.requestor.apply_sack(sack)
        with self.assertRaises(WindowError):
            self.requestor.send()
        sack = self.receiver.receive(0, nominal_bytes=256).sack
        self.requestor.apply_sack(sack)
        self.assertEqual(self.requestor.send(), 128)

    def test_late_sack_can_ack_a_new_packet_without_regressing_cumulative_ack(self):
        for _ in range(4):
            self.requestor.send()
        late = self.receiver.receive(3, nominal_bytes=1).sack
        newer = self.receiver.receive(0, nominal_bytes=1).sack
        self.requestor.apply_sack(replace(newer, sack_bitmap=1))
        self.assertEqual(self.requestor.cack_psn, 0)
        self.assertEqual(self.requestor.apply_sack(late), (3,))
        self.assertEqual(self.requestor.cack_psn, 0)

    def test_probe_identifier_is_not_used_as_ack_offset(self):
        for _ in range(4):
            self.requestor.send()
        probe = Sack(3, 0, 1, 2, PSN_MASK, 0, 0, pr=True)
        self.assertEqual(self.requestor.apply_sack(probe), ())
        self.assertEqual(self.requestor.outstanding, (0, 1, 2, 3))

    def test_invalid_sack_does_not_mutate_requestor(self):
        self.requestor.send()
        valid = self.receiver.receive(0, nominal_bytes=1).sack
        for changed in (replace(valid, spdcid=3), replace(valid, cack_psn=1),
                        replace(valid, ack_psn_offset=1),
                        replace(valid, ack_psn_offset=-2),
                        replace(valid, sack_bitmap=0b10)):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.requestor.apply_sack(changed)
            self.assertEqual(self.requestor.outstanding, (0,))
            self.assertEqual(self.requestor.cack_psn, PSN_MASK)

    def test_mpr_is_ignored_when_dynamic_mpr_was_not_negotiated(self):
        self.requestor.send()
        sack = replace(self.receiver.receive(0, nominal_bytes=1).sack, mpr=255)
        self.requestor.apply_sack(sack)
        self.assertEqual(self.requestor.window, 128)

    def test_lost_data_ack_reordering_and_rollover_through_binary_feedback(self):
        requestor = Requestor(0xFFFF80, 1, 2, window=512)
        receiver = Receiver(0xFFFF80, 1, 2, window=512)
        psns = [requestor.send() for _ in range(300)]
        missing_indices = {0, 63, 128, 255, 299}
        order = [i for i in range(len(psns)) if i not in missing_indices]
        random.Random(42).shuffle(order)
        feedback = []
        for index in order:
            packet = receiver.receive(psns[index], nominal_bytes=256)
            feedback.append(packet.sack.encode())
        # Drop most feedback packets and reorder those that remain.
        feedback = feedback[::7]
        random.Random(43).shuffle(feedback)
        for encoded in feedback:
            requestor.apply_sack(Sack.decode(encoded))
        for psn in requestor.outstanding:
            sack = receiver.receive(psn, nominal_bytes=256).sack
            requestor.apply_sack(Sack.decode(sack.encode()))
        self.assertEqual(requestor.outstanding, ())
        self.assertEqual(receiver.cack_psn, psns[-1])
        self.assertEqual(receiver.rx_rcvd_bytes, 300 * 256)

    def test_window_and_ambiguous_serial_numbers_fail_explicitly(self):
        for window in (0, 64, 129, 32768):
            with self.subTest(window=window), self.assertRaises(ValueError):
                Requestor(0, 1, 2, window=window)
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.receiver.receive((PSN_MASK + (1 << 23)) & PSN_MASK, nominal_bytes=1)


if __name__ == "__main__":
    unittest.main()
