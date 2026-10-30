"""One-way sync of a Discord forum into GitHub issues.

Each forum post becomes an issue (starter message = body), each later message a comment.
Hidden markers in issue / comment bodies hold all sync state, so a re-run is idempotent.
Closed state syncs both ways: a `done` / `drop` tag closes the issue, and an issue closed
on GitHub tags its post `done` and archives it.
"""

import argparse
import base64
import os
import re
import time
from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from discord_mcp import server

GITHUB = "https://api.github.com"
SKIP_TAG = "discussion"
CLOSED_TAGS = {"done", "drop"}
MESSAGE_TYPES = {0, 19}  # default, reply; skips system messages (pins, renames)
# Anchored at the end: the real marker is always appended last, so a marker typed into
# message content cannot take its place.
THREAD_MARK = re.compile(r"<!-- discord-thread:(\d+) state:(open|closed) -->\Z")
MSG_MARK = re.compile(r"<!-- discord-msg:(\d+) -->\Z")


def _name(user: dict) -> str:
    return user.get("global_name") or user["username"]


def _attachment_path(message_id: str, filename: str) -> str:
    return f"attachments/{message_id}/{filename}"


def render(message: dict, guild_id: str, repo: str) -> str:
    """Markdown for one Discord message: author line, content, attachments."""
    content = message["content"]
    for user in message.get("mentions", []):
        content = re.sub(rf"<@!?{user['id']}>", f"@{_name(user)}", content)
    # Zero-width space after "@" so GitHub does not ping a same-named GitHub account.
    content = content.replace("@", "@\u200b")
    link = server._message(message, guild_id)["link"]
    lines = [f"**{_name(message['author'])}** · {message['timestamp'][:10]} · [Discord]({link})"]
    if content:
        lines += ["", content]
    for a in message.get("attachments", []):
        path = quote(_attachment_path(message["id"], a["filename"]))
        url = f"https://github.com/{repo}/blob/HEAD/{path}?raw=true"
        image = a.get("content_type", "").startswith("image/")
        lines += ["", f"{'!' if image else ''}[{a['filename']}]({url})"]
    return "\n".join(lines)


def reconcile(discord: str, github: str | None, last: str | None) -> tuple[str, str | None]:
    """Resolve open / closed against the last synced state (None = no issue yet).

    Returns the resolved state and the side that must change to match it
    ("github", "discord" or None). When both sides changed, Discord wins.
    """
    if github is None or last is None:
        return discord, None
    if discord != last:
        return discord, "github" if github != discord else None
    if github != last:
        return github, "discord"
    return last, None


def _rate_limit_wait(response: httpx.Response) -> float | None:
    """Seconds to wait before retrying a rate-limited GitHub request, or None if not limited."""
    if response.status_code not in {HTTPStatus.FORBIDDEN, HTTPStatus.TOO_MANY_REQUESTS}:
        return None
    if retry_after := response.headers.get("retry-after"):
        return float(retry_after)
    if response.headers.get("x-ratelimit-remaining") == "0":
        return max(float(response.headers["x-ratelimit-reset"]) - time.time(), 1)
    # Secondary limit without headers: GitHub asks for at least a minute.
    return 60 if "secondary rate limit" in response.text else None


def issue_fields(thread: dict, tags: set[str], state: str, body: str, labels: set[str]) -> dict:
    """Issue fields for a post. `labels` = labels to keep besides its tags (added on GitHub)."""
    reason = "not_planned" if "drop" in tags else "completed"
    return {
        "title": thread["name"],
        "body": f"{body}\n\n<!-- discord-thread:{thread['id']} state:{state} -->",
        "labels": sorted(labels | (tags - CLOSED_TAGS)),
        "state": state,
        **({"state_reason": reason} if state == "closed" else {}),
    }


def thread_messages(thread_id: str) -> list[dict]:
    messages: list[dict] = []
    after = "0"
    while True:
        page = server._req(
            "GET", f"/channels/{thread_id}/messages", params={"limit": server.PAGE, "after": after}
        )
        messages += page
        if len(page) < server.PAGE:
            break
        after = max(page, key=lambda m: int(m["id"]))["id"]
    return sorted((m for m in messages if m["type"] in MESSAGE_TYPES), key=lambda m: int(m["id"]))


class Sync:
    def __init__(self, repo: str, token: str, *, dry_run: bool) -> None:
        self.repo = repo
        self.dry_run = dry_run
        self.github = httpx.Client(
            base_url=GITHUB,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )

    def _get_all(self, path: str, **params: str) -> list[dict]:
        items: list[dict] = []
        url: str | None = path
        query: dict | None = {"per_page": 100, **params}
        while url:
            response = self._request("GET", url, params=query)
            response.raise_for_status()
            items += response.json()
            url = response.links.get("next", {}).get("url")
            query = None  # the next link carries the query
        return items

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One GitHub request, retried after rate limits; the caller checks the status."""
        response = self.github.request(method, path, **kwargs)
        while wait := _rate_limit_wait(response):
            print(f"rate limited, waiting {wait:.0f} s")
            time.sleep(wait)
            response = self.github.request(method, path, **kwargs)
        return response

    def _write(self, method: str, path: str, **kwargs: Any) -> Any:
        print(f"{method} {path}")
        if self.dry_run:
            return None
        response = self._request(method, path, **kwargs)
        response.raise_for_status()
        if method in {"POST", "PUT"}:
            time.sleep(1)  # GitHub asks for >= 1 s between content-creating requests
        return response.json() if response.content else None

    def _discord_state(self, thread_id: str, tags: set[str], close_tag: str | None) -> None:
        """Close the post with `close_tag`, or reopen it when None."""
        print(f"discord {thread_id} -> {close_tag or 'open'}")
        if self.dry_run:
            return
        if close_tag:
            server._update_tags(thread_id, [close_tag], "add")
            server.close_post(thread_id)
        else:
            server._update_tags(thread_id, sorted(tags & CLOSED_TAGS), "remove")
            server.reopen_post(thread_id)

    def _upload_attachments(self, message: dict) -> None:
        for a in message.get("attachments", []):
            path = f"/repos/{self.repo}/contents/{_attachment_path(message['id'], a['filename'])}"
            exists = self._request("GET", path)
            if exists.status_code == HTTPStatus.OK:
                continue
            if exists.status_code != HTTPStatus.NOT_FOUND:
                exists.raise_for_status()
            content = b""
            if not self.dry_run:
                download = httpx.get(a["url"], timeout=60)
                download.raise_for_status()
                content = download.content
            self._write(
                "PUT",
                path,
                json={
                    "message": f"Add Discord attachment {a['filename']}",
                    "content": base64.b64encode(content).decode(),
                },
            )

    def _sync_comments(
        self, number: int | None, replies: list[dict], guild_id: str, *, has_comments: bool
    ) -> None:
        existing: dict[str, dict] = {}
        if has_comments:
            for c in self._get_all(f"/repos/{self.repo}/issues/{number}/comments"):
                if mark := MSG_MARK.search(c["body"] or ""):
                    existing[mark[1]] = c
        issue_path = f"/repos/{self.repo}/issues/{number or '<new>'}"
        for m in replies:
            body = f"{render(m, guild_id, self.repo)}\n\n<!-- discord-msg:{m['id']} -->"
            comment = existing.pop(m["id"], None)
            if comment and comment["body"] == body:
                continue
            self._upload_attachments(m)
            if comment:
                self._write(
                    "PATCH",
                    f"/repos/{self.repo}/issues/comments/{comment['id']}",
                    json={"body": body},
                )
            else:
                self._write("POST", f"{issue_path}/comments", json={"body": body})
        for comment in existing.values():  # message deleted on Discord
            self._write("DELETE", f"/repos/{self.repo}/issues/comments/{comment['id']}")

    def sync_thread(self, thread: dict, tag_names: dict[str, str], issue: dict | None) -> None:
        tags = {tag_names.get(i, i) for i in thread.get("applied_tags", [])}
        if SKIP_TAG in tags:
            return
        messages = thread_messages(thread["id"])
        starter = next((m for m in messages if m["id"] == thread["id"]), None)
        replies = [m for m in messages if m["id"] != thread["id"]]
        guild_id = thread["guild_id"]
        mark = THREAD_MARK.search(issue["body"]) if issue else None
        state, stale = reconcile(
            "closed" if tags & CLOSED_TAGS else "open",
            issue["state"] if issue else None,
            mark[2] if mark else None,
        )
        if stale == "discord" and issue:  # `and issue` narrows the type; stale implies it
            close_tag = None
            if state == "closed":
                close_tag = "drop" if issue.get("state_reason") == "not_planned" else "done"
            self._discord_state(thread["id"], tags, close_tag)
            tags = (tags - CLOSED_TAGS) | ({close_tag} if close_tag else set())

        body = render(starter, guild_id, self.repo) if starter else "_Starter deleted on Discord._"
        github_only = {label["name"] for label in (issue or {}).get("labels", [])} - set(
            tag_names.values()
        )
        fields = issue_fields(thread, tags, state, body, github_only)
        if starter and (issue is None or fields["body"] != issue["body"]):
            self._upload_attachments(starter)
        if issue is None:
            # Created open with an "open" marker; a closed post is closed by the PATCH below,
            # so a failed PATCH never leaves a marker that contradicts the issue state.
            opened = issue_fields(thread, tags, "open", body, github_only)
            create = {k: opened[k] for k in ("title", "body", "labels")}
            created = self._write("POST", f"/repos/{self.repo}/issues", json=create)
            number = created["number"] if created else None
            if state == "closed" and number:
                self._write("PATCH", f"/repos/{self.repo}/issues/{number}", json=fields)
        else:
            number = issue["number"]
            current = {
                "title": issue["title"],
                "body": issue["body"],
                "labels": sorted(label["name"] for label in issue["labels"]),
                "state": issue["state"],
            }
            if any(fields[k] != current[k] for k in current):
                self._write("PATCH", f"/repos/{self.repo}/issues/{number}", json=fields)
        has_comments = bool(issue and issue["comments"])
        self._sync_comments(number, replies, guild_id, has_comments=has_comments)

    def run(self, forum_id: str) -> None:
        forum = server._channel(forum_id)
        tag_names = server._tags(forum)
        active = server._req("GET", f"/guilds/{forum['guild_id']}/threads/active")["threads"]
        # Keyed by ID: a post archived between the two calls appears in both lists.
        threads = {t["id"]: t for t in active if t["parent_id"] == forum_id}
        threads |= {t["id"]: t for t in server._archived(forum_id, 10_000)}
        issues: dict[str, dict] = {}
        for issue in self._get_all(f"/repos/{self.repo}/issues", state="all"):
            if mark := THREAD_MARK.search(issue["body"] or ""):
                issues[mark[1]] = issue
        for thread in sorted(threads.values(), key=lambda t: int(t["id"])):
            self.sync_thread(
                {**thread, "guild_id": forum["guild_id"]}, tag_names, issues.get(thread["id"])
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="discord-sync", description="Sync a Discord forum into GitHub issues."
    )
    parser.add_argument("--forum", required=True, help="forum channel ID")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--dry-run", action="store_true", help="print writes, change nothing")
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN not set (GITHUB_TOKEN=$(gh auth token) discord-sync ...)")
    Sync(args.repo, token, dry_run=args.dry_run).run(args.forum)
