"""Software invariants only. Fixtures are fabricated and MUST NOT be imported."""
import copy
import importlib.util
import json
import math
import tempfile
import unittest
from pathlib import Path

from helix_mcp import preset as hlx


class TemplateTests(unittest.TestCase):
    def setUp(self):
        # An artificial JSON fixture, NOT a Line 6 preset or valid model catalog.
        self.doc = {"schema": "L6Preset", "version": 6,
                    "unknown_vendor_field": {"retain": [1, 2, "unchanged"]},
                    "data": {"device": 2162689, "meta": {"name": "TEST_ONLY", "appversion": 123},
                             "tone": {"dsp0": {"block0": {"@model": "TEST_ONLY_MODEL",
                                                              "@path": 0, "@enabled": True,
                                                              "RawLevel": 0.5, "Choice": 1}},
                                      "dsp1": {}, "global": {"@current_snapshot": 0},
                                      "snapshot0": {"@name": "TEST", "@valid": True,
                                                    "blocks": {"dsp0": {"block0": True}}}}}}
        self.plan = {"plan_version": 1, "template_sha256": "test_digest", "name": "TEST_REV2",
                     "edits": [{"path": "/data/tone/dsp0/block0/RawLevel", "expect": 0.5,
                                "value": 0.6, "model": "TEST_ONLY_MODEL", "kind": "number",
                                "min": 0.0, "max": 1.0, "evidence": "Synthetic test only"}]}

    def build(self, plan=None, doc=None):
        return hlx.build_preset(self.doc if doc is None else doc, "test_digest",
                                self.plan if plan is None else plan)

    def test_only_selected_value_and_name_change(self):
        original = copy.deepcopy(self.doc)
        result, changes = self.build()
        expected = copy.deepcopy(original)
        expected["data"]["meta"]["name"] = "TEST_REV2"
        expected["data"]["tone"]["dsp0"]["block0"]["RawLevel"] = 0.6
        self.assertEqual(result, expected)
        self.assertEqual(self.doc, original)
        self.assertEqual(len(changes), 2)

    def test_stale_template_and_expected_value_are_rejected(self):
        plan = copy.deepcopy(self.plan)
        plan["template_sha256"] = "different"
        with self.assertRaises(hlx.PresetError):
            self.build(plan)
        plan["template_sha256"] = "test_digest"
        plan["edits"][0]["expect"] = 0.25
        with self.assertRaises(hlx.PresetError):
            self.build(plan)

    def test_unknown_key_structural_edit_and_model_change_rejected(self):
        for path in ["/data/tone/dsp0/block0/Invented", "/data/tone/dsp0/block0/@model",
                     "/data/device", "/data/tone/global/@current_snapshot"]:
            with self.subTest(path=path):
                plan = copy.deepcopy(self.plan)
                plan["edits"][0]["path"] = path
                with self.assertRaises(hlx.PresetError):
                    self.build(plan)
        plan = copy.deepcopy(self.plan)
        plan["edits"][0]["model"] = "OTHER_MODEL"
        with self.assertRaises(hlx.PresetError):
            self.build(plan)

    def test_bounds_finite_values_boolean_and_missing_evidence(self):
        for replacement in [{"value": 1.01}, {"value": math.inf}, {"value": True},
                            {"min": None}, {"max": -1}, {"evidence": ""}]:
            with self.subTest(replacement=replacement):
                plan = copy.deepcopy(self.plan)
                plan["edits"][0].update(replacement)
                with self.assertRaises(hlx.PresetError):
                    self.build(plan)

    def test_snapshot_and_controller_assignments_block_parameter_edits(self):
        for addition in [{"controllers": {"dsp0": {"block0": {"RawLevel": {"@controller": 9}}}}},
                         {"snapshot1": {"@name": "TEST", "controllers": {"RawLevel": 0.9}}},
                         {"snapshot2": {"unexpected_format": {"RawLevel": 0.9}}}]:
            with self.subTest(addition=addition):
                doc = copy.deepcopy(self.doc)
                doc["data"]["tone"].update(addition)
                with self.assertRaises(hlx.PresetError):
                    self.build(doc=doc)

    def test_integer_enum_rejects_fraction(self):
        plan = copy.deepcopy(self.plan)
        plan["edits"][0].update(path="/data/tone/dsp0/block0/Choice", expect=1,
                                value=1.5, kind="integer", min=0, max=2)
        with self.assertRaises(hlx.PresetError):
            self.build(plan)
        plan["edits"][0]["kind"] = "number"
        with self.assertRaises(hlx.PresetError):
            self.build(plan)

    def test_malformed_kind_and_huge_number_are_controlled_errors(self):
        for replacement in [{"kind": []}, {"kind": {}}, {"value": 10 ** 400}]:
            plan = copy.deepcopy(self.plan)
            plan["edits"][0].update(replacement)
            with self.assertRaises(hlx.PresetError):
                self.build(plan)

    def test_integer_comparison_does_not_use_float_tolerance(self):
        self.assertEqual(len(list(hlx.differences(1000000, 1000001))), 1)
        self.assertEqual(len(list(hlx.differences(1000000, 1000000.5))), 1)
        self.assertEqual(list(hlx.differences(1, 1.0)), [])

    def test_duplicate_edits_rejected(self):
        plan = copy.deepcopy(self.plan)
        plan["edits"].append(copy.deepcopy(plan["edits"][0]))
        with self.assertRaises(hlx.PresetError):
            self.build(plan)

    def test_wrong_device_and_unknown_format_rejected(self):
        for field, value in [("device", 2162694), ("version", 99)]:
            doc = copy.deepcopy(self.doc)
            if field == "device":
                doc["data"][field] = value
            else:
                doc[field] = value
            with self.assertRaises(hlx.PresetError):
                self.build(doc=doc)

    def test_json_duplicate_and_overflow_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            for text in ['{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}']:
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(hlx.PresetError):
                    hlx.read_json(path)

    def test_existing_output_and_source_never_overwritten(self):
        result, _ = self.build()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "TEST_DO_NOT_IMPORT.hlx"
            report = hlx.write_build(path, result, {"hardware_import_verified": False})
            original = path.read_bytes()
            with self.assertRaises(hlx.PresetError):
                hlx.write_build(path, self.doc, {})
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(json.loads(report.read_text())["hardware_import_verified"])

    def test_comparison_ignores_metadata_but_detects_tone_changes(self):
        actual = copy.deepcopy(self.doc)
        actual["data"]["meta"]["appversion"] += 1
        actual["data"]["tone"]["dsp0"]["block0"]["RawLevel"] += 1e-8
        self.assertEqual(list(hlx.differences(hlx.comparison_payload(self.doc),
                                            hlx.comparison_payload(actual))), [])
        actual["data"]["tone"]["dsp0"]["block0"]["RawLevel"] = 0.9
        self.assertEqual(len(list(hlx.differences(hlx.comparison_payload(self.doc),
                                                hlx.comparison_payload(actual)))), 1)


if __name__ == "__main__":
    unittest.main()
