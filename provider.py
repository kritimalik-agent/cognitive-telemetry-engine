import abc
import os

import numpy as np
from dotenv import load_dotenv


class LLMProvider(abc.ABC):
    """The contract every LLM backend must fulfill. Nothing else in the project touches SDK code."""

    # Shared NLI cross-encoder — loaded once on first use, reused by all provider instances.
    _nli_model = None

    @abc.abstractmethod
    def complete(self, messages: list[dict]) -> str:
        """Send a list of chat messages, get the model's reply as a plain string."""

    def nli_contradiction_score(self, premise: str, hypothesis: str) -> float:
        # NLI cross-encoder: jointly encodes both texts and returns P(contradiction).
        # This is categorically stronger than cosine similarity — it detects logical conflict,
        # not just vector distance. Threshold 0.30 is the validated cutoff.
        if LLMProvider._nli_model is None:
            # TF backend must be set before the first sentence_transformers import.
            os.environ.setdefault("SENTENCE_TRANSFORMERS_BACKEND", "tensorflow")
            from sentence_transformers import CrossEncoder
            # ~60MB download on first run, cached to ~/.cache/huggingface afterward.
            LLMProvider._nli_model = CrossEncoder("cross-encoder/nli-deberta-v3-small")
        raw_logits = LLMProvider._nli_model.predict([(premise, hypothesis)])
        # Output shape (1, 3): logits for [contradiction, entailment, neutral] in that order.
        logits = raw_logits[0]
        exp_logits = np.exp(logits - np.max(logits))
        probs = exp_logits / exp_logits.sum()
        return float(probs[0])  # contradiction probability: 0 = aligned, 1 = contradicts intent


class AnthropicProvider(LLMProvider):
    """Concrete Anthropic backend. The only file that imports the anthropic SDK."""

    def __init__(self, model_name: str) -> None:
        load_dotenv()
        import anthropic

        self._model_name = model_name
        self._client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    def complete(self, messages: list[dict]) -> str:
        # Standard chat completion — returns the model's first text block.
        response = self._client.messages.create(
            model=self._model_name,
            max_tokens=4096,
            messages=messages,
        )
        return response.content[0].text


class GeminiProvider(LLMProvider):
    """Concrete Google Gemini backend. Swap in by setting LLM_PROVIDER=gemini in .env."""

    def __init__(self, model_name: str) -> None:
        load_dotenv()
        from google import genai

        self._model_name = model_name
        # genai.Client reads GEMINI_API_KEY from the environment automatically.
        self._client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    def complete(self, messages: list[dict]) -> str:
        from google.genai import types

        # Gemini calls the model's turn "model", not "assistant" — translate on the way in.
        contents = [
            types.Content(
                role="model" if msg["role"] == "assistant" else msg["role"],
                parts=[types.Part.from_text(text=msg["content"])],
            )
            for msg in messages
        ]
        response = self._client.models.generate_content(
            model=self._model_name,
            contents=contents,
        )
        return response.text


def build_provider_from_env() -> LLMProvider:
    """Factory that reads LLM_PROVIDER and LLM_MODEL from env and returns the right backend."""
    load_dotenv()
    provider_name = os.environ.get("LLM_PROVIDER", "anthropic").lower()
    model_name = os.environ.get("LLM_MODEL", "claude-opus-4-8")

    if provider_name == "anthropic":
        return AnthropicProvider(model_name=model_name)

    if provider_name == "gemini":
        return GeminiProvider(model_name=model_name)

    raise ValueError(
        f"Unknown LLM_PROVIDER '{provider_name}'. "
        "Add a new concrete class in provider.py and register it here."
    )
