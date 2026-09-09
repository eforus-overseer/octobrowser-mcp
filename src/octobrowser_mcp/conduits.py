"""
Conduits to the Octo Browser API.

Two conduits mirror the two halves of the Octo Browser API:

- ``LocalConduit`` -- nearside client API on port 58888: launch/halt profiles,
  list running profiles, one-time profiles, sign-in.
- ``CloudConduit`` -- cloud automation API: profile lookup and upkeep, tags,
  proxies, team extensions. Needs an API token.

API reference: https://documenter.getpostman.com/view/1801428/UVC6i6eA
"""

import asyncio
import logging
import os
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

logger = logging.getLogger("octobrowser_mcp.conduits")

# Cloud API origin. The docs list mirrors for providers that fence off the main
# host (https://app.octobrowser-mirror1.com / .net / .org) -- swap the origin
# with the OCTO_API_URL environment variable.
DEFAULT_CLOUD_API_URL = "https://app.octobrowser.net/api/v2/automation"

# Page sizes the cloud API is willing to accept
VALID_PAGE_LENS = (10, 25, 50, 100)

# Tag hues the cloud API accepts (raw hex is refused)
TAG_COLORS = ("grey", "blue", "cyan", "orange", "green", "purple", "red", "yellow")

# Throttle handling: the docs ask callers to wait out Retry-After on HTTP 429
MAX_RETRIES = 5
MAX_BACKOFF = 15.0


class OctoApiFault(Exception):
    """Trouble reported by an Octo Browser API.

    Attributes:
        status_code: HTTP status code of the failed response.
        code: Octo error code -- a string for the cloud API ("profiles.stop_error"),
            an integer for local launch errors (see START_ERROR_CODES in the docs).
        data: Payload of the error response, if any.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | int | None = None,
        data: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.data = data


class _HttpConduit:
    """Shared HTTP plumbing: lazy client, 429 retries, connection faults."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float,
        headers: dict[str, str] | None = None,
        connection_hint: str = "",
    ) -> None:
        self.base_url = base_url
        self._timeout = timeout
        self._headers = headers or {}
        self._connection_hint = connection_hint
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Fetch or spin up the underlying HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self._timeout,
                headers=self._headers,
            )
        return self._client

    async def close(self) -> None:
        """Shut the HTTP client and release resources."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _send(self, method: str, endpoint: str, **kwargs: Any) -> httpx.Response:
        """Fire a request, pausing and retrying while the API answers 429.

        Raises:
            ConnectionError: If the API is out of reach.
            OctoApiFault: If the throttle does not lift within MAX_RETRIES.
        """
        client = await self._get_client()
        backoff = 1.0

        for attempt in range(MAX_RETRIES + 1):
            try:
                response = await client.request(method, endpoint, **kwargs)
            except httpx.RequestError as exc:
                raise ConnectionError(
                    f"Failed to reach {self.base_url}: {exc}. {self._connection_hint}".strip()
                ) from exc

            if response.status_code != 429:
                logger.debug("%s %s -> %s", method, endpoint, response.status_code)
                return response

            if attempt == MAX_RETRIES:
                break

            pause = _retry_delay(response, backoff)
            logger.warning(
                "Throttled on %s %s, sleeping %.1fs (attempt %d/%d)",
                method,
                endpoint,
                pause,
                attempt + 1,
                MAX_RETRIES,
            )
            await asyncio.sleep(pause)
            backoff = min(backoff * 2, MAX_BACKOFF)

        raise OctoApiFault(
            f"Throttle not cleared after {MAX_RETRIES} retries on {method} {endpoint}. "
            "Ease the request rate or upgrade the subscription plan.",
            status_code=429,
        )


def _parse_json(response: httpx.Response) -> Any:
    """Body as JSON; a non-JSON body becomes a message rather than a crash."""
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        logger.debug("Non-JSON response from %s", response.request.url)
        return {"msg": response.text[:200]}


def _retry_delay(response: httpx.Response, fallback: float) -> float:
    """Seconds to hold before retrying, taken from the Retry-After header."""
    raw = response.headers.get("Retry-After")
    if raw:
        try:
            delay = float(raw)
        except ValueError:
            return fallback
        if delay > 0:
            return delay
    return fallback


class CloudConduit(_HttpConduit):
    """
    Async conduit to the Octo Browser cloud API.

    Handles profile lookup and upkeep, tags, proxies and team extensions.
    Needs an API token (OCTO_API_TOKEN environment variable or api_token argument).

    Every cloud response is wrapped in {"success", "msg", "data"}; the methods
    below hand back the unwrapped ``data`` payload.
    """

    def __init__(self, api_token: str | None = None, base_url: str | None = None) -> None:
        """
        Wire up the cloud API conduit.

        Args:
            api_token: Octo API token. Falls back to the OCTO_API_TOKEN env var.
            base_url: API base URL. Falls back to the OCTO_API_URL env var, then
                to the default host. Use a mirror if your provider fences off the main one.
        """
        self.api_token = api_token or os.environ.get("OCTO_API_TOKEN")
        headers = {"X-Octo-Api-Token": self.api_token} if self.api_token else {}
        super().__init__(
            base_url or os.environ.get("OCTO_API_URL") or DEFAULT_CLOUD_API_URL,
            timeout=30.0,
            headers=headers,
        )

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        """
        Run a request and hand back the full response envelope.

        Raises:
            ValueError: If the API token is missing.
            ConnectionError: If the API is out of reach.
            OctoApiFault: If the API flags a failure.
        """
        if not self.api_token:
            raise ValueError(
                "OCTO_API_TOKEN is not set. Set the environment variable "
                "or pass api_token to the constructor."
            )

        response = await self._send(method, endpoint, **kwargs)
        payload: Any = _parse_json(response)

        if response.is_success and (not isinstance(payload, dict) or payload.get("success", True)):
            return payload if isinstance(payload, dict) else {"data": payload}

        raise _cloud_fault(response, payload, endpoint)

    async def _request_data(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        """Run a request and hand back the unwrapped ``data`` payload."""
        envelope = await self._request(method, endpoint, **kwargs)
        return envelope.get("data")

    async def search_profiles(
        self,
        search: str | None = None,
        tags: list[str] | None = None,
        page: int = 0,
        page_len: int = 100,
        fields: str | None = None,
        ordering: str | None = None,
        status: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Look up profiles by title prefix or tags.

        Args:
            search: Title prefix -- the API matches from the beginning of the title,
                not anywhere inside it.
            tags: Tags to filter by. Several tags mean AND: only profiles carrying
                all of them come back.
            page: Page number, starting from 0.
            page_len: Results per page; must be 10, 25, 50 or 100 (nearest is used).
            fields: Comma-separated fields to return (default: "title,status,tags").
            ordering: Sort order -- "created", "-created", "active", "-active",
                "title", "-title". A "-" prefix means descending.
            status: Filter by numeric profile status.

        Returns:
            List of profiles with the requested fields (uuid is always included).
        """
        if page_len not in VALID_PAGE_LENS:
            page_len = min(VALID_PAGE_LENS, key=lambda x: abs(x - page_len))

        params: dict[str, Any] = {
            "page": page,
            "page_len": page_len,
            # 'uuid' is always returned, it must not be listed in fields
            "fields": fields if fields is not None else "title,status,tags",
        }
        if search:
            params["search"] = search
        if tags:
            params["search_tags"] = ",".join(tags)
        if ordering is not None:
            params["ordering"] = ordering
        if status is not None:
            params["status"] = status

        data = await self._request_data("GET", "/profiles", params=params)
        return list(data or [])

    async def find_profile_by_name(
        self, name: str, exact_match: bool = True
    ) -> dict[str, Any] | None:
        """
        Track down a profile by title.

        Args:
            name: Profile title. The API searches by prefix, so `name` must be
                the beginning of the title.
            exact_match: If True, insist on an exact title match.

        Returns:
            Profile dict with uuid, title, etc., or None if nothing matched.
        """
        profiles = await self.search_profiles(search=name, page_len=100)

        if exact_match:
            for candidate in profiles:
                if candidate.get("title") == name:
                    return candidate
            return None

        return profiles[0] if profiles else None

    async def get_profile_uuid_by_name(self, name: str) -> str | None:
        """
        Resolve a profile UUID by exact title match.

        Args:
            name: Profile title.

        Returns:
            UUID string, or None if nothing matched.
        """
        profile = await self.find_profile_by_name(name, exact_match=True)
        uuid = profile.get("uuid") if profile else None
        return str(uuid) if uuid else None

    # === Profile CRUD ===

    async def get_profile(self, uuid: str) -> dict[str, Any]:
        """Fetch full profile data by UUID.

        Args:
            uuid: Profile UUID.

        Returns:
            Profile data: title, fingerprint, proxy, tags, storage options, etc.
        """
        data = await self._request_data("GET", f"/profiles/{uuid}")
        return dict(data or {})

    async def create_profile(self, data: dict[str, Any]) -> dict[str, Any]:
        """Mint a profile.

        Any field left out is generated by Octo, so pass only what you want to
        pin down (title, tags, proxy, fingerprint, cookies, ...).

        Args:
            data: Profile configuration.

        Returns:
            Dict with the UUID of the freshly minted profile.
        """
        created = await self._request_data("POST", "/profiles", json=data)
        return dict(created or {})

    async def update_profile(self, uuid: str, data: dict[str, Any]) -> dict[str, Any]:
        """Revise a profile by UUID.

        Args:
            uuid: Profile UUID.
            data: Fields to revise.

        Returns:
            Dict with the UUID of the revised profile.
        """
        updated = await self._request_data("PATCH", f"/profiles/{uuid}", json=data)
        return dict(updated or {})

    async def delete_profiles(
        self, uuids: list[str], skip_trash_bin: bool = True
    ) -> dict[str, Any]:
        """Discard profiles by UUID.

        Args:
            uuids: Profile UUIDs to discard.
            skip_trash_bin: Discard permanently instead of moving to the trash bin.

        Returns:
            Dict with deleted_uuids and active_uuids (running profiles are skipped).
        """
        result = await self._request_data(
            "DELETE", "/profiles", json={"uuids": uuids, "skip_trash_bin": skip_trash_bin}
        )
        return dict(result or {})

    async def import_cookies(
        self, uuid: str, cookies: list[dict[str, Any]] | str
    ) -> dict[str, Any]:
        """Load cookies into a profile.

        Args:
            uuid: Profile UUID.
            cookies: Cookies as a JSON array (JSON/Mozilla format) or as a
                Netscape-format string.

        Returns:
            Import result.
        """
        result = await self._request_data(
            "POST", f"/profiles/{uuid}/import_cookies", json={"cookies": cookies}
        )
        return {"result": result}

    async def transfer_profiles(
        self, uuids: list[str], receiver_email: str, transfer_proxy: bool = False
    ) -> dict[str, Any]:
        """Hand profiles over to another account.

        Args:
            uuids: Profile UUIDs to hand over (up to 100 per request).
            receiver_email: Email of the receiving account.
            transfer_proxy: Also hand over the proxies attached to the profiles.

        Returns:
            Transfer result.
        """
        result = await self._request_data(
            "POST",
            "/profiles/transfer",
            json={
                "uuids": uuids,
                "receiver_email": receiver_email,
                "transfer_proxy": transfer_proxy,
            },
        )
        return {"result": result}

    async def force_stop_profiles(self, uuids: list[str]) -> dict[str, Any]:
        """Force-halt running profiles across the whole team (cloud-side).

        Unlike LocalConduit.force_stop_profile this also halts profiles
        running on other machines.

        Args:
            uuids: Profile UUIDs to halt.

        Returns:
            Result payload; failed UUIDs are listed under "failed".
        """
        result = await self._request_data("POST", "/profiles/force_stop", json={"uuids": uuids})
        return dict(result or {})

    # === Team extensions ===

    async def get_team_extensions(self, page_size: int = 25) -> list[dict[str, Any]]:
        """Gather every extension the team uses.

        The endpoint is paginated, so all pages are walked.

        Args:
            page_size: Items per request.

        Returns:
            List of extensions with uuid, name and version.
        """
        extensions: list[dict[str, Any]] = []
        start = 0

        for _ in range(100):  # hard stop, ~2500 extensions at the default page size
            page = await self._request_data(
                "GET", "/teams/extensions", params={"start": start, "limit": page_size}
            )
            page = list(page or [])
            extensions.extend(page)
            if len(page) < page_size:
                break
            start += page_size

        return extensions

    async def delete_team_extensions(self, uuids: list[str]) -> dict[str, Any]:
        """Discard team extensions by UUID.

        Extensions in use by a running profile reappear in the list once that
        profile halts -- halt the profiles first.

        Args:
            uuids: Extension UUIDs (up to 100 per request), e.g. "abc123@2.0.12".

        Returns:
            Deletion result.
        """
        result = await self._request_data("DELETE", "/teams/extensions", json={"uuids": uuids})
        return {"result": result}

    # === Tags ===

    async def get_tags(self) -> list[dict[str, Any]]:
        """Gather every tag.

        Returns:
            List of tags with uuid, name and color.
        """
        data = await self._request_data("GET", "/tags")
        return list(data or [])

    async def create_tag(self, name: str, color: str = "grey") -> dict[str, Any]:
        """Mint a tag.

        Args:
            name: Tag name.
            color: One of TAG_COLORS -- grey, blue, cyan, orange, green,
                purple, red, yellow.

        Returns:
            Freshly minted tag data.
        """
        _validate_tag_color(color)
        created = await self._request_data("POST", "/tags", json={"name": name, "color": color})
        return dict(created or {})

    async def update_tag(
        self,
        uuid: str,
        name: str | None = None,
        color: str | None = None,
    ) -> dict[str, Any]:
        """Revise a tag by UUID.

        Args:
            uuid: Tag UUID.
            name: New tag name.
            color: New tag color, one of TAG_COLORS.

        Returns:
            Revised tag data.
        """
        data: dict[str, Any] = {}
        if name is not None:
            data["name"] = name
        if color is not None:
            _validate_tag_color(color)
            data["color"] = color
        updated = await self._request_data("PATCH", f"/tags/{uuid}", json=data)
        return dict(updated or {})

    async def delete_tag(self, uuid: str) -> dict[str, Any]:
        """Discard a tag by UUID.

        Args:
            uuid: Tag UUID.

        Returns:
            Deletion result.
        """
        result = await self._request_data("DELETE", f"/tags/{uuid}")
        return {"result": result}

    # === Proxies ===

    async def get_proxies(self) -> list[dict[str, Any]]:
        """Gather every saved proxy.

        Returns:
            List of proxies with uuid, type, host, port, title, profiles_count.
        """
        data = await self._request_data("GET", "/proxies")
        return list(data or [])

    async def create_proxy(self, data: dict[str, Any]) -> dict[str, Any]:
        """Mint a saved proxy.

        Args:
            data: Proxy config -- type, host, port and optionally login, password,
                title, change_ip_url, external_id.

        Returns:
            Freshly minted proxy data.
        """
        created = await self._request_data("POST", "/proxies", json=data)
        return dict(created or {})

    async def update_proxy(self, uuid: str, data: dict[str, Any]) -> dict[str, Any]:
        """Revise a proxy by UUID.

        Args:
            uuid: Proxy UUID.
            data: Fields to revise.

        Returns:
            Revised proxy data.
        """
        updated = await self._request_data("PATCH", f"/proxies/{uuid}", json=data)
        return dict(updated or {})

    async def delete_proxy(self, uuid: str) -> dict[str, Any]:
        """Discard a proxy by UUID.

        Args:
            uuid: Proxy UUID.

        Returns:
            Deletion result.
        """
        result = await self._request_data("DELETE", f"/proxies/{uuid}")
        return {"result": result}


def _validate_tag_color(color: str) -> None:
    """Refuse hues the API will not take (it wants names, not hex)."""
    if color not in TAG_COLORS:
        raise ValueError(f"Invalid tag color '{color}'. Allowed: {', '.join(TAG_COLORS)}")


def _cloud_fault(response: httpx.Response, payload: Any, endpoint: str) -> OctoApiFault:
    """Assemble an OctoApiFault from a failed cloud API response."""
    body = payload if isinstance(payload, dict) else {}
    msg = body.get("msg") or ""
    code = body.get("code")
    status = response.status_code

    if status in (401, 403):
        # 403 also covers "No active subscription", so keep the API's own reason
        reason = f"{msg} ({code})" if msg and code else (msg or f"HTTP {status}")
        message = (
            f"Cloud API access denied: {reason}. Check OCTO_API_TOKEN, its permissions "
            "and that the account has an active subscription."
        )
    elif status == 404:
        message = f"Resource not found: {endpoint}"
    else:
        message = msg or f"Octo API error {status} on {endpoint}"
        if msg and code:
            message = f"{msg} ({code})"

    return OctoApiFault(message, status_code=status, code=code, data=body.get("data"))


class LocalConduit(_HttpConduit):
    """
    Async conduit to the Octo Browser local API.

    The local API runs on port 58888 next to the desktop app and offers:
    - profile launch/halt
    - list of running profiles
    - sign-in
    - one-time (temporary) profiles
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 58888,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        """
        Wire up the local API conduit.

        Args:
            host: Host running Octo Browser (default: localhost).
            port: Local API port (default: 58888).
            username: Octo account email for auto-sign-in.
            password: Octo account password for auto-sign-in.
        """
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        super().__init__(
            f"http://{host}:{port}/api",
            timeout=60.0,
            connection_hint="Make sure Octo Browser is running.",
        )

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        """
        Run a request against the local API.

        ws:// endpoints in the response are rewritten to the configured host.

        Raises:
            ConnectionError: If Octo Browser is out of reach.
            OctoApiFault: If the local API flags a failure.
        """
        response = await self._send(method, endpoint, **kwargs)
        payload: Any = _parse_json(response)

        if not response.is_success:
            body = payload if isinstance(payload, dict) else {}
            raise OctoApiFault(
                body.get("msg") or f"Octo local API error {response.status_code} on {endpoint}",
                status_code=response.status_code,
                code=body.get("code"),
                data=body.get("data"),
            )

        return self._rehome_ws_endpoints(payload)

    def _rehome_ws_endpoints(self, data: Any) -> Any:
        """
        Recursively swap 127.0.0.1/localhost in ws:// URLs for the configured host.

        Needed when Octo Browser runs on another machine or inside Docker.
        """
        if isinstance(data, dict):
            return {k: self._rehome_ws_endpoints(v) for k, v in data.items()}
        if isinstance(data, list):
            return [self._rehome_ws_endpoints(item) for item in data]
        if isinstance(data, str) and data.startswith(("ws://", "wss://")):
            parsed = urlparse(data)
            if parsed.hostname in ("127.0.0.1", "localhost"):
                netloc = f"{self.host}:{parsed.port}" if parsed.port else self.host
                return urlunparse(
                    (
                        parsed.scheme,
                        netloc,
                        parsed.path,
                        parsed.params,
                        parsed.query,
                        parsed.fragment,
                    )
                )
        return data

    # === Sign-in ===

    async def login(
        self, email: str, password: str, api_token: str | None = None
    ) -> dict[str, Any]:
        """
        Sign in to an Octo Browser account (needs Octo Browser 1.8.0+).

        Args:
            email: Account email.
            password: Account password.
            api_token: Optional API token to attach to the session.

        Returns:
            Sign-in response.
        """
        payload: dict[str, Any] = {"email": email, "password": password}
        if api_token:
            payload["api_token"] = api_token
        result = await self._request("POST", "/auth/login", json=payload)
        return dict(result or {})

    async def logout(self) -> dict[str, Any]:
        """Sign out of the current account (needs Octo Browser 1.8.0+)."""
        result = await self._request("POST", "/auth/logout")
        return dict(result or {})

    async def get_username(self) -> dict[str, Any]:
        """Report the currently signed-in user."""
        result = await self._request("GET", "/username")
        return dict(result or {})

    async def ensure_logged_in(self) -> bool:
        """
        Confirm sign-in and sign in if credentials were supplied.

        Returns:
            True if signed in, False otherwise.
        """
        try:
            user = await self.get_username()
            if user.get("username"):
                return True
        except (OctoApiFault, ConnectionError) as exc:
            logger.debug("Not signed in yet: %s", exc)

        if self.username and self.password:
            try:
                await self.login(self.username, self.password)
                return True
            except (OctoApiFault, ConnectionError) as exc:
                logger.warning("Auto-sign-in failed: %s", exc)
        return False

    # === Profile management ===

    async def get_active_profiles(self) -> list[dict[str, Any]]:
        """
        Report the profiles running on this machine right now.

        Returns:
            List of profiles with uuid, state, ws_endpoint, debug_port, browser_pid.
        """
        result = await self._request("GET", "/profiles/active")
        if isinstance(result, dict):
            return list(result.get("data", []))
        return list(result) if isinstance(result, list) else []

    async def start_profile(
        self,
        uuid: str,
        headless: bool = False,
        debug_port: bool | int = True,
        timeout: int = 120,
        flags: list[str] | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        """
        Launch a profile.

        Args:
            uuid: Profile UUID.
            headless: Run without a GUI.
            debug_port: True picks a free automation port, an int (1024-65534)
                pins a specific one, False switches off CDP.
            timeout: Launch timeout in seconds; raise it for slow proxies.
            flags: Extra Chromium flags (Octo advises against these).
            password: Profile password, if the profile is protected.

        Returns:
            Dict with ws_endpoint, debug_port, state and connection_data.
        """
        await self.ensure_logged_in()

        if flags is None:
            flags = ["--start-maximized"]

        payload: dict[str, Any] = {
            "uuid": uuid,
            "headless": headless,
            "debug_port": debug_port,
            "timeout": timeout,
            "flags": flags,
            "only_local": True,
        }
        if password:
            payload["password"] = password

        logger.info("Launching profile %s (headless=%s)", uuid, headless)
        result = await self._request("POST", "/profiles/start", json=payload)
        return dict(result or {})

    async def stop_profile(self, uuid: str) -> dict[str, Any]:
        """
        Gracefully halt a running profile.

        Args:
            uuid: Profile UUID.
        """
        logger.info("Halting profile %s", uuid)
        result = await self._request("POST", "/profiles/stop", json={"uuid": uuid})
        return dict(result or {})

    async def force_stop_profile(self, uuid: str) -> dict[str, Any]:
        """
        Force-halt a running profile (needs Octo Browser 1.7+).

        Reach for this when a graceful halt will not take.

        Args:
            uuid: Profile UUID.
        """
        logger.info("Force-halting profile %s", uuid)
        result = await self._request("POST", "/profiles/force_stop", json={"uuid": uuid})
        return dict(result or {})

    async def get_profile_by_uuid(self, uuid: str) -> dict[str, Any] | None:
        """
        Find a running profile by UUID.

        Args:
            uuid: Profile UUID.

        Returns:
            Profile dict if it is running, None otherwise.
        """
        profiles = await self.get_active_profiles()
        for profile in profiles:
            if profile.get("uuid") == uuid:
                return profile
        return None

    async def get_or_start_profile(
        self,
        uuid: str,
        headless: bool = False,
        debug_port: bool | int = True,
        password: str | None = None,
    ) -> dict[str, Any]:
        """
        Hand back the running profile, launching it if need be.

        This is the recommended way to obtain a ws_endpoint: it covers the case
        where the profile is already running (locally or launched by someone else).

        Args:
            uuid: Profile UUID.
            headless: Run without a GUI.
            debug_port: True picks a free automation port, an int pins one.
            password: Profile password, if the profile is protected.

        Returns:
            Profile dict with ws_endpoint and an 'already_running' flag.
        """
        profile = await self.get_profile_by_uuid(uuid)
        if profile:
            profile["already_running"] = True
            return profile

        try:
            result = await self.start_profile(
                uuid=uuid,
                headless=headless,
                debug_port=debug_port,
                password=password,
            )
        except OctoApiFault:
            # The profile may have been launched in parallel; the error code for
            # that differs between client versions, so consult the running list.
            profile = await self.get_profile_by_uuid(uuid)
            if profile:
                profile["already_running"] = True
                return profile
            raise

        result["already_running"] = False
        return result

    # === One-time profiles ===

    async def start_one_time_profile(
        self,
        fingerprint_os: str = "win",
        headless: bool = False,
        debug_port: bool | int = True,
        proxy: dict[str, Any] | None = None,
        cookies: list[dict[str, Any]] | None = None,
        start_pages: list[str] | None = None,
        flags: list[str] | None = None,
        timeout: int = 60,
    ) -> dict[str, Any]:
        """
        Mint and launch a one-time (temporary) profile.

        One-time profiles are not synced and vanish when halted, which makes
        them quicker to launch and halt. One such request counts as 4 against the
        throttle.

        Args:
            fingerprint_os: Fingerprint OS -- win, mac, lin or android.
            headless: Run without a GUI.
            debug_port: True picks a free automation port, an int pins one.
            proxy: Proxy config (type, host, port, login, password).
            cookies: Cookies to inject.
            start_pages: URLs to open on launch.
            flags: Extra Chromium flags.
            timeout: Launch timeout in seconds.

        Returns:
            Dict with uuid, ws_endpoint and profile info.
        """
        await self.ensure_logged_in()

        profile_data: dict[str, Any] = {"fingerprint": {"os": fingerprint_os}}

        if proxy:
            profile_data["proxy"] = proxy
        if cookies:
            profile_data["cookies"] = cookies
        if start_pages:
            profile_data["start_pages"] = start_pages

        payload: dict[str, Any] = {
            "profile_data": profile_data,
            "headless": headless,
            "debug_port": debug_port,
            "timeout": timeout,
        }
        if flags:
            payload["flags"] = flags

        logger.info("Launching one-time profile (os=%s, headless=%s)", fingerprint_os, headless)
        result = await self._request("POST", "/profiles/one_time/start", json=payload)
        return dict(result or {})

    # === System ===

    async def get_version(self) -> dict[str, Any]:
        """
        Report Octo Browser version info.

        Returns:
            Dict with current, latest and update_required.
        """
        result = await self._request("GET", "/update")
        return dict(result or {})

    async def health_check(self) -> bool:
        """
        Probe whether the local API answers.

        Returns:
            True if the API responds, False otherwise.
        """
        try:
            await self.get_version()
            return True
        except (OctoApiFault, ConnectionError) as exc:
            logger.debug("Health probe failed: %s", exc)
            return False


def sniff_ws_endpoint(data: dict[str, Any]) -> str | None:
    """
    Sniff out the CDP WebSocket endpoint in an API response.

    Checks the usual keys first, then combs the whole response.

    Args:
        data: API response dictionary.

    Returns:
        WebSocket URL, or None if the response carries none.
    """
    for key in ("ws_endpoint", "wss_endpoint", "cdp_url", "cdp", "ws"):
        val = data.get(key)
        if isinstance(val, str) and val.startswith(("ws://", "wss://")):
            return val

    def comb(node: Any) -> str | None:
        if isinstance(node, dict):
            for value in node.values():
                if isinstance(value, str) and value.startswith(("ws://", "wss://")):
                    return value
                hit = comb(value)
                if hit:
                    return hit
        elif isinstance(node, list):
            for item in node:
                hit = comb(item)
                if hit:
                    return hit
        return None

    return comb(data)
