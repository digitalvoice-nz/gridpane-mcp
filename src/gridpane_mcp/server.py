import json
import os
import sys
import logging
import httpx
from mcp.server.fastmcp import FastMCP

API_URL = os.getenv("GRIDPANE_API_URL", "https://my.gridpane.com/oauth/api/v1")
API_TOKEN = os.getenv("GRIDPANE_API_TOKEN", "")

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("gridpane-mcp")

mcp = FastMCP("gridpane")


def _headers():
    return {
        "Authorization": f"Bearer {API_TOKEN}",
        "Accept": "application/json",
    }


async def _get(path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{API_URL}/{path}", headers=_headers(), params=params)
        r.raise_for_status()
        return r.json()


async def _post(path: str, data: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{API_URL}/{path}", headers=_headers(), json=data)
        r.raise_for_status()
        return r.json()


async def _delete(path: str) -> dict:
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.delete(f"{API_URL}/{path}", headers=_headers())
        r.raise_for_status()
        return r.json()


# ── Server Tools ──────────────────────────────────────────────


@mcp.tool()
async def list_servers() -> str:
    """List all GridPane servers with their IPs, status, and site counts."""
    data = await _get("server", {"per_page": 100})
    lines = []
    for s in data["data"]:
        status = s.get("status") or "unknown"
        sites = len(s.get("sites", []))
        lines.append(
            f'ID: {s["id"]} | {s["label"]:40s} | {s.get("ip",""):16s} | '
            f'{status:8s} | sites={sites}'
        )
    lines.insert(0, f"Total servers: {len(data['data'])}\n")
    return "\n".join(lines)


@mcp.tool()
async def get_server(server_id: int) -> str:
    """Get detailed info for a specific server by its ID."""
    data = await _get(f"server/{server_id}")
    return json.dumps(data.get("data", data), indent=2)


@mcp.tool()
async def search_server(name: str) -> str:
    """Search for a server by name (partial match). Returns matching servers with their sites."""
    data = await _get("server", {"per_page": 100})
    matches = [s for s in data["data"] if name.lower() in s["label"].lower()]
    if not matches:
        return f"No servers found matching '{name}'"
    lines = []
    for s in matches:
        sites = ", ".join(site["url"] for site in s.get("sites", []))
        lines.append(
            f'ID: {s["id"]} | {s["label"]} | {s.get("ip","")} | '
            f'{s.get("status") or "unknown"}\n  Sites: {sites}'
        )
    return "\n\n".join(lines)


# ── Site Tools ────────────────────────────────────────────────


@mcp.tool()
async def list_sites(per_page: int = 200) -> str:
    """List all WordPress sites across all servers."""
    data = await _get("site", {"per_page": per_page})
    lines = []
    for s in data["data"]:
        lines.append(
            f'ID: {s["id"]} | {s["url"]:50s} | type={s.get("type",""):10s} | '
            f'php={s.get("php_version","")} | ssl={s.get("is_ssl","")} | '
            f'server_id={s.get("server_id","")}'
        )
    lines.insert(0, f"Total sites: {len(data['data'])}\n")
    return "\n".join(lines)


@mcp.tool()
async def get_site(site_id: int) -> str:
    """Get detailed info for a specific site by its ID."""
    data = await _get(f"site/{site_id}")
    return json.dumps(data.get("data", data), indent=2)


@mcp.tool()
async def search_site(domain: str) -> str:
    """Search for a site by domain name (partial match)."""
    data = await _get("site", {"per_page": 500})
    matches = [s for s in data["data"] if domain.lower() in s["url"].lower()]
    if not matches:
        return f"No sites found matching '{domain}'"
    lines = []
    for s in matches:
        lines.append(
            f'ID: {s["id"]} | {s["url"]} | type={s.get("type","")} | '
            f'php={s.get("php_version","")} | ssl={s.get("is_ssl","")} | '
            f'server_id={s.get("server_id","")}'
        )
    return "\n".join(lines)


# ── Site Operations ───────────────────────────────────────────


@mcp.tool()
async def toggle_ssl(site_id: int) -> str:
    """Toggle SSL on/off for a site."""
    data = await _post(f"site/{site_id}/toggle-ssl")
    return json.dumps(data, indent=2)


@mcp.tool()
async def toggle_site_cache(site_id: int) -> str:
    """Toggle Nginx caching for a site."""
    data = await _post(f"site/{site_id}/toggle-nginx-caching")
    return json.dumps(data, indent=2)


@mcp.tool()
async def purge_site_cache(site_id: int) -> str:
    """Purge all server-level caches for a site."""
    data = await _post(f"site/{site_id}/purge-all-cache")
    return json.dumps(data, indent=2)


@mcp.tool()
async def toggle_debug(site_id: int) -> str:
    """Toggle WordPress debug mode for a site."""
    data = await _post(f"site/{site_id}/toggle-wp-debug")
    return json.dumps(data, indent=2)


@mcp.tool()
async def change_php_version(site_id: int, php_version: str) -> str:
    """Change PHP version for a site. Common versions: 7.4, 8.0, 8.1, 8.2, 8.3"""
    data = await _post(f"site/{site_id}/change-php", {"php_version": php_version})
    return json.dumps(data, indent=2)


# ── SSO / WP Admin ───────────────────────────────────────────


@mcp.tool()
async def get_sso_link(site_id: int) -> str:
    """Get a Single Sign-On link to access wp-admin for a site."""
    data = await _post(f"site/{site_id}/sso-link")
    return json.dumps(data, indent=2)


# ── Backup Tools ──────────────────────────────────────────────


@mcp.tool()
async def trigger_backup(site_id: int) -> str:
    """Trigger a manual backup for a site."""
    data = await _post(f"site/{site_id}/backup")
    return json.dumps(data, indent=2)


# ── Server Operations ────────────────────────────────────────


@mcp.tool()
async def restart_nginx(server_id: int) -> str:
    """Restart Nginx on a server."""
    data = await _post(f"server/{server_id}/restart-nginx")
    return json.dumps(data, indent=2)


@mcp.tool()
async def restart_php(server_id: int, php_version: str = "8.1") -> str:
    """Restart PHP-FPM on a server for a specific PHP version."""
    data = await _post(f"server/{server_id}/restart-php", {"php_version": php_version})
    return json.dumps(data, indent=2)


@mcp.tool()
async def restart_mysql(server_id: int) -> str:
    """Restart MySQL/MariaDB/Percona on a server."""
    data = await _post(f"server/{server_id}/restart-mysql")
    return json.dumps(data, indent=2)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
