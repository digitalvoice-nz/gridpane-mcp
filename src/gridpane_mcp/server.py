"""MCP server for GridPane. Thin wrappers over gridpane_client.

Every tool calls an endpoint from GridPane's published API docs:
https://documenter.getpostman.com/view/13664964/TVssjU7Z
"""

import functools
import json
import logging
import os
import sys
from typing import Any, Callable, Literal

import anyio
from mcp.server.fastmcp import FastMCP

from .gridpane_client import GridPaneClient, GridPaneError

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("gridpane-mcp")

mcp = FastMCP(
    "gridpane",
    instructions="Wherever a tool takes `site`, pass the site's domain (example.com) or its numeric id.",
)

# Write access through run_wp_cli needs both this env var and allow_write=True,
# so a model cannot opt itself in.
WP_CLI_WRITE_ENV = "GRIDPANE_WP_CLI_ALLOW_WRITE"


@functools.lru_cache(maxsize=1)
def _client() -> GridPaneClient:
    # Built on first use so the server starts (and lists tools) without a token.
    return GridPaneClient.from_env()


async def _call(fn: Callable[[], Any]) -> Any:
    """Run a blocking client call off the event loop; surface API errors."""
    try:
        return await anyio.to_thread.run_sync(fn)
    except GridPaneError as e:
        detail = str(e)
        if e.body not in (None, "", {}):
            detail += "\n" + (e.body if isinstance(e.body, str) else json.dumps(e.body, indent=2))
        raise RuntimeError(detail) from e


async def _site_id(site: int | str) -> int:
    """Tools take a site's domain or its id."""
    return await _call(lambda: _client().resolve_site_id(site))


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)


def _rows(data: Any) -> list[dict]:
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]
    return data if isinstance(data, list) else []


def _unwrap(data: Any) -> Any:
    return data.get("data", data) if isinstance(data, dict) else data


# ── Servers ───────────────────────────────────────────────────


@mcp.tool()
async def list_servers() -> str:
    """List all GridPane servers with their IPs, status, and site counts."""
    servers = _rows(await _call(lambda: _client().list_servers()))
    lines = [f"Total servers: {len(servers)}\n"]
    for s in servers:
        lines.append(
            f'ID: {s.get("id")} | {str(s.get("label", "")):40s} | {str(s.get("ip", "")):16s} | '
            f'{str(s.get("status") or "unknown"):8s} | sites={len(s.get("sites") or [])}'
        )
    return "\n".join(lines)


@mcp.tool()
async def get_server(server_id: int) -> str:
    """Get detailed info for a specific server by its ID."""
    return _dump(_unwrap(await _call(lambda: _client().get_server(server_id))))


@mcp.tool()
async def search_server(name: str) -> str:
    """Search for a server by name (partial match). Returns matching servers with their sites."""
    servers = _rows(await _call(lambda: _client().list_servers()))
    matches = [s for s in servers if name.lower() in str(s.get("label", "")).lower()]
    if not matches:
        return f"No servers found matching '{name}'"
    out = []
    for s in matches:
        sites = ", ".join(str(site.get("url", "")) for site in s.get("sites") or [])
        out.append(
            f'ID: {s.get("id")} | {s.get("label")} | {s.get("ip", "")} | '
            f'{s.get("status") or "unknown"}\n  Sites: {sites}'
        )
    return "\n\n".join(out)


# ── Sites ─────────────────────────────────────────────────────


def _site_line(s: dict) -> str:
    return (
        f'ID: {s.get("id")} | {str(s.get("url", "")):50s} | type={s.get("type", "")} | '
        f'php={s.get("php_version", "")} | ssl={s.get("is_ssl", "")} | '
        f'server_id={s.get("server_id", "")}'
    )


@mcp.tool()
async def list_sites(per_page: int = 200) -> str:
    """List all WordPress sites across all servers."""
    sites = _rows(await _call(lambda: _client().list_sites(per_page)))
    return "\n".join([f"Total sites: {len(sites)}\n"] + [_site_line(s) for s in sites])


@mcp.tool()
async def get_site(site: int | str) -> str:
    """Get detailed info for a specific site by its ID."""
    return _dump(await _call(lambda: _client().find_site(site)))


@mcp.tool()
async def search_site(domain: str) -> str:
    """Search for a site by domain name (partial match)."""
    sites = _rows(await _call(lambda: _client().list_sites()))
    matches = [s for s in sites if domain.lower() in str(s.get("url", "")).lower()]
    if not matches:
        return f"No sites found matching '{domain}'"
    return "\n".join(_site_line(s) for s in matches)


# ── Site settings (PUT site/{id}) ─────────────────────────────


@mcp.tool()
async def clear_site_cache(site: int | str, scope: Literal["page", "object", "all"] = "all") -> str:
    """Clear a site's page cache, object cache, or both."""
    site_id = await _site_id(site)
    return _dump(await _call(lambda: _client().clear_cache(site_id, scope)))


@mcp.tool()
async def set_nginx_caching(
    site: int | str,
    mode: Literal["redis", "fastcgi", "none"],
    disable_first: bool = False,
) -> str:
    """Set Nginx page caching to redis, fastcgi or none.

    GridPane can't switch straight from redis to fastcgi (or back): the
    active type must be set to none first. disable_first=True sends none
    before the new mode.
    """
    site_id = await _site_id(site)
    return _dump(await _call(lambda: _client().set_nginx_caching(site_id, mode, disable_first)))


@mcp.tool()
async def set_wp_debug(site: int | str, enabled: bool) -> str:
    """Turn GridPane's secure WP debug on (true) or off (false) for a site."""
    site_id = await _site_id(site)
    return _dump(await _call(lambda: _client().set_debug(site_id, enabled)))


@mcp.tool()
async def set_php_version(site: int | str, php_version: str) -> str:
    """Change a site's PHP version, e.g. "8.2". GridPane's docs list 7.2-7.4 but
    are out of date; the API decides which versions it accepts."""
    site_id = await _site_id(site)
    return _dump(await _call(lambda: _client().set_php_version(site_id, php_version)))


# ── Backups ───────────────────────────────────────────────────


@mcp.tool()
async def create_backup(
    site: int | str,
    tag: str | None = None,
    type: Literal["local", "remote"] = "local",
    integration_id: int | None = None,
) -> str:
    """Start a manual backup. Optional tag: letters, digits, dashes, max 32,
    not "manual"/"auto". integration_id is required when type is remote.
    The backup runs in the background; use wait_for_backup to know it exists."""
    site_id = await _site_id(site)
    return _dump(await _call(lambda: _client().create_backup(site_id, type, tag, integration_id)))


@mcp.tool()
async def list_backups(site: int | str, tag: str | None = None, raw: bool = False) -> str:
    """Refresh and list a site's backups. With raw=True, return GridPane's
    response as-is instead of the parsed list."""
    site_id = await _site_id(site)
    if raw:
        return _dump(await _call(lambda: _client().list_backups(site_id)))
    refs = await _call(lambda: _client().find_backups(site_id, tag))
    return _dump([r.to_dict() for r in refs])


@mcp.tool()
async def wait_for_backup(
    site: int | str, tag: str, timeout_seconds: int = 900, interval_seconds: int = 30
) -> str:
    """Poll until a backup with this tag shows up in the site's backup list.
    Returns the backup, or an error after timeout_seconds."""
    site_id = await _site_id(site)
    ref = await _call(
        lambda: _client().wait_for_backup(site_id, tag, timeout_seconds, interval_seconds)
    )
    return _dump(ref.to_dict())


@mcp.tool()
async def restore_backup(
    site: int | str,
    backup: str,
    backup_source: Literal["local", "aws-s3", "backblaze", "dropbox", "wasabi"] = "local",
    source_type: Literal["original", "alternative"] = "original",
    backup_type: Literal["manual", "automatic"] = "manual",
) -> str:
    """Restore a backup, overwriting the live site. backup is the
    "YYYY-MM-DD HH:MM [tag]" string from list_backups."""
    site_id = await _site_id(site)
    return _dump(
        await _call(
            lambda: _client().restore_backup(site_id, backup, backup_source, source_type, backup_type)
        )
    )


@mcp.tool()
async def restore_backup_by_tag(site: int | str, tag: str, backup_source: str | None = None) -> str:
    """Restore the newest backup with this tag, overwriting the live site."""
    site_id = await _site_id(site)
    ref, result = await _call(lambda: _client().restore_by_tag(site_id, tag, backup_source))
    return _dump({"restored": ref.to_dict(), "response": result})


# ── WP-CLI ────────────────────────────────────────────────────


@mcp.tool()
async def run_wp_cli(site: int | str, command: str, allow_write: bool = False) -> str:
    """Run one WP-CLI command on a site, without the leading "wp", e.g.
    "plugin list --status=active". Only read-only commands (plus cache flush)
    are allowed unless allow_write=True AND the server was started with
    GRIDPANE_WP_CLI_ALLOW_WRITE=1. GridPane returns success/failure only,
    not the command's output."""
    if allow_write and os.getenv(WP_CLI_WRITE_ENV) != "1":
        raise RuntimeError(f"allow_write needs {WP_CLI_WRITE_ENV}=1 in the MCP server's environment.")
    site_id = await _site_id(site)
    return _dump(await _call(lambda: _client().wp(site_id, command, allow_write=allow_write)))


# ── GP-CLI over SSH (only registered when GRIDPANE_SSH_ENABLE=1) ──


def _register_ssh_tools() -> None:
    from . import gridpane_ssh as gpssh

    async def _ssh(fn: Callable[[], Any]) -> str:
        result = await _call(fn)
        return _dump(result.to_dict())

    @mcp.tool()
    async def gp_service(
        server: int | str,
        service: Literal["nginx", "php", "mysql", "redis"],
        action: Literal["restart", "reload", "test", "status"],
        php_version: str | None = None,
    ) -> str:
        """Run `gp <service> -<action>` on a server over SSH. server is its
        label, IP or id. php needs php_version (e.g. "8.2"); test and status
        are nginx only."""
        return await _ssh(
            lambda: gpssh.GpCli.from_env(gpssh.server_host(_client(), server)).service(
                service, action, php_version
            )
        )

    @mcp.tool()
    async def gp_clear_cache(site: int | str) -> str:
        """Run `gp fix cached <site>` on the site's server over SSH."""

        def run():
            host, url = gpssh.site_target(_client(), site)
            return gpssh.GpCli.from_env(host).clear_cache(url)

        return await _ssh(run)

    @mcp.tool()
    async def gp_wp(site: int | str, command: str, allow_write: bool = False) -> str:
        """Run WP-CLI through `gp wp` (as the site's system user) over SSH and
        return its output. command has no leading "wp", e.g. "plugin list".
        Same read-only allowlist as run_wp_cli; allow_write also needs
        GRIDPANE_WP_CLI_ALLOW_WRITE=1 in the server's environment."""
        if allow_write and os.getenv(WP_CLI_WRITE_ENV) != "1":
            raise RuntimeError(f"allow_write needs {WP_CLI_WRITE_ENV}=1 in the MCP server's environment.")

        def run():
            host, url = gpssh.site_target(_client(), site)
            return gpssh.GpCli.from_env(host).wp(url, command, allow_write=allow_write, check=False)

        return await _ssh(run)


if os.getenv("GRIDPANE_SSH_ENABLE") == "1":
    _register_ssh_tools()


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
