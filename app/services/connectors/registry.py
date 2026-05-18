from __future__ import annotations

from app.config import get_settings
from app.services.connectors.generic_connector import GenericAPIConnector
from app.services.connectors.railway_connector import RailwayConnector
from app.services.connectors.render_connector import RenderConnector
from app.services.connectors.vercel_connector import VercelConnector


def build_connector(platform: str, token: str, meta: dict | None = None):
    platform = platform.lower().strip()
    meta = meta or {}
    if platform == 'railway':
        return RailwayConnector(token, token_kind=meta.get('token_kind', 'account'))
    if platform == 'vercel':
        return VercelConnector(token, team_id=meta.get('team_id') or get_settings().vercel_team_id)
    if platform == 'render':
        return RenderConnector(token)
    if platform in {'customapi', 'netlify', 'fly', 'replit', 'lovable', 'cursor', 'spiko'}:
        defaults = {
            'netlify': 'https://api.netlify.com/api/v1',
            'fly': 'https://api.machines.dev/v1',
            'replit': meta.get('base_url', ''),
            'lovable': meta.get('base_url', ''),
            'cursor': meta.get('base_url', ''),
            'spiko': meta.get('base_url', ''),
            'customapi': meta.get('base_url', ''),
        }
        return GenericAPIConnector(
            token=token,
            platform=platform,
            base_url=meta.get('base_url') or defaults.get(platform, ''),
            auth=meta.get('auth', 'bearer'),
        )
    raise ValueError(f'Unsupported connector platform: {platform}')
