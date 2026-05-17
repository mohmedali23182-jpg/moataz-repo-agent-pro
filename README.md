# Moataz Repo Agent v2

بوت Telegram + لوحة Web لإدارة مستودعات GitHub وفك ضغط الملفات ورفعها، مع ربط اختياري بـ Supabase.

> يعمل بالصلاحيات التي تمنحها أنت فقط: GitHub PAT أو GitHub App. لا يستخدم بريد GitHub أو كلمة المرور.

## الميزات

- Telegram webhook جاهز لـ Railway.
- لوحة Web شفافة على `/` فيها: إنشاء مستودع، استعراض ملفات، كتابة/حذف ملف، فك ضغط ورفع، قراءة Supabase.
- فك ضغط آمن: zip, rar, 7z, tar, tar.gz.
- GitHub API:
  - list repos
  - create repo
  - list files
  - read/write/delete files
  - upload file
  - unpack archive and upload files
  - create branch
  - create pull request
  - workflow dispatch داخل الكود كخدمة جاهزة للتوسعة
- تخزين GitHub token الخاص بالمستخدم مشفرًا محليًا في SQLite.
- حماية لوحة API عبر ADMIN_API_TOKEN.

## النشر على Railway

1. ارفع هذا المجلد إلى GitHub.
2. افتح Railway وأنشئ Project من GitHub repo.
3. ضع المتغيرات التالية.
4. Deploy.
5. افتح `/health` للتأكد.
6. افتح Telegram وأرسل `/start`.

## متغيرات Railway

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_OWNER_IDS=8185788415,8549357772
PUBLIC_URL=https://your-service.up.railway.app
TELEGRAM_WEBHOOK_SECRET=change_this_long_secret

ADMIN_API_TOKEN=change_this_long_random_token

GITHUB_TOKEN=
GITHUB_DEFAULT_BRANCH=main

DATABASE_PATH=/app/_data/agent.db
ENCRYPTION_KEY=

MAX_UPLOAD_MB=50
MAX_EXTRACTED_MB=200
MAX_EXTRACTED_FILES=500
WORK_DIR=/tmp/moataz_repo_agent

SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=
SUPABASE_ANON_KEY=
SUPABASE_ALLOWED_TABLES=profiles,posts,categories

LOG_LEVEL=INFO
```

## GitHub Token المطلوب

للتجربة السريعة استخدم Classic PAT مع:

- repo
- workflow إذا ستعدل ملفات `.github/workflows`

للإنتاج استخدم Fine-grained token:

- Repository access: المستودعات المطلوبة فقط
- Contents: Read and write
- Metadata: Read-only
- Pull requests: Read and write
- Issues: Read and write اختياري
- Workflows: Read and write إذا تحتاج تعديل GitHub Actions
- Administration: Read and write إذا تريد إضافة collaborators أو إعدادات repo

## أوامر Telegram

```text
/start
/menu
/token github_pat_xxx
/clear_token
/repo https://github.com/OWNER/REPO
/branch main
/info
/repos
/create_repo my-project private
/ls
/ls src
/read README.md
/write path/file.txt | المحتوى الجديد
/delete path/file.txt
/new_branch feature-name
/pr عنوان الطلب | وصف الطلب
```

### رفع ملف

أرسل أي ملف ثم رد عليه:

```text
/upload target/path.ext
```

### فك ضغط ورفع

أرسل ملف zip/rar/7z/tar ثم رد عليه:

```text
/unpack target/folder
```

### Supabase

```text
/supabase posts 10
```

الجدول يجب أن يكون موجودًا في `SUPABASE_ALLOWED_TABLES`.

## اللوحة

افتح رابط Railway الرئيسي. مثال:

```text
https://your-service.up.railway.app/
```

ضع:

- ADMIN_API_TOKEN
- GitHub Token اختياري
- repo
- branch

ثم نفذ من الواجهة.

## ملاحظات أمان مهمة

- لا ترسل GitHub Token في مجموعة عامة.
- الأفضل وضع GITHUB_TOKEN داخل Railway إذا البوت خاص بك فقط.
- إذا تريد نظامًا عامًا لعدة مستخدمين، استخدم GitHub App/OAuth لاحقًا بدل استقبال التوكن يدويًا.
- لا تعطِ صلاحية Administration إلا عند الحاجة.


## Auto Project Normalizer

تمت إضافة خدمة ترتيب المشاريع تلقائيًا بعد فك الضغط. عند استخدام `/unpack` أو رفع أرشيف من اللوحة مع خيار الترتيب، سيقوم البوت بـ:

- اكتشاف جذر المشروع الحقيقي حتى لو كان داخل `target/folder/...`.
- نقل محتويات الجذر الحقيقي إلى بنية نظيفة.
- تجاهل الملفات الثقيلة أو الحساسة مثل `.env`, `node_modules`, `.next`, `.git`.
- إنشاء `.env.example`, `Dockerfile`, `railway.json`, وREADME عند الحاجة.
- رفع النسخة المرتبة إلى GitHub.

الأوامر الجديدة:

```text
/normalize
```

يرتب المشروع ويرسل لك `normalized-project.zip` بدون رفع إلى GitHub.

```text
/unpack target/folder
```

يفك الضغط، يرتب المشروع، ثم يرفعه إلى GitHub. استخدم `.` أو `root` كمسار هدف للرفع إلى جذر المستودع.

```text
/unpack_raw target/folder
```

يفك الضغط ويرفع كما هو بدون ترتيب.

> الترتيب يجعل بنية المشروع قابلة للنشر، لكنه لا يضمن نجاح التشغيل إذا كان الكود نفسه يحتوي أخطاء أو تنقصه متغيرات البيئة وقاعدة البيانات.

## GitHub Agent + Terminal Commands

This version adds a real Telegram-side agent layer:

### Direct GitHub patching

Use `/agent` or `/patch` for deterministic file operations through the GitHub Contents API:

```text
/agent https://github.com/OWNER/REPO
replace app/config.py
<full new file content>
```

Supported operations:

```text
read path/to/file
replace path/to/file
append path/to/file
delete path/to/file
mkdir path/to/folder
regex path/to/file
PATTERN
---
REPLACEMENT
```

### Terminal execution through GitHub Actions

Use `/term` to run safe terminal commands in the target repository through `workflow_dispatch`:

```text
/term https://github.com/OWNER/REPO
npm run build
```

Optional flags, each on its own line:

```text
--workdir=.
--commit
--commit-message=Apply terminal fixes
```

If `AGENT_REQUIRE_APPROVAL=true`, the bot prepares the terminal command and waits for:

```text
/approve
```

Cancel with:

```text
/cancel_term
```

Install the workflow manually if needed:

```text
/install_workflow https://github.com/OWNER/REPO
```

The bot auto-installs `.github/workflows/agent-command.yml` when `/term` is first used. GitHub may need 30-60 seconds before the workflow becomes dispatchable.

### Required GitHub token permissions

For `/agent` patching:

```text
Contents: Read and write
Metadata: Read-only
```

For `/term` and installing/running the workflow:

```text
Contents: Read and write
Actions: Read and write
Workflows: Read and write
```

### Railway variables for agent mode

```env
AGENT_ALLOW_TERMINAL=true
AGENT_REQUIRE_APPROVAL=true
AGENT_MAX_COMMAND_SECONDS=1200
AGENT_ALLOWED_COMMANDS=npm,pnpm,yarn,python,pip,pytest,node,git,ls,cat,sed,grep
AGENT_DEFAULT_WORKDIR=.
```

Security notes:

- Terminal execution is disabled unless `AGENT_ALLOW_TERMINAL=true`.
- Dangerous shell patterns are rejected.
- Only commands whose first word is in `AGENT_ALLOWED_COMMANDS` are accepted.
- Keep `AGENT_REQUIRE_APPROVAL=true` for production.
