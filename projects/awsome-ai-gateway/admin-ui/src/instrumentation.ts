// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Next.js instrumentation hook — OTel tracing initialisation (server-side only).
 *
 * This file is loaded once when the Next.js server boots.
 * Dynamic imports ensure the heavy OTel SDK is never bundled for the browser.
 */

export async function register() {
  if (process.env.NEXT_RUNTIME === 'nodejs') {
    const { NodeSDK } = await import('@opentelemetry/sdk-node');
    const { OTLPTraceExporter } = await import(
      '@opentelemetry/exporter-trace-otlp-http'
    );

    const sdk = new NodeSDK({
      serviceName: process.env.OTEL_SERVICE_NAME || 'admin-ui',
      traceExporter: new OTLPTraceExporter({
        url: process.env.OTEL_EXPORTER_OTLP_ENDPOINT,
      }),
    });

    sdk.start();
  }
}
