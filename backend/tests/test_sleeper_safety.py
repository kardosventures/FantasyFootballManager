import ast
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.sleeper import SleeperClient


def test_sleeper_module_contains_only_get_http_calls():
    path = Path(__file__).parents[1] / "app" / "sleeper.py"
    tree = ast.parse(path.read_text())
    method_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"get", "post", "put", "patch", "delete"}
    }
    assert method_calls == {"get"}
    source = path.read_text().lower()
    assert "selenium" not in source
    assert "playwright" not in source
    assert "password" not in source


@pytest.mark.asyncio
async def test_client_rejects_non_public_base():
    with pytest.raises(ValueError):
        Settings(sleeper_base_url="https://example.com/v1")


@pytest.mark.asyncio
async def test_live_shaped_get_uses_documented_path():
    def handler(request: httpx.Request):
        assert request.method == "GET"
        assert request.url.host == "api.sleeper.app"
        return httpx.Response(200, json={"league_id": "l1"})

    client = SleeperClient(Settings(), transport=httpx.MockTransport(handler))
    try:
        response = await client.league("l1")
        assert response.payload == {"league_id": "l1"}
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_draft_picks_cache_busts_the_live_pick_stream():
    def handler(request: httpx.Request):
        assert request.method == "GET"
        assert request.url.path == "/v1/draft/d1/picks"
        assert request.url.params.get("fresh")
        return httpx.Response(200, json=[])

    client = SleeperClient(Settings(), transport=httpx.MockTransport(handler))
    try:
        response = await client.draft_picks("d1")
        assert response.payload == []
    finally:
        await client.aclose()
