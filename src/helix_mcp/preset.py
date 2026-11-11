#!/usr/bin/env python3
"""Inspect and conservatively edit an exported Helix Floor preset.

This is an offline template editor, not a Line 6 schema validator or USB driver.
No model definitions, factory presets, or guessed parameter defaults are bundled.
Python 3.10+; standard library only.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


FLOOR_DEVICE_ID = 2162689
MAX_INPUT_BYTES = 8 * 1024 * 1024
NAME_RE = re.compile(r"[A-Za-z0-9 _-]{1,16}\Z")
BLOCK_RE = re.compile(r"block\d+\Z")


class PresetError(ValueError):
    pass


def reject_constant(value):
    raise PresetError(f"Non-finite JSON number: {value}")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PresetError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def walk(value, path=()):
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk(child, path + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk(child, path + (str(index),))


def pointer(parts):
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def split_pointer(value):
    if not isinstance(value, str) or not value.startswith("/"):
        raise PresetError("An edit path must be an absolute JSON pointer.")
    parts = value[1:].split("/")
    if any(re.search(r"~(?![01])", part) for part in parts):
        raise PresetError(f"Invalid JSON pointer escaping: {value}")
    return [p.replace("~1", "/").replace("~0", "~") for p in parts]


def read_json(path):
    path = Path(path)
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise PresetError(f"Input exceeds {MAX_INPUT_BYTES} bytes: {path}")
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_object,
                          parse_constant=reject_constant)
    except (ValueError, RecursionError) as exc:
        raise PresetError(f"Invalid or unsupported JSON: {exc}") from exc
    for parts, value in walk(data):
        if isinstance(value, float) and not math.isfinite(value):
            raise PresetError(f"Non-finite number at {pointer(parts)}")
    return data, hashlib.sha256(raw).hexdigest()


def validate_template(doc):
    if not isinstance(doc, dict):
        raise PresetError("The preset root must be an object.")
    if doc.get("schema", "L6Preset") != "L6Preset":
        raise PresetError("Only single L6Preset files are supported, not setlists/bundles.")
    if type(doc.get("version")) is not int or doc["version"] != 6:
        raise PresetError("This starter supports observed format version 6 only.")
    data = doc.get("data")
    if (not isinstance(data, dict) or type(data.get("device")) is not int
            or data["device"] != FLOOR_DEVICE_ID):
        raise PresetError("Use an unmodified .hlx exported from the target Helix Floor.")
    meta, tone = data.get("meta"), data.get("tone")
    if not isinstance(meta, dict) or not isinstance(meta.get("name"), str):
        raise PresetError("Missing data.meta.name.")
    if not isinstance(tone, dict) or not isinstance(tone.get("dsp0"), dict):
        raise PresetError("Missing data.tone.dsp0.")
    return tone


def nonempty(value):
    return value is not None and value != {} and value != [] and value != ""


def assignment_risks(tone):
    """Conservative screening; not a complete reverse engineered schema."""
    risks = []
    for parts, value in walk(tone):
        if not parts or not nonempty(value):
            continue
        key = parts[-1].lower().lstrip("@")
        if key in {"controller", "controllers", "assignment", "assignments", "param"}:
            risks.append(pointer(("data", "tone") + parts))
    metadata = {"@name", "@tempo", "@pedalstate", "@ledcolor", "@valid"}
    for key, snapshot in tone.items():
        if not re.fullmatch(r"snapshot\d+", key):
            continue
        if not isinstance(snapshot, dict):
            risks.append(pointer(("data", "tone", key)))
            continue
        for field, value in snapshot.items():
            base = ("data", "tone", key, field)
            if field in metadata and not isinstance(value, (dict, list)):
                continue
            if field == "blocks":
                # A known bypass-state tree consists only of object nodes/bool leaves.
                if any(not isinstance(v, (dict, bool)) for _, v in walk(value)):
                    risks.append(pointer(base))
            elif nonempty(value):
                risks.append(pointer(base))
    return sorted(set(risks))


def inspect_template(doc, digest):
    tone = validate_template(doc)
    blocks = []
    for dsp_name in ("dsp0", "dsp1"):
        dsp = tone.get(dsp_name, {})
        if not isinstance(dsp, dict):
            raise PresetError(f"Invalid {dsp_name}.")
        for block_name, block in dsp.items():
            if not BLOCK_RE.fullmatch(block_name) or not isinstance(block, dict):
                continue
            params = []
            for key, value in block.items():
                if not key.startswith("@") and type(value) in (int, float, bool):
                    params.append({"path": pointer(("data", "tone", dsp_name, block_name, key)),
                                   "value": value, "python_type": type(value).__name__})
            blocks.append({"path": pointer(("data", "tone", dsp_name, block_name)),
                           "model": block.get("@model"),
                           "enabled": block.get("@enabled"), "parameters": params})
    return {"name": doc["data"]["meta"]["name"], "template_sha256": digest,
            "device_id": doc["data"]["device"], "format_version": doc["version"],
            "blocks": blocks, "assignment_or_snapshot_risks": assignment_risks(tone),
            "hardware_import_verified": False,
            "note": "Raw file values are not necessarily HX Edit display values. "
                    "Ranges and units must be verified for each existing parameter."}


def validate_name(name):
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or not name.strip():
        raise PresetError("Use 1-16 ASCII letters/digits/spaces/_/- for this starter's name.")


def same_value(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def build_preset(doc, template_digest, plan):
    tone = validate_template(doc)
    if (not isinstance(plan, dict) or type(plan.get("plan_version")) is not int
            or plan["plan_version"] != 1):
        raise PresetError("Unsupported or missing plan_version.")
    if plan.get("template_sha256") != template_digest:
        raise PresetError("Template hash changed; inspect it and create a fresh plan.")
    name = plan.get("name")
    validate_name(name)
    edits = plan.get("edits")
    if not isinstance(edits, list):
        raise PresetError("edits must be a JSON array.")
    risks = assignment_risks(tone)
    if edits and risks:
        raise PresetError("Parameter edits require an unassigned, simple template. "
                          "Review these snapshot/controller paths: " + ", ".join(risks[:12]))
    result = copy.deepcopy(doc)
    changes, seen = [], set()
    for edit in edits:
        if not isinstance(edit, dict):
            raise PresetError("Each edit must be an object.")
        required = {"path", "expect", "value", "model", "kind", "evidence"}
        if not required.issubset(edit):
            raise PresetError("An edit lacks path/expect/value/model/kind/evidence.")
        parts = split_pointer(edit["path"])
        if (len(parts) != 5 or parts[:2] != ["data", "tone"]
                or parts[2] not in {"dsp0", "dsp1"}
                or not BLOCK_RE.fullmatch(parts[3]) or parts[4].startswith("@")):
            raise PresetError("Only an existing direct block parameter may be edited: " + edit["path"])
        canonical = pointer(parts)
        if canonical in seen:
            raise PresetError("Duplicate edit path: " + canonical)
        seen.add(canonical)
        try:
            block = result["data"]["tone"][parts[2]][parts[3]]
            old = block[parts[4]]
        except (KeyError, TypeError) as exc:
            raise PresetError("Parameter does not exist: " + canonical) from exc
        if not isinstance(block, dict) or not isinstance(block.get("@model"), str):
            raise PresetError("Block has no observed model ID: " + canonical)
        if block["@model"] != edit["model"]:
            raise PresetError("Model mismatch: " + canonical)
        if not same_value(old, edit["expect"]):
            raise PresetError("Expected value mismatch: " + canonical)
        if not isinstance(edit["evidence"], str) or not edit["evidence"].strip():
            raise PresetError("Record the source for the raw value/range: " + canonical)
        value, kind = edit["value"], edit["kind"]
        if not isinstance(kind, str):
            raise PresetError("kind must be a string: " + canonical)
        if kind == "boolean":
            if type(old) is not bool or type(value) is not bool:
                raise PresetError("Boolean edit has incompatible types: " + canonical)
        elif kind in {"number", "integer"}:
            if not finite_number(old) or not finite_number(value):
                raise PresetError("Numeric edit has incompatible/non-finite values: " + canonical)
            if type(old) is int and kind != "integer":
                raise PresetError("An observed integer requires kind=integer; "
                                  "confirm its parameter schema before supporting fractional values: " + canonical)
            low, high = edit.get("min"), edit.get("max")
            if not finite_number(low) or not finite_number(high) or low > high:
                raise PresetError("Supply verified finite min/max bounds: " + canonical)
            if not (low <= old <= high and low <= value <= high):
                raise PresetError("Value is outside the supplied bounds: " + canonical)
            if kind == "integer" and (type(old) is not int or type(value) is not int):
                raise PresetError("Integer/enum edit requires integer values: " + canonical)
        else:
            raise PresetError("kind must be number, integer or boolean: " + canonical)
        block[parts[4]] = value
        changes.append({"path": canonical, "before": old, "after": value,
                        "model": edit["model"], "evidence": edit["evidence"]})
    old_name = result["data"]["meta"]["name"]
    result["data"]["meta"]["name"] = name
    changes.insert(0, {"path": "/data/meta/name", "before": old_name, "after": name})
    return result, changes


def encode_json(data):
    return (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def write_new(path, data):
    path = Path(path)
    encoded = encode_json(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with path.open("xb") as stream:
            created = True
            stream.write(encoded)
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise


def write_build(out, doc, report):
    out = Path(out)
    report_path = out.with_suffix(out.suffix + ".report.json")
    if out.suffix.lower() != ".hlx":
        raise PresetError("Output must have the .hlx extension.")
    if out.exists() or report_path.exists():
        raise PresetError("Output already exists. Choose a new revision filename.")
    encoded = encode_json(doc)
    report["output_sha256"] = hashlib.sha256(encoded).hexdigest()
    out.parent.mkdir(parents=True, exist_ok=True)
    created = []
    try:
        with out.open("xb") as preset_stream:
            created.append(out)
            with report_path.open("xb") as report_stream:
                created.append(report_path)
                preset_stream.write(encoded)
                report_stream.write(encode_json(report))
    except Exception:
        for created_path in created:
            created_path.unlink(missing_ok=True)
        raise
    return report_path


def differences(a, b, parts=()):
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            path = parts + (key,)
            if key not in a:
                yield {"path": pointer(path), "kind": "added", "actual": b[key]}
            elif key not in b:
                yield {"path": pointer(path), "kind": "missing", "expected": a[key]}
            else:
                yield from differences(a[key], b[key], path)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            yield {"path": pointer(parts), "kind": "list_length", "expected": len(a), "actual": len(b)}
        for i, (left, right) in enumerate(zip(a, b)):
            yield from differences(left, right, parts + (str(i),))
    elif type(a) is int and type(b) is int:
        if a != b:
            yield {"path": pointer(parts), "kind": "value", "expected": a, "actual": b}
    elif type(a) is int or type(b) is int:
        if not finite_number(a) or not finite_number(b) or not same_value(a, b):
            yield {"path": pointer(parts), "kind": "value", "expected": a, "actual": b}
    elif finite_number(a) and finite_number(b):
        if not math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-5):
            yield {"path": pointer(parts), "kind": "value", "expected": a, "actual": b}
    elif type(a) is not type(b) or a != b:
        yield {"path": pointer(parts), "kind": "value", "expected": a, "actual": b}


def comparison_payload(doc):
    validate_template(doc)
    return {"device": doc["data"]["device"], "name": doc["data"]["meta"]["name"],
            "tone": doc["data"]["tone"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    inspect = subs.add_parser("inspect", help="List observed blocks/values without modifying anything")
    inspect.add_argument("template", type=Path)
    inspect.add_argument("--out", type=Path)
    plan = subs.add_parser("plan", help="Create a hash-bound empty edit plan")
    plan.add_argument("template", type=Path)
    plan.add_argument("--name", required=True)
    plan.add_argument("--out", required=True, type=Path)
    build = subs.add_parser("build", help="Build a separate candidate .hlx from explicit observed edits")
    build.add_argument("template", type=Path)
    build.add_argument("--plan", required=True, type=Path)
    build.add_argument("--out", required=True, type=Path)
    compare = subs.add_parser("compare", help="Compare intended tone with an HX Edit re-export")
    compare.add_argument("expected", type=Path)
    compare.add_argument("actual", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "compare":
            expected, _ = read_json(args.expected)
            actual, _ = read_json(args.actual)
            changes = list(differences(comparison_payload(expected), comparison_payload(actual)))
            result = {"tone_data_matches": not changes, "differences": changes,
                      "note": "A data comparison does not verify the selected hardware slot or audible sound."}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1 if changes else 0
        doc, digest = read_json(args.template)
        summary = inspect_template(doc, digest)
        if args.command == "inspect":
            if args.out:
                write_new(args.out, summary)
            else:
                print(json.dumps(summary, ensure_ascii=False, indent=2))
        elif args.command == "plan":
            validate_name(args.name)
            result = {"plan_version": 1, "template_sha256": digest, "name": args.name,
                      "song": {"artist": "", "title": "", "version_or_section": ""},
                      "design_notes": "", "sources": [], "edits": []}
            write_new(args.out, result)
            print(json.dumps({"plan": str(args.out), "status": "empty_plan_not_a_song_preset"}))
        else:
            plan_doc, plan_digest = read_json(args.plan)
            generated, changes = build_preset(doc, digest, plan_doc)
            report = {"created_utc": datetime.now(timezone.utc).isoformat(),
                      "template_sha256": digest, "plan_sha256": plan_digest,
                      "status": "candidate_not_imported", "hardware_import_verified": False,
                      "changes": changes, "song": plan_doc.get("song"),
                      "design_notes": plan_doc.get("design_notes"), "sources": plan_doc.get("sources", [])}
            report_path = write_build(args.out, generated, report)
            print(json.dumps({"preset": str(args.out), "report": str(report_path),
                              "status": "candidate_not_imported"}, ensure_ascii=False))
        return 0
    except (OSError, UnicodeError, json.JSONDecodeError, PresetError, RecursionError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
