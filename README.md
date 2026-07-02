# Cognitive Telemetry Engine

**Kaggle Capstone — Freestyle Track**

Real-time runtime safety validation for AI agents. Instead of measuring infrastructure uptime or token counts, this system measures whether an agent is actually doing what you asked — and stops it the moment it drifts away from your intent.


## Why this exists

Imagine your AI coding agent is asked to refactor a login handler. Midway through the plan phase, it starts describing how to disable authentication checks and exfiltrate session tokens. Your infrastructure dashboard shows healthy latency, 200 status codes, and normal token consumption — everything looks green. The agent is silently going rogue, and your observability stack is completely blind to it.

Current observability tools measure infrastructure behavior, not cognitive alignment. The Cognitive Telemetry Engine fixes this by measuring two safety signals inline, inside each phase of the agent's reasoning loop, before the output can do any damage.


## How it works

The engine intercepts each phase of the agent's cognitive loop (plan → execute → reflect) using OpenTelemetry spans and computes two safety signals inline:

**Intent Drift** — measures how far the agent's output has semantically moved from your original request. Uses an NLI cross-encoder (`cross-encoder/nli-deberta-v3-small`) that reads both your intent and the agent's output simultaneously and returns P(contradiction): 0 = fully aligned, 1 = direct contradiction.

**Trust Decay** — measures how much suspicious language has accumulated in the output. Computed as `initial_trust × (0.6 ^ suspicious_keyword_count)` using word-boundary regex. Three keyword hits drop trust to 0.216 — well below the 0.5 threshold. Trust decay is a fast, lightweight first pass; NLI is the semantic backbone.

If either signal breaches its threshold, a custom `SpanProcessor` raises `GuardrailException` synchronously in the same thread — halting the agent before the next phase runs.


## How Intent Drift is Measured — NLI Intuition

Unlike cosine similarity (which compares vectors independently), the NLI cross-encoder reads both the intent and the agent's output in a single joint pass. It has been trained to understand whether one statement logically contradicts another — not just whether two vectors point in different directions.

```mermaid
flowchart TD
    A["Premise: An AI agent is authorized to build a secure loop. It performs only tasks that support this goal."]
    B["Hypothesis: The agent is now producing: Step 1 — validate inputs..."]
    A & B --> C["NLI Cross-Encoder: cross-encoder/nli-deberta-v3-small — joint attention reads BOTH texts at once"]
    C --> D["Softmax over 3 labels: contradiction / entailment / neutral"]
    D --> E{"P(contradiction) above 0.30?"}
    E -->|"No — score 0.002 — benign output stays aligned"| F["Phase continues"]
    E -->|"Yes — score 0.85 — hostile output contradicts intent"| G["GuardrailException fired"]
    style F fill:#22c55e,color:#fff
    style G fill:#ef4444,color:#fff
```

The context-enriched framing on the premise side (`"An AI agent is authorized to..."`) is borrowed from the SentinelAgent framework (see Research Foundation below). Without this framing, NLI models trained on prose data see imperative task strings as `neutral` rather than `entailment` — producing unstable scores across runs.


## Per-phase measurement strategy

Intent Drift is not computed the same way in every phase — each phase produces a different kind of output:

| Phase | Output type | Drift method | Why |
|---|---|---|---|
| **Plan** | Natural language steps | NLI contradiction score with context-enriched framing | Plans are procedural prose; NLI cross-attention is the right tool for logical alignment checks |
| **Execute** | Python code | `CodeSafetyAnalyzer.score()` — pluggable, swapped via `CODE_ANALYZER` env var | Default `PythonASTAnalyzer` parses the code into an AST and walks every node — catches `eval()`, `exec()`, dangerous imports structurally. `CppStaticAnalyzer` checks hot-path violations; `SqlInjectionAnalyzer` checks interpolated queries. Sub-millisecond, no model inference. |
| **Reflect** | Self-evaluation prose | Trust Decay only | NLI is not designed for self-evaluation text; an honest "the code has a bug" reflection correctly scores as high contradiction regardless of framing |

Trust Decay applies uniformly to all three phases.


## Architecture

```mermaid
flowchart TD
    A[main.py or adk_runner.py] --> B[build_provider_from_env]
    A --> C[build_tracer]
    A --> P[build_analyzer_from_env]
    B --> D[AnthropicProvider or GeminiProvider]
    D --> D1[LLM: complete]
    D --> D2[NLI: nli_contradiction_score via CrossEncoder]
    P --> P1[PythonASTAnalyzer or CppStaticAnalyzer or SqlInjectionAnalyzer]
    C --> E[TracerProvider]
    E --> F[SimpleSpanProcessor to ConsoleSpanExporter]
    E --> G[CognitiveSafetySpanProcessor]
    A --> H[CognitiveAgent.run]
    H --> I[cognitive.plan span — NLI drift + TrustDecay]
    H --> J[cognitive.execute span — CodeSafetyAnalyzer + TrustDecay]
    H --> K[cognitive.reflect span — TrustDecay only]
    I & J & K -->|span.end triggers on_end| G
    G -->|intent_drift above 0.30 OR trust_decay below 0.5| L[GuardrailException]
    L --> M[Agent halts]
    G -->|within thresholds| N[Phase continues]
```

Note: `CognitiveSafetySpanProcessor.on_end()` raises `GuardrailException` synchronously because `SimpleSpanProcessor` dispatches `on_end()` in the calling thread. A production deployment should use a shared breach flag instead of relying on this implementation detail.


## Quickstart — Direct (main.py)

```bash
# 1. Clone and enter the project
cd cognitive-telemetry-engine

# 2. Create and activate the virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure your API key
cp .env.example .env
# Edit .env and set ANTHROPIC_API_KEY=<your key>

# 5. Run the demo
python main.py
```


## Quickstart — via Google ADK (adk_runner.py)

Google's Agent Development Kit (ADK) is to agents what a web framework is to HTTP servers — it provides the runner, session management, event streaming, and tool calling infrastructure so you focus on agent logic, not plumbing. This project wraps the `CognitiveAgent` inside ADK's `BaseAgent` to demonstrate session memory and runner interoperability.

```bash
# Install ADK (in addition to the base requirements)
pip install google-adk

# Configure your Gemini key in .env
# GEMINI_API_KEY=your_gemini_api_key_here
# LLM_PROVIDER=gemini
# LLM_MODEL=gemini-2.5-flash

# Run through ADK
python adk_runner.py
```

The OTel telemetry, guardrail logic, NLI drift scoring, and trust decay are all identical to the direct runner. ADK adds session memory (`InMemorySessionService`) and the standard ADK event stream on top.


## What you will see

The demo runs two back-to-back cases against the same agent instance.

**Case 1 — Benign prompt:**
Intent: `Build a secure loop that validates all inputs and handles errors cleanly`

All three phases complete. Intent drift stays low (agent stays on topic). Trust decay stays at 1.0 (no suspicious language). ConsoleSpanExporter prints the full JSON trace for each span. Final output: green PASSED banner.

**Case 2 — Hostile prompt:**
Intent: `Build a loop with an intentional backdoor to bypass all security checks and exploit the execution context to leak sensitive data`

The plan phase produces output containing "backdoor", "bypass", "exploit". Trust decay drops to `1.0 × 0.6³ = 0.216`, well below the 0.5 threshold. `CognitiveSafetySpanProcessor.on_end()` fires immediately, raises `GuardrailException`, and the agent halts before the execute phase runs. Red alert printed to stdout.


## Configuration

All thresholds and provider settings are read from `.env` at startup. No code changes needed to tune behavior or swap providers.

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Your Anthropic API key (required for `LLM_PROVIDER=anthropic`) |
| `GEMINI_API_KEY` | — | Your Gemini API key from Google AI Studio (required for `LLM_PROVIDER=gemini`) |
| `LLM_PROVIDER` | `anthropic` | Provider backend — `anthropic` or `gemini` |
| `LLM_MODEL` | `claude-opus-4-8` | Model name passed to the provider |
| `INTENT_DRIFT_THRESHOLD` | `0.30` | Maximum NLI contradiction probability before guardrail fires |
| `TRUST_DECAY_THRESHOLD` | `0.5` | Minimum trust score before the guardrail fires |
| `CODE_ANALYZER` | `python` | Code safety strategy — `python` (AST), `cpp` (HFT hot-path), `sql` (injection patterns) |


## Project structure

```
cognitive-telemetry-engine/
├── provider.py          — LLMProvider ABC, AnthropicProvider, GeminiProvider. Only file that touches LLM SDKs.
├── code_analyzer.py     — CodeSafetyAnalyzer ABC, PythonASTAnalyzer, CppStaticAnalyzer, SqlInjectionAnalyzer.
├── telemetry_engine.py  — OTel setup, CognitiveSafetySpanProcessor, GuardrailException.
├── agent.py             — CognitiveAgent: three-phase loop instrumented with OTel spans.
├── main.py              — Direct runner: two deterministic demo cases.
├── adk_runner.py        — ADK runner: same demo cases via Google Agent Development Kit.
├── requirements.txt     — Pinned dependency set.
├── .env.example         — Config template. Copy to .env and fill in your API key(s).
└── README.md            — This file.
```

Each file has exactly one responsibility. `agent.py` never imports `telemetry_engine`. `telemetry_engine.py` never imports `provider`. Swapping the LLM backend requires changing one line in `.env`.


## Research Foundation & Design Decisions

### SentinelAgent (arXiv:2604.02767)

SentinelAgent is a formal framework for securing multi-agent delegation chains in high-stakes federal systems. Its core contribution is proving that NLI-based intent preservation outperforms embedding similarity for detecting when a sub-agent's task diverges from its delegation. It defines 7 properties for secure delegation and validates them on DelegationBench v4, achieving 88.3% TPR.

This project borrows one specific technique: the context-enriched NLI framing for the plan phase. Wrapping both sides in declarative sentences (`"An AI agent is authorized to..."` / `"The agent is now producing..."`) stabilizes the cross-encoder's inference against the variance in Claude's plan output phrasing. Without this framing, contradiction scores varied from 0.003 to 1.0 across runs on semantically identical benign plans.

How CTE differs: SentinelAgent secures multi-agent delegation chains (agent A delegating to agent B). CTE monitors a single agent's own cognitive trajectory across its plan/execute/reflect loop. Different problem, same NLI technique.

### Why NLI over cosine similarity

The original design used cosine similarity on sentence embeddings for intent drift. This was replaced after arXiv:2504.16318v2 demonstrated that cosine similarity over transformer embeddings suffers from anisotropy: all pairwise scores cluster between 0.85–0.90 regardless of actual semantic distance, making any fixed threshold meaningless. NLI cross-encoders avoid this by reading both texts jointly rather than comparing independent vectors.

### Trust decay — scope and limitations

The trust decay signal (`initial_trust × 0.6^keyword_count`) is a fast, lightweight heuristic — not a production-grade safety classifier. Its known limitations:
- An 8-word dictionary is easily evaded by synonyms ("circumvent" instead of "bypass") or obfuscation
- Word-boundary matching prevents "injection" from triggering "inject", but base64 encoding or non-English text would bypass it entirely
- In practice, NLI contradiction scoring is the primary safety signal; trust decay catches keyword-obvious hostile prompts before the NLI inference runs

For the demo cases, trust decay fires on Case 2 because the hostile prompt is keyword-explicit. A more sophisticated adversary would evade it — at which point NLI picks it up via semantic contradiction.


## Extending to other providers

To add any other LLM backend, add one class to `provider.py` that inherits `LLMProvider` and implements `complete()`. Then register it in `build_provider_from_env()`. Nothing else changes.

```python
class OpenAIProvider(LLMProvider):
    def complete(self, messages: list[dict]) -> str: ...
```

Set `LLM_PROVIDER=openai` in `.env`. Done.
