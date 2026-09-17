import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

API = "https://discord.com/api/v10"
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


def _messages(channel_id: str, limit: int) -> list[dict]:
    messages = _req("GET", f"/channels/{channel_id}/messages", params={"limit": min(limit, 100)})
    return [
        {
            "id": m["id"],
            "author": m["author"]["username"],
            "content": m["content"],
            "timestamp": m["timestamp"],
            "attachments": [a["url"] for a in m.get("attachments", [])],
        }
        for m in reversed(messages)
    ]


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
    if include_archived:
        path = f"/channels/{forum_id}/threads/archived/public"
        threads += _req("GET", path, params={"limit": limit})["threads"]
    return [_post(t, _tags(forum)) for t in threads[:limit]]


@mcp.tool()
def read_post(thread_id: str, limit: int = 50) -> dict:
    """Read a forum post: metadata plus messages, oldest first."""
    thread = _channel(thread_id)
    return {
        **_post(thread, _tags(_channel(thread["parent_id"]))),
        "messages": _messages(thread_id, limit),
    }


@mcp.tool()
def read_channel(channel_id: str, limit: int = 50) -> list[dict]:
    """Read recent messages from a text channel, oldest first."""
    return _messages(channel_id, limit)


@mcp.tool()
def create_post(forum_id: str, title: str, content: str, tags: list[str] | None = None) -> dict:
    """Create a forum post. `tags` are tag names or IDs."""
    forum = _channel(forum_id)
    body: dict[str, Any] = {"name": title, "message": {"content": content}}
    if tags:
        body["applied_tags"] = _resolve_tags(forum, tags)
    return _post(_req("POST", f"/channels/{forum_id}/threads", json=body), _tags(forum))


@mcp.tool()
def set_tags(thread_id: str, tags: list[str]) -> dict:
    """Replace tags on a forum post. `tags` are tag names or IDs."""
    forum = _channel(_channel(thread_id)["parent_id"])
    body = {"applied_tags": _resolve_tags(forum, tags)}
    return _post(_req("PATCH", f"/channels/{thread_id}", json=body), _tags(forum))


@mcp.tool()
def close_post(thread_id: str, lock: bool = False) -> dict:
    """Archive a forum post. `lock=True` also prevents reopening."""
    thread = _req("PATCH", f"/channels/{thread_id}", json={"archived": True, "locked": lock})
    return _post(thread, _tags(_channel(thread["parent_id"])))


def main() -> None:
    mcp.run()
