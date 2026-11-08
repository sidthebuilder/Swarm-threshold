"""Load the project ``.env`` into the process environment without exposing a value.

Why this module exists
----------------------
A model-backed sweep needs the endpoint URL and the API key in its own process environment,
because ``client.py`` reads the environment variable *named* by the config immediately before
each request. Every other way of getting the key there is worse:

* a command line argument lands the key in shell history and in the process table;
* an argument in a terminal that echoes its input lands the key in a transcript;
* a literal in a config or source file lands the key in the repository;
* a value passed through a log line or an artifact lands the key in the results.

Reading the gitignored ``.env`` file straight into :data:`os.environ` keeps the key out of all
of those places. ``.env`` is listed in ``.gitignore`` and must stay there.

The discipline this module keeps
--------------------------------
* It returns variable **names**. It never returns, prints, logs, caches or hashes a value.
* A malformed line is skipped without being echoed, because the text of a line in this file
  may itself be a secret. There is therefore no parse error message that could quote one.
* ``os.environ.setdefault`` means a variable already exported in the shell wins over the file,
  so an operator can override the file for one command without editing it.
* A missing or unreadable file is not an error. Every command calls this, and the local
  scripted commands need no variables at all.

This is a deliberate, narrow divergence from the earlier rule that no module in this project
reads a ``.env`` file. The rule it replaces was "the key must never appear in a command line,
a log or an artifact", and loading the file programmatically is the stricter way to keep it.
``client.py`` itself still reads no file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

PROJECT_ENV_FILENAME = ".env"


def project_env_file() -> Path:
    """Path of the project-root ``.env``, resolved from this file rather than the cwd."""
    return Path(__file__).resolve().parent.parent / PROJECT_ENV_FILENAME


def _strip_quotes(value: str) -> str:
    """Drop one layer of matching surrounding quotes, as a shell would."""
    for quote in ('"', "'"):
        if len(value) >= 2 and value.startswith(quote) and value.endswith(quote):
            return value[1:-1]
    return value


def load_env_file(path: str | Path) -> list[str]:
    """Populate ``os.environ`` from ``KEY=VALUE`` lines and return only the names set.

    Blank lines and ``#`` comments are skipped. Surrounding quotes are stripped from the
    value. A name already present in the environment is left untouched and is therefore not
    listed in the return value, so the result answers "which names did this call introduce",
    never "what is in them".
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    assigned: list[str] = []
    for line in raw.splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        name, _, value = entry.partition("=")
        name = name.strip()
        if not name or any(character.isspace() for character in name):
            continue  # not a variable assignment; skipped silently, never echoed
        already_set = name in os.environ
        os.environ.setdefault(name, _strip_quotes(value.strip()))
        if not already_set:
            assigned.append(name)
    return assigned


def variable_status(names: Iterable[str]) -> list[tuple[str, bool]]:
    """Report, by NAME only, whether each variable holds a non-empty value.

    The value is tested for emptiness and then dropped. It is never returned or printed.
    """
    return [
        (name, bool(os.environ.get(name, "").strip())) for name in names if name
    ]


def missing_variables(names: Iterable[str]) -> list[str]:
    """Names from ``names`` that are unset or empty, in the order given."""
    return [name for name, is_set in variable_status(names) if not is_set]
