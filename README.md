# discord-mcp

Minimal MCP server for Discord forum and text channels.

## Setup

1. [Discord Developer Portal](https://discord.com/developers/applications) → Bot → copy token; enable **Message Content Intent** (required to read message text).
2. OAuth2 → URL Generator → scope `bot`; permissions: View Channels, Read Message History, Send Messages, Send Messages in Threads, Create Public Threads, Manage Threads. Open the URL to invite.
3. Register:

```sh
claude mcp add discord -e DISCORD_BOT_TOKEN=... -- uv --directory /path/to/discord-mcp run discord-mcp
```

Any MCP client works with the equivalent `command` / `args` / `env`.

Channel IDs: Discord → Settings → Advanced → Developer Mode → right-click channel → Copy ID.

## Tools

| Tool | Args |
|---|---|
| `list_tags` | `forum_id` |
| `list_posts` | `forum_id`, `include_archived=false`, `limit=50` |
| `read_post` | `thread_id`, `limit=50` |
| `read_channel` | `channel_id`, `limit=50`, `before` (message ID, page backwards); each message has `link`, `reply_to`, `bot` |
| `create_post` | `forum_id`, `title`, `content`, `tags=[]` |
| `reply_post` | `thread_id`, `content`, `reply_to` (message ID → Discord reply) |
| `set_tags` / `add_tags` / `remove_tags` | `thread_id`, `tags` |
| `close_post` | `thread_id`, `lock=false` |

`tags` accept names or IDs.

## Sync a forum to GitHub issues

`discord-sync` mirrors a forum channel into a GitHub repo's issues, run by hand:

```sh
DISCORD_BOT_TOKEN=... GITHUB_TOKEN=$(gh auth token) \
  uv --directory /path/to/discord-mcp run discord-sync --forum <forum-id> --repo owner/name [--dry-run]
```

- Post → issue (title, starter message as body, tags as labels); later messages → comments. Edited / deleted messages update / delete their comment; comments without a sync marker are left alone.
- Attachments are committed to the target repo under `attachments/<message-id>/` — use a private repo when the forum content should stay private.
- Closed state syncs both ways: a `done` / `drop` tag closes the issue (`drop` = not planned); an issue closed on GitHub tags its post `done` and archives it; reopening either side reopens the other. Posts tagged `discussion` are skipped.
- All state lives in hidden markers in issue and comment bodies, so a re-run is idempotent. `--dry-run` prints every write and changes nothing.

## Dev

```sh
uv sync --all-groups
uv run ruff format . && uv run ruff check . && uv run ty check src/ && uv run pytest
```
