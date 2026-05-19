-- =========================================================
-- Moataz Repo Agent - Supabase Database Schema
-- =========================================================

-- 1. جدول المستخدمين (Users)
CREATE TABLE IF NOT EXISTS users (
    telegram_id BIGINT PRIMARY KEY,
    github_token_enc TEXT,
    repo TEXT,
    branch TEXT,
    installation_id TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 2. جدول الملفات الشخصية (Profiles)
CREATE TABLE IF NOT EXISTS profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_id BIGINT REFERENCES users(telegram_id),
    full_name TEXT,
    bio TEXT,
    avatar_url TEXT,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 3. جدول المنشورات (Posts)
CREATE TABLE IF NOT EXISTS posts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    author_id BIGINT REFERENCES users(telegram_id),
    title TEXT NOT NULL,
    content TEXT,
    status TEXT DEFAULT 'draft',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 4. جدول التصنيفات (Categories)
CREATE TABLE IF NOT EXISTS categories (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Multistreaming additions
CREATE TABLE IF NOT EXISTS stream_channels (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  telegram_id INTEGER NOT NULL,
  chat_id INTEGER,
  username TEXT,
  title TEXT NOT NULL,
  rtmp_url TEXT DEFAULT '',
  stream_key_enc TEXT DEFAULT '',
  enabled INTEGER DEFAULT 1,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(telegram_id, username)
);

CREATE TABLE IF NOT EXISTS stream_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  telegram_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  source TEXT NOT NULL,
  source_type TEXT NOT NULL,
  status TEXT NOT NULL,
  destinations_json TEXT DEFAULT '[]',
  selected_channels_json TEXT DEFAULT '[]',
  pid INTEGER,
  started_at TEXT DEFAULT CURRENT_TIMESTAMP,
  ended_at TEXT,
  error TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_stream_channels_user_enabled ON stream_channels(telegram_id, enabled);
CREATE INDEX IF NOT EXISTS idx_stream_channels_username ON stream_channels(telegram_id, username);
CREATE INDEX IF NOT EXISTS idx_stream_history_user_status ON stream_history(telegram_id, status);
