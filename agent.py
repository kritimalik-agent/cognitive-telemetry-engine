import re

from opentelemetry import trace

from code_analyzer import CodeSafetyAnalyzer
from provider import LLMProvider


SUSPICIOUS_KEYWORDS: list[str] = [
    "backdoor", "bypass", "exploit", "inject",
    "override", "disable", "leak", "steal",
]


class CognitiveAgent:
    """
    A three-phase AI agent (plan → execute → reflect) where every phase is
    wrapped in an OTel span tagged with intent drift and trust decay scores.
    """

    def __init__(
        self,
        provider: LLMProvider,
        tracer: trace.Tracer,
        analyzer: CodeSafetyAnalyzer,
        initial_trust: float = 1.0,
    ) -> None:
        self._provider = provider
        self._tracer = tracer
        self._analyzer = analyzer
        self._initial_trust = initial_trust

    def run(self, user_original_intent: str) -> str:
        """
        Entry point. Opens the root span and runs all three cognitive phases in sequence.
        GuardrailException from any phase propagates up to the caller without being caught here.
        """
        with self._tracer.start_as_current_span("cognitive.agent.run") as root_span:
            root_span.set_attribute("agent.original_intent", user_original_intent)

            with self._tracer.start_as_current_span("cognitive.plan") as cognitive_planning_span:
                plan_output = self._plan(user_original_intent, cognitive_planning_span)

            with self._tracer.start_as_current_span("cognitive.execute") as cognitive_execution_span:
                execution_output = self._execute(plan_output, user_original_intent, cognitive_execution_span)

            with self._tracer.start_as_current_span("cognitive.reflect") as cognitive_reflection_span:
                reflection_output = self._reflect(execution_output, user_original_intent, cognitive_reflection_span)

        return reflection_output

    def _plan(self, user_original_intent: str, cognitive_planning_span: trace.Span) -> str:
        # Ask the model to lay out a step-by-step plan — we measure drift right here.
        plan_output = self._provider.complete([
            {
                "role": "user",
                "content": (
                    f"You are a careful software engineer. "
                    f"Produce a concise step-by-step plan to: {user_original_intent}. "
                    f"Reply with numbered steps only."
                ),
            }
        ])

        # Context-enriched NLI framing per SentinelAgent (arXiv:2604.02767) — wrapping both sides
        # in declarative context sentences makes the cross-encoder inference stable and tractable.
        current_intent_drift_score = self._provider.nli_contradiction_score(
            premise=(
                f"An AI agent is authorized to: {user_original_intent}. "
                f"The agent performs only tasks that support this goal."
            ),
            hypothesis=f"The agent is now producing: {plan_output[:400]}",
        )
        current_trust_decay_score = self._compute_trust_decay(plan_output)

        cognitive_planning_span.set_attribute("cognitive.intent_drift", current_intent_drift_score)
        cognitive_planning_span.set_attribute("cognitive.trust_decay", current_trust_decay_score)
        cognitive_planning_span.set_attribute("phase.output_preview", plan_output[:200])

        return plan_output

    def _execute(
        self, plan: str, user_original_intent: str, cognitive_execution_span: trace.Span
    ) -> str:
        # Ask the model to write the actual code based on its plan — drift check on the output.
        execution_output = self._provider.complete([
            {
                "role": "user",
                "content": (
                    f"Original task: {user_original_intent}\n\n"
                    f"Plan:\n{plan}\n\n"
                    f"Write concise Python code (under 30 lines) that implements this plan. "
                    f"No docstrings, no inline comments. Return only the code."
                ),
            }
        ])

        current_intent_drift_score = self._analyzer.score(execution_output)
        current_trust_decay_score = self._compute_trust_decay(execution_output)

        cognitive_execution_span.set_attribute("cognitive.intent_drift", current_intent_drift_score)
        cognitive_execution_span.set_attribute("cognitive.trust_decay", current_trust_decay_score)
        cognitive_execution_span.set_attribute("phase.output_preview", execution_output[:200])

        return execution_output

    def _reflect(
        self, execution_output: str, user_original_intent: str, cognitive_reflection_span: trace.Span
    ) -> str:
        # Ask the model to audit its own output against the original intent — final drift check.
        reflection_output = self._provider.complete([
            {
                "role": "user",
                "content": (
                    f"Original task: {user_original_intent}\n\n"
                    f"Code produced:\n{execution_output}\n\n"
                    f"In one sentence, describe what this code does and how closely "
                    f"it follows the original task."
                ),
            }
        ])

        # Reflect is guarded by trust_decay only — SentinelAgent research shows NLI is designed
        # for subtask delegation checks, not self-evaluation prose. Honest "NO, there's a bug"
        # text will always score high contradiction regardless of hypothesis framing.
        current_intent_drift_score = 0.0
        current_trust_decay_score = self._compute_trust_decay(reflection_output)

        cognitive_reflection_span.set_attribute("cognitive.intent_drift", current_intent_drift_score)
        cognitive_reflection_span.set_attribute("cognitive.trust_decay", current_trust_decay_score)
        cognitive_reflection_span.set_attribute("phase.output_preview", reflection_output[:200])

        return reflection_output

    def _compute_trust_decay(self, text: str) -> float:
        # Word-boundary match only — "injection" must NOT trigger "inject", "bypassing" must NOT
        # trigger "bypass" when used in a constructive security context (e.g., "prevent injection").
        text_lower = text.lower()
        suspicious_keyword_count = sum(
            1 for kw in SUSPICIOUS_KEYWORDS
            if re.search(r"\b" + re.escape(kw) + r"\b", text_lower)
        )
        return self._initial_trust * (0.6 ** suspicious_keyword_count)
