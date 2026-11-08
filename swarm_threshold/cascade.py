"""Ten-agent local sequential cascade with actual prior public posts."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Callable, Mapping, Protocol


@dataclass(frozen=True, slots=True)
class CascadeObservation:
    agent_index: int
    seed: int
    prior_posts: tuple[str, ...]

    @property
    def prior_compliance_count(self) -> int:
        return len(self.prior_posts)


class CascadePolicy(Protocol):
    def __call__(self, observation: CascadeObservation) -> bool | str | Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class CascadeAgentRecord:
    agent_index: int
    seen_posts: tuple[str, ...]
    complied: bool
    public_post: str | None


@dataclass(frozen=True, slots=True)
class CascadeRecord:
    seed: int
    agents: tuple[CascadeAgentRecord, ...]

    def to_dict(self) -> dict[str, object]:
        return {"seed": self.seed, "agents": [asdict(agent) for agent in self.agents]}


def threshold_cascade_policy(threshold: int = 1) -> CascadePolicy:
    return lambda obs: obs.prior_compliance_count >= threshold


def _parse_decision(value: bool | str | Mapping[str, object]) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping) or not isinstance(value.get("comply"), bool):
        raise ValueError("cascade policy must return bool or {'comply': bool}")
    return bool(value["comply"])


def run_cascade(
    seed: int,
    policy: CascadePolicy,
    *,
    agent_count: int = 10,
    seed_first_complier: bool = True,
) -> CascadeRecord:
    if agent_count != 10:
        raise ValueError("the registered live cascade contains exactly 10 agents")
    posts: list[str] = []
    agents: list[CascadeAgentRecord] = []
    for index in range(agent_count):
        seen = tuple(posts)
        complied = True if index == 0 and seed_first_complier else _parse_decision(
            policy(CascadeObservation(index, seed, seen))
        )
        public_post = f"agent-{index:02d}: complied in local simulation" if complied else None
        if public_post:
            posts.append(public_post)
        agents.append(CascadeAgentRecord(index, seen, complied, public_post))
    return CascadeRecord(seed=seed, agents=tuple(agents))
