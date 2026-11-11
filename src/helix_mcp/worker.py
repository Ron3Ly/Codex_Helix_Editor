"""Outbound-only Windows worker for the bounded Helix job queue.

The import job deliberately stages a preset for manual import. No handler in
this version writes a preset to Helix hardware or simulates keyboard/mouse input.
Run this worker as the logged-in Windows user, not as a Session 0 service.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client
import importlib.resources
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping
import urllib.error
import urllib.parse
import urllib.request


LOG = logging.getLogger("helix_worker")
MAX_PRESET_BYTES = 256 * 1024
MAX_RESULT_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_DIAGNOSTIC_BYTES = 4 * 1024 * 1024
JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
WORKER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
SLOT = re.compile(r"(?:0[1-9]|[12][0-9]|3[0-2])[A-D]\Z")
ALLOWED_KINDS = frozenset({"hx_discover", "hx_launch", "hx_inspect", "hx_import"})


class WorkerError(Exception):
    """A short, non-secret error suitable for reporting to the server."""


class TransportError(WorkerError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON numbers are not supported.")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _read_json(path: Path, limit: int) -> Any:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise WorkerError("Local diagnostic or journal exceeds its size limit.")
    try:
        return json.loads(raw.decode("utf-8-sig"), parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise WorkerError("Local diagnostic or journal is not valid JSON.") from exc


def _write_new(path: Path, content: bytes) -> None:
    """Create without overwriting; spool directories are published atomically."""
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise WorkerError(f"{name} must be an integer between {minimum} and {maximum}.")
    return value


def _keys(payload: dict[str, Any], allowed: set[str], required: set[str] | None = None) -> None:
    if set(payload) - allowed:
        raise WorkerError("The job contains unsupported payload fields.")
    if required and not required <= set(payload):
        raise WorkerError("The job is missing required payload fields.")


def _bounded_result(result: dict[str, Any]) -> dict[str, Any]:
    # No job in this version can claim that a hardware preset has been applied.
    result = dict(result)
    result["hardware_applied"] = False
    if len(_json_bytes(result)) <= MAX_RESULT_BYTES:
        return result
    return {
        "status": str(result.get("status", "diagnostic_ready"))[:80],
        "hardware_applied": False,
        "result_truncated": True,
        "diagnostic_path": str(result.get("diagnostic_path", ""))[:2048],
        "message": "The complete diagnostic remains on the Windows worker; the upload was size-limited.",
    }


@dataclass(frozen=True)
class WorkerConfig:
    server_url: str
    token_file: Path
    worker_id: str
    data_dir: Path
    hx_edit_exe: Path | None = None
    allow_loopback_http: bool = False
    request_timeout: float = 15.0

    def __post_init__(self) -> None:
        try:
            parsed = urllib.parse.urlsplit(self.server_url)
            port = parsed.port
        except ValueError as exc:
            raise WorkerError("HELIX_SERVER_URL is invalid.") from exc
        if (not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or any(c.isspace() for c in self.server_url)):
            raise WorkerError("HELIX_SERVER_URL must be an origin URL without a path, credentials, query, or fragment; do not append /mcp.")
        loopback = parsed.hostname.lower() == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            pass
        if parsed.scheme != "https" and not (parsed.scheme == "http" and self.allow_loopback_http and loopback):
            raise WorkerError("HTTPS is required; HTTP is allowed only for explicitly enabled loopback tests.")
        if port is not None and not 1 <= port <= 65535:
            raise WorkerError("HELIX_SERVER_URL has an invalid port.")
        if not WORKER_ID.fullmatch(self.worker_id):
            raise WorkerError("HELIX_WORKER_ID must contain 1-64 ASCII letters, digits, underscores, or hyphens.")
        if not math.isfinite(self.request_timeout) or not 1 <= self.request_timeout <= 60:
            raise WorkerError("HTTP timeout must be between 1 and 60 seconds.")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> WorkerConfig:
        env = os.environ if env is None else env
        names = ("HELIX_SERVER_URL", "HELIX_WORKER_TOKEN_FILE", "HELIX_WORKER_ID", "HELIX_WORKER_DATA_DIR")
        if any(not env.get(name, "").strip() for name in names):
            raise WorkerError("Set HELIX_SERVER_URL, HELIX_WORKER_TOKEN_FILE, HELIX_WORKER_ID, and HELIX_WORKER_DATA_DIR.")
        executable = env.get("HELIX_HX_EDIT_EXE", "").strip()
        return cls(
            server_url=env["HELIX_SERVER_URL"].strip(),
            token_file=Path(env["HELIX_WORKER_TOKEN_FILE"]).expanduser(),
            worker_id=env["HELIX_WORKER_ID"].strip(),
            data_dir=Path(env["HELIX_WORKER_DATA_DIR"]).expanduser(),
            hx_edit_exe=Path(executable).expanduser() if executable else None,
            allow_loopback_http=env.get("HELIX_WORKER_ALLOW_LOOPBACK_HTTP") == "1",
        )

    def read_token(self) -> str:
        try:
            with self.token_file.open("rb") as stream:
                raw = stream.read(4097)
            token = raw.decode("ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise WorkerError("The configured worker token file could not be read as ASCII.") from exc
        if len(raw) > 4096 or not 32 <= len(token) <= 512 or any(not 33 <= ord(c) <= 126 for c in token):
            raise WorkerError("The configured worker token file must contain a 32-512 character ASCII token without whitespace.")
        return token


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        # In particular, never forward the worker Authorization header elsewhere.
        return None


class WorkerClient:
    def __init__(self, config: WorkerConfig, *, opener: Any = None):
        self.config = config
        self.opener = opener or urllib.request.build_opener(
            _NoRedirects(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
        )

    def post(self, route: str, body: dict[str, Any]) -> dict[str, Any]:
        if not (route == "/worker/poll" or re.fullmatch(r"/worker/jobs/[A-Za-z0-9][A-Za-z0-9_-]{0,95}/result", route)):
            raise WorkerError("Unsupported worker API route.")
        encoded = _json_bytes(body)
        if len(encoded) > MAX_RESPONSE_BYTES:
            raise WorkerError("Worker request exceeds its size limit.")
        request = urllib.request.Request(
            self.config.server_url.rstrip("/") + route,
            data=encoded,
            headers={"Authorization": "Bearer " + self.config.read_token(), "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.config.request_timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # Do not log the response body, URL, request, or Authorization header.
            raise TransportError(f"Worker endpoint returned HTTP {exc.code}.", exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise TransportError("Worker endpoint connection failed.") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise TransportError("Worker endpoint response exceeded its size limit.")
        try:
            result = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant) if raw else {}
        except (ValueError, UnicodeError, RecursionError):
            raise TransportError("Worker endpoint did not return valid JSON.") from None
        if not isinstance(result, dict):
            raise TransportError("Worker endpoint did not return a JSON object.")
        return result


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    payload: dict[str, Any] = field(repr=False)
    lease_token: str = field(repr=False)
    expires_at: float

    @classmethod
    def parse(cls, value: Any) -> Job:
        if not isinstance(value, dict):
            raise WorkerError("The server returned an invalid job envelope.")
        if not {"id", "kind", "payload", "lease_token", "expires_at"} <= set(value):
            raise WorkerError("The server returned an incomplete job envelope.")
        job_id, kind, token = value["id"], value["kind"], value["lease_token"]
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise WorkerError("The server returned an invalid job identifier.")
        if not isinstance(kind, str) or kind not in ALLOWED_KINDS or not isinstance(value["payload"], dict):
            raise WorkerError("The server returned an unsupported job kind or payload.")
        if not isinstance(token, str) or not 1 <= len(token) <= 512 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise WorkerError("The server returned an invalid lease token.")
        expiry = value["expires_at"]
        try:
            if isinstance(expiry, str):
                parsed = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("Timezone required.")
                expiry = parsed.timestamp()
            elif type(expiry) not in (int, float):
                raise ValueError("Expiry must be a time.")
            if not math.isfinite(expiry):
                raise ValueError("Finite expiry required.")
        except (ValueError, TypeError, OverflowError):
            raise WorkerError("The server returned an invalid lease expiry.") from None
        return cls(job_id, kind, value["payload"], token, float(expiry))


@dataclass(frozen=True)
class Outcome:
    success: bool
    result: dict[str, Any]
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"success": self.success, "result": _bounded_result(self.result), "error": self.error}


def _interactive_windows() -> None:
    if sys.platform != "win32":
        raise WorkerError("HX Edit operations require a logged-in Windows desktop session.")
    import ctypes
    session_id = ctypes.c_uint32()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id)) or session_id.value == 0:
        raise WorkerError("Run the worker in the logged-in user's desktop session, not a Session 0 service.")


class JobRunner:
    def __init__(self, config: WorkerConfig):
        self.config = config
        self.root = config.data_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _directory(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(exist_ok=True)
        if not path.resolve().is_relative_to(self.root):
            raise WorkerError("Worker data directories must remain within HELIX_WORKER_DATA_DIR.")
        return path

    def execute(self, job: Job) -> Outcome:
        if job.expires_at <= time.time():
            return Outcome(False, {"status": "lease_expired", "hardware_applied": False}, "The job lease expired before execution.")
        try:
            self._validate_payload(job)
            fingerprint = hashlib.sha256(_json_bytes({"kind": job.kind, "payload": job.payload})).hexdigest()
            folder = self._directory("jobs") / job.id
            try:
                folder.mkdir()
            except FileExistsError:
                # A crash or lost result acknowledgement never authorizes rerunning
                # an action with the same ID. Only a complete matching result may be reused.
                if not folder.resolve().is_relative_to(self.root):
                    raise WorkerError("Existing job journal is outside the worker data directory.")
                try:
                    marker = _read_json(folder / "started.json", 4096)
                    saved = _read_json(folder / "outcome.json", MAX_RESULT_BYTES + 4096)
                    if marker.get("fingerprint") != fingerprint or type(saved.get("success")) is not bool or not isinstance(saved.get("result"), dict):
                        raise ValueError("Journal mismatch.")
                    return Outcome(saved["success"], _bounded_result(saved["result"]), saved.get("error"))
                except (OSError, ValueError, AttributeError, WorkerError):
                    return Outcome(False, {"status": "previous_outcome_unknown", "hardware_applied": False},
                                   "This job already has a local journal and will not be executed again.")

            _write_new(folder / "started.json", _json_bytes({"id": job.id, "kind": job.kind, "fingerprint": fingerprint, "started_at": datetime.now(timezone.utc).isoformat()}))
            try:
                if job.kind == "hx_import":
                    outcome = self._stage_preset(job)
                else:
                    outcome = self._diagnostic(job)
            except WorkerError as exc:
                outcome = Outcome(False, {"status": "failed", "hardware_applied": False}, str(exc))
            except Exception:
                # Errors from dependencies can contain paths or command strings;
                # keep the queue result terse and never include environment/token data.
                outcome = Outcome(False, {"status": "failed", "hardware_applied": False}, "The Windows job failed; inspect the local worker files.")
            _write_new(folder / "outcome.json", _json_bytes(outcome.as_dict()))
            return outcome
        except WorkerError as exc:
            return Outcome(False, {"status": "rejected", "hardware_applied": False}, str(exc))
        except OSError:
            return Outcome(False, {"status": "local_io_error", "hardware_applied": False}, "The worker could not create its local journal.")

    def _validate_payload(self, job: Job) -> None:
        payload = job.payload
        if job.kind in {"hx_discover", "hx_launch"}:
            _keys(payload, set())
        elif job.kind == "hx_inspect":
            _keys(payload, {"process_id", "max_nodes", "max_depth", "timeout_seconds"}, {"process_id"})
            _integer(payload["process_id"], "process_id", 1, 2147483647)
            _integer(payload.get("max_nodes", 500), "max_nodes", 1, 2000)
            _integer(payload.get("max_depth", 8), "max_depth", 0, 20)
            _integer(payload.get("timeout_seconds", 20), "timeout_seconds", 5, 60)
        elif job.kind == "hx_import":
            _keys(payload, {"preset_json", "sha256", "target_setlist", "target_setlist_name", "target_slot", "preset_name", "launch_hx_edit"},
                  {"preset_json", "sha256", "target_setlist", "target_slot", "preset_name"})
            text = payload["preset_json"]
            if not isinstance(text, str):
                raise WorkerError("preset_json must be UTF-8 JSON text.")
            try:
                raw = text.encode("utf-8")
            except UnicodeError:
                raise WorkerError("preset_json contains invalid Unicode.") from None
            if not 1 <= len(raw) <= MAX_PRESET_BYTES:
                raise WorkerError("Preset data must be no more than 256 KiB.")
            digest = payload["sha256"]
            if not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest) or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), digest.lower()):
                raise WorkerError("Preset SHA-256 verification failed.")
            try:
                preset = json.loads(text, parse_constant=_reject_constant)
                if not isinstance(preset, dict):
                    raise ValueError("Object required.")
            except (ValueError, RecursionError):
                raise WorkerError("Preset data must be a valid JSON object.") from None
            _integer(payload["target_setlist"], "target_setlist", 1, 8)
            if not isinstance(payload["target_slot"], str) or not SLOT.fullmatch(payload["target_slot"]):
                raise WorkerError("target_slot must be 01A through 32D.")
            for key, limit in (("preset_name", 64), ("target_setlist_name", 128)):
                if key in payload and (not isinstance(payload[key], str) or not payload[key].strip() or len(payload[key]) > limit or any(ord(c) < 32 for c in payload[key])):
                    raise WorkerError(f"{key} has invalid contents.")
            if "launch_hx_edit" in payload and type(payload["launch_hx_edit"]) is not bool:
                raise WorkerError("launch_hx_edit must be a boolean.")
        else:
            raise WorkerError("Unsupported job kind.")

    def _stage_preset(self, job: Job) -> Outcome:
        spool = self._directory("spool")
        destination = spool / job.id
        if destination.exists():
            raise WorkerError("A spool entry already exists for this job; it will not be overwritten.")
        temporary = Path(tempfile.mkdtemp(prefix=".stage-", dir=spool))
        payload = job.payload
        try:
            _write_new(temporary / "preset.hlx", payload["preset_json"].encode("utf-8"))
            manifest = {
                "schema_version": 1, "job_id": job.id, "status": "manual_import_required",
                "hardware_applied": False, "preset_file": "preset.hlx", "sha256": payload["sha256"].lower(),
                "preset_name": payload["preset_name"], "target_setlist": payload["target_setlist"],
                "target_setlist_name": payload.get("target_setlist_name"), "target_slot": payload["target_slot"],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "instructions": "In HX Edit, verify Helix Floor, the target setlist and slot, back up the destination, then import preset.hlx. This worker has not performed the import.",
            }
            _write_new(temporary / "manifest.json", _json_bytes(manifest))
            # Each job ID has an exclusive journal before reaching this point.
            # On Windows rename also refuses an existing destination directory.
            if destination.exists():
                raise WorkerError("The spool destination already exists and will not be overwritten.")
            os.rename(temporary, destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        result: dict[str, Any] = {
            "status": "manual_import_required", "hardware_applied": False,
            "preset_path": str(destination / "preset.hlx"), "manifest_path": str(destination / "manifest.json"),
            "sha256": payload["sha256"].lower(), "target_setlist": payload["target_setlist"],
            "target_slot": payload["target_slot"], "preset_name": payload["preset_name"],
        }
        if payload.get("launch_hx_edit", False):
            try:
                launched = self._run_powershell("Launch", job.id + "-launch", {})
                result["launch"] = {"success": launched.success, "status": launched.result.get("status"), "error": launched.error}
            except WorkerError as exc:
                # A launch failure does not undo the successfully staged preset.
                result["launch"] = {"success": False, "error": str(exc)}
        return Outcome(True, result)

    def _diagnostic(self, job: Job) -> Outcome:
        mode = {"hx_discover": "Discover", "hx_launch": "Launch", "hx_inspect": "Inspect"}[job.kind]
        return self._run_powershell(mode, job.id, job.payload)

    def _run_powershell(self, mode: str, diagnostic_id: str, payload: dict[str, Any]) -> Outcome:
        _interactive_windows()
        output = self._directory("diagnostics") / (diagnostic_id + ".json")
        if output.exists():
            raise WorkerError("A diagnostic file already exists for this job; it will not be overwritten.")
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        powershell = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if not powershell.is_file():
            raise WorkerError("Windows PowerShell 5.1 was not found at its system path.")
        timeout = payload.get("timeout_seconds", 20)
        resource = importlib.resources.files("helix_mcp").joinpath("resources/HxEdit.Windows.ps1")
        with importlib.resources.as_file(resource) as script:
            if not script.is_file():
                raise WorkerError("The packaged HX Edit diagnostic script is missing.")
            args = [str(powershell), "-NoProfile", "-NonInteractive", "-File", str(script), "-Mode", mode,
                    "-OutputPath", str(output), "-TimeoutSeconds", str(timeout)]
            if mode == "Launch":
                executable = self.config.hx_edit_exe
                if executable is None or not executable.is_absolute() or not executable.is_file() or not re.fullmatch(r"HX\s*Edit\.exe", executable.name, re.IGNORECASE):
                    raise WorkerError("Set HELIX_HX_EDIT_EXE to the absolute path of your installed HX Edit executable.")
                args += ["-ExecutablePath", str(executable)]
            if mode == "Inspect":
                args += ["-ProcessId", str(payload["process_id"]), "-MaxNodes", str(payload.get("max_nodes", 500)), "-MaxDepth", str(payload.get("max_depth", 8))]
            # Never give a child process the worker token or token file setting.
            child_env = {k: v for k, v in os.environ.items() if not k.upper().startswith("HELIX_")}
            try:
                process = subprocess.run(args, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, env=child_env, timeout=timeout + 15, check=False)
            except subprocess.TimeoutExpired:
                raise WorkerError("HX Edit diagnostic exceeded the process time limit; hardware was not imported.") from None
            except OSError:
                raise WorkerError("Windows PowerShell could not be started.") from None
        if not output.is_file():
            raise WorkerError("HX Edit diagnostic produced no JSON file. Check the local PowerShell execution policy and installation.")
        diagnostic = _read_json(output, MAX_DIAGNOSTIC_BYTES)
        if not isinstance(diagnostic, dict):
            raise WorkerError("HX Edit diagnostic was not a JSON object.")
        status = str(diagnostic.get("Status", "unknown"))[:80]
        result = _bounded_result({"status": status, "hardware_applied": False, "diagnostic_path": str(output), "diagnostic": diagnostic})
        success = process.returncode == 0
        return Outcome(success, result, None if success else f"HX Edit diagnostic returned exit code {process.returncode}.")


class WindowsWorker:
    def __init__(self, config: WorkerConfig, *, client: WorkerClient | None = None, runner: JobRunner | None = None):
        self.config = config
        self.client = client or WorkerClient(config)
        self.runner = runner or JobRunner(config)

    def run_once(self) -> str:
        response = self.client.post("/worker/poll", {"worker_id": self.config.worker_id})
        if "job" not in response:
            raise TransportError("Worker poll response is missing its job field.")
        if response["job"] is None:
            return "idle"
        job = Job.parse(response["job"])
        outcome = self.runner.execute(job)
        body = {"worker_id": self.config.worker_id, "lease_token": job.lease_token, **outcome.as_dict()}
        for attempt in range(3):
            try:
                self.client.post(f"/worker/jobs/{job.id}/result", body)
                LOG.info("Job %s: %s; hardware_applied=false", job.id, outcome.result.get("status", "completed"))
                return "completed" if outcome.success else "failed"
            except TransportError as exc:
                if exc.status is not None and 400 <= exc.status < 500:
                    LOG.warning("Result for job %s was not accepted (HTTP %s); it will not be executed again.", job.id, exc.status)
                    return "result_unreported"
                if attempt < 2:
                    time.sleep(0.5 * (2 ** attempt))
        LOG.warning("Result delivery for job %s failed; the local journal is retained and the action will not be repeated.", job.id)
        return "result_unreported"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Outbound Windows HX Edit diagnostic / manual preset staging worker.")
    parser.add_argument("--once", action="store_true", help="Poll once and exit after any received job/result submission.")
    parser.add_argument("--poll-interval", type=float, default=3.0, help="Seconds between polls (1-60; default 3).")
    args = parser.parse_args(argv)
    if not math.isfinite(args.poll_interval) or not 1 <= args.poll_interval <= 60:
        parser.error("--poll-interval must be between 1 and 60 seconds")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        _interactive_windows()
        config = WorkerConfig.from_env()
        config.read_token()
        worker = WindowsWorker(config)
        while True:
            try:
                status = worker.run_once()
                if args.once:
                    return 0 if status in {"idle", "completed"} else 1
            except TransportError as exc:
                LOG.warning("%s", str(exc))
                if args.once or exc.status in {401, 403}:
                    return 2
            except WorkerError as exc:
                LOG.error("%s", str(exc))
                if args.once:
                    return 2
            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        return 0
    except WorkerError as exc:
        LOG.error("%s", str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
