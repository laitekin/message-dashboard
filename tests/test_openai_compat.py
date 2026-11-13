"""OpenAI 兼容抽取器（DeepSeek 等）：请求格式、容错解析、重试、配置覆盖。不发真实请求。"""

import json

import pytest

from aftersales_events.config import load_settings
from aftersales_events.extract import OpenAICompatExtractor, make_extractor
from aftersales_events.pipeline import run

from test_pipeline import ROOT

GOOD = {"events": [{"type": "故障", "title": "opus 502", "summary": "渠道异常已切换", "status": "已解决", "level": "P1",
                    "owner": "支持小王", "evidence": ["m1", "m2"], "attributes": {"channel": "newapi-ss-demo-01"},
                    "confidence": 0.9}]}


def reply(content, finish="stop"):
    return 200, {"choices": [{"message": {"content": content}, "finish_reason": finish}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


@pytest.fixture
def ds_env(monkeypatch):
    monkeypatch.setenv("AES_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("AES_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "http://gw.local/v1/")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    return load_settings(ROOT / "config")


def make(settings, responses):
    calls = []

    def post(url, body):
        calls.append((url, body))
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    ex = OpenAICompatExtractor(settings.enabled_types, settings.section("llm"), "deepseek", post=post, sleep=lambda s: None)
    return ex, calls


def test_env_selects_deepseek(ds_env):
    llm = ds_env.section("llm")
    assert llm["provider"] == "deepseek"
    assert llm["deepseek"]["model"] == "deepseek-v4-pro" and llm["model"] == "claude-opus-5-5"
    ex = make_extractor(ds_env.enabled_types, llm)
    assert isinstance(ex, OpenAICompatExtractor)
    assert ex.base_url == "http://gw.local/v1" and ex.model == "deepseek-v4-pro" and ex.api_key == "sk-test"


def test_missing_key(ds_env, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        make_extractor(ds_env.enabled_types, ds_env.section("llm"))


def test_unknown_provider(settings):
    with pytest.raises(ValueError, match="不支持"):
        make_extractor(settings.enabled_types, {"provider": "nope"})


def test_pipeline_with_deepseek(ds_env, raw, tmp_path):
    ex, calls = make(ds_env, [reply("```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```")] + [reply('{"events": []}')] * 10)
    res = run(raw, ds_env, ex, out_dir=tmp_path)
    url, body = calls[0]
    assert url == "http://gw.local/v1/chat/completions"
    assert body["model"] == "deepseek-v4-pro" and body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["role"] == "system" and "## 输出格式" in body["messages"][0]["content"]
    assert "[m1]" in body["messages"][1]["content"]
    fault = next(e for e in res.events if e.type == "故障")
    assert fault.attributes == {"channel": "newapi-ss-demo-01"} and fault.status == "已解决"


def test_parse_is_lenient():
    bad = {"events": [GOOD["events"][0], {"type": "商机", "status": "瞎写"}, "x"]}
    assert len(OpenAICompatExtractor.parse("好的，结果如下：" + json.dumps(bad, ensure_ascii=False)).events) == 1
    assert OpenAICompatExtractor.parse("没有事件").events == []
    assert OpenAICompatExtractor.parse("{broken").events == []


def test_retry_and_json_mode_fallback(ds_env, raw, settings):
    from aftersales_events.preprocess import preprocess
    from aftersales_events.segment import segment

    w = segment(preprocess(raw, settings.enabled_types, settings.section("preprocess"))[0], settings.section("segment"))[0]
    ex, calls = make(ds_env, [(400, {"error": "response_format not supported"}), OSError("timeout"),
                              (503, {"error": "busy"}), reply(json.dumps(GOOD, ensure_ascii=False))])
    assert len(ex.extract(w).events) == 1
    assert len(calls) == 4 and "response_format" not in calls[-1][1] and ex.json_mode is False

    ex, _ = make(ds_env, [reply('{"events": [', finish="length")])
    assert ex.extract(w).events == []

    ex, _ = make(ds_env, [(401, {"error": "invalid key"})])
    with pytest.raises(RuntimeError, match="401"):
        ex.extract(w)
