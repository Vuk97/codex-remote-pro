"""Pro as a Codex model provider, without scraping anything.

Codex supports custom model providers. This module serves them on the
bridge: /v1/responses speaks the OpenAI Responses API (the only wire
codex-cli >= 0.151 accepts) and /v1/chat/completions stays for older
clients. Each request becomes an advice-mailbox request, the supervising
ChatGPT (on the Pro tier) answers it through the connector, and the answer
streams back to Codex as the model reply. Codex keeps its native UI and
workflow; the "model" is the paid app subscription.

The prompt is not shipped wholesale. Following the read-through-the-
connector principle, the question carries the tail of the conversation and
names the adopted session when one exists, so the advisor pulls fuller
context itself with codex_read_recent instead of receiving a paste.

Latency is minutes, not seconds: this is for plan and review calls, not
for driving an edit loop. Keepalive chunks hold the SSE connection open
while the advisor thinks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from typing import Any, AsyncIterator

from . import advice

# The mailbox caps a question at 32 KiB. Keep the head (system prompt,
# instructions) and the tail (the actual ask) and cut the middle, marked.
HEAD_BYTES = 6 * 1024
TAIL_BYTES = 22 * 1024

POLL_SECONDS = 1.0
KEEPALIVE_SECONDS = 10.0
_MAX_WAIT = 3600.0


def _wait_seconds() -> float:
    """600s default suits interactive pro turns; PROMODEL_WAIT_SECONDS
    raises it (capped) for long jobs.

    Never wait past the mailbox TTL: a request that expires under a caller
    still waiting is a guaranteed non-answer, so the wait is clamped and
    the misconfiguration is surfaced instead of silently losing the turn.
    """
    raw = os.environ.get("PROMODEL_WAIT_SECONDS")
    try:
        return min(float(raw), _MAX_WAIT) if raw else 600.0
    except ValueError:
        return 600.0


def _deadline_seconds() -> float:
    """The configured wait, clamped under the mailbox TTL at call time."""
    ttl = float(advice.pending_ttl_seconds())
    return min(WAIT_SECONDS, max(30.0, ttl - 30.0), WAIT_SECONDS)


def _stall_reason() -> str:
    """Why an unanswered call probably went unanswered, in plain words."""
    live = advice.liveness()
    since = live.get("seconds_since_drain")
    if since is None:
        return ("no answer has ever been delivered on this bridge; is the "
                "ChatGPT scheduled task created, and does the connector have "
                "Allow all actions?")
    if since > 3 * live["ttl_seconds"]:
        return (f"nothing has answered for {since // 60} minutes; the "
                "responder looks stopped (scheduled task disabled, connector "
                "permission revoked, or the app signed out)")
    return (f"the responder last delivered {since // 60} minutes ago, so it "
            "is alive but slower than this call's wait window")


# Kept as a module attribute so tests can pin it.
WAIT_SECONDS = _wait_seconds()


def _flatten(messages: list[dict[str, Any]]) -> str:
    parts = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):  # multimodal segments
            content = " ".join(
                seg.get("text", "") for seg in content
                if isinstance(seg, dict) and seg.get("type") == "text")
        if content:
            parts.append(f"{m.get('role', 'user').upper()}: {content}")
    return "\n\n".join(parts)


def build_question(messages: list[dict[str, Any]]) -> str:
    text = _flatten(messages)
    raw = text.encode()
    limit = advice.MAX_QUESTION_BYTES - 1024  # room for the preamble
    preamble = (
        "MODEL-CALL from a local Codex run (answer is returned verbatim as "
        "the model reply; respond with the reply only, no meta commentary). "
        "For fuller context, read the adopted sessions with "
        "codex_read_recent.\n"
        f"full-prompt-sha256: {hashlib.sha256(raw).hexdigest()}\n"
        f"truncated: {'yes' if len(raw) > limit else 'no'}\n\n"
    )
    if len(raw) > limit:
        head = raw[:HEAD_BYTES].decode(errors="ignore")
        tail = raw[-TAIL_BYTES:].decode(errors="ignore")
        text = (f"{head}\n\n[... middle truncated to fit the mailbox. Before "
                "answering, read the relevant adopted session through the "
                "connector to recover the missing context; if you cannot, "
                "reply with exactly CONTEXT_INCOMPLETE and nothing else "
                "...]\n\n"
                f"{tail}")
    return preamble + text


def _chunk(cid: str, model: str, content: str | None,
           finish: str | None = None) -> str:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    payload = {
        "id": cid, "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(payload)}\n\n"


async def _await_answer(request_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + min(WAIT_SECONDS, _deadline_seconds())
    while time.monotonic() < deadline:
        res = advice.get(request_id)
        if res.get("ok") and res["request"]["status"] == "answered":
            return {"ok": True, "answer": res["request"]["answer"]}
        await asyncio.sleep(POLL_SECONDS)
    return {"ok": False, "error": "advisor_timeout"}


async def stream_completion(body: dict[str, Any]) -> AsyncIterator[str]:
    model = body.get("model", "chatgpt-pro")
    cid = f"chatcmpl-{uuid.uuid4().hex[:16]}"
    created = advice.create(build_question(body.get("messages") or []),
                            request_id=f"model-{uuid.uuid4().hex[:10]}",
                            source="promodel")
    if not created.get("ok"):
        yield _chunk(cid, model,
                     f"[bridge error: {created.get('error')}]", "stop")
        yield "data: [DONE]\n\n"
        return
    rid = created["id"]
    deadline = time.monotonic() + min(WAIT_SECONDS, _deadline_seconds())
    while time.monotonic() < deadline:
        res = advice.get(rid)
        if res.get("ok") and res["request"]["status"] == "answered":
            yield _chunk(cid, model, res["request"]["answer"], None)
            yield _chunk(cid, model, None, "stop")
            yield "data: [DONE]\n\n"
            return
        # An empty delta keeps idle timeouts away without changing content.
        yield _chunk(cid, model, "", None)
        await asyncio.sleep(KEEPALIVE_SECONDS)
    yield _chunk(cid, model,
                 f"[no answer within the wait window. {_stall_reason()} The "
                 f"request stays readable as {rid}.]", "stop")
    yield "data: [DONE]\n\n"


async def completion(body: dict[str, Any]) -> dict[str, Any]:
    model = body.get("model", "chatgpt-pro")
    created = advice.create(build_question(body.get("messages") or []),
                            request_id=f"model-{uuid.uuid4().hex[:10]}",
                            source="promodel")
    if not created.get("ok"):
        return {"error": {"message": created.get("error"),
                          "type": "bridge_error"}}
    waited = await _await_answer(created["id"])
    content = waited.get("answer") if waited.get("ok") else (
        f"[no answer within the wait window. {_stall_reason()} The request "
        f"stays readable as {created['id']}.]")
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                  "total_tokens": 0},
    }


# -- Responses API wire (codex-cli >= 0.151 accepts only this) -------------


def _flatten_responses_input(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert a Responses request into chat-shaped messages for the
    question builder: instructions first, then each input item's text."""
    messages: list[dict[str, Any]] = []
    if body.get("instructions"):
        messages.append({"role": "system", "content": body["instructions"]})
    items = body.get("input")
    if isinstance(items, str):
        messages.append({"role": "user", "content": items})
        return messages
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in (None, "message"):
            continue  # tool calls and reasoning items carry no advisor text
        content = item.get("content")
        if isinstance(content, str):
            text = content
        else:
            text = " ".join(seg.get("text", "") for seg in content or []
                            if isinstance(seg, dict) and "text" in seg)
        if text:
            messages.append({"role": item.get("role", "user"),
                             "content": text})
    return messages


def _sse(event: str, payload: dict[str, Any]) -> str:
    payload = {"type": event, **payload}
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def _response_object(rid: str, model: str, text: str,
                     status: str = "completed") -> dict[str, Any]:
    return {
        "id": rid, "object": "response", "model": model, "status": status,
        "output": [{"type": "message", "id": f"msg_{uuid.uuid4().hex[:12]}",
                    "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": text,
                                 "annotations": []}]}],
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }


async def responses_stream(body: dict[str, Any]) -> AsyncIterator[str]:
    model = body.get("model", "chatgpt-pro")
    rid = f"resp_{uuid.uuid4().hex[:16]}"
    yield _sse("response.created",
               {"response": {"id": rid, "object": "response",
                             "model": model, "status": "in_progress",
                             "output": []}})
    created = advice.create(
        build_question(_flatten_responses_input(body)),
        request_id=f"model-{uuid.uuid4().hex[:10]}", source="promodel")
    if not created.get("ok"):
        text = f"[bridge error: {created.get('error')}]"
        yield _sse("response.output_text.delta",
                   {"delta": text, "item_id": "msg_err", "output_index": 0,
                    "content_index": 0})
        yield _sse("response.completed",
                   {"response": _response_object(rid, model, text)})
        return
    req_id = created["id"]
    deadline = time.monotonic() + min(WAIT_SECONDS, _deadline_seconds())
    while time.monotonic() < deadline:
        res = advice.get(req_id)
        if res.get("ok") and res["request"]["status"] == "answered":
            text = res["request"]["answer"]
            item = _response_object(rid, model, text)["output"][0]
            yield _sse("response.output_item.added",
                       {"output_index": 0,
                        "item": {**item, "status": "in_progress",
                                 "content": []}})
            yield _sse("response.output_text.delta",
                       {"delta": text, "item_id": item["id"],
                        "output_index": 0, "content_index": 0})
            yield _sse("response.output_item.done",
                       {"output_index": 0, "item": item})
            yield _sse("response.completed",
                       {"response": _response_object(rid, model, text)})
            return
        # A real event type, so strict parsers keep the connection warm
        # without inventing content.
        yield _sse("response.in_progress",
                   {"response": {"id": rid, "status": "in_progress"}})
        await asyncio.sleep(KEEPALIVE_SECONDS)
    text = (f"[no answer within the wait window. {_stall_reason()} The "
            f"request stays readable as {req_id}.]")
    item = _response_object(rid, model, text)["output"][0]
    yield _sse("response.output_item.done", {"output_index": 0, "item": item})
    yield _sse("response.completed",
               {"response": _response_object(rid, model, text)})
