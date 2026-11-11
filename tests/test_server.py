"""MCP transport, authentication and candidate-to-worker flow without hardware."""
import json
import tempfile
import unittest
from pathlib import Path

from starlette.testclient import TestClient

from helix_mcp.config import Settings
from helix_mcp.server import create_app
from helix_mcp.worker import Job, JobRunner, WorkerConfig


def synthetic_preset():
    # Fabricated fixture. Never import into a Line 6 device.
    return {"version": 6, "schema": "L6Preset",
            "data": {"device": 2162689, "meta": {"name": "TEST_ONLY"},
                     "tone": {"dsp0": {"block0": {"@model": "TEST_ONLY_MODEL",
                                                  "@enabled": True, "Drive": 0.5}},
                              "dsp1": {}, "snapshot0": {"@name": "TEST", "@valid": True}}}}


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(Path(self.temp.name), "m" * 48, "w" * 48,
                                 allowed_hosts=("testserver",), target_setlist=8, target_slot="01A")
        self.app = create_app(self.settings)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.seq = 0
        self.mcp_headers = {"Authorization": "Bearer " + self.settings.mcp_token,
                            "Accept": "application/json, text/event-stream",
                            "MCP-Protocol-Version": "2025-11-25"}
        self.worker_headers = {"Authorization": "Bearer " + self.settings.worker_token}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def rpc(self, method, params):
        self.seq += 1
        response = self.client.post("/mcp", headers=self.mcp_headers,
                                    json={"jsonrpc": "2.0", "id": self.seq, "method": method, "params": params})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def tool(self, name, arguments=None):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments or {}})["result"]
        self.assertFalse(result.get("isError", False), result)
        return result["structuredContent"]

    def test_real_initialize_list_and_status_protocol(self):
        result = self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                         "clientInfo": {"name": "software-test", "version": "1"}})
        self.assertEqual(result["result"]["serverInfo"]["name"], "Helix Editor")
        self.assertIn("manual import", result["result"]["instructions"])
        names = {tool["name"] for tool in self.rpc("tools/list", {})["result"]["tools"]}
        self.assertEqual(len(names), 12)
        self.assertIn("stage_preset_for_windows", names)
        self.assertFalse(self.tool("service_status")["automatic_hardware_import"])

    def test_role_tokens_are_not_interchangeable(self):
        response = self.client.post("/mcp", json={})
        self.assertEqual(response.status_code, 401)
        self.assertIn("Bearer", response.headers["www-authenticate"])
        self.assertEqual(self.client.post("/mcp", headers=self.worker_headers, json={}).status_code, 401)
        self.assertEqual(self.client.post("/worker/poll", headers=self.mcp_headers,
                                         json={"worker_id": self.settings.worker_id}).status_code, 401)
        self.assertEqual(self.client.post("/worker/poll", headers=self.worker_headers,
                                         json={"worker_id": "another-worker"}).status_code, 403)

    def test_host_origin_body_and_unknown_fields(self):
        self.assertEqual(self.client.get("/healthz", headers={"Host": "evil.example"}).status_code, 421)
        self.assertEqual(self.client.post("/mcp", headers={**self.mcp_headers, "Origin": "https://evil.example"}, json={}).status_code, 403)
        self.assertEqual(self.client.post("/worker/poll", headers={**self.worker_headers, "Content-Length": "1048577"}, content=b"{}").status_code, 413)
        self.assertEqual(self.client.post("/worker/poll", headers=self.worker_headers,
                                         json={"worker_id": self.settings.worker_id, "command": "anything"}).status_code, 400)
        self.assertEqual(self.client.get("/healthz").json()["status"], "ok")

    def test_template_candidate_staging_and_readback_flow(self):
        raw = json.dumps(synthetic_preset())
        registered = self.tool("register_template", {"display_name": "test fixture only", "preset_json": raw})
        template_id = registered["id"]
        self.assertEqual(len(self.tool("list_templates")["templates"]), 1)
        inspection = self.tool("inspect_template", {"template_id": template_id})
        plan = self.tool("create_edit_plan", {"template_id": template_id, "name": "TEST_LEAD"})
        plan["edits"] = [{"path": "/data/tone/dsp0/block0/Drive", "expect": 0.5, "value": 0.65,
                          "model": inspection["blocks"][0]["model"], "kind": "number",
                          "min": 0, "max": 1, "evidence": "Synthetic fixture, not a real model"}]
        candidate = self.tool("build_candidate", {"template_id": template_id, "plan": plan})
        self.assertEqual(candidate["status"], "candidate_not_imported")
        content = self.tool("get_candidate", {"candidate_id": candidate["id"]})
        self.assertEqual(json.loads(content["preset_json"])["data"]["tone"]["dsp0"]["block0"]["Drive"], 0.65)
        original = self.tool("inspect_template", {"template_id": template_id})
        self.assertEqual(original["blocks"][0]["parameters"][0]["value"], 0.5)
        compared = self.tool("compare_readback", {"candidate_id": candidate["id"], "readback_json": content["preset_json"]})
        self.assertTrue(compared["tone_data_matches"])
        self.assertFalse(compared["target_slot_verified"])
        args = {"candidate_id": candidate["id"], "request_id": "test-stage-1"}
        job = self.tool("stage_preset_for_windows", args)
        self.assertEqual(self.tool("stage_preset_for_windows", args)["id"], job["id"])
        claim = self.client.post("/worker/poll", headers=self.worker_headers,
                                 json={"worker_id": self.settings.worker_id}).json()["job"]
        self.assertEqual(claim["payload"]["sha256"], content["sha256"])
        self.assertEqual(claim["payload"]["target_slot"], "01A")
        # Run the actual staging handler too; no GUI or hardware is involved.
        token_file = Path(self.temp.name) / "worker-token.txt"
        token_file.write_text(self.settings.worker_token)
        worker_config = WorkerConfig("https://helix.example", token_file, self.settings.worker_id,
                                     Path(self.temp.name) / "windows-spool")
        outcome = JobRunner(worker_config).execute(Job.parse(claim))
        self.assertTrue(outcome.success, outcome.error)
        self.assertEqual(Path(outcome.result["preset_path"]).read_text(), content["preset_json"])
        result_body = {"worker_id": self.settings.worker_id, "lease_token": claim["lease_token"],
                       **outcome.as_dict()}
        result_url = "/worker/jobs/" + job["id"] + "/result"
        self.assertEqual(self.client.post(result_url, headers=self.worker_headers, json=result_body).status_code, 200)
        self.assertEqual(self.client.post(result_url, headers=self.worker_headers, json=result_body).status_code, 200)
        final = self.tool("get_job", {"job_id": job["id"]})
        self.assertEqual(final["status"], "succeeded")
        self.assertFalse(final["hardware_applied"])
        self.assertEqual(final["result"]["status"], "manual_import_required")

    def test_mcp_invalid_inputs_return_tool_error_without_server_paths(self):
        result = self.rpc("tools/call", {"name": "inspect_template", "arguments": {"template_id": "../../secret"}})["result"]
        self.assertTrue(result["isError"])
        self.assertNotIn(self.temp.name, json.dumps(result))
        result = self.rpc("tools/call", {"name": "register_template", "arguments": {"display_name": "bad", "preset_json": "not-json"}})["result"]
        self.assertTrue(result["isError"])


if __name__ == "__main__":
    unittest.main()
