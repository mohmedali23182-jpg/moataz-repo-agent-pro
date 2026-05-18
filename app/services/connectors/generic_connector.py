from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

import httpx

from app.services.connectors.base import ConnectorError, ConnectorResult, mask_variables


class GenericAPIConnector:
    """Safe generic REST connector for platforms that expose a token based API.

    Store with:
      /connect customapi TOKEN base_url=https://api.example.com auth=bearer
      /connect render TOKEN base_url=https://api.render.com/v1
    Then call with /connector_call platform METHOD /path {json}
    """

    def __init__(self, token: str, platform: str = 'customapi', base_url: str = '', auth: str = 'bearer') -> None:
        self.token = token.strip()
        self.platform = platform.lower().strip()
        self.base_url = base_url.rstrip('/') + '/' if base_url else ''
        self.auth = (auth or 'bearer').lower()
        if not self.token:
            raise ConnectorError(f'{platform} token is required.')
        if not self.base_url:
            raise ConnectorError(f'{platform} base_url is required. Save it as meta: base_url=https://api.example.com')

    def _headers(self) -> dict[str, str]:
        h = {'Content-Type': 'application/json'}
        if self.auth == 'token':
            h['Authorization'] = f'token {self.token}'
        elif self.auth == 'key':
            h['X-API-Key'] = self.token
        else:
            h['Authorization'] = f'Bearer {self.token}'
        return h

    async def request(self, method: str, path: str, payload: dict[str, Any] | list[Any] | None = None, params: dict[str, Any] | None = None) -> ConnectorResult:
        method = method.upper().strip()
        if method not in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}:
            raise ConnectorError('Unsupported HTTP method.')
        url = urljoin(self.base_url, path.lstrip('/'))
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.request(method, url, headers=self._headers(), json=payload if method != 'GET' else None, params=params)
        try:
            data = r.json()
        except Exception:
            data = {'raw': r.text}
        if r.status_code >= 400:
            raise ConnectorError(f'{self.platform} API error {r.status_code}: {data}')
        if isinstance(data, dict):
            data = mask_variables(data) if all(isinstance(v, str) for v in data.values()) else data
        return ConnectorResult(True, self.platform, f'{method} {path}', f'{self.platform} request completed.', data)

    async def whoami(self) -> ConnectorResult:
        for path in ('/user', '/me', '/account'):
            try:
                return await self.request('GET', path)
            except Exception:
                continue
        raise ConnectorError('Could not detect account endpoint. Use /connector_call with an explicit path.')
