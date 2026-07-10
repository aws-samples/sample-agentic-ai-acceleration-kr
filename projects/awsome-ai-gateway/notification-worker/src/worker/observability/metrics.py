# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from collections.abc import Callable

from opentelemetry.metrics import Counter, Histogram, ObservableGauge

from worker.observability._setup import get_meter


class WorkerMetrics:
    """Notification Worker 커스텀 메트릭 (OBS-02).

    8개 지표:
    - events_received_total      Counter   (channel, event_type)
    - events_processed_total     Counter   (event_type, status: success/failed/skipped)
    - event_processing_duration  Histogram (event_type)
    - emails_sent_total          Counter   (event_type, status: sent/failed)
    - retry_total                Counter   (event_type)
    - errors_total               Counter   (error_type: parse/db/email/redis)
    - db_buffer_size             ObservableGauge
    - uptime_seconds             ObservableGauge
    """

    def __init__(self) -> None:
        meter = get_meter("notification-worker")

        self.events_received_total: Counter = meter.create_counter(
            "worker_events_received_total",
            description="Total Pub/Sub events received (labels: channel, event_type)",
        )
        self.events_processed_total: Counter = meter.create_counter(
            "worker_events_processed_total",
            description="Total events fully processed (labels: event_type, status)",
        )
        self.event_processing_duration: Histogram = meter.create_histogram(
            "worker_event_processing_duration_seconds",
            description="End-to-end event processing duration (labels: event_type)",
            unit="s",
        )
        self.emails_sent_total: Counter = meter.create_counter(
            "worker_emails_sent_total",
            description="Total email send attempts (labels: event_type, status)",
        )
        self.retry_total: Counter = meter.create_counter(
            "worker_retry_total",
            description="Total email send retries (labels: event_type)",
        )
        self.errors_total: Counter = meter.create_counter(
            "worker_errors_total",
            description="Total errors by category (labels: error_type)",
        )

        # Observable gauges use callbacks registered after construction
        self._db_buffer_size_callbacks: list[Callable] = []
        self._uptime_callbacks: list[Callable] = []

        self.db_buffer_size: ObservableGauge = meter.create_observable_gauge(
            "worker_db_buffer_size",
            callbacks=self._db_buffer_size_callbacks,
            description="Current size of the in-memory DB-failure event buffer",
        )
        self.uptime_seconds: ObservableGauge = meter.create_observable_gauge(
            "worker_uptime_seconds",
            callbacks=self._uptime_callbacks,
            description="Worker process uptime in seconds",
            unit="s",
        )

    def register_db_buffer_callback(self, callback: Callable) -> None:
        self._db_buffer_size_callbacks.append(callback)

    def register_uptime_callback(self, callback: Callable) -> None:
        self._uptime_callbacks.append(callback)
