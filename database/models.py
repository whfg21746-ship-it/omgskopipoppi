"""SQLite schema definitions and migration helpers."""

SCHEMA_SQL = """
-- Таблица задач на скрапинг
CREATE TABLE IF NOT EXISTS scrape_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    community_url TEXT NOT NULL UNIQUE,
    community_id TEXT NOT NULL,
    token_address TEXT,
    token_name TEXT,
    chain TEXT,
    market_cap REAL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    scrape_after DATETIME NOT NULL,
    started_at DATETIME,
    completed_at DATETIME,
    status TEXT DEFAULT 'pending',
    usernames_count INTEGER DEFAULT 0,
    error_message TEXT
);

-- Таблица юзернеймов
CREATE TABLE IF NOT EXISTS usernames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    community_id TEXT NOT NULL,
    task_id INTEGER NOT NULL,
    scraped_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES scrape_tasks(id),
    UNIQUE(username, community_id)
);

-- Индексы
CREATE INDEX IF NOT EXISTS idx_tasks_status ON scrape_tasks(status, scrape_after);
CREATE INDEX IF NOT EXISTS idx_usernames_community ON usernames(community_id);
CREATE INDEX IF NOT EXISTS idx_usernames_username ON usernames(username);
"""
