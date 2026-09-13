"""Experimental Tinker policy for speculative placement proposals."""

from __future__ import annotations

import importlib
from typing import Any

from .agent import PlacementPolicyError

__all__ = ["TinkerPlacementModel"]


def _load_tinker() -> Any:
    try:
        return importlib.import_module("tinker")
    except ImportError as exc:
        raise RuntimeError(
            "Tinker policy requires the optional 'training' dependency"
        ) from exc


class TinkerPlacementModel:
    """Adapt a Tinker sampler checkpoint to the placement TextModel protocol."""

    proposer_name = "qwen-tinker"

    def __init__(
        self,
        *,
        model_path: str | None = None,
        base_model: str | None = None,
        service_client: Any | None = None,
        tinker_module: Any | None = None,
    ) -> None:
        if not model_path and not base_model:
            raise ValueError("model_path or base_model is required")
        self._tinker = tinker_module or _load_tinker()
        service = service_client or self._tinker.ServiceClient()
        self._sampling = service.create_sampling_client(
            model_path=model_path,
            base_model=base_model,
        )
        self._tokenizer = self._sampling.get_tokenizer()

    def _render(self, prompt: str, system: str | None) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        apply_template = getattr(self._tokenizer, "apply_chat_template", None)
        if apply_template is None:
            prefix = f"SYSTEM\n{system}\n\n" if system else ""
            return f"{prefix}USER\n{prompt}\n\nASSISTANT\n"
        kwargs = {"tokenize": False, "add_generation_prompt": True}
        try:
            return apply_template(messages, enable_thinking=False, **kwargs)
        except TypeError:
            return apply_template(messages, **kwargs)

    def generate(
        self,
        prompt: str,
        *,
        documents=None,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 8192,
    ) -> str:
        del documents
        rendered = self._render(prompt, system)
        token_ids = self._tokenizer.encode(rendered)
        model_input = self._tinker.types.ModelInput.from_ints(token_ids)
        params = self._tinker.types.SamplingParams(
            max_tokens=max_output_tokens,
            temperature=max(temperature, 0.0),
            stop=["\n\n"],
            seed=0,
        )
        try:
            response = self._sampling.sample(
                prompt=model_input,
                sampling_params=params,
                num_samples=1,
            ).result()
            if not response.sequences:
                raise PlacementPolicyError("Tinker returned no sequences")
            return self._tokenizer.decode(
                response.sequences[0].tokens,
                skip_special_tokens=True,
            ).strip()
        except PlacementPolicyError:
            raise
        except Exception as exc:
            raise PlacementPolicyError("Tinker placement request failed") from exc
