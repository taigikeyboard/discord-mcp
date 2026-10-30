import json

import pytest
import respx
from httpx import Response

from discord_mcp import server, sync

REPO = "o/r"
TAGS = {"t1": "bug", "t2": "done", "t3": "discussion"}
THREAD = {"id": "100", "name": "Post", "guild_id": "g1", "applied_tags": ["t1"]}


def msg(mid, content="hi", **extra):
    return {
        "id": mid,
        "type": 0,
        "channel_id": "100",
        "author": {"username": "alice", "global_name": "Alice"},
        "content": content,
        "timestamp": "2026-01-02T03:04:05Z",
        **extra,
    }


def body(mid, content="hi"):
    return f"{sync.render(msg(mid, content), 'g1', REPO)}\n\n<!-- discord-msg:{mid} -->"


def issue_body(state, content="hi"):
    return f"{sync.render(msg('100', content), 'g1', REPO)}\n\n<!-- discord-thread:100 state:{state} -->"


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.setattr(sync.time, "sleep", lambda _: None)


@pytest.fixture
def discord():
    with respx.mock(base_url=server.API, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def github():
    with respx.mock(base_url=sync.GITHUB, assert_all_called=False) as mock:
        mock.get(path__startswith=f"/repos/{REPO}/contents/").respond(404)
        yield mock


def runner():
    return sync.Sync(REPO, "t", dry_run=False)


def test_render_mentions_and_attachments():
    m = msg(
        "5",
        "ask <@42>",
        mentions=[{"id": "42", "username": "bob"}],
        attachments=[
            {"filename": "a b.png", "content_type": "image/png", "url": "u"},
            {"filename": "log.txt", "content_type": "text/plain", "url": "u"},
        ],
    )
    assert sync.render(m, "g1", REPO) == (
        "**Alice** · 2026-01-02 · [Discord](https://discord.com/channels/g1/100/5)\n\n"
        "ask @\u200bbob\n\n"
        "![a b.png](https://github.com/o/r/blob/HEAD/attachments/5/a%20b.png?raw=true)\n\n"
        "[log.txt](https://github.com/o/r/blob/HEAD/attachments/5/log.txt?raw=true)"
    )


@pytest.mark.parametrize(
    ("discord", "github", "last", "expected"),
    [
        ("open", None, None, ("open", None)),
        ("open", "open", "open", ("open", None)),
        ("closed", "open", "open", ("closed", "github")),
        ("open", "closed", "open", ("closed", "discord")),
        ("open", "open", "closed", ("open", None)),  # reopened on both sides
        ("closed", "closed", "open", ("closed", None)),
        ("open", "closed", "closed", ("open", "github")),  # both changed: Discord wins
    ],
)
def test_reconcile(discord, github, last, expected):
    assert sync.reconcile(discord, github, last) == expected


def test_thread_messages_pages_and_skips_system(discord):
    first = [msg(str(i)) for i in range(1, 101)]
    discord.get("/channels/100/messages", params={"after": "0"}).respond(json=first)
    discord.get("/channels/100/messages", params={"after": "100"}).respond(
        json=[msg("101"), {**msg("102"), "type": 18}]
    )
    assert [m["id"] for m in sync.thread_messages("100")][-2:] == ["100", "101"]


def test_new_post_creates_issue_and_comments(discord, github):
    discord.get("/channels/100/messages").respond(json=[msg("100"), msg("101", "reply")])
    create = github.post(f"/repos/{REPO}/issues").respond(json={"number": 7})
    comment = github.post(f"/repos/{REPO}/issues/7/comments").respond(json={})
    runner().sync_thread(THREAD, TAGS, None)
    assert json.loads(create.calls[0].request.content) == {
        "title": "Post",
        "body": issue_body("open"),
        "labels": ["bug"],
    }
    assert json.loads(comment.calls[0].request.content) == {"body": body("101", "reply")}


def issue(**over):
    return {
        "number": 7,
        "title": "Post",
        "body": issue_body("open"),
        "labels": [{"name": "bug"}],
        "state": "open",
        "comments": 0,
        **over,
    }


def mock_tag_lookup(discord, tags):
    """Routes `server._update_tags` reads: the thread, then its forum's tags."""
    discord.get("/channels/100").respond(json={**THREAD, "parent_id": "f1"})
    discord.get("/channels/f1").respond(
        json={"available_tags": [{"id": i, "name": n} for i, n in tags.items()]}
    )


def test_existing_issue_edits_and_deletes_comments(discord, github):
    discord.get("/channels/100/messages").respond(
        json=[msg("100"), msg("101", "same"), msg("102", "edited")]
    )
    github.get(f"/repos/{REPO}/issues/7/comments").respond(
        json=[
            {"id": 1, "body": body("101", "same")},
            {"id": 2, "body": body("102", "old")},
            {"id": 3, "body": body("103", "gone")},
            {"id": 4, "body": "manual comment, no marker"},
        ]
    )
    patch = github.patch(f"/repos/{REPO}/issues/comments/2").respond(json={})
    delete = github.delete(f"/repos/{REPO}/issues/comments/3").respond(204)
    unexpected = github.route(method__in=["POST", "PATCH", "DELETE"]).respond(500)
    runner().sync_thread(THREAD, TAGS, issue(comments=4))
    assert json.loads(patch.calls[0].request.content) == {"body": body("102", "edited")}
    assert delete.called
    assert not unexpected.called


def test_done_tag_closes_issue(discord, github):
    discord.get("/channels/100/messages").respond(json=[msg("100")])
    patch = github.patch(f"/repos/{REPO}/issues/7").respond(json={})
    runner().sync_thread({**THREAD, "applied_tags": ["t1", "t2"]}, TAGS, issue())
    assert json.loads(patch.calls[0].request.content) == {
        "title": "Post",
        "body": issue_body("closed"),
        "labels": ["bug"],
        "state": "closed",
        "state_reason": "completed",
    }


def test_issue_closed_on_github_marks_post_done(discord, github):
    discord.get("/channels/100/messages").respond(json=[msg("100")])
    mock_tag_lookup(discord, TAGS)
    thread_patch = discord.patch("/channels/100").respond(json={**THREAD, "parent_id": "f1"})
    github.patch(f"/repos/{REPO}/issues/7").respond(json={})
    runner().sync_thread(THREAD, TAGS, issue(state="closed"))
    assert json.loads(thread_patch.calls[0].request.content) == {"applied_tags": ["t1", "t2"]}
    assert json.loads(thread_patch.calls[1].request.content) == {"archived": True, "locked": False}


def test_issue_closed_not_planned_marks_post_drop(discord, github):
    tags = {**TAGS, "t4": "drop"}
    discord.get("/channels/100/messages").respond(json=[msg("100")])
    mock_tag_lookup(discord, tags)
    thread_patch = discord.patch("/channels/100").respond(json={**THREAD, "parent_id": "f1"})
    patch = github.patch(f"/repos/{REPO}/issues/7").respond(json={})
    runner().sync_thread(THREAD, tags, issue(state="closed", state_reason="not_planned"))
    assert json.loads(thread_patch.calls[0].request.content) == {"applied_tags": ["t1", "t4"]}
    assert json.loads(patch.calls[0].request.content)["state_reason"] == "not_planned"


def test_github_only_labels_kept(discord, github):
    discord.get("/channels/100/messages").respond(json=[msg("100")])
    unexpected = github.route(method__in=["POST", "PATCH", "DELETE"]).respond(500)
    runner().sync_thread(THREAD, TAGS, issue(labels=[{"name": "bug"}, {"name": "priority"}]))
    assert not unexpected.called


def test_closed_post_created_open_then_closed(discord, github):
    discord.get("/channels/100/messages").respond(json=[msg("100")])
    create = github.post(f"/repos/{REPO}/issues").respond(json={"number": 7})
    patch = github.patch(f"/repos/{REPO}/issues/7").respond(json={})
    runner().sync_thread({**THREAD, "applied_tags": ["t1", "t2"]}, TAGS, None)
    assert json.loads(create.calls[0].request.content)["body"] == issue_body("open")
    assert json.loads(patch.calls[0].request.content)["body"] == issue_body("closed")


def test_marker_in_content_does_not_spoof():
    spoof = f"see <!-- discord-thread:999 state:closed -->\n\n{issue_body('open')}"
    assert sync.THREAD_MARK.search(spoof).groups() == ("100", "open")
    assert sync.THREAD_MARK.search("<!-- discord-thread:999 state:closed --> text") is None


@pytest.mark.parametrize(
    ("status", "headers", "text", "expected"),
    [
        (200, {}, "", None),
        (403, {}, "Resource not accessible", None),
        (403, {"retry-after": "30"}, "", 30),
        (429, {}, "You have exceeded a secondary rate limit", 60),
    ],
)
def test_rate_limit_wait(status, headers, text, expected):
    assert sync._rate_limit_wait(Response(status, headers=headers, text=text)) == expected


def test_reads_and_writes_retry_after_rate_limit(github):
    limited = Response(403, headers={"retry-after": "1"})
    get = github.get(f"/repos/{REPO}/issues").mock(side_effect=[limited, Response(200, json=[])])
    post = github.post(f"/repos/{REPO}/issues").mock(
        side_effect=[limited, Response(201, json={"number": 1})]
    )
    assert runner()._get_all(f"/repos/{REPO}/issues") == []
    assert runner()._write("POST", f"/repos/{REPO}/issues", json={}) == {"number": 1}
    assert get.call_count == post.call_count == 2


ATTACHED = msg("5", attachments=[{"filename": "a.png", "url": "https://cdn/a.png"}])


def test_attachment_already_uploaded_is_skipped():
    with respx.mock(base_url=sync.GITHUB) as github:  # unmocked download / PUT would fail
        github.get(f"/repos/{REPO}/contents/attachments/5/a.png").respond(200, json={})
        runner()._upload_attachments(ATTACHED)


def test_attachment_check_error_raises():
    with respx.mock(base_url=sync.GITHUB) as github:
        github.get(f"/repos/{REPO}/contents/attachments/5/a.png").respond(500)
        with pytest.raises(sync.httpx.HTTPStatusError):
            runner()._upload_attachments(ATTACHED)


def test_discussion_post_skipped(discord, github):
    runner().sync_thread({**THREAD, "applied_tags": ["t3"]}, TAGS, None)
    assert not discord.calls
    assert not github.calls


def test_dry_run_writes_nothing(discord, github, capsys):
    discord.get("/channels/100/messages").respond(json=[msg("100"), msg("101")])
    sync.Sync(REPO, "t", dry_run=True).sync_thread(THREAD, TAGS, None)
    assert all(c.request.method == "GET" for c in github.calls)
    assert "POST /repos/o/r/issues/<new>/comments" in capsys.readouterr().out
