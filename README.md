# Moataz Repo Agent Pro Ultra

بوت Telegram + لوحة Web لإدارة مستودعات GitHub، فك ضغط المشاريع، ترتيب الجذر للنشر، تشغيل Terminal عبر GitHub Actions، وربط موصلات Railway/Vercel، وربط مزودات AI عبر مفاتيح ترسل من البوت أو من لوحة التحكم.

## التشغيل على Railway

1. ارفع المشروع إلى GitHub.
2. انشره على Railway من المستودع.
3. ضع المتغيرات الأساسية:

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_OWNER_IDS=
PUBLIC_URL=https://your-service.up.railway.app
TELEGRAM_WEBHOOK_SECRET=change_this_secret
ADMIN_API_TOKEN=change_this_admin_token
AGENT_API_TOKEN=change_this_agent_token
ENCRYPTION_KEY=change_this_long_secret_key_32_chars_or_more
DATABASE_PATH=/tmp/agent.db
WORK_DIR=/tmp/moataz_repo_agent
AGENT_ALLOW_TERMINAL=true
AGENT_REQUIRE_APPROVAL=true
AGENT_MAX_COMMAND_SECONDS=1200
AGENT_ALLOWED_COMMANDS=npm,pnpm,yarn,python,pip,pytest,node,git,ls,cat,sed,grep
```

4. افتح `/health` وتأكد أنه يعمل.
5. اضبط Webhook:

```text
https://api.telegram.org/botBOT_TOKEN/setWebhook?url=PUBLIC_URL/api/telegram/webhook/TELEGRAM_WEBHOOK_SECRET&secret_token=TELEGRAM_WEBHOOK_SECRET
```

## أهم أوامر Telegram

### GitHub

```text
/token github_pat_xxx
/switch_repo https://github.com/OWNER/REPO
/current_repo
/connections
/repos
/ls
/read path
/write path | content
/delete path
/create_repo my-project private --unique
```

### فك الضغط والترتيب

```text
/unpack
/unpack target/folder --keep-folder
/normalize
```

الافتراضي يرفع محتوى المشروع الحقيقي إلى جذر المستودع حتى تكتشفه Railway/Vercel.

### Agent وTerminal

```text
/analyze_repo
/agent
replace app/config.py
المحتوى الجديد

/install_workflow
/term
npm run build
/approve
```

### Platform Connectors

يمكن إرسال التوكن من Telegram ولا يلزم وضعه في Railway:

```text
/connect railway RAILWAY_TOKEN
/railway_projects
/railway_project PROJECT_ID
/railway_set_var PROJECT_ID ENV_ID SERVICE_ID KEY=VALUE
/railway_set_vars PROJECT_ID ENV_ID SERVICE_ID
KEY=VALUE
KEY2=VALUE2
```

```text
/connect vercel VERCEL_TOKEN team_id=team_xxx
/vercel_projects
/vercel_set_var PROJECT production KEY=VALUE
/vercel_set_vars PROJECT production
KEY=VALUE
```

### AI Gateway

```text
/ai_connect openrouter TOKEN
/ai_connect openai TOKEN
/ai_connect gemini TOKEN
/ai_connect custom TOKEN https://api.example.com/v1/chat/completions model-name
/ask_ai openrouter حلل هذا الخطأ واقترح التصحيح
/ai_status
```

## لوحة الويب

افتح جذر الدومين `/`، ثم استخدم `ADMIN_API_TOKEN` أو `AGENT_API_TOKEN` في خانة التوكن. اللوحة فيها تبويبات GitHub, Connectors, AI, Terminal, Output.

## الأمان

- لا تطبع التوكنات في اللوجات.
- التوكنات المرسلة من Telegram تحفظ مشفرة في SQLite.
- نفذ الأوامر الحساسة للمالك فقط عبر `TELEGRAM_OWNER_IDS`.
- الطرفية تستخدم allowlist من `AGENT_ALLOWED_COMMANDS`.
- حذف المتغيرات وإعادة النشر يجب أن يتم بحذر.
