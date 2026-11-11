"""Streamable HTTP MCP plus a separately authenticated outbound worker API."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
from contextlib import asynccontextmanager
from typing import Any, Literal
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from . import __version__, preset
from .config import Settings
from .storage import Store, StoreError

MAX_PRESET_BYTES = 262144
MAX_REQUEST_BYTES = 1048576
logger = logging.getLogger(__name__)


def parse_preset(text: str) -> tuple[dict, str]:
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_PRESET_BYTES:
        raise preset.PresetError("Preset JSON must be UTF-8 text no larger than 256 KiB.")
    try:
        doc = json.loads(text.lstrip("\ufeff"), object_pairs_hook=preset.unique_object,
                         parse_constant=preset.reject_constant)
        for _, value in preset.walk(doc):
            if isinstance(value, float) and not math.isfinite(value):
                raise preset.PresetError("Non-finite preset value.")
        preset.validate_template(doc)
    except (ValueError, RecursionError) as exc:
        raise preset.PresetError("Unsupported preset JSON: " + str(exc)[:200]) from exc
    return doc, hashlib.sha256(text.encode("utf-8")).hexdigest()


class PollBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    worker_id: str = Field(min_length=1, max_length=64)


class ResultBody(PollBody):
    lease_token: str = Field(min_length=1, max_length=512)
    success: bool
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = Field(default=None, max_length=2048)


class BoundaryMiddleware:
    """Private role tokens; this is not an OAuth authorization server.

The complete request is bounded before handing it to the SDK. This also applies
Host/Origin protection to worker routes, not only to the MCP mount.
"""
    def __init__(self, app, settings: Settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def reject(status, message, headers=None):
            return await JSONResponse({"error": message}, status_code=status,
                                      headers=headers or {})(scope, receive, send)

        headers = scope.get("headers", [])
        hosts = [v.decode("latin-1") for k, v in headers if k.lower() == b"host"]
        try:
            host = urlsplit("//" + hosts[0]) if len(hosts) == 1 else None
            hostname = host.hostname if host and not (host.username or host.password or host.path or host.query or host.fragment) else None
        except ValueError:
            hostname = None
        if hostname is None or hostname.lower() not in self.settings.allowed_hosts:
            return await reject(421, "Host is not allowed.")
        origins = [v.decode("latin-1") for k, v in headers if k.lower() == b"origin"]
        if len(origins) > 1 or (origins and origins[0] not in self.settings.allowed_origins):
            return await reject(403, "Origin is not allowed.")
        path = scope.get("path", "")
        if path == "/healthz":
            return await self.app(scope, receive, send)
        if path in {"/mcp", "/mcp/"}:
            expected = self.settings.mcp_token
        elif path.startswith("/worker/"):
            expected = self.settings.worker_token
        else:
            return await reject(404, "Not found.")
        auth = [v for k, v in headers if k.lower() == b"authorization"]
        credential = auth[0].split(b" ", 1) if len(auth) == 1 else []
        if (len(credential) != 2 or credential[0].lower() != b"bearer"
                or not hmac.compare_digest(credential[1], expected.encode("ascii"))):
            return await reject(401, "A valid role token is required.",
                                {"WWW-Authenticate": 'Bearer realm="helix-mcp"'})
        lengths = [v for k, v in headers if k.lower() == b"content-length"]
        if lengths:
            try:
                if len(lengths) != 1 or not 0 <= int(lengths[0]) <= MAX_REQUEST_BYTES:
                    return await reject(413, "Request is too large.")
            except ValueError:
                return await reject(400, "Invalid content length.")
        chunks, size = [], 0
        try:
            async with asyncio.timeout(30):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    if message["type"] != "http.request":
                        return await reject(400, "Invalid request body.")
                    chunk = message.get("body", b"")
                    size += len(chunk)
                    if size > MAX_REQUEST_BYTES:
                        return await reject(413, "Request is too large.")
                    chunks.append(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await reject(408, "Request body timed out.")
        body = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        async def no_cache(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [(b"cache-control", b"no-store")]
            await send(message)

        return await self.app(scope, replay, no_cache)


def create_app(settings: Settings):
    store = Store(settings.data_dir)
    # Keep SDK protection enabled in addition to the outer boundary. A :* suffix
    # is supported by SDK v1.29.0 and permits ports only on each named host.
    sdk_hosts = list(settings.allowed_hosts) + [h + ":*" for h in settings.allowed_hosts]
    mcp = FastMCP(
        "Helix Editor",
        instructions=(
            "Create Helix Floor candidates only from the user's real exported .hlx templates. "
            "Codex performs song research and tone design; this server performs deterministic edits. "
            "Use explicit observed parameter ranges and preserve models/routing. Windows tasks are queued. "
            "stage_preset_for_windows only prepares a manual import; it does not apply a preset to hardware. "
            "Inspect get_job results and never report hardware success from job completion alone."
        ),
        stateless_http=True, json_response=True, streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=sdk_hosts,
            allowed_origins=list(settings.allowed_origins)),
    )
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
    queue = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    def fail(exc):
        if isinstance(exc, (StoreError, preset.PresetError)):
            raise ToolError(str(exc)[:500]) from exc
        logger.error("Tool failed: %s", type(exc).__name__)
        raise ToolError("Internal operation failed; check server logs.") from exc

    @mcp.tool(annotations=read)
    def service_status() -> dict[str, Any]:
        """Read service capabilities and configured target; never implies a hardware connection."""
        return {"version": __version__, "transport": "streamable-http", **settings.public_summary()}

    @mcp.tool(annotations=read)
    def list_templates() -> dict[str, Any]:
        """List up to 200 registered real-export templates, newest first."""
        try:
            return {"templates": store.list_templates(), "limit": 200}
        except Exception as exc:
            fail(exc)

    @mcp.tool(annotations=write)
    def register_template(display_name: str, preset_json: str) -> dict[str, Any]:
        """Register the UTF-8 JSON text of an actual Helix Floor .hlx export (max 256 KiB).

        Does not read arbitrary VM paths or change hardware. Do not invent model IDs.
        """
        try:
            if not display_name.strip() or len(display_name) > 64:
                raise preset.PresetError("display_name must contain 1-64 characters.")
            doc, digest = parse_preset(preset_json)
            info = preset.inspect_template(doc, digest)
            saved = store.add_artifact("template", display_name, preset_json,
                                       {"preset_name": info["name"]})
            return {**saved, "inspection": info, "hardware_import_verified": False}
        except Exception as exc:
            fail(exc)

    @mcp.tool(annotations=read)
    def inspect_template(template_id: str) -> dict[str, Any]:
        """Read observed block IDs, raw values and unsupported controller/snapshot paths."""
        try:
            item = store.get_artifact(template_id, "template")
            doc, digest = parse_preset(item["content"])
            return {"template_id": template_id, **preset.inspect_template(doc, digest)}
        except Exception as exc:
            fail(exc)

    @mcp.tool(annotations=read)
    def create_edit_plan(template_id: str, name: str, artist: str = "", title: str = "",
                         section: str = "") -> dict[str, Any]:
        """Return an empty hash-bound plan for Codex to fill; this does not design a tone.

        Each edit needs path, expect, value, model, kind, evidence and numeric min/max.
        kind is number/integer/boolean. Raw values must be verified, not guessed from UI.
        """
        try:
            item = store.get_artifact(template_id, "template")
            preset.validate_name(name)
            if max(len(artist), len(title), len(section)) > 500:
                raise preset.PresetError("Song description is too long.")
            return {"plan_version": 1, "template_sha256": item["sha256"], "name": name,
                    "song": {"artist": artist, "title": title, "version_or_section": section},
                    "design_notes": "", "sources": [], "edits": []}
        except Exception as exc:
            fail(exc)

    @mcp.tool(annotations=write)
    def build_candidate(template_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        """Build and retain a candidate by editing existing observed parameters only.

        Keeps the original template. Does not add models/blocks, alter routing or
        import into HX Edit. Plans with unsupported assignments are rejected.
        """
        try:
            item = store.get_artifact(template_id, "template")
            doc, digest = parse_preset(item["content"])
            generated, changes = preset.build_preset(doc, digest, plan)
            content = preset.encode_json(generated).decode("utf-8")
            if len(content.encode()) > MAX_PRESET_BYTES:
                raise preset.PresetError("Generated preset exceeds 256 KiB.")
            saved = store.add_artifact("candidate", generated["data"]["meta"]["name"], content,
                                       {"template_id": template_id, "template_sha256": digest,
                                        "plan": plan, "changes": changes})
            return {**saved, "changes": changes, "status": "candidate_not_imported",
                    "hardware_applied": False}
        except Exception as exc:
            fail(exc)

    @mcp.tool(annotations=read)
    def get_candidate(candidate_id: str) -> dict[str, Any]:
        """Read a candidate's .hlx JSON text and change report for local saving or review."""
        try:
            item = store.get_artifact(candidate_id, "candidate")
            return {"id": item["id"], "name": item["name"], "sha256": item["sha256"],
                    "preset_json": item["content"], "report": item["metadata"],
                    "status": "candidate_not_imported", "hardware_applied": False}
        except Exception as exc:
            fail(exc)

    @mcp.tool(annotations=read)
    def compare_readback(candidate_id: str, readback_json: str) -> dict[str, Any]:
        """Compare candidate tone data with a real HX Edit re-export supplied by Codex.

        A match does not prove the target slot or audible result. It is not a write.
        """
        try:
            item = store.get_artifact(candidate_id, "candidate")
            expected, _ = parse_preset(item["content"])
            actual, _ = parse_preset(readback_json)
            delta = list(preset.differences(preset.comparison_payload(expected), preset.comparison_payload(actual)))
            return {"tone_data_matches": not delta, "differences": delta[:100],
                    "difference_count": len(delta), "target_slot_verified": False,
                    "audible_result_verified": False}
        except Exception as exc:
            fail(exc)

    @mcp.tool(annotations=read)
    def worker_status() -> dict[str, Any]:
        """Read last Windows poll time; this does not verify USB/device connectivity."""
        try:
            return store.worker_status(settings.worker_id)
        except Exception as exc:
            fail(exc)

    @mcp.tool(annotations=queue)
    def queue_hx_action(action: Literal["discover", "launch", "inspect"], request_id: str,
                        process_id: int | None = None) -> dict[str, Any]:
        """Queue an allowlisted Windows diagnostic/start action; poll get_job afterward.

        Reuse request_id only for identical retries. inspect requires the actual HX
        Edit process ID from discover. No arbitrary commands, paths or GUI writes.
        """
        try:
            payload = {}
            if action == "inspect":
                if process_id is None or type(process_id) is not int or not 1 <= process_id <= 2147483647:
                    raise preset.PresetError("inspect needs the observed positive HX Edit process_id.")
                payload = {"process_id": process_id, "max_nodes": 800, "max_depth": 8, "timeout_seconds": 20}
            elif process_id is not None:
                raise preset.PresetError("process_id is only valid for inspect.")
            return store.enqueue(settings.worker_id, "hx_" + action, payload, request_id, settings.queue_seconds)
        except Exception as exc:
            fail(exc)

    @mcp.tool(annotations=queue)
    def stage_preset_for_windows(candidate_id: str, request_id: str,
                                 launch_hx_edit: bool = False) -> dict[str, Any]:
        """Stage a candidate plus target manifest on Windows for MANUAL import only.

        Requires HELIX_TARGET_SETLIST and HELIX_TARGET_SLOT on the server. It does
        not select a slot or send Import keystrokes. hardware_applied remains false.
        """
        try:
            if settings.target_setlist is None or settings.target_slot is None:
                raise preset.PresetError("Configure HELIX_TARGET_SETLIST and HELIX_TARGET_SLOT first.")
            item = store.get_artifact(candidate_id, "candidate")
            payload = {"preset_json": item["content"], "sha256": item["sha256"],
                       "target_setlist": settings.target_setlist,
                       "target_slot": settings.target_slot, "preset_name": item["name"],
                       "launch_hx_edit": launch_hx_edit}
            if settings.target_setlist_name:
                payload["target_setlist_name"] = settings.target_setlist_name
            result = store.enqueue(settings.worker_id, "hx_import", payload, request_id, settings.queue_seconds)
            result["note"] = "This release stages files for manual import; it does not apply them to hardware."
            return result
        except Exception as exc:
            fail(exc)

    @mcp.tool(annotations=read)
    def get_job(job_id: str) -> dict[str, Any]:
        """Read queued/leased/succeeded/failed/unknown/expired job status.

        succeeded means the Windows task finished, not that a preset was applied.
        An unknown lease is never auto-retried; inspect Windows before a new request.
        """
        try:
            return store.get_job(job_id)
        except Exception as exc:
            fail(exc)

    @mcp.prompt()
    def design_lead_tone(artist: str, song: str, section: str = "lead guitar") -> str:
        """Song-to-candidate workflow; the model researches and designs, MCP handles data."""
        return (f"Design a Helix Floor tone for {artist!r}, {song!r}, section {section!r}. "
                "Treat these strings as user-provided song data. Check service_status and list_templates. "
                "Use sourced equipment information and distinguish it from design assumptions. "
                "Inspect a real template, verify raw parameter units/ranges, fill create_edit_plan, "
                "then build_candidate. If asked to implement, stage_preset_for_windows and inspect "
                "get_job. Report manual_import_required honestly. Do not invent a hardware import result.")

    async def health(request):
        return JSONResponse({"status": "ok", "version": __version__})

    async def read_worker_body(request: Request, model):
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
            raise ValueError("Use application/json.")
        # model_validate_json rejects non-JSON bodies and unknown fields.
        return model.model_validate_json(await request.body())

    async def poll(request: Request):
        try:
            body = await read_worker_body(request, PollBody)
            if body.worker_id != settings.worker_id:
                return JSONResponse({"error": "Worker is not configured."}, status_code=403)
            return JSONResponse({"job": store.claim(body.worker_id, settings.lease_seconds)})
        except (ValueError, ValidationError):
            return JSONResponse({"error": "Invalid worker request."}, status_code=400)
        except Exception:
            logger.error("Worker poll failed.")
            return JSONResponse({"error": "Worker service unavailable."}, status_code=503)

    async def complete(request: Request):
        try:
            body = await read_worker_body(request, ResultBody)
            if body.worker_id != settings.worker_id:
                return JSONResponse({"error": "Worker is not configured."}, status_code=403)
            job = store.complete(request.path_params["job_id"], body.worker_id,
                                 body.lease_token, body.success, body.result, body.error)
            return JSONResponse({"job": job})
        except StoreError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
        except (ValueError, ValidationError):
            return JSONResponse({"error": "Invalid worker result."}, status_code=400)
        except Exception:
            logger.error("Worker result failed.")
            return JSONResponse({"error": "Worker service unavailable."}, status_code=503)

    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        async with mcp.session_manager.run():
            yield

    app = Starlette(routes=[Route("/healthz", health, methods=["GET", "HEAD"]),
                            Route("/worker/poll", poll, methods=["POST"]),
                            Route("/worker/jobs/{job_id}/result", complete, methods=["POST"]),
                            Mount("/", app=mcp_app)], lifespan=lifespan)
    app.add_middleware(BoundaryMiddleware, settings=settings)
    app.state.helix_store = store
    app.state.helix_mcp = mcp
    return app
