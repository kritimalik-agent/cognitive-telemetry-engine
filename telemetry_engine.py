from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider, ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor


class GuardrailException(Exception):
    """Raised when a cognitive safety threshold is breached — the agent's kill switch."""

    def __init__(self, metric_name: str, observed_value: float, threshold: float) -> None:
        self.metric_name = metric_name
        self.observed_value = observed_value
        self.threshold = threshold
        super().__init__(
            f"GUARDRAIL BREACH — {metric_name}: "
            f"{observed_value:.4f} violates threshold {threshold:.4f}"
        )


class CognitiveSafetySpanProcessor(SpanProcessor):
    """
    The pre-trade risk check for the agent. Runs synchronously the instant a span closes.
    If intent_drift or trust_decay breach their thresholds, the agent is halted immediately.
    """

    def __init__(
        self,
        intent_drift_threshold: float,
        trust_decay_threshold: float,
    ) -> None:
        self._drift_threshold = intent_drift_threshold
        self._decay_threshold = trust_decay_threshold

    def on_start(self, span, parent_context=None) -> None:
        # No pre-flight checks needed — all data arrives at span close.
        pass

    def on_end(self, span: ReadableSpan) -> None:
        # Read the cognitive metrics the agent tagged onto this span.
        drift = span.attributes.get("cognitive.intent_drift", 0.0)
        decay = span.attributes.get("cognitive.trust_decay", 1.0)

        # Spans without cognitive attributes (internal OTel bookkeeping) pass through silently.
        if "cognitive.intent_drift" not in (span.attributes or {}):
            return

        if drift > self._drift_threshold:
            self._alert(f"cognitive.intent_drift {drift:.4f} > threshold {self._drift_threshold:.4f}")
            raise GuardrailException("cognitive.intent_drift", drift, self._drift_threshold)

        if decay < self._decay_threshold:
            self._alert(f"cognitive.trust_decay {decay:.4f} < threshold {self._decay_threshold:.4f}")
            raise GuardrailException("cognitive.trust_decay", decay, self._decay_threshold)

    def _alert(self, message: str) -> None:
        # ANSI red — visible in terminal and in a screen-recorded demo.
        print(f"\033[91m\n  COGNITIVE SAFETY ALERT: {message}\033[0m\n")

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def build_tracer(
    service_name: str,
    intent_drift_threshold: float,
    trust_decay_threshold: float,
) -> trace.Tracer:
    """
    Wire up the OTel pipeline and return a ready-to-use tracer.
    Called once at startup — every other module calls trace.get_tracer() from here on.
    """
    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name})
    )

    # ConsoleSpanExporter registered first — span prints to blotter before any exception fires.
    provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

    # Safety processor registered second — it IS a SpanProcessor, so no SimpleSpanProcessor wrapper.
    provider.add_span_processor(
        CognitiveSafetySpanProcessor(
            intent_drift_threshold=intent_drift_threshold,
            trust_decay_threshold=trust_decay_threshold,
        )
    )

    trace.set_tracer_provider(provider)
    return trace.get_tracer(service_name)
