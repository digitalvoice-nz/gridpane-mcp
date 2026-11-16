# gridpane-mcp

An MCP server **and** a plain command-line tool for the [GridPane](https://gridpane.com/) hosting API.

Fork of [fenil0020/gridpane-mcp](https://github.com/fenil0020/gridpane-mcp), reworked so that every call uses an endpoint from GridPane's published API docs:
<https://documenter.getpostman.com/view/13664964/TVssjU7Z> (base URL `https://my.gridpane.com/oauth/api/v1`).

The HTTP calls are in an importable module, `gridpane_mcp.gridpane_client`. The MCP tools and the `gridpane` CLI both wrap it, so scripts can take a backup, wait for it, restore it and clear cache with no MCP or LLM involved.

## What changed from upstream

Upstream's 10 POST tools called endpoints that aren't in GridPane's docs (`site/{id}/toggle-ssl`, `toggle-nginx-caching`, `purge-all-cache`, `toggle-wp-debug`, `change-php`, `sso-link`, `site/{id}/backup`, `server/{id}/restart-nginx|php|mysql`). They have been rebuilt or removed:

| Upstream tool | Now | Documented endpoint |
|---|---|---|
| `purge_site_cache` | `clear_site_cache` (page / object / all) | `PUT site/{id}` `{"clear_cache": ...}` |
| `toggle_site_cache` | `set_nginx_caching` (redis / fastcgi / none) | `PUT site/{id}` `{"nginx_caching": ...}` |
| `toggle_debug` | `set_wp_debug` (explicit true/false) | `PUT site/{id}` `{"debug": ...}` |
| `change_php_version` | `set_php_version` | `PUT site/{id}` `{"php_version": ...}` |
| `trigger_backup` | `create_backup` (with `tag`) | `POST backups/{id}` |
| — | `list_backups` | `GET backups/refresh/{id}` then `GET backups/original/{id}` |
| — | `wait_for_backup` | polls the two above |
| — | `restore_backup`, `restore_backup_by_tag` | `POST backups/restore/{id}` |
| — | `run_wp_cli` (read-only allowlist) | `PUT site/run-wp-cli/{id}` |
| `toggle_ssl` | **removed** | none documented. SSL is set per domain: `PUT domain/{domain.id}` (not wrapped yet) |
| `get_sso_link` | **removed** | none documented |
| `restart_nginx`, `restart_php`, `restart_mysql` | `gp_service`, over SSH (see [GP-CLI over SSH](#gp-cli-over-ssh-optional)) | none in the API; GP-CLI `gp ngx\|php\|mysql -restart` |

The removed API calls were not kept behind a flag. Nothing public shows those endpoints exist, and a guessed POST that happens to do something unexpected is worse than no tool. Service restarts come back through the optional SSH backend, which uses GridPane's documented GP-CLI instead.

Unchanged: `list_servers`, `get_server`, `search_server`, `list_sites`, `get_site`, `search_site` (these already matched the docs).

## Install

You need a GridPane API token (GridPane: Settings > GridPane API). Keep it in an environment variable; never paste it into a chat.

All commands below are for **Windows PowerShell 5.1**.

Install [uv](https://docs.astral.sh/uv/) if you don't have it:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Store the token for your user account (prompts for it, so it doesn't land in your shell history), then open a new terminal:

```powershell
$t = Read-Host "GridPane API token" -AsSecureString; [Environment]::SetEnvironmentVariable('GRIDPANE_API_TOKEN', [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($t)), 'User')
```

Run the MCP server straight from this fork:

```powershell
uvx --from git+https://github.com/digitalvoice-nz/gridpane-mcp gridpane-mcp
```

Install the `gridpane` CLI (and `gridpane-mcp`) as tools on your PATH:

```powershell
uv tool install git+https://github.com/digitalvoice-nz/gridpane-mcp
```

If uv fails with `invalid peer certificate: UnknownIssuer` (common behind antivirus or corporate proxies that inspect HTTPS), add `--system-certs` after `install` so uv uses the Windows certificate store.

Run these from your own PowerShell window, not from a terminal inside the Claude desktop app: Windows redirects that app's writes to `%APPDATA%` into a private folder, so a tool installed from there won't run anywhere else.

The client itself trusts the Windows certificate store too (through `truststore`), so HTTPS scanning by antivirus such as Norton's Web/Mail Shield doesn't break API calls. Set `GRIDPANE_CA_BUNDLE` to a PEM file to use that instead.

**Rate limits.** GridPane allows about 12 requests per window per endpoint group and 60 overall. On HTTP 429 the client waits as long as GridPane's `retry-after-endpoint` header says, then retries, up to 5 times, printing a note to stderr.

Or run the CLI once without installing:

```powershell
uvx --from git+https://github.com/digitalvoice-nz/gridpane-mcp gridpane --help
```

### Claude Code

```powershell
claude mcp add gridpane -- uvx --from git+https://github.com/digitalvoice-nz/gridpane-mcp gridpane-mcp
```

The server reads `GRIDPANE_API_TOKEN` from your environment, so the token isn't written into the MCP config.

### Claude Desktop / Cursor

```json
{
  "mcpServers": {
    "gridpane": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/digitalvoice-nz/gridpane-mcp", "gridpane-mcp"]
    }
  }
}
```

## CLI

Output is JSON on stdout; progress and errors go to stderr.

Every command that takes a site accepts its **domain** (`example.com`; `www.` and `https://` are ignored) or its numeric id. `--server` takes a server's label, IP or id. MCP tools take `site` the same way. `gridpane sites` lists id, domain, server, PHP version and caching for every site (`--search` filters, `--full` gives every field), and `gridpane servers` lists servers.

GridPane's `staging.<domain>` and `canary.<domain>` copies are found too, but the API refuses calls for them ("That site does not belong to the bearer of this access token"). Only the `gridpane ssh` commands can act on them.

```powershell
gridpane sites --search example.com
gridpane clear-cache example.com --scope all
gridpane backup example.com --tag task-42 --wait --timeout 1800
gridpane backups example.com --tag task-42
gridpane restore example.com --tag task-42
gridpane debug example.com off
gridpane php example.com 8.2
gridpane nginx-cache example.com fastcgi --disable-first
gridpane wp example.com plugin list --status=active
```

| Exit code | Meaning |
|---|---|
| 0 | OK |
| 1 | API error or invalid input |
| 2 | Bad arguments |
| 3 | `--wait` / `wait-backup` timed out |
| 4 | WP-CLI command blocked by the allowlist |
| 5 | Restore cancelled at the prompt |
| 6 | `gridpane ssh`: SSH backend off, SSH failed, argument rejected, or the remote command exited non-zero |

`restore` asks for confirmation when run in a terminal. With stdin redirected (a pipeline) it doesn't ask. `--yes` skips the prompt.

A pipeline step in PowerShell 5.1:

```powershell
gridpane backup $siteId --tag $taskId --wait; if ($LASTEXITCODE -ne 0) { throw "backup failed or timed out" }
```

```powershell
if ($regressionFailed) { gridpane restore $siteId --tag $taskId --yes }
```

## Python

```python
from gridpane_mcp.gridpane_client import GridPaneClient

with GridPaneClient.from_env() as gp:
    site_id = gp.resolve_site_id("example.com")  # or the numeric id
    before = gp.find_backups(site_id, tag=task_id)
    gp.create_backup(site_id, tag=task_id)
    gp.wait_for_backup(site_id, task_id, timeout=1800, exclude=before)
    # ... agent edits the site, regression checks run ...
    if failed:
        gp.restore_by_tag(site_id, task_id)
    gp.clear_cache(site_id, "all")
```

## How the backup helpers work

- **Tags** must be letters, digits and dashes, up to 32 characters. `manual` and `auto` are reserved. A task id like `task-42` fits.
- **Waiting.** GridPane has no job-status endpoint. `wait_for_backup` calls `backups/refresh` and then `backups/original` every `interval` seconds until a backup with the tag appears. Pass the list taken *before* `create_backup` as `exclude` (the CLI's `backup --wait` does this) so an older backup with the same tag doesn't count. `wait-backup` has no "before" list, so it returns at once if any backup already has that tag. While waiting, the CLI prints one line per check to stderr, with the elapsed time and how many backups the list held; stdout stays JSON.
- **Restoring by tag** picks the newest backup with that tag. The restore call needs `backup_source` and `backup_type`; they come from the backup list where it says, otherwise `local` and `manual`, which is what `create_backup(type="local", tag=...)` makes.
- **Parsing the list.** `backups/original` returns one comma-separated string per storage and type, e.g. `{"backups": {"local": {"automatic": "2026-11-05 02:00,2026-11-12 02:00"}, "wasabi": {...}}}`. Each entry is the "YYYY-MM-DD HH:MM [tag]" string the restore endpoint takes. The parser splits these and reads the storage and type from the keys. When the newest tagged backup exists both locally and remotely, restore-by-tag uses the local copy.

## WP-CLI

`PUT site/run-wp-cli/{id}` runs **any** WP-CLI command, so by default only a read-only allowlist is accepted: `core version`, `option get`, `plugin list`, `theme list`, `post list`, `cache flush` and similar (see `WP_CLI_READ_ONLY` in `gridpane_client.py`). Shell control characters (`; & | $` and backticks) and the global flags `--exec`, `--require`, `--ssh`, `--http`, `--path` are also refused.

Opting in to anything else:

- **CLI:** `gridpane wp example.com --allow-write plugin update akismet` (put `--allow-write` before the command).
- **Python:** `gp.wp(site_id, "plugin update akismet", allow_write=True)`.
- **MCP:** the tool's `allow_write=true` **and** `GRIDPANE_WP_CLI_ALLOW_WRITE=1` in the server's environment. Both are required, so a model can't opt itself in.

Per GridPane's docs, the response only says whether the commands ran. It does not return their output, so `plugin list` and the other read commands are mostly useful as a check that WP-CLI runs on the site.

## GP-CLI over SSH (optional)

GridPane's REST API can't restart services, and its WP-CLI endpoint doesn't return output. [GP-CLI](https://gridpane.com/kb/gp-cli-quick-reference/), GridPane's command-line tool on each server, does both. GP-CLI is only reachable over SSH, and GridPane servers accept SSH as **root only**. So this backend gives whatever runs it root on every server it has a key for. It is **off** unless `GRIDPANE_SSH_ENABLE=1`.

Only these commands can be sent. Each is built from validated arguments and quoted for the remote shell; nothing free-form reaches it:

| Command | CLI | MCP tool |
|---|---|---|
| `gp ngx -restart` / `-reload` / `-t` / `-status` | `gridpane ssh service nginx restart --server sh-akl-02` | `gp_service` |
| `gp php 8.2 -restart` / `-reload` | `gridpane ssh service php restart --php-version 8.2 --server sh-akl-02` | `gp_service` |
| `gp mysql -restart` / `-reload` | `gridpane ssh service mysql restart --server sh-akl-02` | `gp_service` |
| `gp redis -restart` / `-reload` | `gridpane ssh service redis restart --server sh-akl-02` | `gp_service` |
| `gp fix cached <site>` | `gridpane ssh clear-cache --site example.com` | `gp_clear_cache` |
| `gp fix cached` (every site on the server) | `gridpane ssh clear-cache --server sh-akl-02 --all-sites` | — |
| `gp wp <site> <command>` | `gridpane ssh wp --site example.com plugin list --status=active` | `gp_wp` |

- **Left out on purpose:** `-stop` and `-start`, so a pipeline can't take a service down and leave it down. Backups and restores stay on the REST API, which is the only way to tag a V2 backup and restore it by tag. V1 (legacy) backup commands aren't wrapped.
- **`gp wp`** runs WP-CLI as the site's system user (plain `wp` as root would leave root-owned files) and returns its output. It uses the same read-only allowlist as `run_wp_cli`, with the same opt-ins: `--allow-write` on the CLI; `allow_write=true` plus `GRIDPANE_WP_CLI_ALLOW_WRITE=1` for MCP.
- **Finding the server:** `--site` (domain or id) and `--server` (label, IP or id) look up the server's IP through the REST API, so they need `GRIDPANE_API_TOKEN` too. `--site-id` and `--server-id` still work as aliases. `--host 203.0.113.10 --site-url example.com` skips the API.
- **SSH client:** the OpenSSH client built into Windows 10 and later, so your keys, `ssh-agent` and `known_hosts` apply. Host keys are always checked (`StrictHostKeyChecking=yes`) and password prompts are off (`BatchMode=yes`). Connect once by hand to accept each server's host key.
- **MCP:** the `gp_*` tools are only registered when `GRIDPANE_SSH_ENABLE=1` is set where the MCP server runs.

Setup in PowerShell 5.1. Generate a key for the pipeline, then add the `.pub` file to each server in GridPane (Settings > SSH Keys):

```powershell
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\gridpane_pipeline" -C "gridpane-pipeline"
```

Accept the server's host key once by hand, checking the fingerprint:

```powershell
ssh -i "$env:USERPROFILE\.ssh\gridpane_pipeline" root@203.0.113.10 exit
```

Turn the backend on for your user account, then open a new terminal:

```powershell
[Environment]::SetEnvironmentVariable('GRIDPANE_SSH_ENABLE', '1', 'User'); [Environment]::SetEnvironmentVariable('GRIDPANE_SSH_KEY', "$env:USERPROFILE\.ssh\gridpane_pipeline", 'User')
```

```powershell
gridpane ssh service nginx test --server sh-akl-02
```

## Checked against the live API

- `GET site` and `GET server`: `url`, `server_id` and a nested `server` (`ip`, `label`) on each site; `per_page` (up to 500) and Laravel-style pagination (`meta.last_page`) work. GET site lists primary sites only.
- PHP 8.5 is in use on GridPane sites, so the docs' 7.2–7.4 list is out of date.
- `GET backups/refresh` + `GET backups/original`: response format above, read correctly for local and Wasabi automatic backups.
- Rate-limit headers: `x-ratelimit-endpoint-limit: 12`, `x-ratelimit-total-limit: 60`, `retry-after-endpoint` in seconds.

## Not verified against the live API

These points are still unchecked:

- How a **tagged manual** backup appears in that list. The site checked had only automatic backups, so the `manual` key and the tag's position are taken from the restore endpoint's documented format.
- Whether `backups/refresh` picks up a new backup as soon as it finishes, and how long a backup takes, so the default 30 s poll and 30 min timeout are guesses.
- Which PHP versions `php_version` accepts (8.5 exists; the docs list only 7.2–7.4).
- Whether `set_nginx_caching(..., disable_first=True)` works in one go, or GridPane needs the `none` change to finish before it accepts the new mode.
- Whether GridPane passes WP-CLI arguments through a shell (the allowlist assumes it might).
- Every PUT/POST endpoint above. They match the docs, but none has been called.
- **SSH backend:** no GP-CLI command has been run on a real server. Syntax is from GridPane's GP-CLI quick reference. Not confirmed:
  - that `gp wp` passes arguments containing spaces through intact;
  - the exit codes GP-CLI returns on failure.

`uv.lock` was already out of step with upstream's `pyproject.toml` and hasn't been regenerated here (uv wasn't available). Run `uv lock` before using `uv sync --locked`; `uvx --from git+...` doesn't use it.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `GRIDPANE_API_TOKEN` | (required) | GridPane API personal access token |
| `GRIDPANE_API_URL` | `https://my.gridpane.com/oauth/api/v1` | API base URL |
| `GRIDPANE_CA_BUNDLE` | Windows store | PEM file of trusted certificates for API calls, instead of the OS store |
| `GRIDPANE_WP_CLI_ALLOW_WRITE` | unset | `1` lets the MCP `run_wp_cli` and `gp_wp` tools run commands outside the allowlist when the call also sets `allow_write` |
| `GRIDPANE_SSH_ENABLE` | unset | `1` turns on the GP-CLI SSH backend (CLI `gridpane ssh`, MCP `gp_*` tools) |
| `GRIDPANE_SSH_KEY` | ssh default | Private key file for the servers |
| `GRIDPANE_SSH_USER` | `root` | SSH user (GridPane allows root only) |
| `GRIDPANE_SSH_PORT` | `22` | SSH port |
| `GRIDPANE_SSH_KNOWN_HOSTS` | ssh default | A separate known_hosts file, if wanted |

## License

MIT
