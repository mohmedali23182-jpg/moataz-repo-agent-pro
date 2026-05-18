from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings


class AIError(RuntimeError):
    pass


@dataclass
class AIResponse:
    provider: str
    model: str
    text: str
    raw: dict[str, Any] | None = None


OPENAI_COMPATIBLE_DEFAULTS: dict[str, tuple[str, str]] = {
    'openai': ('https://api.openai.com/v1/chat/completions', 'gpt-4o-mini'),
    'openrouter': ('https://openrouter.ai/api/v1/chat/completions', 'openai/gpt-4o-mini'),
    'groq': ('https://api.groq.com/openai/v1/chat/completions', 'llama-3.1-8b-instant'),
    'mistral': ('https://api.mistral.ai/v1/chat/completions', 'mistral-small-latest'),
    'together': ('https://api.together.xyz/v1/chat/completions', 'meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo'),
    'perplexity': ('https://api.perplexity.ai/chat/completions', 'sonar'),
    'deepseek': ('https://api.deepseek.com/chat/completions', 'deepseek-chat'),
    'xai': ('https://api.x.ai/v1/chat/completions', 'grok-2-latest'),
    'fireworks': ('https://api.fireworks.ai/inference/v1/chat/completions', 'accounts/fireworks/models/llama-v3p1-8b-instruct'),
    'huggingface': ('https://router.huggingface.co/v1/chat/completions', 'meta-llama/Meta-Llama-3.1-8B-Instruct'),
    'lovable': ('', ''),
    'cursor': ('', ''),
    'spiko': ('', ''),
    'custom': ('', ''),
}


class AIGateway:
    """Unified AI gateway.

    Supports OpenAI-compatible APIs, Gemini REST, Anthropic Messages, and Cohere Chat.
    Custom providers are accepted when the user stores a compatible base_url and model.
    """

    def __init__(self, provider: str, token: str, base_url: str = '', model: str = '') -> None:
        self.provider = provider.lower().strip()
        self.token = token.strip()
        self.base_url = base_url.strip()
        self.model = model.strip()
        if not self.token:
            raise AIError('AI token is required. استخدم /ai_connect provider TOKEN')
        default_url, default_model = OPENAI_COMPATIBLE_DEFAULTS.get(self.provider, ('', ''))
        if self.provider == 'anthropic':
            default_url, default_model = 'https://api.anthropic.com/v1/messages', 'claude-3-5-haiku-latest'
        if self.provider == 'cohere':
            default_url, default_model = 'https://api.cohere.com/v2/chat', 'command-r-plus'
        self.base_url = self.base_url or default_url
        self.model = self.model or default_model or get_settings().ai_default_model
        if not self.base_url and self.provider != 'gemini':
            raise AIError('AI base_url is required for this provider. استخدم /ai_connect provider TOKEN base_url model')

    async def ask(self, prompt: str, system: str = '', temperature: float = 0.2) -> AIResponse:
        if self.provider == 'gemini':
            return await self._ask_gemini(prompt, system, temperature)
        if self.provider == 'anthropic':
            return await self._ask_anthropic(prompt, system, temperature)
        if self.provider == 'cohere':
            return await self._ask_cohere(prompt, system, temperature)
        return await self._ask_openai_compatible(prompt, system, temperature)

    async def _ask_openai_compatible(self, prompt: str, system: str, temperature: float) -> AIResponse:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({'role': 'system', 'content': system})
        messages.append({'role': 'user', 'content': prompt})
        headers = {'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json'}
        if self.provider == 'openrouter':
            headers['HTTP-Referer'] = 'https://moataz-repo-agent.local'
            headers['X-Title'] = 'Moataz Repo Agent'
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(self.base_url, headers=headers, json={'model': self.model, 'messages': messages, 'temperature': temperature})
        data = r.json() if r.headers.get('content-type', '').startswith('application/json') else {'raw': r.text}
        if r.status_code >= 400:
            raise AIError(f'AI API error {r.status_code}: {data}')
        text = (((data.get('choices') or [{}])[0].get('message') or {}).get('content') or '').strip()
        return AIResponse(self.provider, self.model, text, data)

    async def _ask_gemini(self, prompt: str, system: str, temperature: float) -> AIResponse:
        model = self.model or 'gemini-1.5-flash'
        url = self.base_url or f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.token}'
        parts = []
        if system:
            parts.append({'text': system})
        parts.append({'text': prompt})
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(url, json={'contents': [{'parts': parts}], 'generationConfig': {'temperature': temperature}})
        data = r.json() if r.headers.get('content-type', '').startswith('application/json') else {'raw': r.text}
        if r.status_code >= 400:
            raise AIError(f'Gemini API error {r.status_code}: {data}')
        candidates = data.get('candidates') or []
        content = candidates[0].get('content', {}) if candidates else {}
        text = ''.join(p.get('text', '') for p in content.get('parts', []))
        return AIResponse(self.provider, model, text.strip(), data)

    async def _ask_anthropic(self, prompt: str, system: str, temperature: float) -> AIResponse:
        headers = {
            'x-api-key': self.token,
            'anthropic-version': '2023-06-01',
            'content-type': 'application/json',
        }
        payload: dict[str, Any] = {
            'model': self.model,
            'max_tokens': 2048,
            'temperature': temperature,
            'messages': [{'role': 'user', 'content': prompt}],
        }
        if system:
            payload['system'] = system
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(self.base_url, headers=headers, json=payload)
        data = r.json() if r.headers.get('content-type', '').startswith('application/json') else {'raw': r.text}
        if r.status_code >= 400:
            raise AIError(f'Anthropic API error {r.status_code}: {data}')
        text = ''.join(block.get('text', '') for block in data.get('content', []) if isinstance(block, dict)).strip()
        return AIResponse(self.provider, self.model, text, data)

    async def _ask_cohere(self, prompt: str, system: str, temperature: float) -> AIResponse:
        headers = {'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json'}
        messages = []
        if system:
            messages.append({'role': 'system', 'content': system})
        messages.append({'role': 'user', 'content': prompt})
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(self.base_url, headers=headers, json={'model': self.model, 'messages': messages, 'temperature': temperature})
        data = r.json() if r.headers.get('content-type', '').startswith('application/json') else {'raw': r.text}
        if r.status_code >= 400:
            raise AIError(f'Cohere API error {r.status_code}: {data}')
        text = (data.get('message', {}).get('content', [{}])[0].get('text') or data.get('text') or '').strip()
        return AIResponse(self.provider, self.model, text, data)
