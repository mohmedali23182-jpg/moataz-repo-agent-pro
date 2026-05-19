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

### فك الضغط والترتيب والاستبدال الكامل

```text
/unpack
/unpack target/folder --keep-folder
/normalize
```

الافتراضي يرفع محتوى المشروع الحقيقي إلى جذر المستودع حتى تكتشفه Railway/Vercel.

للاستبدال الكامل: أرسل ملف ZIP/RAR/7Z/TAR ثم رد عليه بأحد الأوامر:

```text
/replace
/replace https://github.com/OWNER/REPO
/replace --dry-run
/replace --keep README.md .env.example
/replace --target apps/web
/replace --no-delete
/replace --force
```

`/replace` يفك الضغط، يكتشف جذر المشروع، يرتب الملفات، يحذف الملفات القديمة داخل النطاق المطلوب، ثم يرفع المشروع الجديد بCommit واحد عبر Git Data API. استخدم `--dry-run` للمعاينة بدون حذف أو رفع.

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

## Download Center و Google Drive

تمت إضافة أوامر تحميل قانونية مباشرة للملفات وروابط APK المباشرة، بدون تجاوز Google Play أو استخدام جلسات غير رسمية.

### أوامر التحميل

```text
/download_file DIRECT_URL [filename]
/apk DIRECT_APK_URL
/download_to_repo DIRECT_URL path/in/repo.apk
```

> روابط Google Play مثل `play.google.com/store/apps/details?...` صفحات متجر وليست ملفات APK مباشرة. البوت يرفض تجاوز المتجر ويطلب رابط ملف مباشر تملك حق تحميله.

### Google Drive

يدعم OAuth Access Token أو JSON حساب خدمة. عند استخدام حساب خدمة، شارك مجلد Drive مع بريد حساب الخدمة أولًا ثم استخدم `folder_id`.

```text
/gdrive_connect ACCESS_TOKEN folder_id=FOLDER_ID
/gdrive_status
```

أو رد على رسالة تحتوي JSON حساب الخدمة:

```text
/gdrive_connect folder_id=FOLDER_ID
```

رفع ملف Telegram إلى Drive:

```text
/gdrive_upload folder_id=FOLDER_ID email=user@example.com
```

تحميل رابط مباشر ورفعه إلى Drive:

```text
/download_to_gdrive DIRECT_URL folder_id=FOLDER_ID email=user@example.com
```

## AI Gateway الموسع

يدعم OpenAI-compatible providers إضافة إلى Gemini وAnthropic وCohere:

```text
/ai_connect openrouter TOKEN
/ai_connect openai TOKEN
/ai_connect gemini TOKEN
/ai_connect anthropic TOKEN
/ai_connect groq TOKEN
/ai_connect mistral TOKEN
/ai_connect together TOKEN
/ai_connect perplexity TOKEN
/ai_connect deepseek TOKEN
/ai_connect xai TOKEN
/ai_connect cohere TOKEN
/ai_connect huggingface TOKEN
/ai_connect fireworks TOKEN
/ai_connect custom TOKEN https://api.example.com/v1/chat/completions model-name
/ask_ai openrouter حلل هذا الخطأ
```

## Agentic Upgrade: Planner + Memory + Sandbox

This build adds real execution primitives inspired by advanced coding agents while staying deployable on Railway:

### New Telegram commands

```text
/plan <repo_url optional>
افحص المشروع وشغل build واقترح إصلاحًا

/task <repo_url optional>
1. analyze repository
2. install workflow
3. run npm run build

/approve_plan TASK_ID
/task_status [TASK_ID]
/task_logs
/task_cancel [TASK_ID]

/index_repo
/memory_status
/forget_repo_memory

/fix_last_error
/autofix

/sandbox_run <repo_url optional>
npm run build

/codeact <repo_url optional>
python -m compileall app
```

### What is actually executed

- Repository analysis uses the GitHub REST/Git Data API.
- Terminal/Sandbox execution runs through GitHub Actions `workflow_dispatch` and returns real logs.
- `/replace` performs a real atomic Git commit with Git Data API.
- Memory is stored in SQLite in `repo_memory`.
- Task state is stored in SQLite in `agent_tasks` and `agent_task_logs`.

### New environment variables

```env
AGENT_MODE=planner
AGENT_REQUIRE_PLAN_APPROVAL=true
AGENT_MAX_STEPS=12
AGENT_MAX_RETRIES=3
AGENT_PROGRESS_INTERVAL_SECONDS=4
AGENT_CODEACT_ENABLED=true
AGENT_SANDBOX_MODE=github_actions
AGENT_BLOCKED_COMMANDS=rm -rf,curl|bash,wget|bash,shutdown,reboot,mkfs,dd if=
MEMORY_ENABLED=true
MEMORY_BACKEND=sqlite
VECTOR_MEMORY_ENABLED=false
CHROMA_PATH=/tmp/chroma
```


## 🎥 قسم البث المباشر داخل نفس البوت

تم دمج بوت البث كقسم داخل بوت Moataz Repo Agent نفسه، وليس كبوت ثانٍ. هذا صحيح لأن Telegram يسمح بـ webhook واحد فقط لكل توكن بوت.

### أوامر البث

```text
/stream
/stream_platform Facebook facebook rtmps://live-api-s.facebook.com:443/rtmp STREAM_KEY
/stream_platform Telegram telegram rtmp://your-telegram-rtmp-url STREAM_KEY
/stream_start https://youtube.com/watch?v=VIDEO_ID
/stream_status
/stream_stop
```

### المتطلبات

- نفس `TELEGRAM_BOT_TOKEN`
- نفس `TELEGRAM_OWNER_IDS`
- نفس قاعدة SQLite عبر `DATABASE_PATH`
- تثبيت `ffmpeg`
- تثبيت `yt-dlp`

مفاتيح RTMP تحفظ مشفرة داخل SQLite باستخدام `ENCRYPTION_KEY`.

### متغيرات إضافية

```env
FFMPEG_PATH=ffmpeg
YTDLP_PATH=yt-dlp
STREAM_VIDEO_BITRATE=4500k
STREAM_AUDIO_BITRATE=160k
STREAM_BUFFER_SIZE=9000k
STREAM_FPS=30
STREAM_GOP=60
STREAM_FALLBACK_PRESET=veryfast
STREAM_GRACEFUL_STOP_SECONDS=8
```

### ملاحظة تشغيل

على Railway يجب أن تكون الخطة والموارد مناسبة، لأن FFmpeg يستهلك CPU وBandwidth. للبث المستقر متعدد الوجهات يفضّل VPS.

---

## تحديثات البث المباشر

راجع الملف `STREAMING_DEPLOY.md` لمعرفة أوامر البث، إضافة قنوات تليجرام، بث الصوت كفيديو، ومتغيرات النشر المطلوبة.

## Auto Project Normalizer Report

This project was normalized automatically after archive extraction.

- Detected project type: `python`
- Original detected root: `/tmp/moataz_repo_agent/8549357772/moataz-repo-agent-pro-streaming-plus-2_replace_extracted`
- Normalized root: repository root
- Suitable platforms: `Railway, Render, Docker`

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
