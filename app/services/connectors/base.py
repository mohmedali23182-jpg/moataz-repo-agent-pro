from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class ConnectorError(RuntimeError):
    pass


SENSITIVE_RE = re.compile(r'(TOKEN|KEY|SECRET|PASSWORD|PASS|DATABASE_URL|DIRECT_URL|JWT|PRIVATE)', re.I)


def mask_secret(value: str | None, keep: int = 4) -> str:
    if not value:
        return ''
    if len(value) <= keep * 2:
        return '*' * len(value)
    return value[:keep] + '…' + value[-keep:]


def parse_env_text(text: str) -> dict[str, str]:
    variables: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[len('export '):].strip()
        if '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', key):
            continue
        variables[key] = value
    return variables


def mask_variables(values: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in values.items():
        result[key] = mask_secret(value) if SENSITIVE_RE.search(key) else value
    return result


@dataclass
class ConnectorResult:
    ok: bool
    platform: str
    action: str
    message: str
    data: dict[str, Any] | list[Any] | None = None
