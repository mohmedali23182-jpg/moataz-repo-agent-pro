# Moataz Repo Agent Pro

بوت Telegram + Web API لإدارة مستودعات GitHub وفك ضغط المشاريع وترتيبها وتشغيل أوامر طرفية عبر GitHub Actions، مع تكامل Supabase اختياري.

## التشغيل على Railway

ارفع المشروع إلى GitHub ثم انشره على Railway. الخدمة تعمل عبر `Dockerfile` و `scripts/start.sh` وتستخدم متغير `PORT` تلقائيًا.

اختبار الصحة:

```text
https://YOUR_DOMAIN/health
```

## متغيرات Railway الأساسية

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_OWNER_IDS=8549357772
PUBLIC_URL=https://YOUR_DOMAIN.up.railway.app
TELEGRAM_WEBHOOK_SECRET=change_this_secret

ADMIN_API_TOKEN=change_this_admin_token
AGENT_API_TOKEN=change_this_external_agent_token

GITHUB_TOKEN=
GITHUB_DEFAULT_BRANCH=main

DATABASE_PATH=/tmp/agent.db
ENCRYPTION_KEY=change_this_long_secret
WORK_DIR=/tmp/moataz_repo_agent

AGENT_ALLOW_TERMINAL=true
AGENT_REQUIRE_APPROVAL=true
AGENT_MAX_COMMAND_SECONDS=1200
AGENT_ALLOWED_COMMANDS=npm,pnpm,yarn,python,pip,pytest,node,git,ls,cat,sed,grep
AGENT_DEFAULT_WORKDIR=.
AGENT_WORKFLOW_FILE=agent-command.yml

SUPABASE_URL=
SUPABASE_ANON_KEY=
SUPABASE_SERVICE_ROLE_KEY=
SUPABASE_ALLOWED_TABLES=profiles,posts,categories
SUPABASE_ALLOW_SQL=false
DATABASE_URL=
DIRECT_URL=
```

## Webhook Telegram

```text
https://api.telegram.org/botBOT_TOKEN/setWebhook?url=https://YOUR_DOMAIN/api/telegram/webhook/TELEGRAM_WEBHOOK_SECRET&secret_token=TELEGRAM_WEBHOOK_SECRET
```

والتحقق:

```text
https://api.telegram.org/botBOT_TOKEN/getWebhookInfo
```

## أوامر Telegram المهمة

```text
/token github_pat_xxx
/repo https://github.com/OWNER/REPO
/branch main
/info
/ls
/read path/to/file
/write path/to/file | content
/delete path/to/file
/create_repo name private
/create_repo name private --unique
```

## أوامر Agent

تعديل ملف:

```text
/agent https://github.com/OWNER/REPO
replace app/config.py
ضع المحتوى الجديد هنا
```

إنشاء مجلد:

```text
/agent https://github.com/OWNER/REPO
mkdir app/services/new_module
```

قراءة أو تحليل ملف:

```text
/agent https://github.com/OWNER/REPO
read app/main.py
```

تحليل مستودع:

```text
/analyze_repo https://github.com/OWNER/REPO
```

## Terminal عبر GitHub Actions

ثبت Workflow الطرفية أولًا:

```text
/install_workflow https://github.com/OWNER/REPO
```

ثم نفذ أمرًا:

```text
/term https://github.com/OWNER/REPO
npm run build
```

إذا كان `AGENT_REQUIRE_APPROVAL=true` أرسل:

```text
/approve
```

أو:

```text
/cancel_term
```

> الطرفية هنا ليست جلسة Codespace تفاعلية مباشرة. التنفيذ يتم عبر GitHub Actions workflow_dispatch، وهو أنسب وأكثر ثباتًا للبوت. يوجد أمر `/codespace` لإنشاء أو عرض Codespaces عند توفر صلاحية التوكن.

## Supabase

قراءة جدول مصرح:

```text
/supabase posts 10
```

تنفيذ SQL على قاعدة تملكها فقط:

```text
/supabase_sql select now();
```

يتطلب:

```env
SUPABASE_ALLOW_SQL=true
DIRECT_URL=postgresql://...
```

## External Agent API

يمكن ربط البوت بأي منصة خارجية عبر `AGENT_API_TOKEN`.

الهيدر:

```text
Authorization: Bearer AGENT_API_TOKEN
```

أو:

```text
X-Agent-Token: AGENT_API_TOKEN
```

Endpoints:

```text
POST /api/agent/apply
POST /api/agent/analyze
POST /api/agent/install-workflow
POST /api/agent/term
POST /api/supabase/sql
```

مثال:

```json
{
  "repo": "https://github.com/OWNER/REPO",
  "branch": "main",
  "instruction": "mkdir app/new"
}
```

## Ultra Agent Update

This build adds:

- Repo Session Manager per Telegram user.
- `/connections`, `/current_repo`, `/switch_repo`, `/disconnect_repo`, `/disconnect_all`.
- GitHub capability checks: viewer, visible repos, scopes when exposed by GitHub, and create-repo inference.
- Safer archive upload: `/unpack` now normalizes and uploads the detected project root to repository root by default. Use `--keep-folder` to intentionally upload into a subfolder.
- External API endpoints:
  - `GET /api/connections/status?telegram_id=...`
  - `POST /api/repo/switch`
  - `POST /api/repo/disconnect`
- Plain-text `ENCRYPTION_KEY` is now accepted and safely derived into a Fernet key.

Recommended Railway variables:

```env
AGENT_ALLOW_TERMINAL=true
AGENT_REQUIRE_APPROVAL=true
AGENT_MAX_COMMAND_SECONDS=1200
AGENT_ALLOWED_COMMANDS=npm,pnpm,yarn,python,pip,pytest,node,git,ls,cat,sed,grep
AGENT_DEFAULT_WORKDIR=.
AGENT_API_TOKEN=change_this_strong_token
AGENT_WORKFLOW_FILE=agent-command.yml
SUPABASE_ALLOW_SQL=false
```
