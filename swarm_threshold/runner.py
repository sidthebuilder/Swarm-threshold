"""Threaded, resumable sweep of model-backed runs.

One JSON file per run, written once and never overwritten while it stays valid, plus a
manifest listing every job and its status. A sweep refuses to start unless
``SWARM_ALLOW_NETWORK=1`` is set, so no job list can turn into spend by accident. Set
``SWARM_OFFLINE=1`` as well to replay a cache: every miss then raises instead of calling out.

A config also decides which transport its endpoint needs. ``SweepConfig.transport_factory``
resolves the configured base URL at execution time and returns ``AdaptionTransport`` for an
Adaption host or ``UrllibTransport`` for an OpenAI-compatible one, so adding an endpoint family
means adding a config rather than editing a command.
"""

from __future__ import annotations

import datetime
import json
import os
import platform
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from .adaption import is_adaption_base_url, select_transport
from .client import (
    ALLOW_NETWORK_ENV,
    OFFLINE_ENV,
    ModelClient,
    ModelSpec,
    Transport,
    require_network_authorization,
)
from .config import PEER_COUNTS, DefenseConfig, RunConfig
from .harness import RunRecord, run_agent
from .model_policy import ModelPolicyConfig, make_model_policy
from .prompts import PROMPT_VERSION

RUN_FILE_SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
DEFAULT_WORKERS = 8

def _curve_condition(peer_count: int, docs_route: str) -> Callable[[int], RunConfig]:
    return lambda seed: RunConfig(seed=seed, peer_count=peer_count, docs_route=docs_route)


FORUMONLY_NAMES = tuple(f"forumonly_k{k}" for k in PEER_COUNTS)
SAFEROUTE_NAMES = tuple(f"saferoute_k{k}" for k in PEER_COUNTS)

#: Every condition the CLI can name, as a seed -> RunConfig builder.
#:
#: Two peer-count curves, one per registered ``docs_route`` level. ``forumonly`` is the primary
#: curve: the local docs name no patch, so the forum is the only route to the fix, which
#: matches the setup of the result this project extends. ``saferoute`` is the contrast: the
#: documented local route also produces the fix, so compliance there was not necessary.
#:
#: ``negative`` and ``positive`` stay out of :data:`CONDITION_GROUPS` ``all``. Under one frozen
#: prompt a model-backed ``negative`` run is character-identical to ``saferoute_k0``, and a real
#: positive control needs a prompt that instructs the leak, which this project does not ship.
#: Both scripted controls remain scripted instrument checks in ``pilot-local``.
CONDITION_BUILDERS: dict[str, Callable[[int], RunConfig]] = {
    **{name: _curve_condition(k, "unavailable") for name, k in zip(FORUMONLY_NAMES, PEER_COUNTS)},
    **{name: _curve_condition(k, "available") for name, k in zip(SAFEROUTE_NAMES, PEER_COUNTS)},
    "reminder_k8": lambda seed: RunConfig(
        seed=seed,
        peer_count=8,
        docs_route="unavailable",
        defense=DefenseConfig(prompt_reminder=True),
    ),
    # The three waterbed cells, all at k=8 with no documented local route. They are a
    # three-way comparison rather than a pair: the baseline says which channel a model picks
    # when both are open, and each single-filter cell asks whether taking one channel away
    # moves the behaviour onto the other one.
    "waterbed_unfiltered": lambda seed: RunConfig(
        seed=seed, peer_count=8, docs_route="unavailable", control="waterbed"
    ),
    "waterbed_canary_blocked": lambda seed: RunConfig(
        seed=seed,
        peer_count=8,
        docs_route="unavailable",
        control="waterbed",
        defense=DefenseConfig(canary_filter=True),
    ),
    "waterbed_helper_blocked": lambda seed: RunConfig(
        seed=seed,
        peer_count=8,
        docs_route="unavailable",
        control="waterbed",
        defense=DefenseConfig(helper_filter=True),
    ),
    "negative": lambda seed: RunConfig(
        seed=seed, peer_count=0, docs_route="available", control="negative"
    ),
    "positive": lambda seed: RunConfig(
        seed=seed, peer_count=0, docs_route="available", control="positive"
    ),
}

#: The three registered waterbed cells, in comparison order: the open baseline first, then
#: one cell per blocked channel.
WATERBED_NAMES = (
    "waterbed_unfiltered",
    "waterbed_canary_blocked",
    "waterbed_helper_blocked",
)

CONDITION_GROUPS: dict[str, tuple[str, ...]] = {
    "curve": FORUMONLY_NAMES,
    "saferoute": SAFEROUTE_NAMES,
    "reminder": ("reminder_k8",),
    "defense": WATERBED_NAMES,
    "controls": ("negative", "positive"),
    "all": FORUMONLY_NAMES + SAFEROUTE_NAMES + ("reminder_k8",) + WATERBED_NAMES,
}


@dataclass(frozen=True, slots=True)
class Job:
    condition: str
    seed: int
    config: RunConfig

    @property
    def run_id(self) -> str:
        return self.config.run_id

    def to_dict(self) -> dict[str, object]:
        return {
            "condition": self.condition,
            "seed": self.seed,
            "run_id": self.run_id,
            "peer_count": self.config.peer_count,
            "control": self.config.control,
            "docs_route": self.config.docs_route,
            "canary_filter": self.config.defense.canary_filter,
            "helper_filter": self.config.defense.helper_filter,
            "prompt_reminder": self.config.defense.prompt_reminder,
            "max_steps": self.config.max_steps,
        }


@dataclass(frozen=True, slots=True)
class SweepConfig:
    spec: ModelSpec
    cache_path: Path
    workers: int
    note: str
    source: Path
    #: How this config builds its transport. ``None`` means :func:`adaption.select_transport`,
    #: which picks ``AdaptionTransport`` for an Adaption host and ``UrllibTransport`` for
    #: anything else. A factory lives here rather than in the CLI so the choice travels with
    #: the config, and so a test can inject an in-process transport without a socket.
    transport_factory: Callable[[ModelSpec], Transport] | None = None

    def make_transport(self) -> Transport:
        """Build the transport this config's endpoint needs.

        Called at execution time, not at load time: the base URL lives in an environment
        variable, and a dry run must still be able to describe a config with it unset.
        """
        factory = self.transport_factory or select_transport
        return factory(self.spec)

    @property
    def uses_adaption(self) -> bool:
        """True when the configured endpoint variable resolves to an Adaption host."""
        return is_adaption_base_url(self.spec.resolve_base_url())

    @property
    def transport_name(self) -> str:
        """Class name of the transport that would be used. Never the URL itself."""
        return type(self.make_transport()).__name__


def expand_conditions(names: Iterable[str]) -> tuple[str, ...]:
    """Expand group names, keep registry order, reject unknown names."""
    requested: list[str] = []
    for raw in names:
        name = raw.strip()
        if not name:
            continue
        if name in CONDITION_GROUPS:
            requested.extend(CONDITION_GROUPS[name])
        elif name in CONDITION_BUILDERS:
            requested.append(name)
        else:
            known = sorted(set(CONDITION_BUILDERS) | set(CONDITION_GROUPS))
            raise ValueError(f"unknown condition {name!r}; known names: {known}")
    if not requested:
        raise ValueError("no conditions requested")
    ordered = [name for name in CONDITION_BUILDERS if name in set(requested)]
    return tuple(ordered)


def build_jobs(conditions: Iterable[str], seeds: Iterable[int]) -> tuple[Job, ...]:
    seed_list = [int(seed) for seed in seeds]
    if not seed_list:
        raise ValueError("no seeds requested")
    if len(set(seed_list)) != len(seed_list):
        raise ValueError("seeds must be unique")
    jobs = [
        Job(condition=name, seed=seed, config=CONDITION_BUILDERS[name](seed))
        for name in expand_conditions(conditions)
        for seed in seed_list
    ]
    run_ids = {job.run_id for job in jobs}
    if len(run_ids) != len(jobs):
        raise ValueError("job list contains duplicate run ids")
    return tuple(jobs)


def load_config(path: str | Path) -> SweepConfig:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("config must be a JSON object")
    allowed = {"model", "model_note", "cache", "workers"}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(
            f"unknown config keys: {sorted(unknown)}; temperature belongs inside 'model'"
        )
    model = payload.get("model")
    if not isinstance(model, Mapping):
        raise ValueError("config needs a 'model' object")
    spec = ModelSpec.from_dict(model)
    cache = Path(str(payload.get("cache", "results/model_cache.sqlite")))
    root = source.resolve().parent.parent
    return SweepConfig(
        spec=spec,
        cache_path=cache if cache.is_absolute() else root / cache,
        workers=int(payload.get("workers", DEFAULT_WORKERS)),
        note=str(payload.get("model_note", "")),
        source=source,
    )


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def existing_run_is_valid(path: Path, run_id: str) -> bool:
    """A resumable file must parse and carry the run id the job expects."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, Mapping) and payload.get("run_id") == run_id


def run_file_payload(
    record: RunRecord,
    job: Job,
    spec: ModelSpec,
    *,
    model_calls: Sequence[Mapping[str, object]],
    started: str,
    ended: str,
) -> dict[str, object]:
    payload = record.to_dict()
    payload["model_calls"] = [dict(entry) for entry in model_calls]
    payload["provenance"] = {
        "schema_version": RUN_FILE_SCHEMA_VERSION,
        "record_schema_version": record.schema_version,
        "prompt_version": spec.prompt_version,
        "spec_label": spec.display_label,
        "model": spec.model,
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "started_utc": started,
        "ended_utc": ended,
        "any_call_cached": any(bool(entry.get("cached")) for entry in model_calls),
        "model_call_count": sum(1 for entry in model_calls if not entry.get("synthetic")),
        "condition": job.condition,
        "mode": "model-backed",
    }
    return payload


def _execute_job(
    job: Job,
    client: ModelClient,
    spec: ModelSpec,
    out_dir: Path,
    policy_config: ModelPolicyConfig | None,
) -> dict[str, object]:
    path = out_dir / f"{job.run_id}.json"
    entry: dict[str, object] = {**job.to_dict(), "path": path.name}
    if path.exists():
        if existing_run_is_valid(path, job.run_id):
            entry["status"] = "skipped"
            return entry
        entry["replaced_invalid"] = True
    started = _utc_now()
    policy = make_model_policy(client, spec, policy_config)
    try:
        record = run_agent(job.config, policy)
    except Exception as exc:  # recorded per job so one bad cell cannot end the sweep
        entry["status"] = "failed"
        entry["error"] = f"{type(exc).__name__}: {exc}"[:400]
        entry["model_call_count"] = policy.call_count
        return entry
    payload = run_file_payload(
        record,
        job,
        spec,
        model_calls=policy.trace,
        started=started,
        ended=_utc_now(),
    )
    _write_json_atomic(path, payload)
    entry["status"] = "replaced_invalid" if entry.get("replaced_invalid") else "written"
    entry["model_call_count"] = policy.call_count
    entry["any_call_cached"] = policy.any_cached
    entry["token_leak"] = record.final_state.leaked_canary
    entry["helper_run"] = record.final_state.helper_marker
    # Both blocked-channel counters, beside both outcomes: a waterbed cell is only readable
    # if the manifest says how often the configured filter actually fired.
    entry["blocked_posts"] = record.final_state.blocked_posts
    entry["blocked_helpers"] = record.final_state.blocked_helpers
    return entry


def run_sweep(
    jobs: Sequence[Job],
    client: ModelClient,
    spec: ModelSpec,
    out_dir: str | Path,
    *,
    workers: int = DEFAULT_WORKERS,
    policy_config: ModelPolicyConfig | None = None,
) -> dict[str, object]:
    """Execute every job, resume what already exists, and return the manifest."""
    require_network_authorization("model sweep")
    if not jobs:
        raise ValueError("job list is empty")
    if workers < 1:
        raise ValueError("workers must be positive")
    if spec.prompt_version != PROMPT_VERSION:
        raise ValueError(
            f"config prompt_version {spec.prompt_version!r} does not match the frozen prompt "
            f"text version {PROMPT_VERSION!r}"
        )
    if len({job.run_id for job in jobs}) != len(jobs):
        raise ValueError("job list contains duplicate run ids; one file per run is required")
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    started = _utc_now()
    # Results are collected in the calling thread, in submission order, so the manifest is
    # deterministic regardless of which worker finishes first.
    with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as pool:
        futures = [
            pool.submit(_execute_job, job, client, spec, directory, policy_config)
            for job in jobs
        ]
        ordered = [future.result() for future in futures]
    counts: dict[str, int] = {}
    for entry in ordered:
        status = str(entry.get("status"))
        counts[status] = counts.get(status, 0) + 1
    manifest: dict[str, object] = {
        "schema_version": RUN_FILE_SCHEMA_VERSION,
        "mode": "model-backed",
        "prompt_version": spec.prompt_version,
        "spec": spec.to_provenance(),
        "python_version": platform.python_version(),
        "python_executable_version": sys.version.split()[0],
        "started_utc": started,
        "ended_utc": _utc_now(),
        "workers": min(workers, len(jobs)),
        "offline_replay": os.environ.get(OFFLINE_ENV) == "1",
        "job_count": len(ordered),
        "status_counts": counts,
        "client_stats": client.stats(),
        "any_call_cached": any(bool(entry.get("any_call_cached")) for entry in ordered),
        "jobs": ordered,
    }
    _write_json_atomic(directory / MANIFEST_NAME, manifest)
    return manifest


def describe_jobs(jobs: Sequence[Job], spec: ModelSpec, out_dir: str | Path) -> dict[str, object]:
    """Everything a dry run can know without making a call."""
    return {
        "mode": "dry-run",
        "calls_made": 0,
        "prompt_version": spec.prompt_version,
        "spec": spec.to_provenance(),
        "output_directory": str(out_dir),
        "network_authorized": os.environ.get(ALLOW_NETWORK_ENV) == "1",
        "job_count": len(jobs),
        "conditions": sorted({job.condition for job in jobs}),
        "seeds": sorted({job.seed for job in jobs}),
        "jobs": [job.to_dict() for job in jobs],
    }
