"""`gridpane` command-line tool. Uses gridpane_client directly; no MCP.

Prints JSON to stdout so pipeline scripts can parse it. Exit codes:
0 ok, 1 API or input error, 2 bad arguments, 3 timed out waiting,
4 WP-CLI command blocked by the allowlist, 5 restore cancelled,
6 `gridpane ssh`: SSH off or failed, argument rejected, or the remote
command exited non-zero.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from . import gridpane_ssh as gpssh
from .gridpane_client import (
    BACKUP_SOURCES,
    CLEAR_CACHE_SCOPES,
    NGINX_CACHING_MODES,
    RESTORE_BACKUP_TYPES,
    RESTORE_SOURCE_TYPES,
    BackupTimeout,
    GridPaneClient,
    GridPaneError,
    WpCliNotAllowed,
)


def _out(data: Any) -> None:
    print(json.dumps(data, indent=2, default=str))


def _err(msg: str) -> None:
    print(f"gridpane: {msg}", file=sys.stderr)


def _rows(data: Any) -> list:
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return data["data"]
    return data if isinstance(data, list) else []


def cmd_servers(gp: GridPaneClient, a) -> int:
    data = gp.list_servers()
    if a.full:
        _out(data)
        return 0
    _out([
        {"id": s.get("id"), "label": s.get("label"), "ip": s.get("ip"),
         "status": s.get("status"), "sites": len(s.get("sites") or [])}
        for s in _rows(data)
    ])
    return 0


def _site_summary(s: dict) -> dict:
    server = s.get("server") if isinstance(s.get("server"), dict) else {}
    return {
        "id": s.get("id"),
        "url": s.get("url"),
        "server": server.get("label"),
        "server_id": s.get("server_id"),
        "server_ip": server.get("ip"),
        "php_version": s.get("php_version"),
        "nginx_caching": s.get("nginx_caching"),
        "is_ssl": s.get("is_ssl"),
    }


def cmd_sites(gp: GridPaneClient, a) -> int:
    sites = gp.all_sites()
    if a.search:
        sites = [s for s in sites if a.search.lower() in str(s.get("url", "")).lower()]
    sites = sorted(sites, key=lambda s: str(s.get("url", "")))
    _out(sites if a.full else [_site_summary(s) for s in sites])
    return 0


def cmd_site(gp: GridPaneClient, a) -> int:
    # By the name given: a staging/canary domain resolves from the server
    # list, and fetching it by id would be refused.
    _out(gp.find_site(a.site_ref))
    return 0


def cmd_clear_cache(gp: GridPaneClient, a) -> int:
    _out(gp.clear_cache(a.site_id, a.scope))
    return 0


def cmd_nginx_cache(gp: GridPaneClient, a) -> int:
    _out(gp.set_nginx_caching(a.site_id, a.mode, disable_first=a.disable_first))
    return 0


def cmd_debug(gp: GridPaneClient, a) -> int:
    _out(gp.set_debug(a.site_id, a.state == "on"))
    return 0


def cmd_php(gp: GridPaneClient, a) -> int:
    _out(gp.set_php_version(a.site_id, a.version))
    return 0


def cmd_backup(gp: GridPaneClient, a) -> int:
    if a.wait and not a.tag:
        _err("--wait needs --tag: the tag is how the new backup is recognised.")
        return 2
    before = gp.find_backups(a.site_id, a.tag) if a.wait else []
    result = gp.create_backup(a.site_id, a.type, a.tag, a.integration_id)
    if not a.wait:
        _out(result)
        return 0
    _err(f"backup requested; waiting up to {a.timeout}s for tag {a.tag!r}...")
    ref = gp.wait_for_backup(a.site_id, a.tag, a.timeout, a.interval, exclude=before)
    _out({"requested": result, "backup": ref.to_dict()})
    return 0


def cmd_backups(gp: GridPaneClient, a) -> int:
    if a.raw:
        _out(gp.list_backups(a.site_id))
    else:
        _out([r.to_dict() for r in gp.find_backups(a.site_id, a.tag)])
    return 0


def cmd_wait_backup(gp: GridPaneClient, a) -> int:
    _out(gp.wait_for_backup(a.site_id, a.tag, a.timeout, a.interval).to_dict())
    return 0


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return True  # scripts and pipelines: no prompt
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_restore(gp: GridPaneClient, a) -> int:
    if a.tag:
        ref = gp.resolve_tag(a.site_id, a.tag, a.source)
        backup = ref.backup
        source = a.source or ref.backup_source or "local"
        btype = a.backup_type or ref.backup_type or "manual"
    else:
        backup, source, btype = a.backup, a.source or "local", a.backup_type or "manual"
    summary = f"site {a.site_id}: restore '{backup}' ({source}, {a.source_type}, {btype})"
    _err(summary)
    if not a.yes and not _confirm("This overwrites the live site. Continue?"):
        _err("cancelled.")
        return 5
    _out({
        "restored": {"backup": backup, "backup_source": source, "backup_type": btype},
        "response": gp.restore_backup(a.site_id, backup, source, a.source_type, btype),
    })
    return 0


def cmd_wp(gp: GridPaneClient, a) -> int:
    words = list(a.command)
    allow = a.allow_write
    # argparse's REMAINDER swallows a flag placed right after site_id.
    if words and words[0] == "--allow-write":
        allow, words = True, words[1:]
    _out(gp.wp(a.site_id, " ".join(words), allow_write=allow))
    return 0


# ── gridpane ssh ... (GP-CLI backend) ────────────────────────


class _LazyApi:
    """REST client built only if a command needs to look up a host or site."""

    def __init__(self):
        self._gp: GridPaneClient | None = None

    def __call__(self) -> GridPaneClient:
        if self._gp is None:
            self._gp = GridPaneClient.from_env()
        return self._gp

    def close(self):
        if self._gp is not None:
            self._gp.close()


def _ssh_target(a, api: _LazyApi, need_site: bool) -> tuple[str, str | None]:
    site_url = getattr(a, "site_url", None)
    if a.host:
        host = a.host
    elif getattr(a, "site", None):
        host, url = gpssh.site_target(api(), a.site)
        site_url = site_url or url
    elif getattr(a, "server", None):
        host = gpssh.server_host(api(), a.server)
    else:
        raise gpssh.GpCliError("Give --site, --server or --host.")
    if need_site and not site_url:
        raise gpssh.GpCliError("This needs a site: give --site, or --site-url with --host/--server.")
    return host, site_url


def _ssh_out(a, result: gpssh.GpResult) -> int:
    if a.raw:
        sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
    else:
        _out(result.to_dict())
    return 0 if result.exit_code == 0 else 6


def cmd_ssh_service(a, api: _LazyApi) -> int:
    host, _ = _ssh_target(a, api, need_site=False)
    return _ssh_out(a, gpssh.GpCli.from_env(host).service(a.service, a.action, a.php_version))


def cmd_ssh_clear_cache(a, api: _LazyApi) -> int:
    host, site_url = _ssh_target(a, api, need_site=not a.all_sites)
    gp = gpssh.GpCli.from_env(host)
    result = gp.clear_cache(None, all_sites=True) if a.all_sites else gp.clear_cache(site_url)
    return _ssh_out(a, result)


def cmd_ssh_wp(a, api: _LazyApi) -> int:
    words = list(a.command)
    allow = a.allow_write
    if words and words[0] == "--allow-write":
        allow, words = True, words[1:]
    if not words:
        raise gpssh.GpCliError("No WP-CLI command given.")
    host, site_url = _ssh_target(a, api, need_site=True)
    # check=False: a failing WP-CLI command still prints its output.
    return _ssh_out(a, gpssh.GpCli.from_env(host).wp(site_url, " ".join(words), allow_write=allow, check=False))


def _add_ssh_parser(sub) -> None:
    ssh = sub.add_parser(
        "ssh",
        help="Run GP-CLI on a server over SSH (needs GRIDPANE_SSH_ENABLE=1 and a root SSH key).",
        description="Run allowlisted GP-CLI commands on a GridPane server over SSH. "
        "Off unless GRIDPANE_SSH_ENABLE=1. Uses your OpenSSH client, keys and known_hosts. "
        "The server is found from --site or --server through the API, or given with --host.",
    )
    ssub = ssh.add_subparsers(dest="ssh_cmd", required=True, metavar="ssh-command")

    def target(sp, site: bool):
        g = sp.add_argument_group("target")
        if site:
            g.add_argument("--site", "--site-id", dest="site", metavar="SITE",
                           help="Site domain or id; its server is looked up through the API.")
        g.add_argument("--server", "--server-id", dest="server", metavar="SERVER",
                       help="Server label, IP or id, looked up through the API.")
        g.add_argument("--host", help="Server IP or host name; skips the API lookup.")
        if site:
            g.add_argument("--site-url", help="Site domain to act on with --host or --server.")
        sp.add_argument("--raw", action="store_true", help="Print the remote output instead of JSON.")

    sp = ssub.add_parser("service", help="Restart, reload, test or check a service.",
                         description="gp ngx|php|mysql|redis -restart|-reload (nginx also -t, -status).")
    sp.add_argument("service", choices=gpssh.SERVICES)
    sp.add_argument("action", choices=("restart", "reload", "test", "status"))
    sp.add_argument("--php-version", help="Required for php, e.g. 8.2.")
    target(sp, site=True)
    sp.set_defaults(fn=cmd_ssh_service)

    sp = ssub.add_parser("clear-cache", help="gp fix cached for one site, or --all-sites on the server.")
    sp.add_argument("--all-sites", action="store_true", help="Clear every site on the server.")
    target(sp, site=True)
    sp.set_defaults(fn=cmd_ssh_clear_cache)

    sp = ssub.add_parser(
        "wp",
        help="gp wp: WP-CLI as the site's system user, with its output.",
        description="Example: gridpane ssh wp --site example.com plugin list --status=active. "
        "Put gridpane's own options before the WP-CLI command.",
    )
    sp.add_argument("--allow-write", action="store_true",
                    help="Allow commands outside the read-only allowlist.")
    target(sp, site=True)
    sp.add_argument("command", nargs=argparse.REMAINDER, help="WP-CLI command without the leading 'wp'.")
    sp.set_defaults(fn=cmd_ssh_wp)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gridpane",
        description="GridPane API from the command line (no MCP). "
        "Reads GRIDPANE_API_TOKEN and optional GRIDPANE_API_URL from the environment.",
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="command")

    def site_cmd(name: str, fn, help: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help, description=help)
        sp.add_argument("site_id", metavar="site", help="Site domain (example.com) or id.")
        sp.set_defaults(fn=fn)
        return sp

    sp = sub.add_parser("servers", help="List servers: id, label, IP, site count.")
    sp.add_argument("--full", action="store_true", help="Print GridPane's whole response.")
    sp.set_defaults(fn=cmd_servers)

    sp = sub.add_parser("sites", help="List sites: id, domain, server, PHP, caching.")
    sp.add_argument("--search", help="Only sites whose domain contains this text.")
    sp.add_argument("--full", action="store_true", help="Every field GridPane returns, not the summary.")
    sp.set_defaults(fn=cmd_sites)

    site_cmd("site", cmd_site, "Show one site.")

    sp = site_cmd("clear-cache", cmd_clear_cache, "Clear a site's caches.")
    sp.add_argument("--scope", choices=CLEAR_CACHE_SCOPES, default="all")

    sp = site_cmd("nginx-cache", cmd_nginx_cache, "Set Nginx page caching.")
    sp.add_argument("mode", choices=NGINX_CACHING_MODES)
    sp.add_argument("--disable-first", action="store_true",
                    help="Send 'none' first; needed to switch between redis and fastcgi.")

    sp = site_cmd("debug", cmd_debug, "Turn secure WP debug on or off.")
    sp.add_argument("state", choices=("on", "off"))

    sp = site_cmd("php", cmd_php, "Change a site's PHP version.")
    sp.add_argument("version", help="e.g. 8.2")

    sp = site_cmd("backup", cmd_backup, "Create a manual backup, optionally waiting for it.")
    sp.add_argument("--tag", help="Letters, digits and dashes, max 32. Not 'manual' or 'auto'.")
    sp.add_argument("--type", choices=("local", "remote"), default="local")
    sp.add_argument("--integration-id", type=int, help="Required with --type remote.")
    sp.add_argument("--wait", action="store_true", help="Poll until the tagged backup exists.")
    sp.add_argument("--timeout", type=int, default=1800, help="Seconds to wait (default 1800).")
    sp.add_argument("--interval", type=int, default=30, help="Seconds between polls (default 30).")

    sp = site_cmd("backups", cmd_backups, "Refresh and list a site's backups.")
    sp.add_argument("--tag", help="Only backups with this tag.")
    sp.add_argument("--raw", action="store_true", help="Print GridPane's response unparsed.")

    sp = site_cmd("wait-backup", cmd_wait_backup, "Wait until a backup with this tag exists.")
    sp.add_argument("--tag", required=True)
    sp.add_argument("--timeout", type=int, default=1800)
    sp.add_argument("--interval", type=int, default=30)

    sp = site_cmd("restore", cmd_restore, "Restore a backup over the live site.")
    which = sp.add_mutually_exclusive_group(required=True)
    which.add_argument("--tag", help="Restore the newest backup with this tag.")
    which.add_argument("--backup", help="Exact backup, 'YYYY-MM-DD HH:MM [tag]'.")
    sp.add_argument("--source", choices=BACKUP_SOURCES, help="Default: from the backup list, else local.")
    sp.add_argument("--source-type", choices=RESTORE_SOURCE_TYPES, default="original")
    sp.add_argument("--backup-type", choices=RESTORE_BACKUP_TYPES,
                    help="Default: from the backup list, else manual.")
    sp.add_argument("--yes", "-y", action="store_true",
                    help="Don't ask for confirmation (never asked when stdin is not a terminal).")

    sp = site_cmd("wp", cmd_wp, "Run one WP-CLI command, e.g.: gridpane wp 123 plugin list")
    sp.add_argument("command", nargs=argparse.REMAINDER, help="WP-CLI command without the leading 'wp'.")
    sp.add_argument("--allow-write", action="store_true",
                    help="Allow commands outside the read-only allowlist. "
                    "Put it before the WP-CLI command.")

    _add_ssh_parser(sub)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Rate-limit waits and other client warnings go to stderr.
    logging.basicConfig(level=logging.WARNING, format="gridpane: %(message)s", stream=sys.stderr)
    try:
        if args.cmd == "ssh":
            api = _LazyApi()
            try:
                return args.fn(args, api)
            finally:
                api.close()
        with GridPaneClient.from_env() as gp:
            # Every site command takes a domain or an id; turn it into the id.
            if getattr(args, "site_id", None) is not None:
                args.site_ref = args.site_id
                args.site_id = gp.resolve_site_id(args.site_id)
            return args.fn(gp, args)
    except gpssh.GpCliError as e:
        _err(str(e))
        if e.body:
            print(e.body, file=sys.stderr)
        return 6
    except BackupTimeout as e:
        _err(str(e))
        return 3
    except WpCliNotAllowed as e:
        _err(str(e))
        return 4
    except GridPaneError as e:
        _err(str(e))
        if e.body not in (None, "", {}):
            print(json.dumps(e.body, indent=2, default=str) if not isinstance(e.body, str) else e.body,
                  file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
