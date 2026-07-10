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

    # Database
    db_url: str = "postgresql+asyncpg://notification_worker_user:notification_worker_password_change_me@postgres:5432/gateway"
    db_pool_size: int = 5
    db_pool_overflow: int = 3
    db_ssl_mode: str = "disable"
    # RDS Proxy 경유 시 0 으로 설정 (PostgreSQL pinning 회피). Aurora 직접 연결 시엔 기본값 유지.
    db_statement_cache_size: int = 100

    # Redis
    redis_url: str = "redis://redis:6379/0"
    redis_tls_enabled: bool = False

    # Email sender
    email_sender_type: str = "mock"  # mock | ses | smtp | internal_api
    email_sender_address: str = "noreply@llm-gateway.local"
    email_sender_name: str = "LLM Gateway"

    # SES (optional)
    aws_ses_region: str | None = None

    # SMTP (optional)
    smtp_host: str | None = None
    smtp_port: int | None = None

    # Internal API (optional)
    email_api_url: str | None = None

    # Reliability
    notification_buffer_max: int = 1_000
    health_check_interval: int = 60  # seconds

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"  # json | console

    # OTel
    otel_exporter_otlp_endpoint: str = "http://otel-collector:4317"
    otel_service_name: str = "notification-worker"
    otel_traces_sampler_arg: float = 1.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
