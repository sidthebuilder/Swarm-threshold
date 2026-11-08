"""Transport for the Adaption Responses API.

The rest of this project speaks the OpenAI chat shape: ``client.ModelClient`` builds
``{"model", "messages", "temperature", "max_tokens", "seed"}`` and reads
``{"choices": [{"message": {"content"}, "finish_reason"}], "usage"}`` back. Adaption serves a
Responses API instead, so this module translates in both directions and nothing above it
changes. ``ModelClient._extract``, the reply cache, the retry budget, the policy layer and the
record contract are untouched.

Verified surface this module targets, and nothing beyond it:

* ``POST {base}/responses`` with ``Content-Type: application/json`` and
  ``Authorization: Bearer <key>``.
* Request fields that exist: ``model``, ``input`` (``{role, content}`` items with role
  ``user``, ``assistant``, ``system`` or ``developer``), ``instructions``,
  ``max_output_tokens``, ``temperature``, ``store``, ``stream``, ``tools``, ``tool_choice``,
  ``max_tool_calls``, ``previous_response_id``, ``agentic_memory``, ``background``.
* There is **no** ``seed`` and **no** ``response_format``. Both are dropped. Dropping ``seed``
  is a result-integrity matter, not a detail: a seed here selects an independent draw rather
  than a reproducible one, so offline replay is guaranteed by the reply cache alone. That
  limitation is registered in ``PREREGISTRATION.md`` and repeated in every Adaption config.
* ``GET {base}/models?type=hosted`` lists the hosted models a key can reach. Availability
  varies by key, which is why ``verify`` checks it before a sweep instead of assuming it.

Error contract, deliberately identical to ``client.UrllibTransport`` so the existing retry
logic needs no change:

* an HTTP error becomes :class:`client.HTTPStatusError` with the status, a body excerpt and
  ``Retry-After``;
* a connection failure becomes :class:`client.TransportError`;
* a reply that arrives with status ``failed``, ``cancelled`` or a non-terminal status becomes
  :class:`AdaptionResponseError`, a ``TransportError`` subclass, so the bounded backoff in
  ``ModelClient`` handles it and the error code travels in the message. The cost of that
  choice is recorded rather than hidden: a deterministic failure such as an unknown model id
  is retried until the budget is spent, which is why ``verify`` exists;
* a body or reply this module cannot translate becomes :class:`AdaptionTranslationError`,
  which is **not** a ``ValueError`` on purpose: ``harness.run_agent`` catches ``ValueError``
  around the policy call, and a translation bug must not be recorded as a model parse error.

Secret handling is unchanged. The key is read from the environment variable *named* by the
config, used for one request, and never stored, hashed, logged or printed. No error message in
this module quotes message content or a URL, because a prompt carries the per-run canary.

Sockets stay confined: ``urllib_http_call`` below is the only socket in this module, and
``AdaptionTransport`` accepts any replacement callable, so tests run fully in process.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .client import (
    HTTPStatusError,
    ModelClientError,
    ModelSpec,
    Transport,
    TransportError,
    UrllibTransport,
    require_network_authorization,
)

#: Hosts this transport recognizes as Adaption. Matched on the resolved host, never printed.
ADAPTION_HOST = "adaptionlabs.ai"

RESPONSES_PATH = "/responses"
MODELS_PATH = "/models"
CHAT_COMPLETIONS_PATH = "/chat/completions"
HOSTED_MODELS_QUERY = "type=hosted"

#: Chat body keys this module knows how to express as a Responses request.
TRANSLATED_BODY_KEYS = frozenset({"model", "messages", "temperature", "max_tokens"})
#: Chat body keys the Responses API has no equivalent for. Dropped, by name, on purpose.
DROPPED_BODY_KEYS = frozenset({"seed", "response_format"})
#: Roles an ``input`` item may carry.
INPUT_ROLES = frozenset({"user", "assistant", "system", "developer"})
#: Statuses that carry a final reply.
TERMINAL_STATUSES = frozenset({"completed", "incomplete"})
#: Statuses that report a server-side failure.
FAILURE_STATUSES = frozenset({"failed", "cancelled"})

_BODY_EXCERPT_CHARS = 400


class AdaptionTranslationError(ModelClientError):
    """A request body or reply this transport cannot translate.

    A ``ModelClientError`` and therefore a ``RuntimeError``: retrying cannot fix it. It is
    deliberately **not** a ``ValueError``, because ``harness.run_agent`` catches ``ValueError``
    around the policy call and would file a translation bug as a model parse error.
    """


class AdaptionResponseError(TransportError):
    """The endpoint answered, but with no usable reply.

    Subclasses ``client.TransportError`` so ``ModelClient`` applies its existing bounded
    backoff. ``code`` and ``message`` come from the reply's ``error`` object and are repeated
    in the exception text so they survive into a manifest entry.
    """

    def __init__(self, status: str, code: str = "", message: str = "") -> None:
        super().__init__(
            f"adaption response status={status or 'missing'} "
            f"code={code or 'none'}: {message or 'no message'}"
        )
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """One HTTP reply, whatever its status. Errors are values here, not exceptions.

    Keeping a 4xx or 5xx as a value means status handling lives in exactly one place and a
    test can produce ``429`` with ``Retry-After`` without constructing an exception.
    """

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""

    def header(self, name: str) -> str | None:
        """Case-insensitive lookup, so a plain dict and an HTTP message object both work."""
        wanted = name.lower()
        for key, value in self.headers.items():
            if str(key).lower() == wanted:
                return value
        return None

    def retry_after(self) -> float | None:
        raw = self.header("Retry-After")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return None

    def excerpt(self) -> str:
        """Short, decoded body excerpt for an error message, bounded like the chat client's."""
        return self.body[:_BODY_EXCERPT_CHARS].decode("utf-8", "replace")

    def json_value(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdaptionTranslationError(
                f"endpoint returned HTTP {self.status} with a body that is not JSON: {exc}"
            ) from None


#: ``(method, url, headers, body, timeout) -> HttpResponse``.
HttpCall = Callable[[str, str, Mapping[str, str], "bytes | None", float], HttpResponse]


def urllib_http_call(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | None,
    timeout: float,
) -> HttpResponse:
    """The only socket in this module.

    An HTTP error status is returned as an :class:`HttpResponse` rather than raised, so the
    caller decides what each status means. A connection-level failure propagates and the
    caller turns it into a ``client.TransportError``.
    """
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(
                int(response.status), dict(response.headers.items()), response.read()
            )
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        return HttpResponse(
            int(exc.code), dict(exc.headers.items()) if exc.headers else {}, payload
        )


# -- URL and host helpers --------------------------------------------------------------


def api_root(url: str) -> str:
    """Strip a trailing endpoint path so one base URL value serves every call.

    ``ModelClient`` appends ``/chat/completions`` to the configured base URL, and an operator
    may well set the base URL to the full ``/responses`` endpoint. Both forms reduce to the
    same API root, so neither needs a different config.
    """
    root = url.strip().rstrip("/")
    for suffix in (CHAT_COMPLETIONS_PATH, RESPONSES_PATH):
        if root.endswith(suffix):
            root = root[: -len(suffix)].rstrip("/")
    return root


def responses_url(url: str) -> str:
    return api_root(url) + RESPONSES_PATH


def models_url(url: str, *, hosted_only: bool = True) -> str:
    listing = api_root(url) + MODELS_PATH
    return f"{listing}?{HOSTED_MODELS_QUERY}" if hosted_only else listing


def is_adaption_base_url(url: str) -> bool:
    """True when a resolved base URL points at an Adaption host.

    Takes the host apart instead of searching the string, so a URL that merely mentions the
    name elsewhere cannot select this transport. The URL itself is never printed.
    """
    candidate = url.strip()
    if not candidate:
        return False
    if "//" not in candidate:
        candidate = f"//{candidate}"
    try:
        host = (urllib.parse.urlsplit(candidate).hostname or "").lower()
    except ValueError:
        return False
    return host == ADAPTION_HOST or host.endswith("." + ADAPTION_HOST)


def auth_headers(api_key_env: str = "") -> dict[str, str]:
    """Headers for one request.

    The key is read from the environment variable *named* by the config, placed in one header,
    and never stored, hashed, logged or printed. An unset variable raises with the name only.
    """
    headers = {"Content-Type": "application/json"}
    if api_key_env:
        key = os.environ.get(api_key_env, "")
        if not key:
            raise ModelClientError(
                f"environment variable {api_key_env} is not set; put it in the project .env "
                "or export it in the shell that runs the sweep. Its value is never printed, "
                "stored or written to an artifact."
            )
        headers["Authorization"] = f"Bearer {key}"
    return headers


# -- request translation ---------------------------------------------------------------


def _message(index: int, message: Any) -> dict[str, str]:
    """Validate one chat message. No error here quotes content: it can hold the canary."""
    if not isinstance(message, Mapping):
        raise AdaptionTranslationError(f"message {index} is not a mapping")
    unknown = set(message) - {"role", "content"}
    if unknown:
        raise AdaptionTranslationError(
            f"message {index} carries keys {sorted(unknown)}, which have no Responses "
            "equivalent"
        )
    role = message.get("role")
    content = message.get("content")
    if role not in INPUT_ROLES:
        raise AdaptionTranslationError(
            f"message {index} has role {role!r}; the Responses API accepts "
            f"{sorted(INPUT_ROLES)}"
        )
    if not isinstance(content, str):
        raise AdaptionTranslationError(f"message {index} content must be a string")
    return {"role": str(role), "content": content}


def _split_instructions(
    messages: Sequence[Any],
) -> tuple[str | None, list[dict[str, str]]]:
    """Take exactly one leading system message as ``instructions``; refuse any other.

    A system message at a non-zero index cannot be folded into ``instructions`` without
    moving it, and moving it would silently change the prompt the model sees. The frozen
    prompt layer never produces one, so this is a loud failure rather than a repair.
    """
    normalized = [_message(index, item) for index, item in enumerate(messages)]
    instructions: str | None = None
    start = 0
    if normalized[0]["role"] == "system":
        instructions = normalized[0]["content"]
        start = 1
    for index in range(start, len(normalized)):
        if normalized[index]["role"] == "system":
            raise AdaptionTranslationError(
                f"message {index} is a system message, but only a single leading system "
                "message can become 'instructions'. Reordering it would change the prompt "
                "the model sees, so this request is refused instead."
            )
    turns = normalized[start:]
    if not turns:
        raise AdaptionTranslationError(
            "a Responses request needs at least one input message; a lone system message "
            "would leave 'input' empty"
        )
    return instructions, turns


def _token_budget(value: Any) -> int:
    try:
        budget = int(value)
    except (TypeError, ValueError):
        raise AdaptionTranslationError("max_tokens must be an integer") from None
    if budget < 1:
        raise AdaptionTranslationError("max_tokens must be positive")
    return budget


def _temperature(value: Any) -> float:
    try:
        temperature = float(value)
    except (TypeError, ValueError):
        raise AdaptionTranslationError("temperature must be a number") from None
    if not 0.0 <= temperature <= 2.0:
        raise AdaptionTranslationError("temperature must be in [0, 2]")
    return temperature


def translate_request(body: Mapping[str, Any]) -> dict[str, Any]:
    """Turn an OpenAI chat body into a Responses request body.

    Rules, all of them explicit:

    * exactly one leading system message becomes ``instructions``;
    * every remaining message becomes an ``input`` item, in order, with its role kept;
    * ``max_tokens`` becomes ``max_output_tokens`` and ``temperature`` passes through;
    * ``store`` is always sent as ``false``. Adaption already defaults to false, but saying
      so on every call means a change of default cannot start retaining prompts that contain
      the per-run canary;
    * ``seed`` and ``response_format`` are dropped, because the API has neither;
    * any other key is refused rather than dropped, so a future change to the chat body
      cannot silently stop reaching the endpoint.
    """
    if not isinstance(body, Mapping):
        raise AdaptionTranslationError("request body must be a mapping")
    unknown = set(body) - TRANSLATED_BODY_KEYS - DROPPED_BODY_KEYS
    if unknown:
        raise AdaptionTranslationError(
            f"no Responses translation for chat body keys {sorted(unknown)}; add an explicit "
            "rule for each one instead of dropping it silently"
        )
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise AdaptionTranslationError("request body needs a non-empty model id")
    messages = body.get("messages")
    if (
        not isinstance(messages, Sequence)
        or isinstance(messages, (str, bytes))
        or not messages
    ):
        raise AdaptionTranslationError("request body needs a non-empty messages list")
    instructions, turns = _split_instructions(messages)
    request: dict[str, Any] = {"model": model, "input": turns, "store": False}
    if instructions is not None:
        request["instructions"] = instructions
    if "max_tokens" in body:
        request["max_output_tokens"] = _token_budget(body["max_tokens"])
    if "temperature" in body:
        request["temperature"] = _temperature(body["temperature"])
    return request


# -- reply translation -----------------------------------------------------------------


def _usage(payload: Mapping[str, Any]) -> dict[str, int]:
    raw = payload.get("usage")
    usage: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}

    def count(name: str) -> int:
        try:
            return max(0, int(usage.get(name) or 0))
        except (TypeError, ValueError):
            return 0

    return {
        "prompt_tokens": count("input_tokens"),
        "completion_tokens": count("output_tokens"),
        "total_tokens": count("total_tokens"),
    }


def finish_reason(payload: Mapping[str, Any]) -> str:
    """Map a response status onto the ``finish_reason`` the trace already records."""
    if str(payload.get("status") or "") == "completed":
        return "stop"
    raw = payload.get("incomplete_details")
    details: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    reason = str(details.get("reason") or "unknown")
    if reason == "max_output_tokens":
        return "length"
    return f"incomplete:{reason}"


def extract_text(payload: Mapping[str, Any]) -> str:
    """Collect the reply text from ``output``, including any refusal.

    A refusal is surfaced as the reply text on purpose. Dropping it would turn a refusal into
    an empty reply and lose the run's actual outcome; as text it reaches the strict parser,
    which records a parse error that the analysis already reports per condition.

    Output items that are not messages, such as reasoning or tool items, carry no reply text
    for this harness and are skipped. ``output_text`` is used only as a fallback, because it
    aggregates text parts and would hide a refusal.
    """
    pieces: list[str] = []
    output = payload.get("output")
    if isinstance(output, Sequence) and not isinstance(output, (str, bytes)):
        for item in output:
            if not isinstance(item, Mapping) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
                continue
            for part in content:
                if not isinstance(part, Mapping):
                    continue
                kind = part.get("type")
                if kind == "output_text":
                    value = part.get("text")
                elif kind == "refusal":
                    value = part.get("refusal")
                else:
                    continue
                if isinstance(value, str) and value:
                    pieces.append(value)
    if pieces:
        return "\n".join(pieces)
    fallback = payload.get("output_text")
    return fallback if isinstance(fallback, str) else ""


def translate_response(payload: Any) -> dict[str, Any]:
    """Turn a Responses reply into the chat shape ``ModelClient._extract`` already reads.

    ``failed`` and ``cancelled`` raise with the error code and message. So does any
    non-terminal status: this transport never requests ``background`` or ``stream``, so
    ``queued`` or ``in_progress`` means no reply arrived, and caching an empty reply under a
    sample key would quietly corrupt a replay.
    """
    if not isinstance(payload, Mapping):
        raise AdaptionTranslationError("endpoint reply was not a JSON object")
    status = str(payload.get("status") or "")
    raw_error = payload.get("error")
    error: Mapping[str, Any] = raw_error if isinstance(raw_error, Mapping) else {}
    if status not in TERMINAL_STATUSES:
        code = str(error.get("code") or "") or f"status_{status or 'missing'}"
        message = str(error.get("message") or "")
        if not message and status not in FAILURE_STATUSES:
            message = (
                "no final reply: this transport never requests background or streaming, so a "
                "non-terminal status is treated as a transient fault and retried"
            )
        raise AdaptionResponseError(status, code, message)
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": extract_text(payload)},
                "finish_reason": finish_reason(payload),
            }
        ],
        "usage": _usage(payload),
    }


# -- transport -------------------------------------------------------------------------


class AdaptionTransport:
    """``client.Transport`` implementation for the Adaption Responses API.

    ``is_network`` is ``True`` so ``ModelClient`` keeps applying the ``SWARM_ALLOW_NETWORK``
    gate to every call, including the ones a test routes through an injected HTTP callable.
    """

    is_network = True

    def __init__(self, http: HttpCall | None = None) -> None:
        self._http: HttpCall = urllib_http_call if http is None else http

    # -- Transport protocol ----------------------------------------------------

    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        """Translate out, call ``{base}/responses``, translate back into the chat shape."""
        request_body = json.dumps(translate_request(body)).encode("utf-8")
        payload = self._request_json(
            "POST", responses_url(url), headers, request_body, timeout
        )
        return translate_response(payload)

    def get_status(self, url: str, timeout: float) -> int:
        """Present only to satisfy the ``Transport`` protocol.

        The Responses API is a hosted service with no ``/health`` endpoint, so ``wake``
        no-ops for Adaption configs and nothing in the sweep calls this. ``verify`` is the
        readiness check: it lists the hosted models the key can actually reach.
        """
        return self._fetch("GET", url, {}, None, timeout).status

    # -- model listing ---------------------------------------------------------

    def list_hosted_models(
        self,
        base_url: str,
        api_key_env: str = "",
        *,
        timeout: float = 60.0,
        hosted_only: bool = True,
    ) -> tuple[str, ...]:
        """``GET {base}/models?type=hosted`` and return the model ids, de-duplicated.

        Used only by the explicit ``verify`` command. Hosted availability varies by key, so
        this is the one honest way to check a configured model id before spending anything.
        """
        if not base_url:
            raise ModelClientError(
                "base_url is empty; set the variable named by the config's base_url_env"
            )
        if self.is_network:
            require_network_authorization("model listing")
        payload = self._request_json(
            "GET",
            models_url(base_url, hosted_only=hosted_only),
            auth_headers(api_key_env),
            None,
            timeout,
        )
        return model_ids(payload)

    # -- plumbing --------------------------------------------------------------

    def _fetch(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> HttpResponse:
        try:
            response = self._http(method, url, dict(headers), body, timeout)
        except (
            urllib.error.URLError,
            TimeoutError,
            http.client.HTTPException,
            ConnectionError,
            OSError,
        ) as exc:
            # Same contract as client.UrllibTransport, so the existing backoff applies.
            raise TransportError(str(exc)) from None
        if not isinstance(response, HttpResponse):
            raise AdaptionTranslationError(
                "the injected HTTP callable must return an adaption.HttpResponse"
            )
        return response

    def _request_json(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> Any:
        response = self._fetch(method, url, headers, body, timeout)
        if response.status >= 400:
            raise HTTPStatusError(
                response.status, response.excerpt(), response.retry_after()
            )
        return response.json_value()


def model_ids(payload: Any) -> tuple[str, ...]:
    """Read model ids out of a listing reply, in order, without duplicates.

    Only the path and the ``type=hosted`` query are verified facts, so the envelope is read
    defensively instead of asserting one invented field name: a bare list, or a list under
    ``data``, ``models`` or ``items``, and entries that are either strings or objects naming
    themselves with ``id``, ``model`` or ``name``.
    """
    entries: Any = None
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
        entries = payload
    elif isinstance(payload, Mapping):
        for key in ("data", "models", "items"):
            candidate = payload.get(key)
            if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes)):
                entries = candidate
                break
    if entries is None:
        raise AdaptionTranslationError(
            "model listing contained no recognizable list of models"
        )
    found: list[str] = []
    for entry in entries:
        if isinstance(entry, str) and entry:
            found.append(entry)
        elif isinstance(entry, Mapping):
            for name_key in ("id", "model", "name"):
                value = entry.get(name_key)
                if isinstance(value, str) and value:
                    found.append(value)
                    break
    return tuple(dict.fromkeys(found))


def select_transport(spec: ModelSpec) -> Transport:
    """Choose a transport from the resolved endpoint host, not from a config label.

    Resolution happens at call time because the base URL lives in an environment variable:
    a dry run can describe a config with the variable unset, and only an execution needs it.

    An Adaption config with ``json_mode`` true is refused here rather than later. ``json_mode``
    is part of the reply cache key, so a true value would label every cached reply with a
    request field that was dropped before it reached the endpoint.
    """
    if is_adaption_base_url(spec.resolve_base_url()):
        if spec.json_mode:
            raise ModelClientError(
                f"config for model {spec.model!r} sets json_mode true against an Adaption "
                "endpoint, but the Responses API has no response_format field. json_mode is "
                "part of the reply cache key, so a true value would attribute cached replies "
                "to a request this transport never sends. Set json_mode false."
            )
        return AdaptionTransport()
    return UrllibTransport()
