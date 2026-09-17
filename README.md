# discord-mcp

Minimal MCP server for Discord forum and text channels.

## Setup

Bot needs `View Channels`, `Read Message History`, `Send Messages in Threads`, `Create Public Threads`, `Manage Threads`.

```json
{
  "mcpServers": {
    "discord": {
      "command": "uv",
      "args": ["--directory", "/path/to/discord-mcp", "run", "discord-mcp"],
      "env": { "DISCORD_BOT_TOKEN": "..." }
    }
  }
}
```

## Tools

| Tool | Args |
|---|---|
| `list_tags` | `forum_id` |
| `list_posts` | `forum_id`, `include_archived=false`, `limit=50` |
| `read_post` | `thread_id`, `limit=50` |
| `read_channel` | `channel_id`, `limit=50` |
| `create_post` | `forum_id`, `title`, `content`, `tags=[]` (names or IDs) |
| `set_tags` | `thread_id`, `tags` (names or IDs) |
| `close_post` | `thread_id`, `lock=false` |

## Dev

```sh
uv sync --all-groups
uv run ruff format . && uv run ruff check . && uv run ty check src/
```
