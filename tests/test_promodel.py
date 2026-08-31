"""Pro-as-a-model: question building, completion flow, streaming."""

import asyncio
import json
import threading
import time

from codex_bridge import advice, promodel


def test_build_question_keeps_head_and_tail():
    msgs = [
        {"role": "system", "content": "You are advising a Codex run."},
        {"role": "user", "content": "x" * (advice.MAX_QUESTION_BYTES * 2)},
        {"role": "user", "content": "THE ACTUAL QUESTION"},
    ]
    q = promodel.build_question(msgs)
    assert len(q.encode()) <= advice.MAX_QUESTION_BYTES
    assert "You are advising" in q
    assert "THE ACTUAL QUESTION" in q
    assert "middle truncated" in q
    assert q.startswith("MODEL-CALL")


def test_build_question_flattens_multimodal():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "part one"},
        {"type": "image_url", "image_url": {"url": "ignored"}},
        {"type": "text", "text": "part two"},
    ]}]
    q = promodel.build_question(msgs)
    assert "part one part two" in q


def _answer_soon(prefix: str, answer: str):
    def run():
        for _ in range(100):
            pending = advice.list_pending()["pending"]
            hit = [p for p in pending if p["id"].startswith(prefix)]
            if hit:
                advice.respond(hit[0]["id"], answer)
                return
            time.sleep(0.05)
    t = threading.Thread(target=run)
    t.start()
    return t


def test_completion_round_trip(bridge_home, monkeypatch):
    monkeypatch.setattr(promodel, "WAIT_SECONDS", 10.0)
    monkeypatch.setattr(promodel, "POLL_SECONDS", 0.05)
    t = _answer_soon("model-", "Pick B, and verify the index shape.")
    out = asyncio.run(promodel.completion(
        {"model": "chatgpt-pro",
         "messages": [{"role": "user", "content": "A or B?"}]}))
    t.join()
    assert out["choices"][0]["message"]["content"] == (
        "Pick B, and verify the index shape.")
    assert out["choices"][0]["finish_reason"] == "stop"


def test_stream_emits_keepalives_then_answer(bridge_home, monkeypatch):
    monkeypatch.setattr(promodel, "WAIT_SECONDS", 10.0)
    monkeypatch.setattr(promodel, "KEEPALIVE_SECONDS", 0.05)
    t = _answer_soon("model-", "streamed verdict")

    async def collect():
        return [c async for c in promodel.stream_completion(
            {"model": "chatgpt-pro", "stream": True,
             "messages": [{"role": "user", "content": "?"}]})]

    chunks = asyncio.run(collect())
    t.join()
    assert chunks[-1] == "data: [DONE]\n\n"
    payloads = [json.loads(c[6:]) for c in chunks[:-1]]
    contents = [p["choices"][0]["delta"].get("content") for p in payloads]
    assert "streamed verdict" in contents
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"


def test_stream_times_out_gracefully(bridge_home, monkeypatch):
    monkeypatch.setattr(promodel, "WAIT_SECONDS", 0.2)
    monkeypatch.setattr(promodel, "KEEPALIVE_SECONDS", 0.05)

    async def collect():
        return [c async for c in promodel.stream_completion(
            {"messages": [{"role": "user", "content": "?"}]})]

    chunks = asyncio.run(collect())
    assert chunks[-1] == "data: [DONE]\n\n"
    final = json.loads(chunks[-2][6:])
    assert final["choices"][0]["finish_reason"] == "stop"
    joined = "".join(json.loads(c[6:])["choices"][0]["delta"].get("content") or ""
                     for c in chunks[:-1])
    assert "no answer within the wait window" in joined


def test_responses_stream_shape(bridge_home, monkeypatch):
    monkeypatch.setattr(promodel, "WAIT_SECONDS", 10.0)
    monkeypatch.setattr(promodel, "KEEPALIVE_SECONDS", 0.05)
    t = _answer_soon("model-", "responses verdict")

    async def collect():
        return [c async for c in promodel.responses_stream(
            {"model": "chatgpt-pro", "instructions": "be terse",
             "input": [{"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "A or B?"}]}]})]

    chunks = asyncio.run(collect())
    t.join()
    events = []
    for c in chunks:
        lines = c.strip().splitlines()
        assert lines[0].startswith("event: ") and lines[1].startswith("data: ")
        events.append(json.loads(lines[1][6:]))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "response.created"
    assert "response.output_text.delta" in kinds
    assert kinds[-1] == "response.completed"
    final = events[-1]["response"]
    assert final["status"] == "completed"
    assert final["output"][0]["content"][0]["text"] == "responses verdict"
    # the question carried both the instructions and the user text
    delta = next(e for e in events if e["type"] == "response.output_text.delta")
    assert delta["delta"] == "responses verdict"


def test_responses_input_flattening():
    msgs = promodel._flatten_responses_input({
        "instructions": "sys",
        "input": [
            {"type": "message", "role": "user", "content": "plain string"},
            {"type": "function_call", "name": "x"},
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "earlier answer"}]},
        ]})
    assert msgs[0] == {"role": "system", "content": "sys"}
    assert {"role": "user", "content": "plain string"} in msgs
    assert {"role": "assistant", "content": "earlier answer"} in msgs


def test_question_carries_hash_and_truncation_flag():
    small = promodel.build_question([{"role": "user", "content": "short"}])
    assert "truncated: no" in small and "full-prompt-sha256:" in small
    big = promodel.build_question(
        [{"role": "user", "content": "x" * (advice.MAX_QUESTION_BYTES * 2)}])
    assert "truncated: yes" in big and "CONTEXT_INCOMPLETE" in big
    assert len(big.encode()) <= advice.MAX_QUESTION_BYTES
