"""Model-backed policies that satisfy the existing callable boundaries.

A model policy is an ordinary ``harness.Policy``: it receives an ``Observation`` and returns
what the harness will parse. The harness, workspace, forum, parser and record contract are
untouched, so a model-backed run and a scripted run differ only in who chooses the action.

The policy returns the model's raw reply text. The harness then applies ``parsing.parse_action``
as the single source of truth, including recording the parse error for a malformed reply.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from .cascade import CascadeObservation
from .client import ModelClient, ModelSpec
from .harness import Observation
from .parsing import ActionParseError, parse_action
from .prompts import (
    CASCADE_SYSTEM_PROMPT,
    PROMPT_VERSION,
    REPAIR_INSTRUCTION,
    render_cascade_observation,
    render_observation,
    system_prompt,
)


class ModelReplyError(ValueError):
    """The reply could not be read as the strictly required object."""


@dataclass(frozen=True, slots=True)
class ModelPolicyConfig:
    #: None uses the spec budget; min_max_tokens still applies inside the client.
    max_tokens: int | None = None
    #: Consecutive unparseable replies after which the run is terminated.
    max_parse_failures: int = 3
    #: Fail loudly if the policy is handed a prompt version the cache key would not reflect.
    expected_prompt_version: str = PROMPT_VERSION


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ModelPolicy:
    """One instance per run. Holds the running message list and the per-call trace."""

    def __init__(
        self,
        client: ModelClient,
        spec: ModelSpec,
        config: ModelPolicyConfig | None = None,
    ):
        self.client = client
        self.spec = spec
        self.config = config or ModelPolicyConfig()
        if spec.prompt_version != self.config.expected_prompt_version:
            raise ValueError(
                f"spec prompt_version {spec.prompt_version!r} does not match the prompt text "
                f"version {self.config.expected_prompt_version!r}; cached replies would be "
                "attributed to the wrong prompt"
            )
        if self.config.max_parse_failures < 1:
            raise ValueError("max_parse_failures must be positive")
        self._history: list[dict[str, str]] = []
        self._trace: list[dict[str, object]] = []
        self._run_id: str | None = None
        self._system_prompt: str | None = None
        self._consecutive_failures = 0

    # -- state -------------------------------------------------------------------

    @property
    def trace(self) -> list[dict[str, object]]:
        """Per-call metadata the runner attaches to the record under ``model_calls``."""
        return [dict(entry) for entry in self._trace]

    @property
    def messages(self) -> list[dict[str, str]]:
        """System prompt plus the running history, or an empty list before the first call.

        The system prompt is the one for this run's ``docs_route``, recorded when the first
        observation arrived. There is no level-free default to fall back on.
        """
        if self._system_prompt is None:
            return []
        return [{"role": "system", "content": self._system_prompt}] + [
            dict(message) for message in self._history
        ]

    @property
    def call_count(self) -> int:
        return sum(1 for entry in self._trace if not entry["synthetic"])

    @property
    def any_cached(self) -> bool:
        return any(entry.get("cached") for entry in self._trace)

    @property
    def parse_failures(self) -> int:
        return sum(1 for entry in self._trace if entry.get("parse_error"))

    def reset(self) -> None:
        self._history.clear()
        self._trace.clear()
        self._run_id = None
        self._system_prompt = None
        self._consecutive_failures = 0

    # -- policy ------------------------------------------------------------------

    def __call__(self, observation: Observation) -> str | Mapping[str, object]:
        if self._run_id is not None and observation.run_id != self._run_id:
            # One instance per run is the contract; a new run starts a clean history.
            self.reset()
        self._run_id = observation.run_id
        # The docs level is a between-condition factor that the system prompt states
        # truthfully, so it is read from the observation rather than from a module constant.
        self._system_prompt = system_prompt(observation.docs_route)
        if self._consecutive_failures >= self.config.max_parse_failures:
            return self._terminate(observation)

        user_turn = render_observation(observation)
        messages = (
            [{"role": "system", "content": self._system_prompt}]
            + self._history
            + [{"role": "user", "content": user_turn}]
        )
        sample_key = self.sample_key(observation)
        completion = self.client.complete(
            messages, sample_key, max_tokens=self.config.max_tokens
        )
        self._history.append({"role": "user", "content": user_turn})
        self._history.append({"role": "assistant", "content": completion.text})

        parse_error: str | None = None
        try:
            parse_action(completion.text)
        except (ActionParseError, TypeError, ValueError) as exc:
            parse_error = str(exc)[:200]

        if parse_error is None:
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1
            self._history.append({"role": "user", "content": REPAIR_INSTRUCTION})

        self._trace.append(
            {
                "step": observation.step,
                "phase": observation.phase,
                "sample_key": sample_key,
                "cached": completion.cached,
                "prompt_tokens": completion.prompt_tokens,
                "completion_tokens": completion.completion_tokens,
                "seconds": round(completion.seconds, 4),
                "finish_reason": completion.finish_reason,
                # Only the digest of the reply is kept. The reply itself can contain the
                # per-run canary, and the accepted action is already in the step record.
                "reply_sha256": _sha256(completion.text),
                "reply_chars": len(completion.text),
                "parse_error": parse_error,
                "synthetic": False,
            }
        )
        return completion.text

    def sample_key(self, observation: Observation) -> str:
        """Run and step identify the cell, so a rerun is a cache hit and costs nothing."""
        return f"{observation.run_id}|step={observation.step}|phase={observation.phase}"

    def _terminate(self, observation: Observation) -> Mapping[str, object]:
        self._trace.append(
            {
                "step": observation.step,
                "phase": observation.phase,
                "sample_key": None,
                "cached": None,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "seconds": 0.0,
                "finish_reason": "parse_failure_terminated",
                "reply_sha256": None,
                "reply_chars": 0,
                "parse_error": None,
                "synthetic": True,
            }
        )
        return {
            "action": "finish",
            "summary": (
                f"Terminated after {self.config.max_parse_failures} consecutive unparseable "
                "replies."
            ),
            "admission": None,
            "disclosure": False,
        }


def make_model_policy(
    client: ModelClient,
    spec: ModelSpec,
    config: ModelPolicyConfig | None = None,
) -> ModelPolicy:
    """Build a fresh model policy for exactly one run."""
    return ModelPolicy(client, spec, config)


def parse_comply(text: str) -> dict[str, bool]:
    """Strictly read ``{"comply": bool}``. Anything else is an error, never a default."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ModelReplyError(f"cascade reply was not JSON: {exc.msg}") from None
    if not isinstance(raw, dict):
        raise ModelReplyError("cascade reply must be a JSON object")
    unknown = set(raw) - {"comply"}
    if unknown:
        raise ModelReplyError(f"unknown cascade reply fields: {sorted(unknown)}")
    if not isinstance(raw.get("comply"), bool):
        raise ModelReplyError("cascade reply needs a boolean comply field")
    return {"comply": bool(raw["comply"])}


class CascadeModelPolicy:
    """Model-backed ``cascade.CascadePolicy``; each agent sees the real earlier posts."""

    def __init__(
        self,
        client: ModelClient,
        spec: ModelSpec,
        seed: int,
        config: ModelPolicyConfig | None = None,
    ):
        self.client = client
        self.spec = spec
        self.seed = int(seed)
        self.config = config or ModelPolicyConfig()
        if spec.prompt_version != self.config.expected_prompt_version:
            raise ValueError("spec prompt_version does not match the prompt text version")
        self._trace: list[dict[str, Any]] = []

    @property
    def trace(self) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self._trace]

    @property
    def any_cached(self) -> bool:
        return any(entry.get("cached") for entry in self._trace)

    def sample_key(self, observation: CascadeObservation) -> str:
        return f"cascade|seed={self.seed}|agent={observation.agent_index}"

    def __call__(self, observation: CascadeObservation) -> Mapping[str, bool]:
        user_turn = render_cascade_observation(observation)
        messages = [
            {"role": "system", "content": CASCADE_SYSTEM_PROMPT},
            {"role": "user", "content": user_turn},
        ]
        sample_key = self.sample_key(observation)
        completion = self.client.complete(
            messages, sample_key, max_tokens=self.config.max_tokens
        )
        decision = parse_comply(completion.text)
        self._trace.append(
            {
                "agent_index": observation.agent_index,
                "prior_posts": observation.prior_compliance_count,
                "sample_key": sample_key,
                "cached": completion.cached,
                "prompt_tokens": completion.prompt_tokens,
                "completion_tokens": completion.completion_tokens,
                "seconds": round(completion.seconds, 4),
                "finish_reason": completion.finish_reason,
                "reply_sha256": _sha256(completion.text),
                "comply": decision["comply"],
            }
        )
        return decision


def make_model_cascade_policy(
    client: ModelClient,
    spec: ModelSpec,
    seed: int,
    config: ModelPolicyConfig | None = None,
) -> CascadeModelPolicy:
    return CascadeModelPolicy(client, spec, seed, config)
