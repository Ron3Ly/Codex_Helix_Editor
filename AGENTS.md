# Codex Helix Editor

## Goal and architecture

The user uses Windows, Codex Desktop and Helix Floor. The MCP service runs in a
Linux VM on Proxmox VE. The Windows worker polls outbound HTTPS in the interactive
logged-in user session. HX Edit and the USB-connected Helix stay on Windows.

Read README.md and VALIDATION.md before changing behavior. Source is in
src/helix_mcp. The package requires Python 3.12+. The existing preset editor is
in preset.py and the packaged PowerShell diagnostic is resources/HxEdit.Windows.ps1.

## Honest capability boundaries

This release stages .hlx files for manual import. It has no calibrated automatic
HX Edit import adapter. `succeeded` means the worker task completed; it does not
mean the hardware preset was applied. Preserve `manual_import_required` and
`hardware_applied=false` until an actual adapter and hardware verification exist.
Do not replace that missing implementation with unobserved coordinates, invented
AutomationIds, or a guessed Home-key sequence.

Codex researches the song and designs the tone. The MCP is deterministic and does
not call OpenAI APIs. Do not add an API-key requirement without a concrete need.

## Data and job invariants

- Start from a real user-exported Helix Floor template. Do not invent model IDs,
  parameter names or raw/display-value conversions. Preserve models and routing.
- Keep original templates; generated candidates are separate artifacts.
- Honor hash, expected-value, range/type, snapshot and controller checks. Unknown
  formats need investigation rather than a bypass flag.
- Tools use artifact IDs, never arbitrary server filesystem paths or shell commands.
- MCP and worker tokens remain separate. Do not print secrets or commit .env,
  token files, SQLite data, user presets, audio, or IRs.
- Use SQLite transactions and request IDs for queue idempotency. An expired lease
  is unknown and must never be automatically replayed.
- Keep server and worker payload contracts aligned; run the actual staging handler
  in the end-to-end software test when altering them.
- The registration target is explicit configuration. Do not assume a setlist is
  empty or overwrite a different slot as a fallback.

## Tone workflow

Record the song/version/section and the user's guitar/output setup. Separate
sourced equipment facts from reproduction assumptions. Do not say audio was heard
unless it was actually available and analyzed. Use inspect_template, then fill a
hash-bound plan with observed raw ranges before build_candidate. If asked to send
it to Windows, stage the candidate and read get_job; report the required manual
import. After the user re-exports from HX Edit, compare the tone data and separately
verify the slot and audible result.

## Validation and delivery

Run `python -m unittest discover -s tests -v` after installing the package, or use
`PYTHONPATH=src` during source development. Fixtures are artificial and must never
be imported into hardware. Tests cover MCP protocol, authentication, queue leases,
SQLite backup, data preservation and Windows staging. Windows GUI execution and
real device import require a separate observed Windows session.

Update docs when CLI flags, env variables, endpoints or capability boundaries
change. Deploy files live in deploy/. Preserve current infrastructure choices unless
the user requests otherwise. Never claim a PVE deploy or Codex registration occurred
merely because code and instructions were published.
