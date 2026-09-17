import pytest
import respx
from httpx import Response
from mcp.server.mcpserver.exceptions import ToolError

from discord_mcp import server

FORUM = {
    "id": "f1",
    "guild_id": "g1",
    "available_tags": [{"id": "t1", "name": "bug"}, {"id": "t2", "name": "done"}],
}
THREAD = {
    "id": "p1",
    "name": "Post",
    "parent_id": "f1",
    "applied_tags": ["t1"],
    "thread_metadata": {"archived": False, "locked": False},
    "message_count": 2,
}
MSG = {
    "id": "m1",
    "author": {"username": "alice"},
    "content": "hi",
    "timestamp": "2026-01-01T00:00:00Z",
    "attachments": [{"url": "https://x/a.png"}],
}


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")


@pytest.fixture
def api():
    with respx.mock(base_url=server.API, assert_all_called=False) as mock:
        mock.get("/channels/f1").respond(json=FORUM)
        mock.get("/channels/p1").respond(json=THREAD)
        yield mock


def test_missing_token_raises(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN")
    with pytest.raises(ToolError, match="DISCORD_BOT_TOKEN"):
        server.list_tags("f1")


def test_http_error_surfaces_status(api):
    api.get("/channels/bad/messages").respond(404, text="nope")
    with pytest.raises(ToolError, match="Discord 404: nope"):
        server.read_channel("bad")


def test_list_tags(api):
    assert server.list_tags("f1") == [{"id": "t1", "name": "bug"}, {"id": "t2", "name": "done"}]


def test_read_channel_oldest_first(api):
    api.get("/channels/c1/messages").respond(json=[{**MSG, "id": "m2"}, MSG])
    ids = [m["id"] for m in server.read_channel("c1")]
    assert ids == ["m1", "m2"]


def test_read_post_resolves_tag_names(api):
    api.get("/channels/p1/messages").respond(json=[MSG])
    post = server.read_post("p1")
    assert post["tags"] == ["bug"]
    assert post["messages"][0]["attachments"] == ["https://x/a.png"]


def test_list_posts_paginates_archived(api):
    api.get("/guilds/g1/threads/active").respond(
        json={"threads": [THREAD, {**THREAD, "parent_id": "other"}]}
    )
    archived = api.get("/channels/f1/threads/archived/public")
    archived.side_effect = [
        Response(
            200,
            json={
                "threads": [
                    {
                        **THREAD,
                        "id": "a1",
                        "thread_metadata": {"archived": True, "archive_timestamp": "ts1"},
                    }
                ],
                "has_more": True,
            },
        ),
        Response(200, json={"threads": [{**THREAD, "id": "a2"}], "has_more": False}),
    ]
    ids = [p["id"] for p in server.list_posts("f1", include_archived=True)]
    assert ids == ["p1", "a1", "a2"]
    assert archived.calls[1].request.url.params["before"] == "ts1"


def test_create_post_with_tag_name(api):
    route = api.post("/channels/f1/threads").respond(json=THREAD)
    server.create_post("f1", "Post", "body", tags=["bug"])
    assert (
        route.calls.last.request.content
        == b'{"name":"Post","message":{"content":"body"},"applied_tags":["t1"]}'
    )


def test_unknown_tag_raises(api):
    with pytest.raises(ToolError, match="Unknown tag: nope"):
        server.create_post("f1", "Post", "body", tags=["nope"])


@pytest.mark.parametrize(
    ("fn", "tags", "expected"),
    [
        (server.set_tags, ["done"], ["t2"]),
        (server.add_tags, ["done", "t1"], ["t1", "t2"]),
        (server.remove_tags, ["bug"], []),
    ],
)
def test_tag_updates(api, fn, tags, expected):
    route = api.patch("/channels/p1").respond(json=THREAD)
    fn("p1", tags)
    assert (
        route.calls.last.request.content
        == b'{"applied_tags":' + str(expected).replace("'", '"').replace(" ", "").encode() + b"}"
    )


def test_reply_post(api):
    route = api.post("/channels/p1/messages").respond(json=MSG)
    assert server.reply_post("p1", "hi")["author"] == "alice"
    assert route.calls.last.request.content == b'{"content":"hi"}'


def test_close_post_locks(api):
    route = api.patch("/channels/p1").respond(
        json={**THREAD, "thread_metadata": {"archived": True, "locked": True}}
    )
    assert server.close_post("p1", lock=True)["locked"] is True
    assert route.calls.last.request.content == b'{"archived":true,"locked":true}'
