import unittest

from hid_descriptor import HIDField, REPORT_TYPE_INPUT
from hover_palm import (
    DEFAULT_PARAMS,
    HoverPalmEvaluator,
    decode_hover_payload,
    extract_hover_report,
    format_fw_header,
    optimize_main_params,
)


def payload(count, *, area=25, a50=0, a20=8, a10=13, peak=1000,
            tx=2, rx=2, fill=80, confidence=2, fw_vote=0, cid=3):
    return bytes([
        0x10 | cid, count, area, a50, a20, a10,
        peak & 0xFF, (peak >> 8) & 0xFF,
        tx, rx, fill, (fw_vote << 4) | confidence,
    ])


class HoverPalmTests(unittest.TestCase):
    def test_decode_signed_payload(self):
        sample = decode_hover_payload(payload(
            6, area=30, a50=5, a20=10, a10=18,
            peak=-123, tx=8, rx=4, fill=55, fw_vote=3,
        ))
        self.assertIsNotNone(sample)
        self.assertEqual(sample.contact_id, 3)
        self.assertEqual(sample.peak_diff, -123)
        self.assertEqual(sample.fw_vote, 3)
        self.assertEqual(sample.confidence, 2)

    def test_rejects_non_tune_or_implausible_tail(self):
        self.assertIsNone(decode_hover_payload(bytes(12)))
        self.assertIsNone(decode_hover_payload(payload(
            1, area=10, a50=8, a20=7, a10=9,
        )))

    def test_direct_touch_guard_wins_at_frame_six(self):
        evaluator = HoverPalmEvaluator()
        result = None
        for count in range(1, 7):
            sample = decode_hover_payload(payload(
                count, area=25, a50=5, a20=8, a10=13,
            ))
            result = evaluator.process(sample, "dev")
        self.assertTrue(result.direct_guard)
        self.assertEqual(result.calculated_vote, 2)
        self.assertEqual(result.area_sum, 150)

    def test_hover_vote_when_direct_guard_is_cleared(self):
        evaluator = HoverPalmEvaluator()
        result = None
        for count in range(1, 7):
            sample = decode_hover_payload(payload(count))
            result = evaluator.process(sample, "dev")
        self.assertFalse(result.direct_guard)
        self.assertEqual(result.hover_count, 6)
        self.assertEqual(result.calculated_vote, 3)

    def test_two_finger_contact_id_switch_keeps_one_frame_sequence(self):
        evaluator = HoverPalmEvaluator()
        result = None
        for count in range(1, 7):
            sample = decode_hover_payload(payload(count, cid=1 if count % 2 else 2))
            result = evaluator.process(sample, "dev")
        self.assertEqual(result.area_sum, 150)
        self.assertEqual(result.hover_count, 6)
        self.assertEqual(result.calculated_vote, 3)

    def test_changed_vote_parameters_recalculate_outcome(self):
        params = dict(DEFAULT_PARAMS)
        params["hover_votes"] = 7
        params["wide_votes"] = 7
        evaluator = HoverPalmEvaluator(params)
        result = None
        for count in range(1, 7):
            result = evaluator.process(decode_hover_payload(payload(count)), "dev")
        self.assertEqual(result.calculated_vote, 0)

    def test_descriptor_field_locates_payload_before_report_tail(self):
        fields = [HIDField(
            report_id=7, report_type=REPORT_TYPE_INPUT,
            bit_offset=16, bit_size=96, report_count=1,
            usage_page=0x09, usages=[0xC5], logical_min=0,
            logical_max=255, flags=3, is_const=True,
        )]
        debug = payload(4, fw_vote=3)
        found = extract_hover_report(
            bytes([7, 0xAA, 0xBB]) + debug + bytes([0xEE]), fields
        )
        self.assertIsNotNone(found)
        self.assertEqual(found[0].touch_count, 4)
        self.assertEqual(found[0].fw_vote, 3)
        self.assertEqual(found[1], "descriptor")
        self.assertEqual(found[2], 2)

    def test_fw_header_uses_main_flag_parameters(self):
        params = dict(DEFAULT_PARAMS)
        params["low_peak_max"] = 1234
        params["finger_thumb_max"] = 37
        header = format_fw_header(params)
        self.assertIn("#define HOVER_LOW_PEAK_DIFF_MAX", header)
        self.assertIn("1234", header)
        self.assertIn("#define HOVER_FINGER_THUMB_SCOPE_MAX", header)
        self.assertIn("37U", header)

    def test_finger_flag_is_exposed(self):
        evaluator = HoverPalmEvaluator()
        sample = decode_hover_payload(payload(1))
        self.assertTrue(evaluator.process(sample, "dev").finger_flag)

    def test_optimizer_fits_three_labelled_sequences(self):
        def seq(kind):
            rows = []
            for count in range(1, 7):
                if kind == "Finger":
                    raw = payload(count, area=25, a50=0, a20=0, a10=0, peak=3000)
                elif kind == "Hover":
                    raw = payload(count)
                else:
                    raw = payload(count, area=35, a50=0, a20=5, a10=10,
                                  peak=1700, tx=8, rx=4, fill=40)
                rows.append(decode_hover_payload(raw))
            return rows
        best, stats = optimize_main_params([
            ("Finger", seq("Finger")),
            ("Hover", seq("Hover")),
            ("Thumb", seq("Thumb")),
        ])
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["accuracy"], 1.0)
        self.assertEqual(best["hover_area_min"], 25)

    def test_strong_thumb_vote(self):
        evaluator = HoverPalmEvaluator()
        result = None
        for count in range(1, 7):
            sample = decode_hover_payload(payload(
                count, area=35, a50=0, a20=5, a10=10,
                peak=1700, tx=8, rx=4, fill=40,
            ))
            result = evaluator.process(sample, "dev")
        self.assertEqual(result.thumb_count, 6)
        self.assertEqual(result.calculated_vote, 2)


if __name__ == "__main__":
    unittest.main()
