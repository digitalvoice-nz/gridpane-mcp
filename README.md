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
| `restart_nginx`, `restart_php`, `restart_mysql` | **removed** | none documented |

The removed tools were not kept behind a flag. Nothing public shows those endpoints exist, and a guessed POST that happens to do something unexpected is worse than no tool.

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

```powershell
gridpane sites --search example.com
gridpane clear-cache 12345 --scope all
gridpane backup 12345 --tag task-42 --wait --timeout 1800
gridpane backups 12345 --tag task-42
gridpane restore 12345 --tag task-42
gridpane debug 12345 off
gridpane php 12345 8.2
gridpane nginx-cache 12345 fastcgi --disable-first
gridpane wp 12345 plugin list --status=active
```

| Exit code | Meaning |
|---|---|
| 0 | OK |
| 1 | API error or invalid input |
| 2 | Bad arguments |
| 3 | `--wait` / `wait-backup` timed out |
| 4 | WP-CLI command blocked by the allowlist |
| 5 | Restore cancelled at the prompt |

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
- **Waiting.** GridPane has no job-status endpoint. `wait_for_backup` calls `backups/refresh` and then `backups/original` every `interval` seconds until a backup with the tag appears. Pass the list taken *before* `create_backup` as `exclude` (the CLI's `backup --wait` does this) so an older backup with the same tag doesn't count.
- **Restoring by tag** picks the newest backup with that tag. The restore call needs `backup_source` and `backup_type`; they come from the backup list where it says, otherwise `local` and `manual`, which is what `create_backup(type="local", tag=...)` makes.
- **Parsing the list.** The docs show no example response for `backups/original`. The parser walks the whole response for "YYYY-MM-DD HH:MM [tag]" strings, the format the restore endpoint takes, and for objects with a date field plus a `tag` field. **Run `gridpane backups <site_id> --raw` against a real site once and check it before relying on `--wait` or `restore --tag` in a pipeline.**

## WP-CLI

`PUT site/run-wp-cli/{id}` runs **any** WP-CLI command, so by default only a read-only allowlist is accepted: `core version`, `option get`, `plugin list`, `theme list`, `post list`, `cache flush` and similar (see `WP_CLI_READ_ONLY` in `gridpane_client.py`). Shell control characters (`; & | $` and backticks) and the global flags `--exec`, `--require`, `--ssh`, `--http`, `--path` are also refused.

Opting in to anything else:

- **CLI:** `gridpane wp 12345 --allow-write plugin update akismet` (put `--allow-write` before the command).
- **Python:** `gp.wp(site_id, "plugin update akismet", allow_write=True)`.
- **MCP:** the tool's `allow_write=true` **and** `GRIDPANE_WP_CLI_ALLOW_WRITE=1` in the server's environment. Both are required, so a model can't opt itself in.

Per GridPane's docs, the response only says whether the commands ran. It does not return their output, so `plugin list` and the other read commands are mostly useful as a check that WP-CLI runs on the site.

## Not verified against the live API

This was built from the docs alone, without an API token. These points could not be checked:

- The response format of `GET backups/original/{id}` (and so the backup parser, `wait_for_backup` and restore-by-tag).
- Whether `backups/refresh` picks up a new backup as soon as it finishes, and how long a backup takes, so the default 30 s poll and 30 min timeout are guesses.
- Which PHP versions `php_version` accepts now. The docs list only 7.2–7.4.
- Whether `set_nginx_caching(..., disable_first=True)` works in one go, or GridPane needs the `none` change to finish before it accepts the new mode.
- Whether `per_page` on `GET server` / `GET site` does anything (kept from upstream; not in the docs).
- Whether GridPane passes WP-CLI arguments through a shell (the allowlist assumes it might).
- Every PUT/POST endpoint above. They match the docs, but none has been called.

`uv.lock` was already out of step with upstream's `pyproject.toml` and hasn't been regenerated here (uv wasn't available). Run `uv lock` before using `uv sync --locked`; `uvx --from git+...` doesn't use it.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `GRIDPANE_API_TOKEN` | (required) | GridPane API personal access token |
| `GRIDPANE_API_URL` | `https://my.gridpane.com/oauth/api/v1` | API base URL |
| `GRIDPANE_WP_CLI_ALLOW_WRITE` | unset | `1` lets the MCP `run_wp_cli` tool run commands outside the allowlist when the call also sets `allow_write` |

## License

MIT
