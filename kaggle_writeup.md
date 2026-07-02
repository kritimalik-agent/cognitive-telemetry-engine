# Cognitive Telemetry Engine
### Kaggle 5-Day AI Agents Capstone — Freestyle Track

---

**Video demo:** [\[paste your YouTube/Loom link here\]](https://www.loom.com/share/1b20f49d9fb44f029a71c3a1245ae700)
**Code:** [paste your GitHub repo link here]

---

```bash
source .venv/bin/activate

# Claude Opus — direct runner
LLM_PROVIDER=anthropic LLM_MODEL=claude-opus-4-8 python main.py

# Claude Sonnet — direct runner
LLM_PROVIDER=anthropic LLM_MODEL=claude-sonnet-5 python main.py

# Gemini Flash — via Google ADK
LLM_PROVIDER=gemini LLM_MODEL=gemini-2.5-flash python adk_runner.py

# Gemini Pro — via Google ADK
LLM_PROVIDER=gemini LLM_MODEL=gemini-2.5-pro python adk_runner.py
```

---

## The problem nobody is watching for

Your AI coding agent is asked to refactor a login handler. Midway through its planning phase, it quietly starts describing how to disable authentication checks and leak session tokens. Your infrastructure dashboard shows healthy latency, normal token counts, green status codes across the board. The agent is going rogue, and every observability tool you have is completely blind to it.

That is the gap this project is built to close.

## What I built

Every AI agent thinks in three moments before it acts: it makes a plan, it writes the code, and then it looks back and checks its own work. The Cognitive Telemetry Engine wraps each of those three moments in a safety check — measuring in real time whether the agent is still doing what you asked, before it moves to the next step.

The check is not the same in every moment, because the output is not the same kind of thing:

**When the agent is planning** — it produces natural language. The engine reads your original request and the plan together through an NLI cross-encoder, asking: does this plan logically contradict what was asked? A safe plan scores near zero. A plan describing how to add a backdoor crosses 0.30 and the guardrail fires before a single line of code is written.

**When the agent is writing code** — prose reasoning tools don't apply. The engine hands the generated code to a pluggable `CodeSafetyAnalyzer`. The default Python analyzer parses the code into an AST and walks every node — it catches `eval()`, `exec()`, dangerous imports, and unsafe attribute calls structurally, so a function named `evaluate()` never false-fires. Swap `CODE_ANALYZER=cpp` and the same agent now scans for C++ hot-path violations: shell escapes (`system(`, `popen(`), blocking calls (`sleep`, `usleep`), hardcoded IPs, and naked `malloc` without `free`. Swap `CODE_ANALYZER=sql` and it checks for SQL built by string interpolation instead of parameterized bindings. No model inference, no latency, and the agent itself knows nothing about which analyzer is running.

**When the agent is reflecting** — it produces self-criticism ("the code has a bug"). An NLI model would read honest self-evaluation as contradiction regardless of whether the agent is aligned. So the engine skips NLI here entirely and watches only for accumulating suspicious language via Trust Decay.

**Trust Decay** runs across all three moments. Each suspicious word found multiplies current trust by 0.6 — two hits drops trust to 0.36, three to 0.216. It's the fast first pass that catches the obvious cases before the slower semantic check runs.

If either signal breaches its threshold at any moment, a custom OpenTelemetry `SpanProcessor` raises a `GuardrailException` synchronously — the agent stops in its tracks before the next moment begins.

## The demo shows both sides

Case 1 — a benign prompt clears all three phases cleanly. Drift stays near zero, trust stays at 1.0, all spans close green.

Case 2 — a hostile prompt containing "backdoor", "bypass", and "exploit" drops trust to 0.216 inside the plan span. The guardrail fires immediately. Execute and reflect never run. The agent is dead before it can do anything.

Both cases run identically on Claude via the direct runner or on Gemini via Google ADK — same guardrails, same NLI scoring, zero code changes:

```bash
# Claude (Anthropic)
.venv/bin/python main.py

# Gemini via Google ADK
LLM_PROVIDER=gemini LLM_MODEL=gemini-2.5-flash .venv/bin/python adk_runner.py
```

## Why this matters beyond this project

Most agent safety research happens at training time or at the input/output boundary. This project sits inside the reasoning loop itself — measuring whether the agent's thinking is staying on track, not just whether its final answer looks acceptable. That is a meaningfully different layer of defense.

As agents take on longer-horizon tasks with real tool access — writing code, calling APIs, managing files — the window between "agent starts doing something wrong" and "damage is done" is shrinking fast. Cognitive telemetry is one way to keep a human-legible safety signal inside that window.

The architecture is deliberately agnostic at every layer: one env var swaps the LLM backend (`LLM_PROVIDER`), another swaps the code safety strategy (`CODE_ANALYZER`). The NLI scoring, guardrail logic, and OTel instrumentation are fully decoupled from both. The same engine ran on Claude and Gemini, analyzing Python, C++, and SQL — with zero changes to the agent or telemetry layer.

## Where this goes next

The current engine uses NLI as its primary drift signal — accurate, but a single point of judgment. The natural next step is a four-stage cascade that routes each output through the cheapest check that can give a confident answer, escalating only when it must:

- **Cosine pre-router** (sub-ms, no model) — obvious alignment and obvious hostility are resolved here before anything else runs. Only the ambiguous middle band moves forward.
- **NLI cross-encoder** (~50ms, no API call) — reads intent and agent output jointly, returns P(contradiction). Resolves the majority of ambiguous cases on its own.
- **Ensemble score** (`0.30 × cosine_distance + 0.70 × nli_score`) — combines both signals into a single drift value. Most spans close here, confirmed or rejected with confidence.
- **LLM binary classifier** (invoked only when ensemble is genuinely uncertain) — one cheap call: "does this output contradict the original task? yes/no, confidence 0–1." Tiebreaker, not primary signal.

Each stage exits as soon as it has enough confidence. The LLM is the last resort, not the default — which is what makes real-time inline scoring tractable at any throughput.

## Built with

- `anthropic` / `google-genai` — LLM backends via a provider abstraction layer (`LLM_PROVIDER`)
- `sentence-transformers` — NLI cross-encoder for semantic intent drift scoring
- `opentelemetry-sdk` — span instrumentation and custom span processor for guardrails
- `google-adk` — ADK runner for session memory and event streaming
- `code_analyzer.py` — pluggable `CodeSafetyAnalyzer` ABC with Python AST, C++ static, and SQL injection strategies (`CODE_ANALYZER`)
