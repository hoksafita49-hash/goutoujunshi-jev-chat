import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.pipeline import run_pipeline


def run(evidence, preferences=None):
    calls = []
    async def complete(alias, messages, tokens):
        calls.append(messages)
        if len(calls) == 1:
            return dict(evidence, strategy="先确认时间", stop_condition="尊重拒绝"), {}
        if len(calls) == 2:
            return {"candidates": ["First", "Second", "Third"]}, {}
        return {"order": [0]}, {}
    job = dict(analysis_id="a", conversation_uid="c", context_revision=1, kind="analysis",
        input={"messages": [{"uid": "message-one", "version": 1, "text": "周六下午可以", "role": "other"}],
               "preferences": preferences or {}})
    return asyncio.run(run_pipeline(job, complete)), calls


def test_only_exact_snapshot_quotes_receive_verified_citations():
    result, _ = run({"evidence": [
        {"kind": "fact", "text": "对方给出了时间", "source_message_uids": ["message-one", "foreign"],
         "source_quotes": [{"message_uid": "message-one", "quote": "周六下午"},
                           {"message_uid": "foreign", "quote": "foreign detail"}]},
        {"kind": "hypothesis", "text": "可能愿意见面", "source_message_uids": ["message-one"],
         "source_quotes": [{"message_uid": "message-one", "quote": "不存在的原话"}]}
    ]})
    assert result["facts"] == ["对方给出了时间"]
    assert result["evidence"][0]["citation_status"] == "verified"
    assert result["evidence"][0]["source_message_uids"] == ["message-one"]
    assert result["evidence"][0]["source_quotes"] == [{"message_uid": "message-one", "quote": "周六下午"}]
    assert result["evidence"][1]["citation_status"] == "unavailable"
    assert result["evidence"][1]["source_message_uids"] == []


def test_legacy_output_remains_readable_without_invented_source():
    result, _ = run({"facts": ["旧版事实"], "hypotheses": ["旧版推测"]})
    assert result["facts"] == ["旧版事实"]
    assert [row["citation_status"] for row in result["evidence"]] == ["unavailable", "unavailable"]
    assert all(row["source_quotes"] == [] for row in result["evidence"])


def test_preference_count_bounds_candidates_and_reaches_draft_prompt():
    result, calls = run({"facts": ["事实"]}, {"tone": "自然", "length": "简短", "count": 1, "style": "不用表情"})
    assert len(result["candidates"]) == 1
    assert "1" in calls[1][0]["content"]
    assert json.loads(calls[1][1]["content"])["context"]["preferences"]["style"] == "不用表情"
