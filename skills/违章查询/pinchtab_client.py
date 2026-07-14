#!/usr/bin/env python3
"""
PinchTab Client -- General-purpose Python HTTP client for PinchTab.

Zero external dependencies (stdlib only).  Works with any Python 3.6+.

    from pinchtab_client import PinchTabClient, PinchTabError

    client = PinchTabClient()
    tab_id = client.open_tab("default", "about:blank")
    title = client.evaluate(tab_id, "document.title")
    client.close_tab(tab_id)

Architecture:
  - Server  (9867): instance discovery & management only.
  - Bridges (9868+): ALL tab operations (create, evaluate, snapshot, click,
    find, wait, close, etc.).  Each tab lives on exactly one bridge.

This works around a PinchTab server bug where tab-scoped routes on the
server return 404 for newly created tabs (the locator cache is not
invalidated after ``POST /instances/{id}/tabs/open``).  Talking directly
to the bridge avoids the issue entirely and is also faster (one fewer
proxy hop).

Isolation: each process creates its own tab via ``open_tab()`` and passes
the returned ``tab_id`` to every operation.  Tab-scoped routes on the
bridge guarantee operations never cross-contaminate.  Different profiles
→ different instances → different bridges → independent Chrome cookie jars.
"""

import json
import os
import urllib.request
import urllib.error
import urllib.parse


class PinchTabError(Exception):
    """Error from the PinchTab API or transport layer."""

    def __init__(self, message, code=None, details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class PinchTabClient:
    """HTTP client for PinchTab.

    Uses the **server** (default :9867) for instance discovery and the
    **bridge** (per-instance port) for all tab operations.  This two-tier
    architecture works correctly even when the server's tab-locator cache
    is stale.

    Usage::

        client = PinchTabClient()
        tab_id = client.open_tab("default")        # creates tab on instance
        client.navigate(tab_id, "https://example.com")
        snap = client.snapshot(tab_id)
        result = client.evaluate(tab_id, "1 + 1")
        client.close_tab(tab_id)                   # clean up when done
    """

    def __init__(self, server_url=None, token=None):
        """
        Args:
            server_url: Server base URL.
                        Defaults to ``PINCHTAB_SERVER`` env or
                        ``http://127.0.0.1:9867``.
            token: Auth token.  Defaults to ``PINCHTAB_SESSION``, then
                   ``PINCHTAB_TOKEN``, then ``~/.pinchtab/config.json``.
        """
        if server_url is None:
            server_url = os.environ.get(
                "PINCHTAB_SERVER", "http://127.0.0.1:9867"
            )
        self.server_url = server_url.rstrip("/")

        if token is None:
            token = self._resolve_token()
        self.token = token

        # Auth header format depends on token type.
        if self.token.startswith("ses_"):
            self._auth_header = f"Session {self.token}"
        else:
            self._auth_header = f"Bearer {self.token}"

        # Internal caches.
        # tab_id  -> bridge base URL (populated by open_tab)
        self._tab_bridge = {}
        # instance_id | profile_name -> bridge base URL (lazy-filled)
        self._profile_bridge = {}

    # ── token resolution ──────────────────────────────────────────────

    @staticmethod
    def _resolve_token():
        """Resolve the auth token from environment or config file."""
        # 1. Session token (highest priority)
        session = os.environ.get("PINCHTAB_SESSION")
        if session:
            return session

        # 2. Explicit token env var
        token = os.environ.get("PINCHTAB_TOKEN")
        if token:
            return token

        # 3. Config file
        config_path = os.path.expanduser("~/.pinchtab/config.json")
        try:
            with open(config_path, "r") as f:
                config = json.load(f)
            return config["server"]["token"]
        except (FileNotFoundError, KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "PinchTab token not found. Set PINCHTAB_TOKEN env var "
                "or ensure ~/.pinchtab/config.json has server.token set."
            ) from exc

    # ── low-level HTTP ────────────────────────────────────────────────

    def _http(self, base_url, method, path, body=None, raw=False, timeout=30):
        """Make an HTTP request to *base_url* + *path*.

        Returns parsed JSON (dict/list), or raw ``bytes`` when
        ``raw=True``.  An empty response body yields ``{}``.
        """
        url = f"{base_url}{path}"
        headers = {
            "Authorization": self._auth_header,
            "Content-Type": "application/json",
        }

        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if raw:
                    return resp.read()
                content = resp.read().decode("utf-8")
                if not content:
                    return {}
                return json.loads(content)
        except urllib.error.HTTPError as exc:
            error_msg = f"HTTP {exc.code}"
            error_details = {}
            try:
                error_body = exc.read().decode("utf-8")
                error_json = json.loads(error_body)
                error_msg = error_json.get("error", error_msg)
                error_details = error_json.get("details", {})
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
            raise PinchTabError(error_msg, code=exc.code, details=error_details) from exc
        except urllib.error.URLError as exc:
            raise PinchTabError(f"Connection failed: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise PinchTabError(f"Invalid JSON response: {exc}") from exc

    # ── server-tier helpers (instance management) ─────────────────────

    def _server(self, method, path, body=None, raw=False, timeout=30):
        """Request to the PinchTab **server** (instance management)."""
        return self._http(self.server_url, method, path, body=body, raw=raw, timeout=timeout)

    # ── bridge-tier helpers (tab operations) ──────────────────────────

    def _bridge_for_tab(self, tab_id):
        """Return the bridge base URL that owns *tab_id*."""
        bridge = self._tab_bridge.get(tab_id)
        if bridge is None:
            raise PinchTabError(
                f"No bridge known for tab {tab_id}. "
                f"Create it with open_tab() first."
            )
        return bridge

    def _bridge(self, tab_id, method, path_suffix, body=None, raw=False, timeout=30):
        """Request to the **bridge** that owns *tab_id*.

        *path_suffix* is the path **after** the tab-scoped prefix,
        e.g. ``"/evaluate"`` or ``"/snapshot"`` or ``""`` for tab-level
        operations like close.
        """
        bridge = self._bridge_for_tab(tab_id)
        path = f"/tabs/{tab_id}{path_suffix}"
        return self._http(bridge, method, path, body=body, raw=raw, timeout=timeout)

    # ── bridge URL resolution ─────────────────────────────────────────

    def _resolve_bridge(self, instance_id_or_profile):
        """Return the bridge base URL for an instance or profile name.

        Caches results in ``self._profile_bridge``.
        """
        key = instance_id_or_profile
        if key in self._profile_bridge:
            return self._profile_bridge[key]

        # Fetch instance list from server.
        instances = self.list_instances()
        for inst in instances:
            bridge = inst.get("url") or f"http://127.0.0.1:{inst['port']}"
            # Index by both instance ID and profile name.
            self._profile_bridge[inst["id"]] = bridge
            pn = inst.get("profileName")
            if pn:
                self._profile_bridge[pn] = bridge

        bridge = self._profile_bridge.get(key)
        if bridge is None:
            raise PinchTabError(
                f"No running instance found for '{instance_id_or_profile}'. "
                f"Start one with start_instance() or check list_instances()."
            )
        return bridge

    # ── instance management (server tier) ─────────────────────────────

    def list_instances(self):
        """List all running instances.

        Returns a list of dicts with keys: ``id``, ``profileName``,
        ``port``, ``url``, ``status``, ``mode``, etc.
        """
        resp = self._server("GET", "/instances")
        if isinstance(resp, list):
            return resp
        return resp.get("instances", [])

    def get_instance(self, instance_id):
        """Get a single instance by ID."""
        return self._server("GET", f"/instances/{instance_id}")

    def start_instance(self, profile_id=None, mode=None, port=None):
        """Start a new instance (or return existing).

        Args:
            profile_id: Profile name or hash ID.
            mode: ``"headless"`` or ``"headed"``.
            port: Specific port (auto-allocated if omitted).

        Returns an Instance dict.  Call ``_resolve_bridge()`` afterward
        to populate the bridge URL cache.
        """
        body = {}
        if profile_id:
            body["profileId"] = profile_id
        if mode:
            body["mode"] = mode
        if port:
            body["port"] = port
        resp = self._server("POST", "/instances/start", body)
        # Invalidate cache so next _resolve_bridge picks up the new instance.
        self._profile_bridge.clear()
        return resp

    def stop_instance(self, instance_id):
        """Stop a running instance."""
        return self._server("POST", f"/instances/{instance_id}/stop")

    def find_instance_by_profile(self, profile_name):
        """Return the Instance dict for *profile_name*, or None."""
        for inst in self.list_instances():
            if inst.get("profileName") == profile_name:
                return inst
        return None

    # ── tab lifecycle ─────────────────────────────────────────────────

    def open_tab(self, instance_id_or_profile, url="about:blank"):
        """Open a new tab on the given instance.

        Args:
            instance_id_or_profile: Instance ID (``inst_XXXXXXXX``) or
                profile name (e.g. ``"default"``, ``"shenzhen"``).
            url: Initial URL (default ``"about:blank"``).

        Returns:
            The new tab ID as a hex string.  Pass this to every
            subsequent tab operation.

        Tab lifecycle::

            tab_id = client.open_tab("shenzhen")
            # ... do work ...
            client.close_tab(tab_id)   # always clean up!
        """
        bridge_url = self._resolve_bridge(instance_id_or_profile)

        # Create tab directly on the bridge.
        resp = self._http(bridge_url, "POST", "/tab",
                          {"action": "new", "url": url})
        tab_id = resp.get("tabId")
        if not tab_id:
            raise PinchTabError(f"Bridge returned no tabId: {resp}")

        # Remember which bridge owns this tab.
        self._tab_bridge[tab_id] = bridge_url
        return tab_id

    def close_tab(self, tab_id):
        """Close a tab and release its resources.

        Always call this when a task completes to avoid leaking tabs.
        The ``tabEvictionPolicy: close_lru`` config provides a safety net,
        but explicit cleanup is preferred.
        """
        result = self._bridge(tab_id, "POST", "/close")
        self._tab_bridge.pop(tab_id, None)
        return result

    def list_tabs(self):
        """List all tabs across all known bridges.

        Returns a list of tab dicts with ``id``, ``url``, ``title``.
        """
        tabs = []
        seen_bridges = set()
        for inst in self.list_instances():
            bridge = inst.get("url") or f"http://127.0.0.1:{inst['port']}"
            if bridge in seen_bridges:
                continue
            seen_bridges.add(bridge)
            try:
                resp = self._http(bridge, "GET", "/tabs")
                if isinstance(resp, list):
                    tabs.extend(resp)
                elif isinstance(resp, dict):
                    tabs.extend(resp.get("tabs", []))
            except PinchTabError:
                pass  # bridge may not be reachable
        return tabs

    # ── navigation ────────────────────────────────────────────────────

    def navigate(self, tab_id, url):
        """Navigate a tab to *url*."""
        return self._bridge(tab_id, "POST", "/navigate", {"url": url})

    def reload(self, tab_id):
        """Reload the current page."""
        return self._bridge(tab_id, "POST", "/reload")

    def go_back(self, tab_id):
        """Go back in history."""
        return self._bridge(tab_id, "POST", "/back")

    def go_forward(self, tab_id):
        """Go forward in history."""
        return self._bridge(tab_id, "POST", "/forward")

    # ── JavaScript evaluation ─────────────────────────────────────────

    def evaluate(self, tab_id, expression, await_promise=False):
        """Execute JavaScript in a tab and return the result.

        Args:
            tab_id: Tab ID.
            expression: JavaScript source to evaluate.
            await_promise: Wait for a returned Promise to resolve.

        Returns:
            The JS result value (already parsed from JSON).
        """
        body = {"expression": expression}
        if await_promise:
            body["awaitPromise"] = True
        resp = self._bridge(tab_id, "POST", "/evaluate", body)
        return resp.get("result")

    # ── page content ──────────────────────────────────────────────────

    def snapshot(self, tab_id, filter=None):
        """Get the accessibility-tree snapshot.

        Returns a dict with keys: ``nodes``, ``refs``, ``targets``,
        ``url``, ``title``.
        """
        path = "/snapshot"
        if filter:
            path += "?filter=" + urllib.parse.quote(filter)
        return self._bridge(tab_id, "GET", path)

    def text(self, tab_id):
        """Get visible page text.  Returns a string."""
        resp = self._bridge(tab_id, "GET", "/text")
        return resp.get("text", "")

    def html(self, tab_id):
        """Get the full page HTML.  Returns a string."""
        resp = self._bridge(tab_id, "GET", "/html")
        return resp.get("html", "")

    def current_url(self, tab_id):
        """Get the current URL."""
        resp = self._bridge(tab_id, "GET", "/url")
        return resp.get("url", "")

    def current_title(self, tab_id):
        """Get the current page title."""
        resp = self._bridge(tab_id, "GET", "/title")
        return resp.get("title", "")

    # ── actions ───────────────────────────────────────────────────────

    def action(self, tab_id, action):
        """Perform a single action on a tab.

        Args:
            tab_id: Tab ID.
            action: Action dict, e.g.
                ``{"action": "click", "ref": "e2"}``
                ``{"action": "type", "ref": "e5", "text": "hello"}``
                ``{"action": "press", "key": "Enter"}``
        """
        return self._bridge(tab_id, "POST", "/action", action)

    def actions(self, tab_id, actions_list):
        """Perform a batch of actions sequentially."""
        return self._bridge(tab_id, "POST", "/actions", {"actions": actions_list})

    # ── convenience wrappers ───────────────────────────────────────────

    def click(self, tab_id, ref):
        """Click an element by its accessibility ref (e.g. ``"e2"``)."""
        return self.action(tab_id, {"action": "click", "ref": ref})

    def type_text(self, tab_id, ref, text):
        """Type *text* into an element by its ref."""
        return self.action(tab_id, {"action": "type", "ref": ref, "text": text})

    def press_key(self, tab_id, key):
        """Press a keyboard key (``"Enter"``, ``"Escape"``, ``"Tab"``, etc.)."""
        return self.action(tab_id, {"action": "press", "key": key})

    # ── find & wait ───────────────────────────────────────────────────

    def find(self, tab_id, query, ref_only=False):
        """Find elements by semantic description.

        Args:
            tab_id: Tab ID.
            query: Natural-language description.
            ref_only: Return only the ref string(s).
        """
        body = {"query": query}
        if ref_only:
            body["refOnly"] = True
        return self._bridge(tab_id, "POST", "/find", body)

    def wait(self, tab_id, condition):
        """Wait for a condition.

        Args:
            condition: e.g. ``{"text": "我的主页", "timeout": 30000}``
        """
        return self._bridge(tab_id, "POST", "/wait", condition)

    def wait_for_text(self, tab_id, text, timeout_ms=30000):
        """Wait until *text* appears on the page."""
        return self.wait(tab_id, {"text": text, "timeout": timeout_ms})

    # ── screenshot ────────────────────────────────────────────────────

    def screenshot(self, tab_id):
        """Capture a PNG screenshot.  Returns raw ``bytes``."""
        return self._bridge(tab_id, "GET", "/screenshot", raw=True)

    def screenshot_to_file(self, tab_id, path):
        """Capture a screenshot and save to *path*.  Returns *path*."""
        data = self.screenshot(tab_id)
        with open(path, "wb") as f:
            f.write(data)
        return path

    # ── dialog handling ───────────────────────────────────────────────

    def dialog(self, tab_id, action, text=None):
        """Handle a JavaScript dialog.

        Args:
            action: ``"accept"``, ``"dismiss"``, or ``"prompt"``.
            text: Prompt response (only for ``"prompt"``).
        """
        body = {"action": action}
        if text is not None:
            body["text"] = text
        return self._bridge(tab_id, "POST", "/dialog", body)

    # ── cookies ───────────────────────────────────────────────────────

    def get_cookies(self, tab_id):
        """Get cookies for the tab's current origin."""
        return self._bridge(tab_id, "GET", "/cookies")

    def set_cookies(self, tab_id, cookies):
        """Set cookies for the tab's current origin."""
        return self._bridge(tab_id, "POST", "/cookies", {"cookies": cookies})

    def clear_cookies(self, tab_id):
        """Clear all cookies for the tab's current origin."""
        return self._bridge(tab_id, "DELETE", "/cookies")

    # ── tab lock ──────────────────────────────────────────────────────

    def lock(self, tab_id, owner="pinchtab_client", ttl_sec=600):
        """Lock a tab for exclusive use (TTL 10 min by default)."""
        return self._bridge(tab_id, "POST", "/lock",
                            {"owner": owner, "ttl": ttl_sec})

    def unlock(self, tab_id, owner="pinchtab_client"):
        """Release a tab lock."""
        return self._bridge(tab_id, "POST", "/unlock", {"owner": owner})

    def tab_lock_info(self, tab_id):
        """Get lock information for a tab."""
        return self._bridge(tab_id, "GET", "/lock")
