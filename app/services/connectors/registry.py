from __future__ import annotations

from app.config import get_settings
from app.services.connectors.railway_connector import RailwayConnector
from app.services.connectors.vercel_connector import VercelConnector


def build_connector(platform: str, token: str, meta: dict | None = None):
    platform = platform.lower().strip()
    meta = meta or {}
    if platform == 'railway':
        return RailwayConnector(token, token_kind=meta.get('token_kind', 'account'))
    if platform == 'vercel':
        return VercelConnector(token, team_id=meta.get('team_id') or get_settings().vercel_team_id)
    raise ValueError(f'Unsupported connector platform: {platform}')
