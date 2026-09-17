"""Unit tests for the LLM provider wrapper and JSON schema models (run first: no workflow dep)."""

import json

import httpx
import pytest
from pydantic import ValidationError

from app.agent.llm import LLM, LLMError, mock_response
from app.agent.schemas import SCHEMAS, Draft, Memory, OutlineItem, Review, Spec
from app.config import settings

# ---------- mock provider ----------


def test_mock_mode_forced_by_fixture(mock_mode):
    assert mock_mode is settings
    assert settings.llm_mode == "mock"
    assert settings.mock is True


def test_mock_explicit_even_with_api_key(monkeypatch, mock_mode):
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")
    assert settings.mock is True


async def test_live_mode_without_key_raises_not_fallback_mock(monkeypatch):
    monkeypatch.setattr(settings, "llm_mode", "live")
    monkeypatch.setattr(settings, "llm_api_key", "")
    with pytest.raises(LLMError, match="không tự chuyển sang mock"):
        await LLM().call("analyze", {"prompt": "x"})


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    async def no_sleep(delay):
        pass

    monkeypatch.setattr("app.agent.llm.asyncio.sleep", no_sleep)


def test_mock_covers_all_tasks():
    base_ctx = {
        "prompt": "Truyện về An",
        "options": {},
        "spec": {
            "premise": "p",
            "genre": "g",
            "style": "s",
            "ending": "e",
            "chapter_count": 3,
            "target_words": 800,
        },
        "chapter": {"chapter_number": 1},
        "round": 0,
    }
    for task in SCHEMAS:
        out = mock_response(task, dict(base_ctx))
        assert isinstance(out, dict)
        assert SCHEMAS[task].model_validate(out).model_dump() == out


def test_mock_analyze_prefers_options():
    out = mock_response("analyze", {"prompt": "p", "options": {"chapter_count": 7, "target_words": 1200}})
    assert out["chapter_count"] == 7 and out["target_words"] == 1200


def test_mock_review_one_revision_per_chapter():
    for n in (1, 2, 3):
        ctx = {"chapter": {"chapter_number": n}, "round": 0, "spec": {"chapter_count": 3}}
        assert mock_response("review", ctx)["verdict"] == "revise"
        ctx["round"] = 1
        assert mock_response("review", ctx)["verdict"] == "pass"


# ---------- schema validation ----------


def test_valid_spec():
    s = Spec(premise="x", genre="g", style="s", ending="e", chapter_count=5, target_words=800)
    assert s.chapter_count == 5


@pytest.mark.parametrize("field", ["premise", "genre", "style", "ending"])
def test_spec_requires_all_fields(field):
    data = {
        "premise": "x",
        "genre": "g",
        "style": "s",
        "ending": "e",
        "chapter_count": 5,
        "target_words": 800,
    }
    data.pop(field)
    with pytest.raises(ValidationError):
        Spec(**data)


@pytest.mark.parametrize("count", [0, 31, -1])
def test_spec_chapter_count_bounds(count):
    with pytest.raises(ValidationError):
        Spec(premise="x", genre="g", style="s", ending="e", chapter_count=count, target_words=800)


@pytest.mark.parametrize("words", [199, 5001])
def test_spec_target_words_bounds(words):
    with pytest.raises(ValidationError):
        Spec(premise="x", genre="g", style="s", ending="e", chapter_count=5, target_words=words)


def test_outline_item_beats_nonempty():
    with pytest.raises(ValidationError):
        OutlineItem(chapter_number=1, title="t", summary="s", beats=[])


def test_review_invalid_verdict():
    with pytest.raises(ValidationError):
        Review(verdict="maybe", issues=[], notes="")


def test_draft_min_length():
    with pytest.raises(ValidationError):
        Draft(content="ngắn")


def test_memory_roundtrip():
    m = Memory(summary="s", facts=[], character_states={}, open_threads=[])
    assert m.model_dump()["summary"] == "s"


def test_all_tasks_have_schema():
    for t in ("analyze", "outline", "bible", "write", "revise", "review", "memory", "final_review"):
        assert t in SCHEMAS
        assert SCHEMAS[t].model_json_schema()["type"] == "object"


def test_schemas_dump_json_roundtrip():
    spec = Spec(premise="x", genre="g", style="s", ending="e", chapter_count=2, target_words=800)
    assert Spec.model_validate_json(spec.model_dump_json()) == spec


# ---------- live HTTP failure behavior (never fallback to mock) ----------


def _http_transport(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=settings.llm_base_url)


async def _run_live_llm(monkeypatch, handler, sleep_spy):
    """Patch settings for live mode + httpx client with mock transport; returns LLM result/exception."""
    monkeypatch.setattr(settings, "llm_mode", "live")
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "llm_timeout", 5)
    real_async_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(**kwargs)

    monkeypatch.setattr("app.agent.llm.httpx.AsyncClient", factory)
    return await LLM().call("analyze", {"prompt": "p", "options": {}})


async def test_live_http_500_never_falls_back_to_mock(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, json={"error": "boom"})

    with pytest.raises(LLMError, match="500"):
        await _run_live_llm(monkeypatch, handler, None)
    assert calls["n"] == 3  # retried 3x on 5xx, then hard error


async def test_live_http_401_immediate_no_retry(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401, json={"error": "unauthorized"})

    with pytest.raises(LLMError, match="401"):
        await _run_live_llm(monkeypatch, handler, None)
    assert calls["n"] == 1


async def test_live_request_error_retries_then_raises(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError, match="Không kết nối"):
        await _run_live_llm(monkeypatch, handler, None)
    assert calls["n"] == 3


async def test_live_invalid_json_retries_then_raises(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    with pytest.raises(LLMError, match="không hợp lệ"):
        await _run_live_llm(monkeypatch, handler, None)
    assert calls["n"] == 3


async def test_live_finish_reason_length(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "length", "message": {"content": json.dumps({"premise": "x"})}}]
            },
        )

    with pytest.raises(LLMError, match="token"):
        await _run_live_llm(monkeypatch, handler, None)


async def test_live_success_validates_schema(monkeypatch):
    ok = {"premise": "p", "genre": "g", "style": "s", "ending": "e", "chapter_count": 3, "target_words": 800}

    def handler(request):
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert body["model"] == settings.llm_model
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(ok)}}]}
        )

    out = await _run_live_llm(monkeypatch, handler, None)
    assert out["chapter_count"] == 3


async def test_live_strips_markdown_fence(monkeypatch):
    ok = {"premise": "p", "genre": "g", "style": "s", "ending": "e", "chapter_count": 3, "target_words": 800}
    fenced = "```json\n" + json.dumps(ok) + "\n```"

    def handler(request):
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": fenced}}]}
        )

    out = await _run_live_llm(monkeypatch, handler, None)
    assert out["premise"] == "p"


async def test_live_429_retries_then_raises(monkeypatch):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429, json={})

    with pytest.raises(LLMError, match="429"):
        await _run_live_llm(monkeypatch, handler, None)
    assert calls["n"] == 3
