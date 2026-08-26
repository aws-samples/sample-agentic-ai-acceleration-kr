"""Mock vLLM — OpenAI-compatible mock server for local development."""
from __future__ import annotations

import asyncio
import json
import time
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="Mock vLLM")

MOCK_MODELS = [
    {"id": "meta-llama/Llama-3.1-70B-Instruct", "object": "model", "created": 1700000000, "owned_by": "mock"},
    {"id": "mistral-7b", "object": "model", "created": 1700000000, "owned_by": "mock"},
]


@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": MOCK_MODELS}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    is_stream = body.get("stream", False)
    model = body.get("model", "mock-model")
    messages = body.get("messages", [])
    last_user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "Hello")

    mock_response = f"[Mock Response] You said: {last_user_msg}"
    prompt_tokens = max(1, sum(len(str(m.get("content", ""))) for m in messages) // 4)
    completion_tokens = len(mock_response.split())

    if is_stream:
        return StreamingResponse(
            _stream_chunks(model, mock_response, prompt_tokens, completion_tokens),
            media_type="text/event-stream",
        )

    return JSONResponse({
        "id": f"chatcmpl-{uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": mock_response}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens},
    })


@app.post("/v1/completions")
async def completions(request: Request):
    body = await request.json()
    model = body.get("model", "mock-model")
    prompt = body.get("prompt", "")
    prompt_tokens = max(1, len(str(prompt)) // 4)
    completion_tokens = 10

    return JSONResponse({
        "id": f"cmpl-{uuid4().hex[:8]}",
        "object": "text_completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"text": "[Mock Completion]", "index": 0, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens},
    })


async def _stream_chunks(model: str, content: str, prompt_tokens: int, completion_tokens: int):
    words = content.split()
    for i, word in enumerate(words):
        chunk = {
            "id": f"chatcmpl-{uuid4().hex[:8]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {"content": word + " "}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk)}\n\n"
        await asyncio.sleep(0.01)

    # 마지막 chunk with usage
    final = {
        "id": f"chatcmpl-{uuid4().hex[:8]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"
