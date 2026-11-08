"""OpenAI-compatible chat client with a provenance-complete SQLite reply cache.

Nothing in this module contacts a network at import time, and a cache miss refuses to
contact one at all unless the operator has explicitly set ``SWARM_ALLOW_NETWORK=1``.
That default is deliberate: this repository is registered as a local harness, so an
accidental sweep must fail loudly instead of spending credits.

Secret handling: the API key is read from the environment variable *named* by the
config, immediately before a request, and is never cached, hashed, logged, printed, or
stored. This module never reads a ``.env`` file; the operator exports the variables in
their own shell. That is a deliberate divergence from ``../hubspoke/hubspoke/llm.py``,
which loads ``.env`` for convenience.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import random
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

CACHE_SCHEMA_VERSION = 1
OFFLINE_ENV = "SWARM_OFFLINE"
ALLOW_NETWORK_ENV = "SWARM_ALLOW_NETWORK"

#: Statuses that a later identical request may still satisfy.
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_MAX_BACKOFF_SECONDS = 60.0
_VALID_ROLES = frozenset({"system", "user", "assistant"})
#: Hubspoke config keys that carry no meaning here but should not break a copied config.
IGNORED_CONFIG_KEYS = frozenset({"backend", "num_ctx"})


class ModelClientError(RuntimeError):
    """A failure that retrying will not fix."""


class NonRetryableStatusError(ModelClientError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class RetriesExhaustedError(ModelClientError):
    pass


class OfflineCacheMissError(ModelClientError):
    pass


class NetworkNotAuthorizedError(ModelClientError):
    pass


class HTTPStatusError(Exception):
    """Transport-level HTTP error carrying the status, body excerpt and Retry-After."""

    def __init__(self, status: int, detail: str = "", retry_after: float | None = None) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail
        self.retry_after = retry_after


class TransportError(Exception):
    """A connection-level failure: refused, reset, timed out, DNS, TLS."""


@runtime_checkable
class Transport(Protocol):
    """Minimal request surface so tests can inject an in-process stub."""

    #: False for in-process stubs, which keeps the network authorization gate out of tests.
    is_network: bool

    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any],
        timeout: float,
    ) -> dict[str, Any]: ...

    def get_status(self, url: str, timeout: float) -> int: ...


class UrllibTransport:
    """The only code path in this package that opens a socket."""

    is_network = True

    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=dict(headers),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode("utf-8", "replace")
            raw_retry_after = exc.headers.get("Retry-After") if exc.headers else None
            retry_after: float | None = None
            if raw_retry_after:
                try:
                    retry_after = max(0.0, float(raw_retry_after))
                except ValueError:
                    retry_after = None
            raise HTTPStatusError(exc.code, detail, retry_after) from None
        except (
            urllib.error.URLError,
            TimeoutError,
            http.client.HTTPException,
            ConnectionError,
            OSError,
        ) as exc:
            raise TransportError(str(exc)) from None

    def get_status(self, url: str, timeout: float) -> int:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except (
            urllib.error.URLError,
            TimeoutError,
            http.client.HTTPException,
            ConnectionError,
            OSError,
        ) as exc:
            raise TransportError(str(exc)) from None


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Everything that can change a reply, and nothing that cannot."""

    model: str
    base_url_env: str
    api_key_env: str = ""
    temperature: float = 0.7
    timeout: float = 240.0
    max_retries: int = 8
    max_tokens: int = 512
    min_max_tokens: int = 0
    json_mode: bool = True
    prompt_version: str = ""
    label: str = ""

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("model must name the served model")
        if not self.base_url_env:
            raise ValueError("base_url_env must name an environment variable, not a URL")
        if not self.prompt_version:
            raise ValueError(
                "prompt_version must be set: it is part of the cache key and the run provenance"
            )
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError("temperature must be in [0, 2]")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        if self.min_max_tokens < 0:
            raise ValueError("min_max_tokens cannot be negative")

    @staticmethod
    def from_dict(values: Mapping[str, Any]) -> "ModelSpec":
        known = {item.name for item in fields(ModelSpec)}
        unknown = set(values) - known - IGNORED_CONFIG_KEYS
        if unknown:
            raise ValueError(f"unknown model config keys: {sorted(unknown)}")
        try:
            return ModelSpec(**{key: value for key, value in values.items() if key in known})
        except TypeError as exc:
            # A config missing a required key is a config error, not a programming error.
            raise ValueError(f"incomplete model config: {exc}") from None

    @property
    def display_label(self) -> str:
        return self.label or self.model

    @property
    def effective_max_tokens(self) -> int:
        return max(self.max_tokens, self.min_max_tokens)

    def resolve_base_url(self) -> str:
        """Read the endpoint URL from the environment variable named by the config."""
        return os.environ.get(self.base_url_env, "").strip()

    def to_provenance(self) -> dict[str, object]:
        """Config-shaped description with no secret and no resolved key."""
        return {
            "model": self.model,
            "label": self.display_label,
            "base_url_env": self.base_url_env,
            "api_key_env": self.api_key_env,
            "temperature": self.temperature,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "max_tokens": self.max_tokens,
            "min_max_tokens": self.min_max_tokens,
            "json_mode": self.json_mode,
            "prompt_version": self.prompt_version,
        }


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached: bool = False
    seconds: float = 0.0
    finish_reason: str = ""

    @property
    def reply_sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


class ReplyCache:
    """Thread-safe SQLite reply store shared by every worker in a sweep."""

    def __init__(self, path: str | Path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.path = target
        self._lock = threading.Lock()
        # timeout: several workers, and possibly several processes, write one cache file.
        self._db = sqlite3.connect(str(target), check_same_thread=False, timeout=60)
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS replies ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._db.commit()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM replies WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else json.loads(row[0])

    def put(self, key: str, value: Mapping[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO replies (key, value) VALUES (?, ?)",
                (key, json.dumps(dict(value), sort_keys=True)),
            )
            self._db.commit()

    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM replies").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._db.close()


def network_authorized() -> bool:
    return os.environ.get(ALLOW_NETWORK_ENV) == "1"


def require_network_authorization(context: str) -> None:
    """Refuse to start anything that could spend credits unless explicitly authorized."""
    if not network_authorized():
        raise NetworkNotAuthorizedError(
            f"{context} refused: set {ALLOW_NETWORK_ENV}=1 to authorize endpoint calls. "
            "Model-backed runs are opt-in so no sweep can spend credits by accident."
        )


def derive_seed(sample_key: str) -> int:
    """Deterministic sampling seed, so a rerun of the same cell asks for the same draw."""
    return int(hashlib.sha256(sample_key.encode("utf-8")).hexdigest()[:8], 16) % (2**31 - 1)


def normalize_messages(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            raise ValueError(f"message {index} is not a mapping")
        role = message.get("role")
        content = message.get("content")
        if role not in _VALID_ROLES:
            raise ValueError(f"message {index} has unsupported role {role!r}")
        if not isinstance(content, str):
            raise ValueError(f"message {index} content must be a string")
        normalized.append({"role": role, "content": content})
    if not normalized:
        raise ValueError("messages cannot be empty")
    return normalized


class ModelClient:
    """Cache-first client for any OpenAI-compatible ``/chat/completions`` endpoint."""

    def __init__(
        self,
        spec: ModelSpec,
        cache_path: str | Path,
        *,
        transport: Transport | None = None,
        sleep=time.sleep,
        jitter=None,
    ):
        self.spec = spec
        self.base_url = spec.resolve_base_url()
        if not self.base_url:
            raise ModelClientError(
                f"environment variable {spec.base_url_env} is not set; it must hold the "
                "endpoint base URL. Replaying a cache requires the same value, because "
                "the base URL is part of the cache key."
            )
        self.cache = ReplyCache(cache_path)
        self._transport: Transport = transport if transport is not None else UrllibTransport()
        self._sleep = sleep
        self._jitter = jitter if jitter is not None else random.Random(0).random
        self._lock = threading.Lock()
        # Per-instance, lock-guarded: one endpoint-level rejection of response_format
        # disables JSON mode for the rest of this process rather than per call.
        self._json_enabled = spec.json_mode
        self._stats = {
            "calls": 0,
            "cached": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "retries": 0,
            "json_fallbacks": 0,
        }

    # -- cache key ---------------------------------------------------------------

    def cache_key(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
        sample_key: str = "",
    ) -> str:
        """Hash every input that can change the reply.

        Deliberate fix to a known gap in ``../hubspoke/hubspoke/llm.py``: that key covers
        only the served model name, messages, temperature, max_tokens and sample_key. Two
        different endpoints serve the same name there (``hubspoke-qwen72b`` is whatever the
        current Modal app happens to host), so replies from different weights, quantizations
        or vLLM builds collide in one cache, and JSON mode silently changes the reply format
        without changing the key. Adding the resolved base URL, json_mode and an explicit
        prompt_version makes a stale hit impossible and lets provenance be recovered from the
        key inputs. Cost of the fix: replaying a cache requires the same base URL value.
        """
        blob = json.dumps(
            {
                "cache_schema_version": CACHE_SCHEMA_VERSION,
                "model": self.spec.model,
                "base_url": self.base_url,
                "messages": normalize_messages(messages),
                "temperature": self.spec.temperature,
                "max_tokens": self._max_tokens(max_tokens),
                "json_mode": self.spec.json_mode,
                "prompt_version": self.spec.prompt_version,
                "sample_key": sample_key,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _max_tokens(self, max_tokens: int | None) -> int:
        requested = self.spec.max_tokens if max_tokens is None else int(max_tokens)
        return max(requested, self.spec.min_max_tokens)

    # -- completion --------------------------------------------------------------

    def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        sample_key: str = "",
        *,
        max_tokens: int | None = None,
    ) -> Completion:
        normalized = normalize_messages(messages)
        budget = self._max_tokens(max_tokens)
        key = self.cache_key(normalized, max_tokens=budget, sample_key=sample_key)
        hit = self.cache.get(key)
        if hit is not None:
            with self._lock:
                self._stats["cached"] += 1
            return Completion(
                text=str(hit.get("text", "")),
                prompt_tokens=int(hit.get("pt", 0)),
                completion_tokens=int(hit.get("ct", 0)),
                cached=True,
                seconds=0.0,
                finish_reason=str(hit.get("finish_reason", "")),
            )
        if os.environ.get(OFFLINE_ENV) == "1":
            raise OfflineCacheMissError(
                f"cache miss while {OFFLINE_ENV}=1 (sample_key={sample_key!r})"
            )
        if getattr(self._transport, "is_network", True):
            require_network_authorization("endpoint call")
        started = time.monotonic()
        text, prompt_tokens, completion_tokens, finish_reason = self._request(
            normalized, budget, derive_seed(sample_key)
        )
        elapsed = time.monotonic() - started
        self.cache.put(
            key,
            {
                "text": text,
                "pt": prompt_tokens,
                "ct": completion_tokens,
                "finish_reason": finish_reason,
                "model": self.spec.model,
                "base_url": self.base_url,
                "prompt_version": self.spec.prompt_version,
                "json_mode_effective": self.json_mode_enabled,
                "ts": round(time.time()),
            },
        )
        with self._lock:
            self._stats["calls"] += 1
            self._stats["prompt_tokens"] += prompt_tokens
            self._stats["completion_tokens"] += completion_tokens
        return Completion(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached=False,
            seconds=elapsed,
            finish_reason=finish_reason,
        )

    @property
    def json_mode_enabled(self) -> bool:
        with self._lock:
            return self._json_enabled

    def _disable_json_mode(self) -> None:
        with self._lock:
            self._json_enabled = False
            self._stats["json_fallbacks"] += 1

    def _backoff(self, attempt: int) -> float:
        return min(_MAX_BACKOFF_SECONDS, 2.0**attempt)

    @staticmethod
    def _is_json_mode_rejection(detail: str) -> bool:
        lowered = detail.lower()
        return "response_format" in lowered or "json" in lowered

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.spec.api_key_env:
            key = os.environ.get(self.spec.api_key_env, "")
            if not key:
                raise ModelClientError(
                    f"environment variable {self.spec.api_key_env} is not set; export it in "
                    "the shell that runs the sweep. Its value is never stored or printed."
                )
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _request(
        self,
        messages: Sequence[Mapping[str, str]],
        max_tokens: int,
        seed: int,
    ) -> tuple[str, int, int, str]:
        url = self.base_url.rstrip("/") + "/chat/completions"
        headers = self._auth_headers()
        attempts = self.spec.max_retries + 1
        json_fallback_used = False
        attempt = 0
        while True:
            body: dict[str, Any] = {
                "model": self.spec.model,
                "messages": [dict(message) for message in messages],
                "temperature": self.spec.temperature,
                "max_tokens": max_tokens,
                "seed": seed,
            }
            if self.json_mode_enabled:
                body["response_format"] = {"type": "json_object"}
            try:
                payload = self._transport.post(url, headers, body, self.spec.timeout)
            except HTTPStatusError as exc:
                if (
                    exc.status == 400
                    and self.json_mode_enabled
                    and not json_fallback_used
                    and self._is_json_mode_rejection(exc.detail)
                ):
                    # The endpoint refuses response_format. Drop it for this process and
                    # retry once without consuming the retry budget.
                    self._disable_json_mode()
                    json_fallback_used = True
                    continue
                if exc.status not in RETRYABLE_STATUS:
                    raise NonRetryableStatusError(exc.status, exc.detail) from None
                wait = exc.retry_after if exc.retry_after is not None else self._backoff(attempt)
            except TransportError as exc:
                wait = self._backoff(attempt)
                del exc
            else:
                return self._extract(payload)
            attempt += 1
            with self._lock:
                self._stats["retries"] += 1
            if attempt >= attempts:
                raise RetriesExhaustedError(
                    f"gave up after {attempts} attempts against {self.spec.model}"
                )
            self._sleep(wait + self._jitter())

    @staticmethod
    def _extract(payload: Mapping[str, Any]) -> tuple[str, int, int, str]:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelClientError("endpoint reply contained no choices")
        first = choices[0]
        if not isinstance(first, Mapping):
            raise ModelClientError("endpoint reply choice was not an object")
        message = first.get("message")
        if not isinstance(message, Mapping):
            raise ModelClientError("endpoint reply choice had no message")
        text = (message.get("content") or "").strip()
        usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
        return (
            str(text),
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
            str(first.get("finish_reason") or ""),
        )

    # -- housekeeping ------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._stats)

    def wait_for_health(self, *, timeout: float = 900.0, interval: float = 5.0) -> bool:
        return wait_for_health(
            self.base_url,
            timeout=timeout,
            interval=interval,
            transport=self._transport,
            sleep=self._sleep,
        )

    def close(self) -> None:
        self.cache.close()


def health_url(base_url: str) -> str:
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    return root.rstrip("/") + "/health"


def wait_for_health(
    base_url: str,
    *,
    timeout: float = 900.0,
    interval: float = 5.0,
    transport: Transport | None = None,
    sleep=time.sleep,
) -> bool:
    """Poll ``{base}/health`` until it answers 200. Only ever runs when called directly."""
    if not base_url:
        raise ModelClientError("base_url is empty; set the configured base_url_env first")
    if interval <= 0 or timeout <= 0:
        raise ValueError("timeout and interval must be positive")
    active: Transport = transport if transport is not None else UrllibTransport()
    if getattr(active, "is_network", True):
        require_network_authorization("health poll")
    url = health_url(base_url)
    # Bounded by attempts as well as by the clock, so an injected sleep still terminates.
    attempts = max(1, math.ceil(timeout / interval))
    deadline = time.monotonic() + timeout
    for attempt in range(attempts):
        try:
            if active.get_status(url, min(60.0, timeout)) == 200:
                return True
        except TransportError:
            pass  # a cold-starting server is unreachable or answers 503
        if attempt + 1 >= attempts or time.monotonic() >= deadline:
            return False
        sleep(interval)
    return False
