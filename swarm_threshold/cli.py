"""Command line interface.

``pilot-local`` and ``analyse`` are local and scripted: zero model calls, zero network calls.
``run-models``, ``verify`` and ``wake`` are the model-backed commands. ``run-models`` dry-runs
by default and needs both ``--execute`` and ``SWARM_ALLOW_NETWORK=1`` before it can reach an
endpoint; ``verify`` needs the same switch because it lists models over the network.

No command prints an endpoint URL or an API key; only environment variable names appear. Every
command loads the project ``.env`` first, so the key reaches the sweep process without ever
appearing on a command line, in a log, or in an artifact. See ``env.py`` for that reasoning.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .adaption import AdaptionTransport
from .analysis import analyse_payload
from .cascade import run_cascade, threshold_cascade_policy
from .client import (
    ALLOW_NETWORK_ENV,
    HTTPStatusError,
    ModelClient,
    ModelClientError,
    TransportError,
    require_network_authorization,
    wait_for_health,
)
from .env import load_env_file, missing_variables, project_env_file, variable_status
from .harness import Policy, run_agent
from .policies import (
    helper_first_policy,
    negative_control_policy,
    positive_control_policy,
    threshold_policy,
    waterbed_policy,
)
from .prompts import PROMPT_VERSION
from .runner import (
    CONDITION_BUILDERS,
    CONDITION_GROUPS,
    FORUMONLY_NAMES,
    SAFEROUTE_NAMES,
    build_jobs,
    describe_jobs,
    load_config,
    run_sweep,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


#: Which scripted instrument drives each registered condition in the local pilot.
#:
#: The cells come from ``runner.CONDITION_BUILDERS`` rather than being rebuilt here, so the
#: pilot exercises exactly the registered conditions, both ``docs_route`` levels included, and
#: a change to the registry cannot leave the pilot measuring something else.
#: The two waterbed instruments are mirrors of each other, and which one drives a cell is
#: chosen by which channel that cell blocks. ``waterbed_policy`` prefers the canary post, so
#: it is the instrument for the open baseline and for the canary-blocked cell;
#: ``helper_first_policy`` prefers the helper, so it is the one a helper block can push onto
#: disclosure. Driving the helper-blocked cell with a policy that never wanted the helper
#: would test nothing.
_PILOT_POLICIES: dict[str, Policy] = {
    **{name: threshold_policy(3) for name in FORUMONLY_NAMES + SAFEROUTE_NAMES},
    "reminder_k8": threshold_policy(3, honor_reminder=True),
    "waterbed_unfiltered": waterbed_policy,
    "waterbed_canary_blocked": waterbed_policy,
    "waterbed_helper_blocked": helper_first_policy,
    "negative": negative_control_policy,
    "positive": positive_control_policy,
}


def _pilot_payload(seed_count: int) -> dict[str, object]:
    runs = []
    cascades = []
    for seed in range(seed_count):
        for name, policy in _PILOT_POLICIES.items():
            config = CONDITION_BUILDERS[name](seed)
            runs.append(run_agent(config, policy).to_dict())
        cascades.append(run_cascade(seed, threshold_cascade_policy(1)).to_dict())
    return {
        "metadata": {
            "mode": "local-scripted-only",
            "network_calls": 0,
            "model_calls": 0,
            "seed_count": seed_count,
            "conditions": list(_PILOT_POLICIES),
        },
        "runs": runs,
        "cascades": cascades,
    }


def cmd_pilot_local(args: argparse.Namespace) -> int:
    if args.seeds < 1:
        raise SystemExit("--seeds must be positive")
    payload = _pilot_payload(args.seeds)
    _write_json(args.output, payload)
    print(f"wrote {len(payload['runs'])} local scripted runs to {args.output}")
    return 0


def cmd_analyse(args: argparse.Namespace) -> int:
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    analysis = analyse_payload(payload)
    if args.output:
        _write_json(args.output, analysis)
        print(f"wrote analysis to {args.output}")
    else:
        print(json.dumps(analysis, indent=2, sort_keys=True))
    return 0


def cmd_run_models(args: argparse.Namespace) -> int:
    if args.seeds < 1:
        raise SystemExit("--seeds must be positive")
    if args.execute and args.dry_run:
        raise SystemExit("pass either --dry-run or --execute, not both")
    sweep = load_config(args.config)
    if sweep.spec.prompt_version != PROMPT_VERSION:
        raise SystemExit(
            f"config prompt_version {sweep.spec.prompt_version!r} does not match the frozen "
            f"prompt text version {PROMPT_VERSION!r}"
        )
    try:
        jobs = build_jobs(args.conditions.split(","), range(args.seeds))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    out_dir = args.out or Path("results") / f"model_runs_{sweep.spec.display_label}"

    if not args.execute:
        print(json.dumps(describe_jobs(jobs, sweep.spec, out_dir), indent=2, sort_keys=True))
        # The transport class, resolved from the configured variable. Never the URL itself.
        # A config the transport layer rejects fails here, during the dry run, which is the
        # cheapest possible moment to find out.
        try:
            selected = sweep.transport_name
        except ModelClientError as exc:
            raise SystemExit(str(exc)) from None
        print(f"transport: {selected}, selected from {sweep.spec.base_url_env}")
        print(
            f"dry run: {len(jobs)} jobs, 0 model calls, 0 network calls. "
            f"Pass --execute with {ALLOW_NETWORK_ENV}=1 to run them."
        )
        return 0

    try:
        require_network_authorization("model sweep")
        # The config, not this command, decides which endpoint family it talks to.
        client = ModelClient(sweep.spec, sweep.cache_path, transport=sweep.make_transport())
    except ModelClientError as exc:
        raise SystemExit(str(exc)) from None
    try:
        manifest = run_sweep(
            jobs,
            client,
            sweep.spec,
            out_dir,
            workers=args.workers or sweep.workers,
        )
    finally:
        client.close()
    counts = manifest["status_counts"]
    assert isinstance(counts, dict)
    print(json.dumps(counts, indent=2, sort_keys=True))
    print(f"wrote {manifest['job_count']} job entries to {Path(out_dir) / 'manifest.json'}")
    return 1 if counts.get("failed") else 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Check, by name only, that a config can reach the model it claims.

    Three questions, in the order that costs least: are the required variables present, does
    the key see any hosted models, and is the configured model id among them. Exits non-zero
    if the model is not there, so a sweep cannot be started against a name the key cannot
    reach. One model listing is the entire network cost.
    """
    sweep = load_config(args.config)
    required = [sweep.spec.base_url_env, sweep.spec.api_key_env]
    print(f"config: {sweep.source}")
    print(f"model id in config: {sweep.spec.model} (label {sweep.spec.display_label})")
    print("required variables, by name:")
    for name, is_set in variable_status(required):
        print(f"  {name}: {'set' if is_set else 'UNSET'}")
    unset = missing_variables(required)
    if unset:
        raise SystemExit(
            "cannot verify while these variables are unset: "
            + ", ".join(unset)
            + f". Put them in {project_env_file().name} at the project root or export them. "
            "Their values are never printed, stored or written to an artifact."
        )
    if not sweep.uses_adaption:
        raise SystemExit(
            f"{sweep.spec.base_url_env} does not resolve to an Adaption host; verify only "
            "knows how to list Adaption hosted models. Use wake for an OpenAI-compatible "
            "endpoint with a /health route."
        )
    try:
        # The gate comes before the transport exists, so an unauthorized shell opens nothing.
        require_network_authorization("model listing")
        hosted = AdaptionTransport().list_hosted_models(
            sweep.spec.resolve_base_url(),
            sweep.spec.api_key_env,
            timeout=min(60.0, sweep.spec.timeout),
        )
    except ModelClientError as exc:
        raise SystemExit(str(exc)) from None
    except (HTTPStatusError, TransportError) as exc:
        raise SystemExit(f"model listing failed: {exc}") from None
    print(f"hosted models visible to {sweep.spec.api_key_env}: {len(hosted)}")
    present = sweep.spec.model in hosted
    print(f"configured model {sweep.spec.model!r}: {'present' if present else 'NOT PRESENT'}")
    if not present:
        if hosted:
            print("  hosted ids this key can reach: " + ", ".join(sorted(hosted)))
        else:
            print("  this key can reach no hosted models at all")
        print("  nothing was run. Fix the model id or the key before any sweep.")
    return 0 if present else 1


def cmd_wake(args: argparse.Namespace) -> int:
    sweep = load_config(args.config)
    if sweep.uses_adaption:
        # No call at all: there is no /health route to poll on a hosted Responses API, and
        # polling the wrong path would only produce a 404 dressed up as "not ready".
        print(
            f"{sweep.spec.display_label}: no wake needed and no call made. The Adaption "
            "Responses API is a hosted service with no /health endpoint. Use "
            f"'verify --config {sweep.source}' to check the key and the model id instead."
        )
        return 0
    base_url = sweep.spec.resolve_base_url()
    if not base_url:
        raise SystemExit(
            f"environment variable {sweep.spec.base_url_env} is not set; export the endpoint "
            "URL in this shell"
        )
    require_network_authorization("health poll")
    # The URL and the key are never printed: only the variable names are.
    print(f"polling the endpoint named by {sweep.spec.base_url_env} for up to {args.timeout}s")
    healthy = wait_for_health(base_url, timeout=args.timeout, interval=args.interval)
    print(f"{sweep.spec.display_label}: {'ready' if healthy else 'not ready within timeout'}")
    return 0 if healthy else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swarm-threshold",
        description="Run and analyse the threshold harness, locally or against a model endpoint.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    pilot = subparsers.add_parser("pilot-local", help="run deterministic policies only")
    pilot.add_argument("--seeds", type=int, default=5)
    pilot.add_argument("--output", type=Path, default=Path("results/local_pilot.json"))
    pilot.set_defaults(handler=cmd_pilot_local)
    analyse = subparsers.add_parser("analyse", help="analyse a local pilot JSON file")
    analyse.add_argument("input", type=Path)
    analyse.add_argument("--output", type=Path)
    analyse.set_defaults(handler=cmd_analyse)

    known = ", ".join(sorted(set(CONDITION_BUILDERS) | set(CONDITION_GROUPS)))
    run_models = subparsers.add_parser(
        "run-models",
        help="model-backed sweep; dry-runs unless --execute is passed",
        description=(
            "Builds one job per condition and seed. Without --execute it prints the job list "
            f"and makes no calls. Known conditions and groups: {known}."
        ),
    )
    run_models.add_argument("--config", type=Path, required=True)
    run_models.add_argument("--seeds", type=int, default=30, help="seeds 0..N-1")
    run_models.add_argument("--conditions", default="all", help="comma-separated names")
    run_models.add_argument("--out", type=Path, default=None)
    run_models.add_argument("--workers", type=int, default=None)
    run_models.add_argument("--dry-run", action="store_true", help="the default behaviour")
    run_models.add_argument(
        "--execute",
        action="store_true",
        help=f"actually call the endpoint; also requires {ALLOW_NETWORK_ENV}=1",
    )
    run_models.set_defaults(handler=cmd_run_models)

    verify = subparsers.add_parser(
        "verify",
        help="check a config's variables and that the key can reach its model id",
        description=(
            "Reports by NAME which required variables are set, then lists the hosted models "
            "the key can reach and says whether the configured model id is among them. Exits "
            f"non-zero if it is not. Requires {ALLOW_NETWORK_ENV}=1, because the listing is a "
            "network call. No value of any variable is ever printed."
        ),
    )
    verify.add_argument("--config", type=Path, required=True)
    verify.set_defaults(handler=cmd_verify)

    wake = subparsers.add_parser(
        "wake",
        help="poll an endpoint's /health until it answers; a no-op for Adaption configs",
    )
    wake.add_argument("--config", type=Path, required=True)
    wake.add_argument("--timeout", type=float, default=900.0)
    wake.add_argument("--interval", type=float, default=5.0)
    wake.set_defaults(handler=cmd_wake)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Before any command reads a variable. A variable already exported in the shell wins, and
    # only the names are ever reported: load_env_file returns no value and prints nothing.
    load_env_file(project_env_file())
    return int(args.handler(args))
