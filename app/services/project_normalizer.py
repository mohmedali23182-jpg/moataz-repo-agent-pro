from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PROJECT_MARKERS: dict[str, int] = {
    'package.json': 50,
    'requirements.txt': 40,
    'pyproject.toml': 40,
    'Pipfile': 35,
    'Dockerfile': 30,
    'dockerfile': 30,
    'next.config.js': 30,
    'next.config.mjs': 30,
    'next.config.ts': 30,
    'vite.config.js': 25,
    'vite.config.ts': 25,
    'astro.config.mjs': 25,
    'nuxt.config.ts': 25,
    'svelte.config.js': 25,
    'angular.json': 25,
    'manage.py': 40,
    'go.mod': 35,
    'Cargo.toml': 35,
    'composer.json': 35,
    'pom.xml': 30,
    'build.gradle': 30,
    'build.gradle.kts': 30,
    'pubspec.yaml': 35,
    'railway.json': 20,
    'vercel.json': 20,
    'render.yaml': 20,
}

DIR_MARKERS: dict[str, int] = {
    'src': 15,
    'app': 15,
    'pages': 15,
    'public': 10,
    'prisma': 20,
    'components': 8,
    'lib': 8,
    'server': 8,
}

IGNORED_NAMES = {
    '.git',
    '.hg',
    '.svn',
    'node_modules',
    '.next',
    '.nuxt',
    'dist',
    'build',
    'coverage',
    '__pycache__',
    '.pytest_cache',
    '.mypy_cache',
    '.ruff_cache',
    '.venv',
    'venv',
    'env',
    '.env',
    '.env.local',
    '.env.production',
    '.env.development',
    '.DS_Store',
}

SECRET_FILE_PATTERNS = [
    re.compile(r'^\.env(\..*)?$', re.IGNORECASE),
    re.compile(r'.*secret.*\.(json|txt|env|pem|key)$', re.IGNORECASE),
    re.compile(r'.*private.*\.(pem|key)$', re.IGNORECASE),
]


@dataclass
class NormalizerReport:
    detected: bool
    project_type: str
    original_root: str
    normalized_root: str
    score: int
    added_files: list[str] = field(default_factory=list)
    updated_files: list[str] = field(default_factory=list)
    ignored_items: list[str] = field(default_factory=list)
    env_vars: list[str] = field(default_factory=list)
    suitable_platforms: list[str] = field(default_factory=list)
    needs_database: bool = False
    needs_env: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            'detected': self.detected,
            'project_type': self.project_type,
            'original_root': self.original_root,
            'normalized_root': self.normalized_root,
            'score': self.score,
            'added_files': self.added_files,
            'updated_files': self.updated_files,
            'ignored_items': self.ignored_items[:200],
            'env_vars': self.env_vars,
            'suitable_platforms': self.suitable_platforms,
            'needs_database': self.needs_database,
            'needs_env': self.needs_env,
            'notes': self.notes,
        }

    def telegram_text(self) -> str:
        if not self.detected:
            return (
                '📦 تم فك الضغط، لكن لم يتم اكتشاف مشروع برمجي واضح.\n'
                'تم تجهيز الملفات كما هي بدون ترتيب نشر.\n'
                f'المسار النهائي: <code>{self.normalized_root}</code>'
            )

        lines = [
            '🚀 <b>تم ترتيب المشروع للنشر</b>',
            f'نوع المشروع: <code>{self.project_type}</code>',
            f'مسار المشروع الأصلي: <code>{self.original_root}</code>',
            f'المسار النهائي: <code>{self.normalized_root}</code>',
            f'المنصات المناسبة: <code>{", ".join(self.suitable_platforms) or "غير محدد"}</code>',
        ]
        if self.added_files:
            lines.append('الملفات المضافة: <code>' + ', '.join(self.added_files[:12]) + '</code>')
        if self.env_vars:
            lines.append('متغيرات مطلوبة: <code>' + ', '.join(self.env_vars[:18]) + '</code>')
        if self.needs_database:
            lines.append('قاعدة البيانات: <b>نعم، يحتاج DATABASE_URL غالبًا.</b>')
        if self.ignored_items:
            lines.append(f'تم تجاهل {len(self.ignored_items)} عنصر غير مناسب للنشر أو حساس.')
        lines.append('ملاحظة: تم ترتيب البنية للنشر. نجاح التشغيل النهائي يعتمد على صحة الكود والمتغيرات.')
        return '\n'.join(lines)


def _is_secret_like(path: Path) -> bool:
    return any(pattern.match(path.name) for pattern in SECRET_FILE_PATTERNS)


def _is_ignored(path: Path) -> bool:
    return path.name in IGNORED_NAMES or _is_secret_like(path)


def score_directory(path: Path) -> int:
    score = 0
    for marker, points in PROJECT_MARKERS.items():
        if (path / marker).exists():
            score += points
    for marker, points in DIR_MARKERS.items():
        if (path / marker).is_dir():
            score += points
    if (path / 'prisma' / 'schema.prisma').exists():
        score += 30
    if (path / 'src' / 'app').is_dir():
        score += 8
    return score


def find_project_root(extracted_dir: Path) -> tuple[Path, int]:
    candidates: list[tuple[Path, int]] = []
    for root, dirs, _files in os.walk(extracted_dir):
        root_path = Path(root)
        dirs[:] = [d for d in dirs if d not in IGNORED_NAMES]
        score = score_directory(root_path)
        if score > 0:
            candidates.append((root_path, score))

    if not candidates:
        return extracted_dir, 0

    # Prefer the strongest score. If tied, prefer the shallower path.
    candidates.sort(key=lambda item: (item[1], -len(item[0].relative_to(extracted_dir).parts)), reverse=True)
    return candidates[0]


def detect_project_type(project_root: Path) -> str:
    package_json = project_root / 'package.json'
    if package_json.exists():
        try:
            package = json.loads(package_json.read_text(encoding='utf-8'))
            deps: dict[str, Any] = {}
            deps.update(package.get('dependencies') or {})
            deps.update(package.get('devDependencies') or {})
            scripts = package.get('scripts') or {}
            if 'next' in deps:
                return 'nextjs'
            if 'vite' in deps:
                return 'vite'
            if 'astro' in deps:
                return 'astro'
            if 'nuxt' in deps:
                return 'nuxt'
            if any('bot' in str(v).lower() for v in scripts.values()):
                return 'node-bot'
            return 'node'
        except Exception:
            return 'node'

    if (project_root / 'manage.py').exists():
        return 'django'
    if (project_root / 'requirements.txt').exists() or (project_root / 'pyproject.toml').exists():
        return 'python'
    if (project_root / 'go.mod').exists():
        return 'go'
    if (project_root / 'Cargo.toml').exists():
        return 'rust'
    if (project_root / 'composer.json').exists():
        return 'php'
    if (project_root / 'pubspec.yaml').exists():
        return 'flutter'
    return 'unknown'


def copy_clean_project(project_root: Path, normalized_dir: Path) -> list[str]:
    ignored: list[str] = []
    if normalized_dir.exists():
        shutil.rmtree(normalized_dir)
    normalized_dir.mkdir(parents=True, exist_ok=True)

    def ignore_func(directory: str, names: list[str]) -> set[str]:
        skipped: set[str] = set()
        for name in names:
            p = Path(directory) / name
            if _is_ignored(p):
                skipped.add(name)
                try:
                    ignored.append(str(p.relative_to(project_root)))
                except ValueError:
                    ignored.append(str(p))
        return skipped

    for item in project_root.iterdir():
        if _is_ignored(item):
            ignored.append(item.name)
            continue
        target = normalized_dir / item.name
        if item.is_dir():
            shutil.copytree(item, target, ignore=ignore_func, symlinks=False)
        elif item.is_file():
            shutil.copy2(item, target)
    return ignored


def _append_unique_env(env_path: Path, env_vars: list[str]) -> list[str]:
    existing = env_path.read_text(encoding='utf-8') if env_path.exists() else ''
    lines = existing.splitlines()
    current_keys = {line.split('=', 1)[0].strip() for line in lines if '=' in line and not line.strip().startswith('#')}
    added: list[str] = []
    for key in env_vars:
        if key not in current_keys:
            lines.append(f'{key}=')
            current_keys.add(key)
            added.append(key)
    if lines:
        env_path.write_text('\n'.join(lines).strip() + '\n', encoding='utf-8')
    return added


def infer_env_vars(project_root: Path, project_type: str) -> list[str]:
    vars_: list[str] = []
    text_samples: list[str] = []
    for name in ['package.json', 'requirements.txt', 'pyproject.toml', 'Dockerfile', 'railway.json', 'vercel.json']:
        p = project_root / name
        if p.exists() and p.is_file():
            text_samples.append(p.read_text(encoding='utf-8', errors='ignore'))
    combined = '\n'.join(text_samples).lower()

    if project_type in {'nextjs', 'vite', 'astro', 'nuxt', 'node', 'node-bot'}:
        vars_.extend(['NODE_ENV', 'PORT'])
    if project_type in {'python', 'django'}:
        vars_.extend(['PORT'])
    if (project_root / 'prisma' / 'schema.prisma').exists() or 'prisma' in combined:
        vars_.extend(['DATABASE_URL', 'DIRECT_URL'])
    if 'telegram' in combined or any((project_root / n).exists() for n in ['bot.py', 'telegram_bot.py']):
        vars_.extend(['TELEGRAM_BOT_TOKEN', 'TELEGRAM_WEBHOOK_SECRET', 'TELEGRAM_OWNER_ID'])
    if 'supabase' in combined:
        vars_.extend(['SUPABASE_URL', 'SUPABASE_ANON_KEY', 'SUPABASE_SERVICE_ROLE_KEY'])
    if 'openai' in combined:
        vars_.append('OPENAI_API_KEY')
    if 'openrouter' in combined:
        vars_.append('OPENROUTER_API_KEY')
    if 'github' in combined:
        vars_.append('GITHUB_TOKEN')

    deduped: list[str] = []
    for v in vars_:
        if v not in deduped:
            deduped.append(v)
    return deduped


def ensure_dockerfile(project_root: Path, project_type: str) -> bool:
    dockerfile = project_root / 'Dockerfile'
    if dockerfile.exists():
        return False

    if project_type == 'nextjs':
        dockerfile.write_text("""FROM node:20-bookworm-slim

ENV NODE_ENV=production
WORKDIR /app

COPY package*.json ./
RUN npm install

COPY . .
RUN npx prisma generate || true
RUN npm run build

EXPOSE 3000
CMD ["npm", "start"]
""", encoding='utf-8')
        return True

    if project_type in {'node', 'node-bot', 'vite', 'astro', 'nuxt'}:
        dockerfile.write_text("""FROM node:20-bookworm-slim

ENV NODE_ENV=production
WORKDIR /app

COPY package*.json ./
RUN npm install

COPY . .
RUN npm run build || true

EXPOSE 3000
CMD ["npm", "start"]
""", encoding='utf-8')
        return True

    if project_type in {'python', 'django'}:
        command = 'python manage.py runserver 0.0.0.0:${PORT:-8000}' if project_type == 'django' else 'python main.py'
        dockerfile.write_text(f"""FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["sh", "-c", "{command}"]
""", encoding='utf-8')
        return True

    return False


def ensure_railway_json(project_root: Path, project_type: str) -> bool:
    railway = project_root / 'railway.json'
    if railway.exists():
        return False
    if project_type == 'unknown':
        return False
    railway.write_text(json.dumps({
        '$schema': 'https://railway.com/railway.schema.json',
        'build': {'builder': 'DOCKERFILE'},
        'deploy': {
            'healthcheckTimeout': 120,
            'restartPolicyType': 'ON_FAILURE',
            'restartPolicyMaxRetries': 10,
        },
    }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return True


def ensure_gitignore(project_root: Path) -> bool:
    gitignore = project_root / '.gitignore'
    required = ['.env', '.env.*', '!.env.example', 'node_modules/', '.next/', 'dist/', 'build/', '__pycache__/', '.venv/', 'venv/', '*.db', '*.zip']
    old = gitignore.read_text(encoding='utf-8') if gitignore.exists() else ''
    lines = old.splitlines()
    changed = False
    for item in required:
        if item not in lines:
            lines.append(item)
            changed = True
    if changed:
        gitignore.write_text('\n'.join(lines).strip() + '\n', encoding='utf-8')
    return changed


def ensure_readme(project_root: Path, report: NormalizerReport) -> bool:
    readme = project_root / 'README.md'
    block = f"""

## Auto Project Normalizer Report

This project was normalized automatically after archive extraction.

- Detected project type: `{report.project_type}`
- Original detected root: `{report.original_root}`
- Normalized root: repository root
- Suitable platforms: `{', '.join(report.suitable_platforms) or 'unknown'}`

### Run locally

```bash
# Node / Next.js projects
npm install
npm run build
npm start

# Python projects
pip install -r requirements.txt
python main.py
```

### Deploy

1. Add the environment variables listed in `.env.example`.
2. Deploy on Railway, Render, Docker, or Vercel according to the detected project type.
3. Do not commit real secrets to the repository.

> Structural normalization does not guarantee that application code is bug-free. Runtime success depends on valid code, environment variables, database access, and external service keys.
"""
    if not readme.exists():
        readme.write_text('# Normalized Project\n' + block.lstrip(), encoding='utf-8')
        return True
    old = readme.read_text(encoding='utf-8', errors='ignore')
    if 'Auto Project Normalizer Report' in old:
        return False
    readme.write_text(old.rstrip() + block, encoding='utf-8')
    return True


def suitable_platforms(project_type: str, root: Path) -> list[str]:
    platforms = []
    if (root / 'Dockerfile').exists() or project_type in {'nextjs', 'node', 'node-bot', 'vite', 'astro', 'nuxt', 'python', 'django'}:
        platforms.extend(['Railway', 'Render', 'Docker'])
    if project_type in {'nextjs', 'vite', 'astro', 'nuxt'}:
        platforms.append('Vercel')
    if project_type == 'flutter':
        platforms.append('GitHub Actions APK')
    return platforms


def normalize_project(extracted_dir: str | Path, output_parent: str | Path) -> tuple[Path, NormalizerReport]:
    extracted = Path(extracted_dir).resolve()
    output_parent = Path(output_parent).resolve()
    output_parent.mkdir(parents=True, exist_ok=True)
    normalized = output_parent / 'normalized-project'

    project_root, score = find_project_root(extracted)
    detected = score > 0
    project_type = detect_project_type(project_root) if detected else 'plain-files'

    ignored = copy_clean_project(project_root, normalized)

    report = NormalizerReport(
        detected=detected,
        project_type=project_type,
        original_root=str(project_root),
        normalized_root=str(normalized),
        score=score,
        ignored_items=ignored,
    )

    if detected:
        env_vars = infer_env_vars(normalized, project_type)
        added_env = _append_unique_env(normalized / '.env.example', env_vars)
        if added_env:
            report.added_files.append('.env.example' if len(added_env) == len(env_vars) else '.env.example updated')
        report.env_vars = env_vars
        report.needs_env = bool(env_vars)
        report.needs_database = 'DATABASE_URL' in env_vars

        if ensure_dockerfile(normalized, project_type):
            report.added_files.append('Dockerfile')
        if ensure_railway_json(normalized, project_type):
            report.added_files.append('railway.json')
        if ensure_gitignore(normalized):
            if (normalized / '.gitignore').exists():
                report.updated_files.append('.gitignore')
        report.suitable_platforms = suitable_platforms(project_type, normalized)
        if ensure_readme(normalized, report):
            if 'README.md' not in report.added_files:
                report.updated_files.append('README.md')
    else:
        report.notes.append('لم يتم اكتشاف ملفات تشغيل مشروع، لذلك تم تنظيف الملفات الحساسة فقط.')

    return normalized, report


def make_zip(source_dir: str | Path, output_zip: str | Path) -> Path:
    source_dir = Path(source_dir).resolve()
    output_zip = Path(output_zip).resolve()
    output_zip.parent.mkdir(parents=True, exist_ok=True)
    if output_zip.exists():
        output_zip.unlink()
    with zipfile.ZipFile(output_zip, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        for path in source_dir.rglob('*'):
            if path.is_file():
                zf.write(path, path.relative_to(source_dir))
    return output_zip


def list_files_for_upload(root: Path) -> list[Path]:
    return [p for p in root.rglob('*') if p.is_file() and not _is_ignored(p)]
