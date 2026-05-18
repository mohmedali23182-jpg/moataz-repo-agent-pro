from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    telegram_bot_token: str = Field(default='', alias='TELEGRAM_BOT_TOKEN')
    telegram_owner_ids: str = Field(default='', alias='TELEGRAM_OWNER_IDS')
    public_url: str = Field(default='', alias='PUBLIC_URL')
    telegram_webhook_secret: str = Field(default='change_me', alias='TELEGRAM_WEBHOOK_SECRET')

    admin_api_token: str = Field(default='', alias='ADMIN_API_TOKEN')
    agent_api_token: str = Field(default='', alias='AGENT_API_TOKEN')

    github_token: str = Field(default='', alias='GITHUB_TOKEN')
    github_default_branch: str = Field(default='main', alias='GITHUB_DEFAULT_BRANCH')
    github_app_id: str = Field(default='', alias='GITHUB_APP_ID')
    github_app_private_key: str = Field(default='', alias='GITHUB_APP_PRIVATE_KEY')
    github_client_id: str = Field(default='', alias='GITHUB_CLIENT_ID')
    github_client_secret: str = Field(default='', alias='GITHUB_CLIENT_SECRET')
    github_app_webhook_secret: str = Field(default='', alias='GITHUB_APP_WEBHOOK_SECRET')

    database_path: str = Field(default='/app/_data/agent.db', alias='DATABASE_PATH')
    database_url: str = Field(default='', alias='DATABASE_URL')
    direct_url: str = Field(default='', alias='DIRECT_URL')
    encryption_key: str = Field(default='', alias='ENCRYPTION_KEY')

    max_upload_mb: int = Field(default=50, alias='MAX_UPLOAD_MB')
    max_extracted_mb: int = Field(default=200, alias='MAX_EXTRACTED_MB')
    max_extracted_files: int = Field(default=500, alias='MAX_EXTRACTED_FILES')
    work_dir: str = Field(default='/tmp/moataz_repo_agent', alias='WORK_DIR')

    supabase_url: str = Field(default='', alias='SUPABASE_URL')
    supabase_service_role_key: str = Field(default='', alias='SUPABASE_SERVICE_ROLE_KEY')
    supabase_anon_key: str = Field(default='', alias='SUPABASE_ANON_KEY')
    supabase_allowed_tables: str = Field(default='', alias='SUPABASE_ALLOWED_TABLES')
    supabase_allow_sql: bool = Field(default=False, alias='SUPABASE_ALLOW_SQL')

    agent_allow_terminal: bool = Field(default=False, alias='AGENT_ALLOW_TERMINAL')
    agent_require_approval: bool = Field(default=True, alias='AGENT_REQUIRE_APPROVAL')
    agent_max_command_seconds: int = Field(default=1200, alias='AGENT_MAX_COMMAND_SECONDS')
    agent_allowed_commands: str = Field(default='npm,pnpm,yarn,python,pip,pytest,node,git,ls,cat,sed,grep', alias='AGENT_ALLOWED_COMMANDS')
    agent_default_workdir: str = Field(default='.', alias='AGENT_DEFAULT_WORKDIR')
    agent_workflow_file: str = Field(default='agent-command.yml', alias='AGENT_WORKFLOW_FILE')



    # Platform connectors. Tokens may also be stored per user through Telegram commands.
    connectors_enabled: bool = Field(default=True, alias='CONNECTORS_ENABLED')
    connectors_require_owner: bool = Field(default=True, alias='CONNECTORS_REQUIRE_OWNER')
    railway_api_token: str = Field(default='', alias='RAILWAY_API_TOKEN')
    railway_token: str = Field(default='', alias='RAILWAY_TOKEN')
    vercel_token: str = Field(default='', alias='VERCEL_TOKEN')
    vercel_team_id: str = Field(default='', alias='VERCEL_TEAM_ID')
    render_api_key: str = Field(default='', alias='RENDER_API_KEY')
    netlify_auth_token: str = Field(default='', alias='NETLIFY_AUTH_TOKEN')
    fly_api_token: str = Field(default='', alias='FLY_API_TOKEN')
    replit_token: str = Field(default='', alias='REPLIT_TOKEN')
    google_drive_token: str = Field(default='', alias='GOOGLE_DRIVE_TOKEN')
    google_drive_folder_id: str = Field(default='', alias='GOOGLE_DRIVE_FOLDER_ID')
    connector_api_token: str = Field(default='', alias='CONNECTOR_API_TOKEN')

    # AI gateway. Can be configured via Railway or dynamically via /ai_connect.
    ai_default_provider: str = Field(default='openrouter', alias='AI_DEFAULT_PROVIDER')
    ai_default_model: str = Field(default='openai/gpt-4o-mini', alias='AI_DEFAULT_MODEL')
    ai_base_url: str = Field(default='', alias='AI_BASE_URL')
    ai_api_key: str = Field(default='', alias='AI_API_KEY')
    openai_api_key: str = Field(default='', alias='OPENAI_API_KEY')
    openrouter_api_key: str = Field(default='', alias='OPENROUTER_API_KEY')
    gemini_api_key: str = Field(default='', alias='GEMINI_API_KEY')
    anthropic_api_key: str = Field(default='', alias='ANTHROPIC_API_KEY')
    groq_api_key: str = Field(default='', alias='GROQ_API_KEY')
    mistral_api_key: str = Field(default='', alias='MISTRAL_API_KEY')
    together_api_key: str = Field(default='', alias='TOGETHER_API_KEY')
    perplexity_api_key: str = Field(default='', alias='PERPLEXITY_API_KEY')
    deepseek_api_key: str = Field(default='', alias='DEEPSEEK_API_KEY')
    xai_api_key: str = Field(default='', alias='XAI_API_KEY')
    cohere_api_key: str = Field(default='', alias='COHERE_API_KEY')
    huggingface_api_key: str = Field(default='', alias='HUGGINGFACE_API_KEY')
    fireworks_api_key: str = Field(default='', alias='FIREWORKS_API_KEY')

    # Download center. Direct downloads only; Google Play pages are reported, not bypassed.
    download_allow_html: bool = Field(default=False, alias='DOWNLOAD_ALLOW_HTML')

    log_level: str = Field(default='INFO', alias='LOG_LEVEL')

    @property
    def owner_ids(self) -> set[int]:
        result: set[int] = set()
        for item in self.telegram_owner_ids.split(','):
            item = item.strip()
            if item.isdigit():
                result.add(int(item))
        return result

    @property
    def webhook_path(self) -> str:
        return f'/api/telegram/webhook/{self.telegram_webhook_secret}'

    @property
    def legacy_webhook_path(self) -> str:
        return f'/telegram/webhook/{self.telegram_webhook_secret}'

    @property
    def webhook_url(self) -> str:
        return self.public_url.rstrip('/') + self.webhook_path

    @property
    def allowed_tables(self) -> set[str]:
        return {item.strip() for item in self.supabase_allowed_tables.split(',') if item.strip()}

    @property
    def allowed_commands(self) -> set[str]:
        return {item.strip() for item in self.agent_allowed_commands.split(',') if item.strip()}

    def ensure_dirs(self) -> None:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.work_dir).mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
