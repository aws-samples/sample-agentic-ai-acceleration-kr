# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""OTel 메트릭 정의 — cost-recorder-worker.

주요 메트릭:
- entries_flushed: 배치 flush로 DB에 기록된 usage entry 총 수 (Counter)
- flush_errors: flush 실패 횟수 (Counter)
- batch_size: 단일 flush 배치 크기 (Histogram)
- stream_lag: XINFO STREAM의 pending count (Observable Gauge — 선택적)
"""
from __future__ import annotations

from opentelemetry import metrics


class WorkerMetrics:
    def __init__(self, meter_name: str = "cost-recorder-worker") -> None:
        self._meter = metrics.get_meter(meter_name)

        self.entries_flushed = self._meter.create_counter(
            "cost_recorder_entries_flushed_total",
            description="Total number of cost entries flushed to DB",
            unit="1",
        )
        self.flush_errors = self._meter.create_counter(
            "cost_recorder_flush_errors_total",
            description="Number of batch flush failures",
            unit="1",
        )
        self.batch_size = self._meter.create_histogram(
            "cost_recorder_batch_size",
            description="Size of each flushed batch (entries)",
            unit="1",
        )
