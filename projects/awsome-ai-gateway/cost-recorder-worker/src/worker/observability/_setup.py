# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

import structlog
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from worker.config import Settings

logger = structlog.get_logger(__name__)

_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None


def init_otel(settings: Settings) -> None:
    global _tracer_provider, _meter_provider

    resource = Resource.create({"service.name": settings.otel_service_name})

    _tracer_provider = TracerProvider(resource=resource)
    try:
        span_exporter = OTLPSpanExporter(
            endpoint=settings.otel_exporter_otlp_endpoint, insecure=True
        )
        _tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    except Exception:
        logger.warning("otel_span_exporter_unavailable")
    trace.set_tracer_provider(_tracer_provider)

    try:
        metric_exporter = OTLPMetricExporter(
            endpoint=settings.otel_exporter_otlp_endpoint, insecure=True
        )
        reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=10_000)
        _meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    except Exception:
        logger.warning("otel_metric_exporter_unavailable")
        _meter_provider = MeterProvider(resource=resource)
    metrics.set_meter_provider(_meter_provider)


async def shutdown_otel() -> None:
    global _tracer_provider, _meter_provider
    try:
        if _tracer_provider:
            _tracer_provider.shutdown()
        if _meter_provider:
            _meter_provider.shutdown()
    except Exception:
        logger.warning("otel_shutdown_error")
