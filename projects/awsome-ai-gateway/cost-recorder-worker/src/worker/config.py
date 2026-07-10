# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database — gateway-proxy와 동일 schema, 쓰기 가능한 사용자 필요
    db_url: str = "postgresql+asyncpg://gateway:gateway_dev_password@postgres:5432/gateway"
    db_pool_size: int = 10
    db_pool_overflow: int = 5
    db_ssl_mode: str = "disable"
    # RDS Proxy 경유 시 0 으로 설정 (PostgreSQL pinning 회피). Aurora 직접 연결 시엔 기본값 유지.
    db_statement_cache_size: int = 100

    # Redis
    redis_url: str = "redis://redis:6379/0"
    redis_tls_enabled: bool = False

    # Cost stream configuration — gateway-proxy `services/cost_recorder.py` 와 일치해야 함
    cost_stream_key: str = "cost:stream"
    cost_stream_group: str = "cost-recorder-workers"
    cost_stream_consumer: str = "worker-1"  # replica 구분 (prod에서 hostname 기반 권장)

    # Batch flush: 두 조건 중 먼저 도달한 것으로 flush
    batch_max_size: int = 100  # entries
    batch_max_interval_sec: float = 5.0
    xread_block_ms: int = 5_000  # XREADGROUP BLOCK 대기

    # Daily aggregator cron (KST). 기본 매일 00:10.
    daily_usage_agg_cron: str = "10 0 * * *"

    # Graceful shutdown
    shutdown_grace_period_sec: float = 30.0

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"  # json | console

    # OTel
    otel_exporter_otlp_endpoint: str = "http://otel-collector:4317"
    otel_service_name: str = "cost-recorder-worker"
    otel_traces_sampler_arg: float = 1.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
