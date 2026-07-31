-- Расширения
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Пользователи
CREATE TABLE IF NOT EXISTS users (
  chat_id BIGINT PRIMARY KEY,
  username TEXT,
  first_name TEXT,
  active BOOLEAN DEFAULT true,
  plan TEXT DEFAULT 'free',
  created_at TIMESTAMPTZ DEFAULT NOW(),
  last_seen TIMESTAMPTZ DEFAULT NOW()
);

-- Общий пул вакансий (одна запись на вакансию для всех)
CREATE TABLE IF NOT EXISTS vacancies_pool (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  title TEXT,
  company TEXT,
  salary TEXT,
  location TEXT,
  url TEXT,
  text TEXT,
  shared_analysis JSONB,
  analyzed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Индексы для быстрого поиска
CREATE INDEX IF NOT EXISTS idx_vacancies_pool_source 
  ON vacancies_pool(source);
CREATE INDEX IF NOT EXISTS idx_vacancies_pool_created 
  ON vacancies_pool(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_vacancies_pool_title 
  ON vacancies_pool USING gin(to_tsvector('russian', title));

-- Персональные вакансии (per user)
CREATE TABLE IF NOT EXISTS user_vacancies (
  id SERIAL PRIMARY KEY,
  chat_id BIGINT REFERENCES users(chat_id) ON DELETE CASCADE,
  vacancy_id TEXT REFERENCES vacancies_pool(id) ON DELETE CASCADE,
  personal_score INTEGER,
  status TEXT DEFAULT 'new',
  cover_letter TEXT,
  viewed_at TIMESTAMPTZ,
  applied_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(chat_id, vacancy_id)
);

CREATE INDEX IF NOT EXISTS idx_user_vacancies_chat_id 
  ON user_vacancies(chat_id);
CREATE INDEX IF NOT EXISTS idx_user_vacancies_status 
  ON user_vacancies(chat_id, status);

-- Настройки per user
CREATE TABLE IF NOT EXISTS user_settings (
  chat_id BIGINT REFERENCES users(chat_id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  value TEXT,
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (chat_id, key)
);

-- Дефолтные настройки вставляются триггером при регистрации

-- Каналы per user
CREATE TABLE IF NOT EXISTS user_channels (
  chat_id BIGINT REFERENCES users(chat_id) ON DELETE CASCADE,
  username TEXT NOT NULL,
  title TEXT,
  active BOOLEAN DEFAULT true,
  added_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (chat_id, username)
);

-- Примеры писем per user
CREATE TABLE IF NOT EXISTS cover_examples (
  id SERIAL PRIMARY KEY,
  chat_id BIGINT REFERENCES users(chat_id) ON DELETE CASCADE,
  text TEXT NOT NULL,
  company TEXT,
  vacancy_id TEXT,
  result TEXT DEFAULT 'unknown',
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Резюме per user
CREATE TABLE IF NOT EXISTS user_resumes (
  chat_id BIGINT PRIMARY KEY REFERENCES users(chat_id) ON DELETE CASCADE,
  raw_text TEXT,
  profile JSONB,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- История интервью per user
CREATE TABLE IF NOT EXISTS interview_history (
  id SERIAL PRIMARY KEY,
  chat_id BIGINT REFERENCES users(chat_id) ON DELETE CASCADE,
  vacancy_id TEXT,
  company TEXT,
  interview_date TIMESTAMPTZ,
  notes TEXT,
  result TEXT,
  questions JSONB,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Дайджест — последняя отправка per user
CREATE TABLE IF NOT EXISTS digest_log (
  chat_id BIGINT PRIMARY KEY REFERENCES users(chat_id) ON DELETE CASCADE,
  last_sent_at TIMESTAMPTZ,
  last_vacancy_ids JSONB
);

-- Функция обновления updated_at
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Триггеры
CREATE TRIGGER user_vacancies_updated_at
  BEFORE UPDATE ON user_vacancies
  FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Дефолтные данные для новых пользователей (функция)
CREATE OR REPLACE FUNCTION init_user_defaults(p_chat_id BIGINT)
RETURNS VOID AS $$
BEGIN
  INSERT INTO user_settings (chat_id, key, value) VALUES
    (p_chat_id, 'score_threshold', '7'),
    (p_chat_id, 'monitoring_active', 'true'),
    (p_chat_id, 'hh_keywords', '["IT project manager","руководитель IT проектов","product manager IT","delivery manager"]'),
    (p_chat_id, 'digest_time', '09:00'),
    (p_chat_id, 'follow_up_days', '3')
  ON CONFLICT DO NOTHING;

  INSERT INTO user_channels (chat_id, username, title) VALUES
    (p_chat_id, '@careerhub_pm', 'CareerHub PM'),
    (p_chat_id, '@JobOfferInside', 'Job Offer Inside'),
    (p_chat_id, '@projects_jobs', 'Projects Jobs'),
    (p_chat_id, '@jobs_in_it_remoute', 'IT Remote Jobs'),
    (p_chat_id, '@careerstation_pm', 'CareerStation PM'),
    (p_chat_id, '@avito_career', 'Avito Career'),
    (p_chat_id, '@zvuk_jobs', 'Звук Jobs'),
    (p_chat_id, '@hcareers_jobs', 'HCareers Jobs')
  ON CONFLICT DO NOTHING;
END;
$$ LANGUAGE plpgsql;
