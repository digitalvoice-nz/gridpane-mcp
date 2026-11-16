"""Optional GP-CLI backend: runs a fixed set of `gp` commands over SSH.

GridPane's REST API has no service restarts, and its run-wp-cli endpoint
doesn't return command output. GP-CLI on the server does both. It is only
reachable over SSH, and GridPane servers allow SSH as root only, so this
backend needs a root SSH key for each server. It is off unless
GRIDPANE_SSH_ENABLE=1.

Only these commands can be sent, each built from validated arguments
(no free-form shell):

    gp ngx -t | -status | -reload | -restart
    gp php <X.Y> -reload | -restart
    gp mysql -reload | -restart
    gp redis -reload | -restart
    gp fix cached [<site.url>]
    gp wp <site.url> <wp-cli command>   (same read-only allowlist as the API path)

`-stop` and `-start` are left out: a pipeline shouldn't be able to take a
service down and leave it down. V1 (legacy) backup commands are not wrapped;
backups and restores go through the REST API.

Uses the system OpenSSH client (built into Windows 10+), so your existing
keys, ssh-agent and known_hosts apply. Host keys are always checked
(StrictHostKeyChecking=yes) and password prompts are disabled (BatchMode).
Syntax is from https://gridpane.com/kb/gp-cli-quick-reference/ .
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import asdict, dataclass
from typing import Sequence

from .gridpane_client import GridPaneClient, GridPaneError, check_wp_cli, wp_cli_from_line

SSH_ENABLE_ENV = "GRIDPANE_SSH_ENABLE"

SERVICES = ("nginx", "php", "mysql", "redis")
# Service actions that may be sent. `test`/`status` are nginx only.
SERVICE_ACTIONS = {
    "nginx": ("test", "status", "reload", "restart"),
    "php": ("reload", "restart"),
    "mysql": ("reload", "restart"),
    "redis": ("reload", "restart"),
}
_GP_SERVICE = {"nginx": "ngx", "php": "php", "mysql": "mysql", "redis": "redis"}
_GP_FLAG = {"test": "-t", "status": "-status", "reload": "-reload", "restart": "-restart"}

_PHP_RE = re.compile(r"^\d\.\d$")
_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
_SITE_RE = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z0-9-]{2,63}$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


class GpCliError(GridPaneError):
    """SSH is disabled, an argument was rejected, or the remote command failed."""


@dataclass(frozen=True)
class GpResult:
    host: str
    command: str
    exit_code: int
    stdout: str
    stderr: str

    def to_dict(self) -> dict:
        return asdict(self)


def ssh_enabled() -> bool:
    return os.getenv(SSH_ENABLE_ENV) == "1"


def _check_site_url(site_url: str) -> str:
    site_url = site_url.strip().lower()
    if not _SITE_RE.match(site_url):
        raise GpCliError(f"Not a site domain: {site_url!r}.")
    return site_url


def build_service_command(service: str, action: str, php_version: str | None = None) -> list[str]:
    if service not in SERVICES:
        raise GpCliError(f"service must be one of {', '.join(SERVICES)}; got {service!r}.")
    if action not in SERVICE_ACTIONS[service]:
        raise GpCliError(
            f"{service} action must be one of {', '.join(SERVICE_ACTIONS[service])}; got {action!r}."
        )
    argv = ["gp", _GP_SERVICE[service]]
    if service == "php":
        if not php_version or not _PHP_RE.match(php_version):
            raise GpCliError(f"php needs a version like 8.2; got {php_version!r}.")
        argv.append(php_version)
    elif php_version:
        raise GpCliError("php_version only applies to the php service.")
    argv.append(_GP_FLAG[action])
    return argv


def build_clear_cache_command(site_url: str | None, all_sites: bool = False) -> list[str]:
    if all_sites:
        if site_url:
            raise GpCliError("Give a site URL or all_sites, not both.")
        return ["gp", "fix", "cached"]
    if not site_url:
        raise GpCliError("A site URL is required (or all_sites=True for every site on the server).")
    return ["gp", "fix", "cached", _check_site_url(site_url)]


def build_wp_command(site_url: str, line: str, allow_write: bool = False) -> list[str]:
    commands = wp_cli_from_line(line)
    if not allow_write:
        check_wp_cli(commands)
    (command, (args,)), = commands.items()
    try:
        words = shlex.split(args)
    except ValueError as e:
        raise GpCliError(f"Cannot parse WP-CLI arguments: {e}") from e
    if any(w == "--ssh" or w.startswith("--ssh=") for w in words):
        raise GpCliError("gp wp does not support --ssh.")
    return ["gp", "wp", _check_site_url(site_url), command, *words]


class GpCli:
    """Runs allowlisted `gp` commands on one GridPane server over SSH."""

    def __init__(
        self,
        host: str,
        user: str = "root",
        port: int = 22,
        key_file: str | None = None,
        known_hosts: str | None = None,
        connect_timeout: int = 15,
        command_timeout: int = 600,
        ssh_command: Sequence[str] = ("ssh",),
        require_enabled: bool = True,
    ):
        if require_enabled and not ssh_enabled():
            raise GpCliError(f"The SSH backend is off. Set {SSH_ENABLE_ENV}=1 to use it.")
        if not _HOST_RE.match(host or ""):
            raise GpCliError(f"Not a host name or IP: {host!r}.")
        if not _USER_RE.match(user):
            raise GpCliError(f"Not a valid SSH user: {user!r}.")
        self.host, self.user, self.port = host, user, int(port)
        self.key_file, self.known_hosts = key_file, known_hosts
        self.connect_timeout, self.command_timeout = connect_timeout, command_timeout
        self.ssh_command = list(ssh_command)

    @classmethod
    def from_env(cls, host: str, **kwargs) -> "GpCli":
        return cls(
            host,
            user=os.getenv("GRIDPANE_SSH_USER", "root"),
            port=int(os.getenv("GRIDPANE_SSH_PORT", "22")),
            key_file=os.getenv("GRIDPANE_SSH_KEY") or None,
            known_hosts=os.getenv("GRIDPANE_SSH_KNOWN_HOSTS") or None,
            **kwargs,
        )

    def ssh_argv(self, remote_argv: list[str]) -> list[str]:
        argv = [
            *self.ssh_command,
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"ConnectTimeout={int(self.connect_timeout)}",
            "-p", str(self.port),
            "-l", self.user,
        ]
        if self.key_file:
            argv += ["-i", self.key_file, "-o", "IdentitiesOnly=yes"]
        if self.known_hosts:
            argv += ["-o", f"UserKnownHostsFile={self.known_hosts}"]
        # The remote side runs this through the login shell, so every word is
        # quoted for a POSIX shell and the whole line goes as one argument.
        argv += ["--", self.host, shlex.join(remote_argv)]
        return argv

    def run(self, remote_argv: list[str], check: bool = True) -> GpResult:
        if not remote_argv or remote_argv[0] != "gp":
            raise GpCliError("Only gp commands can be run.")
        command = shlex.join(remote_argv)
        try:
            p = subprocess.run(
                self.ssh_argv(remote_argv),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.command_timeout,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as e:
            raise GpCliError(f"SSH client not found ({self.ssh_command[0]}). Install OpenSSH.") from e
        except subprocess.TimeoutExpired as e:
            raise GpCliError(f"`{command}` on {self.host} timed out after {self.command_timeout}s.") from e
        result = GpResult(self.host, command, p.returncode, p.stdout, p.stderr)
        if check and p.returncode != 0:
            # 255 is ssh's own failure code (connection, auth or host key).
            where = "SSH connection" if p.returncode == 255 else f"`{command}`"
            raise GpCliError(
                f"{where} on {self.host} failed with exit code {p.returncode}.",
                p.returncode,
                (p.stderr or p.stdout).strip(),
            )
        return result

    def service(self, service: str, action: str, php_version: str | None = None) -> GpResult:
        return self.run(build_service_command(service, action, php_version))

    def clear_cache(self, site_url: str | None = None, all_sites: bool = False) -> GpResult:
        return self.run(build_clear_cache_command(site_url, all_sites))

    def wp(self, site_url: str, line: str, allow_write: bool = False, check: bool = True) -> GpResult:
        return self.run(build_wp_command(site_url, line, allow_write), check=check)


# ── Finding the server for a site through the REST API ───────
# Site objects carry `url`, `server_id` and a nested `server` with `ip`
# (checked against a live GET site response).


def server_host(api: GridPaneClient, server: int | str) -> str:
    """IP of a server given by id, IP or label."""
    s = api.find_server(server)
    ip = s.get("ip") if isinstance(s, dict) else None
    if not ip:
        raise GpCliError(f"Server {server!r} has no `ip` in the API response.")
    return str(ip)


def site_target(api: GridPaneClient, site: int | str) -> tuple[str, str]:
    """(server IP, site domain) for a site given by id or domain."""
    s = api.find_site(site)
    if not isinstance(s, dict) or not s.get("url"):
        raise GpCliError(f"Site {site!r}: API response has no `url`.")
    nested = s.get("server") if isinstance(s.get("server"), dict) else {}
    if nested.get("ip"):
        return str(nested["ip"]), str(s["url"])
    if not s.get("server_id"):
        raise GpCliError(f"Site {site!r}: API response has no `server_id`.")
    return server_host(api, int(s["server_id"])), str(s["url"])
