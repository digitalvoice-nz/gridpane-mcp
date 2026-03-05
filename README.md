# gridpane-mcp

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for the [GridPane](https://gridpane.com/) hosting platform API. Manage your WordPress servers and sites through Claude, Cursor, or any MCP-compatible client.

## Features

### Server Management
- **list_servers** — List all servers with IPs, status, and site counts
- **get_server** — Get detailed server info by ID
- **search_server** — Find servers by name (partial match)
- **restart_nginx** — Restart Nginx on a server
- **restart_php** — Restart PHP-FPM for a specific PHP version
- **restart_mysql** — Restart MySQL/MariaDB/Percona

### Site Management
- **list_sites** — List all WordPress sites across all servers
- **get_site** — Get detailed site info by ID
- **search_site** — Find sites by domain name (partial match)
- **toggle_ssl** — Toggle SSL on/off
- **toggle_site_cache** — Toggle Nginx caching
- **purge_site_cache** — Purge all server-level caches
- **toggle_debug** — Toggle WordPress debug mode
- **change_php_version** — Change PHP version (7.4, 8.0, 8.1, 8.2, 8.3)
- **get_sso_link** — Get a Single Sign-On link to wp-admin
- **trigger_backup** — Trigger a manual backup

## Prerequisites

- A [GridPane](https://gridpane.com/) account (Developer plan or above)
- A GridPane API token (Settings > GridPane API > Create New Token)

## Installation

```bash
pip install gridpane-mcp
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv pip install gridpane-mcp
```

## Configuration

Set your GridPane API token as an environment variable:

```bash
export GRIDPANE_API_TOKEN="your_token_here"
```

### Claude Code

```bash
claude mcp add gridpane -e GRIDPANE_API_TOKEN=your_token_here -- gridpane-mcp
```

### Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "gridpane": {
      "command": "gridpane-mcp",
      "env": {
        "GRIDPANE_API_TOKEN": "your_token_here"
      }
    }
  }
}
```

### Cursor

Add to your Cursor MCP settings:

```json
{
  "mcpServers": {
    "gridpane": {
      "command": "gridpane-mcp",
      "env": {
        "GRIDPANE_API_TOKEN": "your_token_here"
      }
    }
  }
}
```

## Usage Examples

Once connected, you can ask your AI assistant things like:

- "List all my GridPane servers"
- "Find sites with 'supple' in the domain"
- "Purge cache for site ID 661035"
- "Restart Nginx on the accesshealth server"
- "What PHP version is accesshealth.com.au running?"
- "Toggle debug mode on site 661035"

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GRIDPANE_API_TOKEN` | (required) | Your GridPane API personal access token |
| `GRIDPANE_API_URL` | `https://my.gridpane.com/oauth/api/v1` | GridPane API base URL |

## License

MIT
