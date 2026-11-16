"""Plain, synchronous client for the documented GridPane API.

Every method here maps to an endpoint in GridPane's published Postman docs:
https://documenter.getpostman.com/view/13664964/TVssjU7Z

Importable with no MCP involved::

    from gridpane_mcp.gridpane_client import GridPaneClient

    gp = GridPaneClient.from_env()
    before = gp.find_backups(123, tag="task-42")
    gp.create_backup(123, tag="task-42")
    ref = gp.wait_for_backup(123, "task-42", exclude=before)
    ...
    gp.restore_by_tag(123, "task-42")

The docs do not show response bodies for the backup list endpoints, so
``find_backups`` parses them defensively (see ``parse_backups``). Check it
against a live response with ``gridpane backups <site_id> --raw`` before
relying on it in a pipeline.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import time
from dataclasses import asdict, dataclass
from typing import Any, Iterable

import httpx

logger = logging.getLogger("gridpane")

DEFAULT_API_URL = "https://my.gridpane.com/oauth/api/v1"

CLEAR_CACHE_SCOPES = ("page", "object", "all")
NGINX_CACHING_MODES = ("redis", "fastcgi", "none")
BACKUP_TYPES = ("local", "remote")
BACKUP_SOURCES = ("local", "aws-s3", "backblaze", "dropbox", "wasabi")
RESTORE_SOURCE_TYPES = ("original", "alternative")
RESTORE_BACKUP_TYPES = ("manual", "automatic")

_TAG_RE = re.compile(r"^[A-Za-z0-9-]{1,32}$")
_RESERVED_TAGS = ("manual", "auto")
_PHP_RE = re.compile(r"^\d+\.\d+$")


class GridPaneError(Exception):
    """An API call failed, or the input was rejected before sending."""

    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class BackupTimeout(GridPaneError):
    """wait_for_backup gave up before the tagged backup appeared."""


class WpCliNotAllowed(GridPaneError):
    """A WP-CLI command is outside the read-only allowlist."""


# ── Backup list parsing ──────────────────────────────────────


@dataclass(frozen=True)
class BackupRef:
    """One backup, in the shape the restore endpoint wants.

    ``backup`` is the documented "YYYY-MM-DD HH:MM [tag]" string.
    ``backup_source`` and ``backup_type`` are inferred from where the entry
    sat in the response; ``None`` means the response gave no clue.
    """

    backup: str
    date: str
    time: str
    tag: str | None
    backup_source: str | None
    backup_type: str | None

    @property
    def sort_key(self) -> str:
        return f"{self.date} {self.time}"

    def to_dict(self) -> dict:
        return asdict(self)


_BACKUP_STR_RE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2})[ T_](\d{2}):(\d{2})(?::\d{2})?(?:\s+([A-Za-z0-9-]{1,32}))?\s*$"
)
_DATETIME_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})[ T_](\d{2}):(\d{2})")
_SOURCE_ALIASES = {
    "local": "local",
    "aws-s3": "aws-s3",
    "aws_s3": "aws-s3",
    "s3": "aws-s3",
    "aws": "aws-s3",
    "backblaze": "backblaze",
    "b2": "backblaze",
    "dropbox": "dropbox",
    "wasabi": "wasabi",
}


def _infer_source(token: Any) -> str | None:
    if not isinstance(token, str):
        return None
    return _SOURCE_ALIASES.get(token.strip().lower())


def _infer_type(token: Any) -> str | None:
    if not isinstance(token, str):
        return None
    t = token.strip().lower()
    if "manual" in t:
        return "manual"
    if "auto" in t:
        return "automatic"
    return None


def parse_backups(payload: Any) -> list[BackupRef]:
    """Pull every backup out of a backups/original response.

    The response format is not documented, so this walks the whole JSON
    tree. Any string shaped like "YYYY-MM-DD HH:MM [tag]" counts as a
    backup, as does an object holding a date-time field plus a ``tag``
    field. The backup source and type come from the keys and values found
    on the way down (e.g. ``{"local": {"manual": [...]}}``).
    """
    found: dict[tuple, BackupRef] = {}

    def add(date: str, hh: str, mm: str, tag: str | None, src, typ):
        tag = tag or None
        backup = f"{date} {hh}:{mm}" + (f" {tag}" if tag else "")
        ref = BackupRef(backup, date, f"{hh}:{mm}", tag, src, typ)
        found.setdefault((backup, src, typ), ref)

    def walk(node: Any, src: str | None, typ: str | None):
        if isinstance(node, str):
            m = _BACKUP_STR_RE.match(node)
            if m:
                add(m.group(1), m.group(2), m.group(3), m.group(4), src, typ)
            return
        if isinstance(node, list):
            for item in node:
                walk(item, src, typ)
            return
        if not isinstance(node, dict):
            return

        # Values on this object can name the source/type for its children.
        for key in ("backup_source", "source", "storage", "provider"):
            src = _infer_source(node.get(key)) or src
        for key in ("backup_type", "type"):
            typ = _infer_type(node.get(key)) or typ

        # An object with a date-time field and a separate tag field.
        tag = node.get("tag") if isinstance(node.get("tag"), str) else None
        if tag is not None:
            for key in ("backup", "datetime", "date_time", "date", "time", "name", "created_at"):
                v = node.get(key)
                if isinstance(v, str):
                    m = _DATETIME_RE.match(v)
                    if m:
                        add(m.group(1), m.group(2), m.group(3), tag.strip(), src, typ)
                        break

        for key, child in node.items():
            walk(
                child,
                _infer_source(key) or src,
                _infer_type(key) or typ,
            )

    walk(payload, None, None)
    # A date field next to a tag field is also seen as a bare, untagged
    # string; drop that duplicate.
    tagged = {(r.sort_key, r.backup_source, r.backup_type) for r in found.values() if r.tag}
    refs = [
        r for r in found.values()
        if r.tag or (r.sort_key, r.backup_source, r.backup_type) not in tagged
    ]
    return sorted(refs, key=lambda r: r.sort_key)


def _as_id(value: int | str) -> int | None:
    """An int, or a string of digits, is an id; anything else is a name."""
    if isinstance(value, bool):
        raise GridPaneError("Expected an id or a domain, got a boolean.")
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text) if text.isdigit() else None


def normalize_domain(value: str) -> str:
    """'HTTPS://www.Example.com:443/path' -> 'example.com'."""
    d = value.strip().lower()
    d = re.sub(r"^[a-z][a-z0-9+.-]*://", "", d)
    d = d.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    d = d.rsplit("@", 1)[-1].split(":", 1)[0].rstrip(".")
    return d[4:] if d.startswith("www.") else d


def _check_tag(tag: str) -> str:
    if not _TAG_RE.match(tag):
        raise GridPaneError(
            f"Invalid backup tag {tag!r}: use letters, digits and dashes, up to 32 characters."
        )
    if tag.lower() in _RESERVED_TAGS:
        raise GridPaneError(f"Backup tag {tag!r} is reserved by GridPane.")
    return tag


def _check_choice(name: str, value: str, choices: Iterable[str]) -> str:
    choices = tuple(choices)
    if value not in choices:
        raise GridPaneError(f"{name} must be one of {', '.join(choices)}; got {value!r}.")
    return value


# ── WP-CLI allowlist ─────────────────────────────────────────

# (command, subcommand words...) prefixes allowed without opting in.
# Read-only, plus `cache flush`, which only empties the object cache.
WP_CLI_READ_ONLY: tuple[tuple[str, ...], ...] = (
    ("cache", "flush"),
    ("cache", "type"),
    ("core", "version"),
    ("core", "is-installed"),
    ("core", "check-update"),
    ("core", "verify-checksums"),
    ("cron", "event", "list"),
    ("cron", "schedule", "list"),
    ("db", "size"),
    ("db", "tables"),
    ("language", "core", "list"),
    ("language", "plugin", "list"),
    ("language", "theme", "list"),
    ("menu", "list"),
    ("option", "get"),
    ("option", "list"),
    ("option", "pluck"),
    ("plugin", "list"),
    ("plugin", "get"),
    ("plugin", "status"),
    ("plugin", "is-active"),
    ("plugin", "is-installed"),
    ("plugin", "verify-checksums"),
    ("post", "list"),
    ("post", "get"),
    ("post", "meta", "get"),
    ("post", "meta", "list"),
    ("rewrite", "list"),
    ("sidebar", "list"),
    ("site", "list"),
    ("theme", "list"),
    ("theme", "get"),
    ("theme", "status"),
    ("theme", "is-active"),
    ("theme", "is-installed"),
    ("transient", "get"),
    ("widget", "list"),
)

# Global WP-CLI flags that can run arbitrary code or reach another host.
_WP_CLI_BLOCKED_FLAGS = ("--exec", "--require", "--ssh", "--http", "--path")
# Characters that would chain a second shell command if GridPane passes the
# string to a shell. Rejected unless the caller opts in to write access.
_WP_CLI_SHELL_CHARS = set(";&|`$<>\\\n\r")


def check_wp_cli(commands: dict[str, list[str]]) -> None:
    """Raise WpCliNotAllowed unless every command is on the read-only list."""
    if not isinstance(commands, dict) or not commands:
        raise GridPaneError("WP-CLI commands must be a non-empty {command: [args, ...]} object.")
    for command, arg_strings in commands.items():
        if isinstance(arg_strings, str) or not isinstance(arg_strings, list) or not arg_strings:
            raise GridPaneError(f"WP-CLI {command!r} needs a non-empty list of strings.")
        for args in arg_strings:
            line = f"{command} {args}"
            if _WP_CLI_SHELL_CHARS & set(line):
                raise WpCliNotAllowed(f"`wp {line}` contains shell control characters.")
            try:
                words = shlex.split(line)
            except ValueError as e:
                raise WpCliNotAllowed(f"Cannot parse `wp {line}`: {e}") from e
            for w in words:
                if any(w == f or w.startswith(f + "=") for f in _WP_CLI_BLOCKED_FLAGS):
                    raise WpCliNotAllowed(f"`wp {line}` uses the blocked flag {w.split('=')[0]}.")
            positional = tuple(w for w in words if not w.startswith("-"))
            if not any(positional[: len(p)] == p for p in WP_CLI_READ_ONLY):
                raise WpCliNotAllowed(
                    f"`wp {line}` is not on the read-only allowlist. "
                    "Pass allow_write=True (CLI: --allow-write) to run it anyway."
                )


def wp_cli_from_line(line: str) -> dict[str, list[str]]:
    """Turn "plugin list --status=active" into {"plugin": ["list --status=active"]}."""
    line = line.strip()
    if line.startswith("wp "):
        line = line[3:].strip()
    command, _, rest = line.partition(" ")
    if not command or not rest.strip():
        raise GridPaneError(f"Expected '<command> <subcommand> [options]', got {line!r}.")
    return {command: [rest.strip()]}


# ── Client ───────────────────────────────────────────────────


def _retry_after(headers: httpx.Headers, attempt: int) -> int:
    """Seconds to wait after a 429.

    GridPane sends its own headers rather than a standard Retry-After, e.g.
    `retry-after-endpoint: 23` alongside `x-ratelimit-endpoint-limit: 12` and
    `x-ratelimit-total-limit: 60`. Uses the longest wait any header asks for,
    plus a second of slack; falls back to 5, 10, 20... seconds.
    """
    waits = []
    for name in ("retry-after", "retry-after-endpoint", "retry-after-total"):
        value = headers.get(name)
        if value and value.strip().isdigit():
            waits.append(int(value))
    if not waits:
        now = time.time()
        for name in ("x-ratelimit-endpoint-reset", "x-ratelimit-total-reset", "x-ratelimit-reset"):
            value = headers.get(name)
            if value and value.strip().isdigit() and int(value) > now:
                waits.append(int(value) - int(now))
    wait = max(waits) + 1 if waits else 5 * 2**attempt
    return max(1, min(wait, 120))


def _tls_verify():
    """TLS trust for API calls: the operating system's certificate store.

    Antivirus HTTPS scanning (e.g. Norton Web/Mail Shield) and corporate
    proxies re-sign traffic with a root they add to the Windows store, which
    Python's bundled list doesn't have. GRIDPANE_CA_BUNDLE points at a PEM
    file instead, if set.
    """
    bundle = os.getenv("GRIDPANE_CA_BUNDLE")
    if bundle:
        return bundle
    try:
        import ssl

        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return True


class GridPaneClient:
    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_API_URL,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        rate_limit_retries: int = 5,
    ):
        if not token:
            raise GridPaneError("GRIDPANE_API_TOKEN is not set.")
        self.base_url = base_url.rstrip("/")
        self.rate_limit_retries = rate_limit_retries
        self._sites: list[dict] | None = None
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            verify=_tls_verify(),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )

    @classmethod
    def from_env(cls, **kwargs) -> "GridPaneClient":
        return cls(
            token=os.getenv("GRIDPANE_API_TOKEN", ""),
            base_url=os.getenv("GRIDPANE_API_URL", DEFAULT_API_URL),
            **kwargs,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _request(self, method: str, path: str, json: Any = None, params: dict | None = None) -> Any:
        attempt = 0
        while True:
            try:
                r = self._http.request(method, path, json=json, params=params)
            except httpx.HTTPError as e:
                raise GridPaneError(f"{method} {path} failed: {e}") from e
            # A 429 is rejected before it is processed, so retrying a POST is safe.
            if r.status_code == 429 and attempt < self.rate_limit_retries:
                wait = _retry_after(r.headers, attempt)
                attempt += 1
                logger.warning(
                    "GridPane rate limit on %s %s; waiting %ds (retry %d of %d)",
                    method, path, wait, attempt, self.rate_limit_retries,
                )
                time.sleep(wait)
                continue
            break
        try:
            body = r.json() if r.content else {}
        except ValueError:
            body = r.text
        if r.is_error:
            raise GridPaneError(f"{method} {path} returned HTTP {r.status_code}", r.status_code, body)
        return body

    # Servers and sites (GET) ─────────────────────────────────
    # `per_page` isn't in the docs, but the live API honours it (up to 500).

    def list_servers(self, per_page: int = 100) -> Any:
        return self._request("GET", "server", params={"per_page": per_page})

    def get_server(self, server_id: int) -> Any:
        return self._request("GET", f"server/{server_id}")

    def list_sites(self, per_page: int = 500) -> Any:
        return self._request("GET", "site", params={"per_page": per_page})

    def get_site(self, site_id: int) -> Any:
        return self._request("GET", f"site/{site_id}")

    # Domain lookup ───────────────────────────────────────────

    def all_sites(self) -> list[dict]:
        """Every site, following pagination; cached for this client's life."""
        if self._sites is None:
            sites: list[dict] = []
            page = 1
            while True:
                data = self._request("GET", "site", params={"per_page": 500, "page": page})
                rows = data.get("data", []) if isinstance(data, dict) else data
                sites.extend(r for r in rows if isinstance(r, dict))
                meta = data.get("meta", {}) if isinstance(data, dict) else {}
                if not rows or page >= int(meta.get("last_page") or 1):
                    break
                page += 1
            self._sites = sites
        return self._sites

    def find_site(self, site: int | str) -> dict:
        """A site by id or by domain ("example.com", "https://www.example.com/")."""
        site_id = _as_id(site)
        if site_id is not None:
            data = self.get_site(site_id)
            return data.get("data", data) if isinstance(data, dict) else data
        want = normalize_domain(str(site))
        sites = self.all_sites()
        for s in sites:
            if normalize_domain(str(s.get("url", ""))) == want:
                return s
        # GET site lists only primary sites. GridPane's staging.<domain> and
        # canary.<domain> copies appear only in each server's `sites` list,
        # and the API refuses them ("does not belong to the bearer of this
        # access token"). Return what the server list knows, which is enough
        # for the SSH backend; REST calls on them will be refused by GridPane.
        data = self.list_servers()
        for server in data.get("data", []) if isinstance(data, dict) else data:
            for s in server.get("sites") or []:
                if normalize_domain(str(s.get("url", ""))) == want and s.get("id"):
                    return {
                        "id": int(s["id"]),
                        "url": s.get("url"),
                        "server_id": server.get("id"),
                        "server": {k: server.get(k) for k in ("id", "label", "ip")},
                        "api_access": False,
                        "note": "Staging/canary copy, listed on its server only. GridPane's API "
                        "refuses calls for it; the `gridpane ssh` commands can reach it.",
                    }
        near = [str(s.get("url")) for s in sites if want and want.split(".")[0] in str(s.get("url", ""))]
        hint = f" Did you mean: {', '.join(near[:5])}?" if near else " `gridpane sites` lists them."
        raise GridPaneError(f"No site with domain {want!r}.{hint}", 404)

    def find_server(self, server: int | str) -> dict:
        """A server by id, IP address or label (case-insensitive)."""
        server_id = _as_id(server)
        if server_id is not None:
            data = self.get_server(server_id)
            return data.get("data", data) if isinstance(data, dict) else data
        want = str(server).strip().lower()
        data = self.list_servers()
        rows = data.get("data", []) if isinstance(data, dict) else data
        for s in rows:
            if want in (str(s.get("ip", "")).lower(), str(s.get("label", "")).lower()):
                return s
        near = [str(s.get("label")) for s in rows if want in str(s.get("label", "")).lower()]
        if len(near) == 1:
            return next(s for s in rows if str(s.get("label")) == near[0])
        hint = f" Matches: {', '.join(near[:5])}." if near else " `gridpane servers` lists them."
        raise GridPaneError(f"No single server matching {server!r}.{hint}", 404)

    def resolve_site_id(self, site: int | str) -> int:
        site_id = _as_id(site)
        return site_id if site_id is not None else int(self.find_site(site)["id"])

    # Site settings: PUT site/{site.id} ───────────────────────

    def update_site(self, site_id: int, body: dict) -> Any:
        return self._request("PUT", f"site/{site_id}", json=body)

    def clear_cache(self, site_id: int, scope: str = "all") -> Any:
        _check_choice("scope", scope, CLEAR_CACHE_SCOPES)
        return self.update_site(site_id, {"clear_cache": scope})

    def set_nginx_caching(self, site_id: int, mode: str, disable_first: bool = False) -> Any:
        """Set Nginx page caching to redis, fastcgi or none.

        GridPane will not switch directly between redis and fastcgi; the
        active type has to be disabled first. ``disable_first=True`` sends
        ``none`` before the new mode. The docs don't say whether the first
        change has to finish before the second is accepted.
        """
        _check_choice("mode", mode, NGINX_CACHING_MODES)
        if disable_first and mode != "none":
            self.update_site(site_id, {"nginx_caching": "none"})
        return self.update_site(site_id, {"nginx_caching": mode})

    def set_debug(self, site_id: int, enabled: bool) -> Any:
        return self.update_site(site_id, {"debug": bool(enabled)})

    def set_php_version(self, site_id: int, version: str) -> Any:
        # The docs list only 7.2-7.4 and are clearly dated, so any X.Y is sent.
        if not _PHP_RE.match(version):
            raise GridPaneError(f"PHP version should look like 8.2; got {version!r}.")
        return self.update_site(site_id, {"php_version": version})

    # WP-CLI: PUT site/run-wp-cli/{site.id} ───────────────────

    def run_wp_cli(self, site_id: int, commands: dict[str, list[str]], allow_write: bool = False) -> Any:
        """Run WP-CLI commands, e.g. {"plugin": ["list --status=active"]}.

        Commands outside WP_CLI_READ_ONLY raise WpCliNotAllowed unless
        ``allow_write`` is set. Per the docs the response only reports
        success; command output is not returned.
        """
        if allow_write:
            if not isinstance(commands, dict) or not commands:
                raise GridPaneError("WP-CLI commands must be a non-empty {command: [args, ...]} object.")
        else:
            check_wp_cli(commands)
        return self._request("PUT", f"site/run-wp-cli/{site_id}", json={"wp": commands})

    def wp(self, site_id: int, line: str, allow_write: bool = False) -> Any:
        return self.run_wp_cli(site_id, wp_cli_from_line(line), allow_write=allow_write)

    # Backups ─────────────────────────────────────────────────

    def create_backup(
        self,
        site_id: int,
        type: str = "local",
        tag: str | None = None,
        integration_id: int | None = None,
    ) -> Any:
        _check_choice("type", type, BACKUP_TYPES)
        body: dict[str, Any] = {"type": type}
        if type == "remote":
            if integration_id is None:
                raise GridPaneError("integration_id is required for a remote backup.")
            body["integration_id"] = int(integration_id)
        if tag:
            body["tag"] = _check_tag(tag)
        return self._request("POST", f"backups/{site_id}", json=body)

    def refresh_backups(self, site_id: int) -> Any:
        return self._request("GET", f"backups/refresh/{site_id}")

    def get_original_backups(self, site_id: int) -> Any:
        return self._request("GET", f"backups/original/{site_id}")

    def list_backups(self, site_id: int, refresh: bool = True) -> Any:
        """Raw backups/original response, refreshed first as the docs advise."""
        if refresh:
            self.refresh_backups(site_id)
        return self.get_original_backups(site_id)

    def find_backups(self, site_id: int, tag: str | None = None, refresh: bool = True) -> list[BackupRef]:
        refs = parse_backups(self.list_backups(site_id, refresh=refresh))
        if tag is not None:
            refs = [r for r in refs if r.tag and r.tag.lower() == tag.lower()]
        return refs

    def wait_for_backup(
        self,
        site_id: int,
        tag: str,
        timeout: float = 1800,
        interval: float = 30,
        exclude: Iterable[BackupRef] = (),
    ) -> BackupRef:
        """Poll refresh + original until a backup tagged ``tag`` appears.

        GridPane has no job-status endpoint, so this is the only way to know
        the backup exists. Pass the result of ``find_backups(site_id, tag)``
        taken before ``create_backup`` as ``exclude`` so an older backup with
        the same tag does not count. Returns the newest new match.
        """
        _check_tag(tag)
        skip = {r.backup for r in exclude}
        deadline = time.monotonic() + timeout
        while True:
            matches = [r for r in self.find_backups(site_id, tag) if r.backup not in skip]
            if matches:
                return matches[-1]
            if time.monotonic() + interval > deadline:
                raise BackupTimeout(f"No backup tagged {tag!r} on site {site_id} after {timeout:.0f}s.")
            time.sleep(interval)

    def restore_backup(
        self,
        site_id: int,
        backup: str,
        backup_source: str = "local",
        source_type: str = "original",
        backup_type: str = "manual",
    ) -> Any:
        _check_choice("backup_source", backup_source, BACKUP_SOURCES)
        _check_choice("source_type", source_type, RESTORE_SOURCE_TYPES)
        _check_choice("backup_type", backup_type, RESTORE_BACKUP_TYPES)
        if not _BACKUP_STR_RE.match(backup):
            raise GridPaneError(f"backup should look like 'YYYY-MM-DD HH:MM [tag]'; got {backup!r}.")
        return self._request(
            "POST",
            f"backups/restore/{site_id}",
            json={
                "backup_source": backup_source,
                "source_type": source_type,
                "backup_type": backup_type,
                "backup": backup.strip(),
            },
        )

    def resolve_tag(self, site_id: int, tag: str, backup_source: str | None = None) -> BackupRef:
        """Newest backup with this tag, optionally limited to one source."""
        _check_tag(tag)
        refs = self.find_backups(site_id, tag)
        if backup_source:
            refs = [r for r in refs if r.backup_source in (backup_source, None)]
        if not refs:
            raise GridPaneError(f"No backup tagged {tag!r} found for site {site_id}.", 404)
        return refs[-1]

    def restore_by_tag(
        self,
        site_id: int,
        tag: str,
        backup_source: str | None = None,
        source_type: str = "original",
    ) -> tuple[BackupRef, Any]:
        """Restore the newest backup tagged ``tag``. Returns (backup, response).

        Source and type come from the backup list where it says; otherwise
        local / manual, which is what ``create_backup(type="local", tag=...)``
        makes.
        """
        ref = self.resolve_tag(site_id, tag, backup_source)
        result = self.restore_backup(
            site_id,
            ref.backup,
            backup_source=backup_source or ref.backup_source or "local",
            source_type=source_type,
            backup_type=ref.backup_type or "manual",
        )
        return ref, result
