# Changelog

## 0.3.0

### Changed

- **Rebranded to OctoBrowser-MCP.** Distribution renamed to `octobrowser-mcp`, the
  console script and MCP server id are now `octobrowser-mcp`, and the import package is
  `octobrowser_mcp`. Internals were renamed for clarity: `octo_client.py` → `conduits.py`
  (`OctoLocalClient` → `LocalConduit`, `OctoCloudClient` → `CloudConduit`,
  `OctoAPIError` → `OctoApiFault`, `extract_ws_endpoint` → `sniff_ws_endpoint`),
  `browser_manager.py` → `helmsman.py` (`BrowserManager` → `Helmsman`), and
  `server.py` → `relay.py`. **The 37 MCP tool names, their arguments and output are
  unchanged** — only the Python surface moved.
- Added a branded hero banner under `assets/` — the official Octo Browser logo (from the
  vendor brand kit, embedded unmodified) on a brand-navy canvas beside an "MCP Server"
  descriptor — plus the standalone official logo/sign SVGs, and restructured the README
  around them. A clear "unofficial · not affiliated with Octo Browser" disclaimer and
  trademark notice accompany the marks.

- **Migrated to MCP SDK 2.x** (`mcp>=2.0.0`). 2.0 removed the low-level
  `@server.list_tools()` / `@server.call_tool()` decorators this server was built on,
  so a clean install crashed on import; the server now uses the `MCPServer` API.
  Tool names, arguments and output text are unchanged.
- Tool schemas are now generated from the Python signatures: argument descriptions come
  from `Field(description=...)` and allowed values from `Literal`, so invalid arguments
  (`wait_until`, `button`, `state`, `direction`, fingerprint `os`) are rejected by the SDK
  before any browser work starts, instead of by hand-written checks.
- Tool failures are returned as protocol-level errors (`is_error`) rather than text that
  merely starts with "Error".

### Added

- Server-level `instructions` describing the start -> connect -> drive flow.
- Tests over the tool surface: full name list, required arguments, enums, argument
  descriptions, dispatch and the screenshot image path.

## 0.2.0

Audit against the current [Octo Browser API reference](https://documenter.getpostman.com/view/1801428/UVC6i6eA).

### Fixed

- **Cloud responses were not unwrapped.** Every cloud endpoint answers with
  `{"success", "msg", "data"}`; the client returned the envelope, so
  `octo_get_profile` reported `N/A` for title, fingerprint, proxy and extensions.
- **`import_cookies` sent a bare array** instead of `{"cookies": [...]}`.
- **`transfer_profiles` sent `email`** instead of `receiver_email`, and could not
  transfer proxies along with the profiles.
- **`create_tag` defaulted to a hex colour.** The API only accepts colour names
  (grey, blue, cyan, orange, green, purple, red, yellow); other values are now rejected locally.
- **429 retries were unbounded** -- a persistently throttled API could hang a tool call
  forever. Retries are now capped at 5 and `Retry-After: 0` no longer means "retry instantly".
- **403 hid the reason.** "No active subscription" was reported as a permissions problem.
- **`octo_get_extensions` only returned the first page** of team extensions.
- **`browser_scroll` with a selector scrolled the page**, not the element.
- **`octo_search_profiles(limit=N)`** could return more than `N` results, because the
  page size is snapped to the nearest value the API accepts.

### Added

- `OctoAPIError` carrying the API's `status_code`, `code` and `data`.
- Profile `password` support when starting protected profiles.
- Cloud-side `force_stop_profiles` (stops profiles running on other machines).
- `skip_trash_bin` on profile deletion, `api_token` on local login, `flags` on one-time profiles.
- `OCTO_API_URL` (mirror hosts) and `OCTO_LOG_LEVEL` environment variables; logging to stderr.
- Test suite over `httpx.MockTransport` (no Octo Browser or network needed) and a CI workflow.

### Changed

- `get_or_start_profile` now recovers from any start failure by re-checking the running
  profiles, instead of matching one hardcoded error code.
- Server-side strings and docstrings unified to English.
- Clean `mypy --strict` run.

## 0.1.0

Initial release: 37 MCP tools over the Octo Browser local and cloud APIs plus
Playwright/CDP browser automation.
