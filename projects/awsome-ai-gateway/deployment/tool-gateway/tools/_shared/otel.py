"""Optional OpenTelemetry exporter for distributed tracing."""

import os
from typing import Optional

try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    OTEL_AVAILABLE = True
except ImportError:
    OTEL_AVAILABLE = False


_tracer: Optional[object] = None


def get_tracer():
    """
    Get or initialize OTEL tracer if OTEL_EXPORTER_OTLP_ENDPOINT is set.

    Returns:
        tracer object or None if OTEL is not configured
    """
    global _tracer

    if _tracer is not None:
        return _tracer

    if not OTEL_AVAILABLE:
        return None

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return None

    try:
        exporter = OTLPSpanExporter(endpoint=endpoint)
        trace_provider = TracerProvider()
        trace_provider.add_span_processor(SimpleSpanProcessor(exporter))
        trace.set_tracer_provider(trace_provider)
        _tracer = trace.get_tracer(__name__)
        return _tracer
    except Exception as e:
        print(f"Failed to initialize OTEL tracer: {e}")
        return None


def create_span(name: str):
    """
    Context manager to create an OTEL span if available.

    Args:
        name: Span name

    Usage:
        with create_span("query_search"):
            # do work
    """
    tracer = get_tracer()
    if tracer:
        return tracer.start_as_current_span(name)

    # Return a no-op context manager
    class NoOpSpan:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    return NoOpSpan()
