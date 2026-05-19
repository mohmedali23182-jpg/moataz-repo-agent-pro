from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.services.ai.gateway import AIGateway, AIError
from app.services.store import Store


@dataclass
class AgentStep:
    order: int
    action: str
    args: dict[str, Any] = field(default_factory=dict)
    description: str = ''


@dataclass
class AgentPlan:
    objective: str
    steps: list[AgentStep]
    requires_approval: bool = True
    raw: str = ''

    def telegram_text(self) -> str:
        lines = ['🧠 <b>خطة الوكيل</b>', f'الهدف: <code>{self.objective[:500]}</code>', '']
        for step in self.steps:
            lines.append(f'{step.order}. <b>{step.action}</b> — {step.description or step.args}')
        lines.append('\nاكتب <code>/approve_plan TASK_ID</code> للتنفيذ أو <code>/task_cancel TASK_ID</code> للإلغاء.')
        return '\n'.join(lines)


SYSTEM_PROMPT = '''You are a senior autonomous software engineering agent planner.
Return ONLY strict JSON with this shape:
{"objective":"...","steps":[{"action":"analyze|read|write|append|delete|mkdir|run|install_workflow|index_repo|fix_last_error","description":"...","args":{}}]}
Rules:
- Prefer safe read/analyze/index before write/run.
- For write, args must include path and content.
- For run, args must include command and optional workdir.
- Never include destructive shell commands.
- Keep plans under 8 steps unless user explicitly requested more.
'''


def heuristic_plan(command: str) -> AgentPlan:
    text = command.strip()
    low = text.lower()
    steps: list[AgentStep] = []
    if any(w in low for w in ['build', 'railway', 'vercel', 'deploy', 'نشر', 'بناء']):
        steps.append(AgentStep(1, 'analyze', {}, 'تحليل هيكل المستودع وملفات النشر'))
        steps.append(AgentStep(2, 'install_workflow', {}, 'تثبيت Workflow الطرفية إذا لم يكن موجودًا'))
        steps.append(AgentStep(3, 'run', {'command': 'ls && (npm run build || python -m compileall app || true)', 'workdir': '.'}, 'تشغيل فحص بناء آمن عبر GitHub Actions'))
        steps.append(AgentStep(4, 'fix_last_error', {}, 'تحليل آخر خطأ واقتراح إصلاح'))
    elif any(w in low for w in ['حلل', 'analyze', 'افحص']):
        steps.append(AgentStep(1, 'analyze', {}, 'تحليل المستودع'))
        steps.append(AgentStep(2, 'index_repo', {}, 'فهرسة ذاكرة المستودع'))
    elif any(w in low for w in ['workflow', 'terminal', 'طرفية']):
        steps.append(AgentStep(1, 'install_workflow', {}, 'تثبيت Workflow الطرفية'))
    else:
        steps.append(AgentStep(1, 'analyze', {}, 'تحليل المشروع أولًا'))
        steps.append(AgentStep(2, 'index_repo', {}, 'فهرسة الملفات المهمة للذاكرة'))
    return AgentPlan(objective=text, steps=steps, raw='heuristic')


def _json_from_text(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith('```'):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```$', '', cleaned)
    try:
        return json.loads(cleaned)
    except Exception:
        m = re.search(r'\{[\s\S]*\}', cleaned)
        if not m:
            raise
        return json.loads(m.group(0))


async def build_plan(store: Store, telegram_id: int, command: str, preferred_provider: str | None = None) -> AgentPlan:
    provider, token, base_url, model = store.get_ai_token(telegram_id, preferred_provider)
    if not token:
        return heuristic_plan(command)
    try:
        gateway = AIGateway(provider, token, base_url, model)
        resp = await gateway.ask(command, SYSTEM_PROMPT, temperature=0.1)
        data = _json_from_text(resp.text)
        steps = []
        for i, item in enumerate(data.get('steps') or [], start=1):
            action = str(item.get('action') or '').strip().lower()
            if not action:
                continue
            steps.append(AgentStep(i, action, item.get('args') or {}, item.get('description') or ''))
        if not steps:
            return heuristic_plan(command)
        return AgentPlan(str(data.get('objective') or command), steps, True, resp.text)
    except Exception:
        return heuristic_plan(command)
