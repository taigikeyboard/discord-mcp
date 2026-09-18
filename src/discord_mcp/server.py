import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

API = "https://discord.com/api/v10"
PAGE = 100
mcp = MCPServer("discord")


def _req(method: str, path: str, **kwargs: Any) -> Any:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise ToolError("DISCORD_BOT_TOKEN not set")
    with httpx.Client(
        base_url=API, headers={"Authorization": f"Bot {token}"}, timeout=30
    ) as client:
        response = client.request(method, path, **kwargs)
    if response.is_error:
        raise ToolError(f"Discord {response.status_code}: {response.text}")
    return response.json() if response.content else None


def _channel(channel_id: str) -> dict:
    return _req("GET", f"/channels/{channel_id}")


def _message(m: dict, guild_id: str | None = None) -> dict:
    return {
        "id": m["id"],
        "author": m["author"]["username"],
        "bot": m["author"].get("bot", False),
        "content": m["content"],
        "timestamp": m["timestamp"],
        "attachments": [a["url"] for a in m.get("attachments", [])],
        "reply_to": (m.get("message_reference") or {}).get("message_id"),
        "link": f"https://discord.com/channels/{guild_id}/{m['channel_id']}/{m['id']}"
        if guild_id
        else None,
    }


def _messages(
    channel_id: str,
    limit: int,
    before: str | None = None,
    guild_id: str | None = None,
    after: str | None = None,
) -> list[dict]:
    path = f"/channels/{channel_id}/messages"
    params = {"limit": min(limit, PAGE)}
    if before:
        params["before"] = before
    if after:
        params["after"] = after
    page = sorted(_req("GET", path, params=params), key=lambda m: int(m["id"]))
    return [_message(m, guild_id) for m in page]


def _archived(forum_id: str, limit: int) -> list[dict]:
    threads: list[dict] = []
    before = None
    while len(threads) < limit:
        params = {
            "limit": min(limit - len(threads), PAGE),
            **({"before": before} if before else {}),
        }
        page = _req("GET", f"/channels/{forum_id}/threads/archived/public", params=params)
        threads += page["threads"]
        if not page["has_more"]:
            break
        before = threads[-1]["thread_metadata"]["archive_timestamp"]
    return threads


def _tags(forum: dict) -> dict[str, str]:
    return {t["id"]: t["name"] for t in forum.get("available_tags", [])}


def _resolve_tags(forum: dict, tags: list[str]) -> list[str]:
    known = _tags(forum)
    by_name = {n: i for i, n in known.items()}
    ids = []
    for t in tags:
        if t in by_name:
            ids.append(by_name[t])
        elif t in known:
            ids.append(t)
        else:
            raise ToolError(f"Unknown tag: {t}")
    return ids


def _post(thread: dict, tags: dict[str, str]) -> dict:
    meta = thread.get("thread_metadata", {})
    return {
        "id": thread["id"],
        "name": thread["name"],
        "tags": [tags.get(i, i) for i in thread.get("applied_tags", [])],
        "archived": meta.get("archived", False),
        "locked": meta.get("locked", False),
        "message_count": thread.get("message_count", 0),
    }


def _update_tags(thread_id: str, tags: list[str], mode: str) -> dict:
    thread = _channel(thread_id)
    forum = _channel(thread["parent_id"])
    ids = _resolve_tags(forum, tags)
    current = thread.get("applied_tags", [])
    match mode:
        case "add":
            applied = current + [i for i in ids if i not in current]
        case "remove":
            applied = [i for i in current if i not in ids]
        case _:
            applied = ids
    body: dict[str, Any] = {"applied_tags": applied}
    if thread.get("thread_metadata", {}).get("archived"):
        # Discord rejects tag edits on an archived thread (50083); unarchive in the
        # same PATCH, then archive again so the post stays closed.
        body["archived"] = False
        _req("PATCH", f"/channels/{thread_id}", json=body)
        thread = _req("PATCH", f"/channels/{thread_id}", json={"archived": True})
    else:
        thread = _req("PATCH", f"/channels/{thread_id}", json=body)
    return _post(thread, _tags(forum))


@mcp.tool()
def list_tags(forum_id: str) -> list[dict]:
    """List available tags of a forum channel."""
    return [{"id": i, "name": n} for i, n in _tags(_channel(forum_id)).items()]


@mcp.tool()
def list_posts(forum_id: str, include_archived: bool = False, limit: int = 50) -> list[dict]:
    """List posts in a forum channel, active first."""
    forum = _channel(forum_id)
    active = _req("GET", f"/guilds/{forum['guild_id']}/threads/active")["threads"]
    threads = [t for t in active if t["parent_id"] == forum_id]
    if include_archived and len(threads) < limit:
        threads += _archived(forum_id, limit - len(threads))
    return [_post(t, _tags(forum)) for t in threads[:limit]]


@mcp.tool()
def read_post(thread_id: str, limit: int = 50) -> dict:
    """Read a forum post: metadata plus messages, oldest first."""
    thread = _channel(thread_id)
    return {
        **_post(thread, _tags(_channel(thread["parent_id"]))),
        "messages": _messages(thread_id, limit, guild_id=thread["guild_id"]),
    }


@mcp.tool()
def read_channel(
    channel_id: str, limit: int = 50, before: str | None = None, after: str | None = None
) -> list[dict]:
    """Read recent messages from a text channel, oldest first.

    `before` is a message ID: page backwards from it. `after` is a message ID: return
    only messages newer than it (oldest first). Each message carries a `link` and,
    when it is a reply, the `reply_to` message ID.
    """
    guild_id = _channel(channel_id)["guild_id"]
    return _messages(channel_id, limit, before, guild_id, after)


@mcp.tool()
def create_post(forum_id: str, title: str, content: str, tags: list[str] | None = None) -> dict:
    """Create a forum post. `tags` are tag names or IDs."""
    forum = _channel(forum_id)
    body: dict[str, Any] = {"name": title, "message": {"content": content}}
    if tags:
        body["applied_tags"] = _resolve_tags(forum, tags)
    return _post(_req("POST", f"/channels/{forum_id}/threads", json=body), _tags(forum))


@mcp.tool()
def reply_post(thread_id: str, content: str, reply_to: str | None = None) -> dict:
    """Send a message in a forum post, thread or text channel.

    `reply_to` is a message ID in that channel: the message is sent as a reply to it.
    """
    body: dict[str, Any] = {"content": content}
    if reply_to:
        body["message_reference"] = {"message_id": reply_to}
    return _message(_req("POST", f"/channels/{thread_id}/messages", json=body))


@mcp.tool()
def set_tags(thread_id: str, tags: list[str]) -> dict:
    """Replace tags on a forum post. `tags` are tag names or IDs."""
    return _update_tags(thread_id, tags, "set")


@mcp.tool()
def add_tags(thread_id: str, tags: list[str]) -> dict:
    """Add tags to a forum post. `tags` are tag names or IDs."""
    return _update_tags(thread_id, tags, "add")


@mcp.tool()
def remove_tags(thread_id: str, tags: list[str]) -> dict:
    """Remove tags from a forum post. `tags` are tag names or IDs."""
    return _update_tags(thread_id, tags, "remove")


@mcp.tool()
def close_post(thread_id: str, lock: bool = False) -> dict:
    """Archive a forum post. `lock=True` also prevents reopening."""
    thread = _req("PATCH", f"/channels/{thread_id}", json={"archived": True, "locked": lock})
    return _post(thread, _tags(_channel(thread["parent_id"])))


@mcp.tool()
def reopen_post(thread_id: str) -> dict:
    """Unarchive (and unlock) a forum post."""
    thread = _req("PATCH", f"/channels/{thread_id}", json={"archived": False, "locked": False})
    return _post(thread, _tags(_channel(thread["parent_id"])))


def main() -> None:
    mcp.run()
