# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""게이트웨이의 STS 클라이언트 — connect 3초 · read 5초 · standard 재시도 총 3회.

무엇이 문제였나
---------------
``main.py`` 가 ``boto3.client("sts", region_name=...)`` 를 ``Config`` 없이 만들었다. botocore
기본값은 connect 60초 · read 60초 · legacy 재시도 총 5회(백오프 최대 약 15초)라, 응답 없이 멈춘
STS(예: STS VPC 엔드포인트 보안 그룹 오설정)에서 AssumeRole 한 번이 약 315초를 쓴다. 이
클라이언트는 세 경로(claude-code native 의 프로필·alias 계정, runtime SigV4, Mantle)가 함께 쓰고,
요청이 그 호출을 기다린다.

고친 것: ``bedrock_account_client.STS_BOTO_CONFIG`` 와 그것으로 만드는 ``create_sts_client`` 하나.
최악 3회 × (3 + 5)초 + 백오프 3초 이내 ≈ 27초, 연결 자체가 안 되면 ≈ 12초.

값은 **실물** 클라이언트(``meta.config``)와 실물 botocore 재시도 스택(로컬 TCP 서버)으로 본다.
"""

from __future__ import annotations

import ast
import socket
import threading
import time
from pathlib import Path

import boto3
import pytest

from app.services.bedrock_account_client import create_sts_client

_SRC = Path(__file__).resolve().parents[2] / "src" / "app"
ROLE = "arn:aws:iam::444455556666:role/app-bedrock"


def test_the_sts_client_has_short_timeouts_and_three_standard_attempts():
    """main.py 가 쓰는 팩토리로 만든 **실물** 클라이언트의 ``meta.config``."""
    client = create_sts_client("ap-northeast-2")
    cfg = client.meta.config
    assert (cfg.connect_timeout, cfg.read_timeout) == (3, 5)
    assert cfg.retries == {"mode": "standard", "total_max_attempts": 3}
    assert client.meta.region_name == "ap-northeast-2"


def test_every_sts_client_on_the_request_path_is_built_with_that_config():
    """``src`` 의 모든 ``*.client("sts", ...)`` 호출은 ``config=STS_BOTO_CONFIG`` 를 넘기는
    팩토리 하나뿐이고, 앱 시작(main.py)은 그 팩토리로 STS 클라이언트를 만든다."""
    sts_calls = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "client"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "sts"
            ):
                kw = {k.arg: k.value for k in node.keywords}
                config = kw.get("config")
                sts_calls.append(
                    (path.relative_to(_SRC).as_posix(), getattr(config, "id", None))
                )
    assert sts_calls == [("services/bedrock_account_client.py", "STS_BOTO_CONFIG")], sts_calls

    main = ast.parse((_SRC / "main.py").read_text(encoding="utf-8"))
    factory_calls = [
        n
        for n in ast.walk(main)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "create_sts_client"
    ]
    assert len(factory_calls) == 1


def test_the_live_sts_client_gives_up_after_three_attempts(monkeypatch, tmp_path):
    """설정이 실제로 먹는지 — 실물 botocore 스택으로 본다. 연결을 받자마자 끊는 로컬 서버에
    AssumeRole 을 보내면 standard 재시도가 정확히 3번 연결하고 포기한다(기본값 legacy 는 5번).
    """
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    srv.settimeout(0.1)
    accepted: list[float] = []
    stop = threading.Event()

    def _serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            accepted.append(time.monotonic())
            conn.close()

    server = threading.Thread(target=_serve, daemon=True)
    server.start()
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)  # 새 기본 세션이 아래 env 를 읽게
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_ENDPOINT_URL_STS", f"http://127.0.0.1:{srv.getsockname()[1]}")
    try:
        client = create_sts_client("ap-northeast-2")
        started = time.monotonic()
        with pytest.raises(Exception, match="(?i)connection"):
            client.assume_role(RoleArn=ROLE, RoleSessionName="probe")
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        server.join(timeout=2)
        srv.close()
    assert len(accepted) == 3, f"연결 {len(accepted)}번 — standard 총 3회가 아니다"
    assert elapsed < 5, f"{elapsed:.1f}s — 백오프는 1초 + 2초 이내여야 한다"
