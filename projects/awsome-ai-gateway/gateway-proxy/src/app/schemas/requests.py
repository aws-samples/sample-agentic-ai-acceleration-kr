# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from typing import Any, Optional

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: str
    content: str | list[dict[str, Any]]


class StreamOptions(BaseModel):
    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stop: Optional[str | list[str]] = None
    stream: bool = False
    stream_options: Optional[StreamOptions] = None
    n: int = 1
    tools: Optional[list[dict[str, Any]]] = None
    tool_choice: Optional[str | dict[str, Any]] = None

    model_config = {"extra": "allow"}


class CompletionRequest(BaseModel):
    model: str
    prompt: str | list[str]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: bool = False

    model_config = {"extra": "allow"}
