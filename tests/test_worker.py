"""Worker boundary tests; no Windows apps, hardware, or live network are used."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error

from helix_mcp import worker


class Response:
    def __init__(self, data: bytes):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, limit):
        return self.data[:limit]


class Opener:
    def __init__(self, data=b'{}', error=None):
        self.data = data
        self.error = error
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        return Response(self.data)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.token_file = self.root / "worker-token.txt"
        self.token = "unit-test-worker-token-0123456789abcdef"
        self.token_file.write_text(self.token + "\n", encoding="ascii")
        self.config = worker.WorkerConfig("https://helix.example.test", self.token_file, "desktop-1", self.root / "data")

    def job(self, kind="hx_import", payload=None, job_id="job-1", expires_at=None):
        if payload is None:
            text = '{"data":{"meta":{"name":"Lead Test"}}}'
            payload = {"preset_json": text, "sha256": hashlib.sha256(text.encode()).hexdigest(),
                       "target_setlist": 8, "target_setlist_name": "AI Tones", "target_slot": "01A",
                       "preset_name": "Lead Test", "launch_hx_edit": False}
        return worker.Job(job_id, kind, payload, "unit-lease-token", expires_at or time.time() + 300)

    def envelope(self, job):
        return {"id": job.id, "kind": job.kind, "payload": job.payload, "lease_token": job.lease_token, "expires_at": job.expires_at}

    def test_https_required_and_loopback_exception_is_explicit(self):
        for url in ("http://helix.example.test", "http://127.0.0.1:8000", "file:///tmp/server", "https://user:secret@example.test", "https://example.test?token=x", "https://example.test/mcp", "https://example.test/worker", "https://example.test//"):
            with self.subTest(url=url), self.assertRaises(worker.WorkerError):
                worker.WorkerConfig(url, self.token_file, "pc", self.root)
        for url in ("http://127.0.0.1:8000", "http://[::1]:8000", "http://localhost:8000"):
            worker.WorkerConfig(url, self.token_file, "pc", self.root, allow_loopback_http=True)
        with self.assertRaises(worker.WorkerError):
            worker.WorkerConfig("http://192.168.1.2:8000", self.token_file, "pc", self.root, allow_loopback_http=True)
        worker.WorkerConfig("https://example.test/", self.token_file, "pc", self.root)

    def test_token_length_and_visible_ascii_match_server_requirements(self):
        for valid in ("a" * 32, "a" * 512):
            self.token_file.write_text(valid + "\r\n", encoding="ascii")
            self.assertEqual(self.config.read_token(), valid)
        for invalid in ("a" * 31, "a" * 513, "a" * 32 + " " + "b", "a" * 32 + "\x7f", "a" * 32 + "\n" + "b"):
            with self.subTest(length=len(invalid)):
                self.token_file.write_text(invalid, encoding="ascii")
                with self.assertRaises(worker.WorkerError):
                    self.config.read_token()

    def test_server_url_from_env_rejects_endpoint_path(self):
        env = {"HELIX_SERVER_URL": "https://example.test/mcp", "HELIX_WORKER_TOKEN_FILE": str(self.token_file),
               "HELIX_WORKER_ID": "pc", "HELIX_WORKER_DATA_DIR": str(self.root)}
        with self.assertRaises(worker.WorkerError):
            worker.WorkerConfig.from_env(env)

    def test_token_file_not_environment_token_and_http_header(self):
        opener = Opener(b'{"job": null}')
        client = worker.WorkerClient(self.config, opener=opener)
        self.assertEqual(client.post("/worker/poll", {"worker_id": "desktop-1"}), {"job": None})
        request, timeout = opener.requests[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.token)
        self.assertEqual(request.method, "POST")
        self.assertEqual(timeout, 15)
        self.assertIsNone(worker._NoRedirects().redirect_request(request, None, 302, "redirect", {}, "https://elsewhere.test"))

    def test_transport_errors_do_not_echo_tokens_or_server_body(self):
        opener = Opener(error=urllib.error.URLError("secret " + self.token))
        with self.assertRaises(worker.TransportError) as raised:
            worker.WorkerClient(self.config, opener=opener).post("/worker/poll", {})
        self.assertNotIn(self.token, str(raised.exception))
        with self.assertRaises(worker.TransportError):
            worker.WorkerClient(self.config, opener=Opener(b"x" * (worker.MAX_RESPONSE_BYTES + 1))).post("/worker/poll", {})

    def test_job_parser_rejects_paths_shell_commands_and_invalid_expiry(self):
        original = self.envelope(self.job())
        for key, value in (("id", "../../outside"), ("kind", "shell"), ("expires_at", True), ("expires_at", "2026-01-01T00:00:00")):
            with self.subTest(key=key), self.assertRaises(worker.WorkerError):
                worker.Job.parse({**original, key: value})
        parsed = worker.Job.parse({**original, "expires_at": "2099-01-01T00:00:00Z"})
        self.assertGreater(parsed.expires_at, time.time())
        self.assertNotIn(parsed.lease_token, repr(parsed))

    def test_staging_preserves_bytes_hash_and_explicit_manual_status(self):
        runner = worker.JobRunner(self.config)
        job = self.job()
        with patch.object(runner, "_run_powershell", side_effect=AssertionError("No launch requested")):
            outcome = runner.execute(job)
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.result["status"], "manual_import_required")
        self.assertIs(outcome.result["hardware_applied"], False)
        preset = Path(outcome.result["preset_path"])
        self.assertEqual(preset.read_bytes(), job.payload["preset_json"].encode("utf-8"))
        self.assertTrue(preset.is_relative_to(self.config.data_dir.resolve()))
        manifest = json.loads(Path(outcome.result["manifest_path"]).read_text())
        self.assertEqual(manifest["target_setlist"], 8)
        self.assertEqual(manifest["target_slot"], "01A")
        self.assertEqual(manifest["target_setlist_name"], "AI Tones")
        self.assertIs(manifest["hardware_applied"], False)
        self.assertNotIn("lease_token", manifest)
        self.assertEqual(list((self.config.data_dir / "spool").glob(".stage-*")), [])

    def test_hash_mismatch_unknown_fields_and_path_payload_are_rejected(self):
        for mutation in ({"sha256": "0" * 64}, {"output_path": "C:/elsewhere/preset.hlx"}, {"executable_path": "C:/evil.exe"}, {"target_setlist": True}, {"target_slot": "../../file"}):
            job = self.job()
            job.payload.update(mutation)
            runner = worker.JobRunner(self.config)
            with patch.object(runner, "_stage_preset") as stage:
                outcome = runner.execute(job)
            self.assertFalse(outcome.success)
            self.assertEqual(outcome.result["status"], "rejected")
            stage.assert_not_called()

    def test_inspection_process_id_cannot_be_command_or_boolean(self):
        runner = worker.JobRunner(self.config)
        for payload in ({"process_id": "12;evil"}, {"process_id": True}, {"process_id": 12, "timeout_seconds": 61}, {"process_id": 12, "max_nodes": 2001}, {"process_id": 12, "script": "evil"}):
            with self.subTest(payload=payload), patch.object(runner, "_diagnostic") as diagnostic:
                self.assertFalse(runner.execute(self.job("hx_inspect", payload)).success)
                diagnostic.assert_not_called()

    def test_same_job_reuses_result_and_changed_job_never_runs_again(self):
        runner = worker.JobRunner(self.config)
        original = self.job()
        first = runner.execute(original)
        with patch.object(runner, "_stage_preset", side_effect=AssertionError("Must not repeat")):
            second = runner.execute(original)
            changed = self.job()
            changed.payload["target_slot"] = "02A"
            third = runner.execute(changed)
        self.assertEqual(second.as_dict(), first.as_dict())
        self.assertFalse(third.success)
        self.assertEqual(third.result["status"], "previous_outcome_unknown")

    def test_incomplete_journal_and_existing_spool_prevent_overwrite(self):
        runner = worker.JobRunner(self.config)
        incomplete = self.config.data_dir / "jobs" / "job-1"
        incomplete.mkdir(parents=True)
        result = runner.execute(self.job())
        self.assertEqual(result.result["status"], "previous_outcome_unknown")
        existing = self.config.data_dir / "spool" / "job-2"
        existing.mkdir(parents=True)
        (existing / "preset.hlx").write_bytes(b"preserve-me")
        result = runner.execute(self.job(job_id="job-2"))
        self.assertFalse(result.success)
        self.assertEqual((existing / "preset.hlx").read_bytes(), b"preserve-me")

    def test_expired_lease_has_no_side_effect(self):
        runner = worker.JobRunner(self.config)
        result = runner.execute(self.job(expires_at=time.time() - 10))
        self.assertEqual(result.result["status"], "lease_expired")
        self.assertEqual(list(self.config.data_dir.iterdir()), [])

    def test_launch_failure_does_not_claim_import_or_lose_staged_file(self):
        runner = worker.JobRunner(self.config)
        job = self.job()
        job.payload["launch_hx_edit"] = True
        with patch.object(runner, "_run_powershell", side_effect=worker.WorkerError("Configured executable missing.")):
            outcome = runner.execute(job)
        self.assertTrue(outcome.success)
        self.assertFalse(outcome.result["launch"]["success"])
        self.assertFalse(outcome.result["hardware_applied"])
        self.assertTrue(Path(outcome.result["preset_path"]).is_file())

    def test_powershell_uses_argument_list_fixed_resource_and_no_token_env(self):
        runner = worker.JobRunner(self.config)
        system_root = self.root / "Windows"
        powershell = system_root / "System32/WindowsPowerShell/v1.0/powershell.exe"
        powershell.parent.mkdir(parents=True)
        powershell.write_bytes(b"unit-test-placeholder")
        seen = []

        def fake_run(args, **kwargs):
            seen.append((args, kwargs))
            output = Path(args[args.index("-OutputPath") + 1])
            output.write_text(json.dumps({"Status": "ok", "PresetsModified": False, "Processes": []}), encoding="utf-8")
            return subprocess.CompletedProcess(args, 0)

        with patch.object(worker, "_interactive_windows"), patch.dict(os.environ, {"SystemRoot": str(system_root), "HELIX_WORKER_TOKEN_FILE": "secret-path", "HELIX_WORKER_TOKEN": "never-forward"}), patch.object(worker.subprocess, "run", side_effect=fake_run):
            outcome = runner.execute(self.job("hx_inspect", {"process_id": 123, "max_nodes": 20}))
        self.assertTrue(outcome.success)
        args, kwargs = seen[0]
        self.assertIsInstance(args, list)
        self.assertEqual(args[0], str(powershell))
        self.assertEqual(Path(args[args.index("-File") + 1]).name, "HxEdit.Windows.ps1")
        self.assertNotIn("-ExecutionPolicy", args)
        self.assertEqual(args[args.index("-ProcessId") + 1], "123")
        self.assertIs(kwargs["shell"], False)
        self.assertLessEqual(kwargs["timeout"], 75)
        self.assertFalse(any(key.upper().startswith("HELIX_") for key in kwargs["env"]))

    def test_large_diagnostic_result_is_bounded_and_never_claims_applied(self):
        result = worker._bounded_result({"status": "ok", "hardware_applied": True, "diagnostic": "x" * worker.MAX_RESULT_BYTES})
        self.assertFalse(result["hardware_applied"])
        self.assertTrue(result["result_truncated"])
        self.assertLess(len(worker._json_bytes(result)), worker.MAX_RESULT_BYTES)

    def test_result_delivery_retry_never_reexecutes_job(self):
        job = self.job()

        class Client:
            def __init__(self, envelope):
                self.envelope = envelope
                self.results = []

            def post(self, route, body):
                if route == "/worker/poll":
                    return {"job": self.envelope}
                self.results.append(body)
                if len(self.results) < 3:
                    raise worker.TransportError("Connection failed.")
                return {"accepted": True}

        client = Client(self.envelope(job))
        runner = worker.JobRunner(self.config)
        with patch.object(runner, "execute", wraps=runner.execute) as execute, patch.object(worker.time, "sleep"):
            status = worker.WindowsWorker(self.config, client=client, runner=runner).run_once()
        self.assertEqual(status, "completed")
        execute.assert_called_once()
        self.assertEqual(len(client.results), 3)
        self.assertEqual(client.results[0], client.results[-1])
        self.assertFalse(client.results[-1]["result"]["hardware_applied"])


if __name__ == "__main__":
    unittest.main()
