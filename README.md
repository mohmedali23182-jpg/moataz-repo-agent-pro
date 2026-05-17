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
