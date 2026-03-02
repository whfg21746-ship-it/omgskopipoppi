# Полный технический отчёт по архитектуре проекта

## 1. Общая архитектура

### 1.1. Назначение

Проект — единый asyncio-сервис, который:
- Мониторит API DexScreener для обнаружения новых криптотокенов с X (Twitter) community-ссылками
- Отправляет Telegram-алерты при обнаружении токенов, проходящих фильтры
- Автоматически скрапит usernames участников X-сообществ через headless-браузер (Playwright)
- Автоматически постит в X-communities от имени пула аккаунтов
- Предоставляет Telegram-бот в качестве единого UI для управления всей системой

### 1.2. Точка входа: `main.py`

Файл: `main.py` (540 строк)

Приложение запускается через `asyncio.run(main())`. Перед запуском проверяется наличие `TELEGRAM_BOT_TOKEN` и `TELEGRAM_USER_IDS` в .env.

Функция `main()` инициализирует все компоненты и запускает три конкурентных цикла через `asyncio.gather()`:

```python
await asyncio.gather(
    monitor.run(),        # Alerter loop (DexScreener API polling)
    scraper_loop(db, bot, pool),  # Scraper loop (Playwright subprocess)
    bot.run(),            # Telegram bot (getUpdates long-polling)
)
```

### 1.3. Связи между модулями

```
main.py (orchestrator)
  |
  +-- config.py              <-- все переменные окружения
  +-- token_pool.py          <-- пул auth_token'ов для scraper'а
  |
  +-- alerter/
  |     +-- monitor.py       <-- DexScreenerMonitor (polling DexScreener API)
  |     +-- filters.py       <-- TokenFilter (фильтрация токенов)
  |
  +-- bot/
  |     +-- telegram_bot.py  <-- TelegramBot (UI, команды, callback'и)
  |
  +-- database/
  |     +-- db.py            <-- Database (SQLite CRUD)
  |     +-- models.py        <-- SCHEMA_SQL (DDL)
  |
  +-- poster/
  |     +-- post_pool.py     <-- PostPool (аккаунты, шаблоны твитов, изображения)
  |     +-- worker.py        <-- post_to_community() (координатор постинга)
  |     +-- x_api.py         <-- XPoster (GraphQL API X/Twitter)
  |
  +-- scraper/
        +-- auth.py          <-- inject_auth_token(), check_session_validity()
        +-- runner.py        <-- CLI entry point для subprocess'а
        +-- worker.py        <-- scrape_community() (Playwright scraping)
```

### 1.4. Коммуникация между компонентами

Модули общаются через:
- **Callback-функции** (`on_alert`, `on_new_task`) — monitor вызывает их при обнаружении нового токена
- **Глобальные ссылки** (`_bot`, `_db`, `_pool`, `_post_pool`) — разделяемое состояние в `main.py`
- **SQLite база данных** — scraper и bot читают/пишут задачи и usernames
- **JSON-файлы на диске** — memory.json, auth_tokens.json, post_config.json, filters.json
- **asyncio.create_task()** — автопостинг запускается как фоновая задача, не блокируя alerter
- **asyncio.create_subprocess_exec()** — scraper запускает Playwright в отдельном процессе (killable)

### 1.5. Зависимости (requirements.txt)

```
aiohttp>=3.10,<4.0        -- HTTP клиент для Telegram API и DexScreener API
playwright==1.49.1         -- headless browser для скрапинга
python-dotenv==1.0.1       -- загрузка .env
curl_cffi                  -- HTTP клиент с Chrome impersonation для X API
XClientTransaction         -- генерация x-client-transaction-id для X API
beautifulsoup4             -- парсинг HTML страницы x.com для XClientTransaction
```

---

## 2. Детальное описание каждого модуля

---

### 2.1. Модуль `alerter/`

#### 2.1.1. `alerter/filters.py`

Файл: `alerter/filters.py` (149 строк)

**Класс `TokenFilter`** — конфигурируемый фильтр токенов с сохранением на диск.

**Инициализация**: загружает дефолтные фильтры из `config.FILTERS`, затем оверрайдит из файла `data/filters.json`.

**Поля фильтров**:
- `chains` (list[str]) — whitelist блокчейнов (по умолчанию `["solana"]`)
- `min_mcap` / `max_mcap` (float) — диапазон market cap ($10,000 - $10,000,000)
- `min_liquidity` (float) — минимальная ликвидность ($5,000)
- `min_age_minutes` / `max_age_hours` (int) — возраст пары (5 мин - 24 ч)

**Методы проверки**:

| Метод | Описание |
|-------|----------|
| `check_chain(chain_id)` | Проверяет, что chain в whitelist |
| `check_pair_age(pair_created_at)` | Проверяет возраст пары в миллисекундах |
| `check_mcap(fdv)` | Проверяет FDV (fully diluted valuation) в диапазоне |
| `check_liquidity(liquidity_usd)` | Проверяет минимальную ликвидность |
| `passes_all(token_data)` | Запускает все проверки, возвращает `(bool, reason)` |

**Методы модификации**: `update_mcap()`, `update_liquidity()`, `update_chains()`, `update_age()`, `set()` — все автоматически вызывают `save()` для персистенции.

**Взаимодействие**: используется `DexScreenerMonitor` и `TelegramBot` (для runtime-конфигурации через inline-кнопки).

#### 2.1.2. `alerter/monitor.py`

Файл: `alerter/monitor.py` (349 строк)

**Класс `TokenMemory`** — in-memory хранилище виденных токенов.
- `tokens: dict[str, dict]` — адрес → метаданные (chain, first_seen, has_community)
- `alerted: set[str]` — множество адресов, по которым уже отправлен алерт
- Персистенция: `data/memory.json`
- `cleanup()` — удаляет записи старше `MEMORY_HOURS` (24 часа)

**Вспомогательные async-функции**:
- `fetch_profiles(session)` — GET к `https://api.dexscreener.com/token-profiles/latest/v1`, возвращает list[dict]
- `fetch_token_data(session, address)` — GET к `https://api.dexscreener.com/latest/dex/tokens/{address}`, возвращает лучшую пару по ликвидности
- `extract_community_url(profile)` — ищет URL вида `x.com/i/communities/{id}` в ссылках профиля
- `extract_community_id(url)` — извлекает ID из community URL через regex
- `extract_twitter_url(profile)` — извлекает любой Twitter/X URL

**Класс `DexScreenerMonitor`** — основной polling loop.

Конструктор принимает:
- `token_filter: TokenFilter`
- `memory: TokenMemory`
- `on_alert: Callable` — callback для отправки алерта
- `on_new_task: Callable` — callback для создания scrape-задачи

**Метод `run()`**: бесконечный цикл с интервалом `CHECK_INTERVAL` (30 сек). При ошибке интервал удваивается. После `API_MAX_CONSECUTIVE_FAILURES` (5) подряд — отправляет алерт.

**Метод `_poll(session)`** (один цикл):
1. Запрашивает `fetch_profiles()` — список последних "promoted" профилей
2. Вызывает `memory.cleanup()` для очистки старых записей
3. Для каждого профиля:
   - Пропускает уже оповещённые (`memory.was_alerted`)
   - Извлекает community URL; если нет — сохраняет в memory без алерта
   - Запрашивает детальные данные пары через `fetch_token_data`
   - Пропускает через `filter.passes_all()`
   - При прохождении формирует текст алерта с markdown-escaping
   - Вызывает `on_alert(alert_text)` и `on_new_task(members_url, community_id, ...)`

**Формат алерта**:
```
New token with X Community!

Token: SafeName ($SafeSymbol)
Chain: solana
Address: 1a2b3c4d...5e6f7g
MCap: $150,000
Liquidity: $25,000

Community: https://x.com/i/communities/123456
Members: https://x.com/i/communities/123456/members

Scraping will start at 14:30 UTC
```

---

### 2.2. Модуль `bot/`

#### 2.2.1. `bot/telegram_bot.py`

Файл: `bot/telegram_bot.py` (1712 строк)

**Класс `TelegramBot`** — полностью самописный async Telegram-бот, работающий через raw HTTP (без python-telegram-bot или aiogram).

**Конструктор** принимает:
- `db: Database` — доступ к БД
- `token_filter: TokenFilter` — runtime-настройка фильтров
- `token_pool: TokenPool` — управление scraper-токенами
- `post_pool: PostPool` — управление аккаунтами/шаблонами для постинга
- `on_add_community` / `on_update_token` / `on_update_tokens_bulk` — callback'и в main.py

**Внутреннее состояние**:
- `_last_update_id` — offset для getUpdates
- `_start_time` — время запуска (для uptime)
- `_buffer: deque[str]` — буфер неотправленных сообщений (до 200)
- `_waiting_for: dict[str, dict]` — state machine для текстового ввода (например, ожидание auth_token после нажатия кнопки "Add Token")
- `_repost_context: dict[str, dict]` — контекст для повторного постинга (хранит community_id, url, token_name, token_symbol)
- `_scraper_paused: bool` — флаг паузы скрапера

**Outbound-методы**:

| Метод | Описание |
|-------|----------|
| `broadcast(text)` | Отправляет текст всем пользователям из `TELEGRAM_USER_IDS` |
| `broadcast_with_markup(text, reply_markup)` | Отправляет с inline-клавиатурой |
| `broadcast_main_keyboard(text)` | Отправляет с persistent reply-клавиатурой |
| `_send_plain(chat_id, text, session)` | Отправка plain text |
| `_send_with_markup(chat_id, text, markup, session)` | Отправка с markup, возвращает dict ответа |
| `_edit_message(chat_id, message_id, text, markup, session)` | Редактирование существующего сообщения |
| `_answer_callback(callback_id, text, session)` | Ответ на callback_query |
| `_send_document(chat_id, filename, content, caption, session)` | Отправка .txt файла |
| `_download_file(file_id, session)` | Скачивание файла (текст) |
| `_download_file_bytes(file_id, session)` | Скачивание файла (bytes) |

**Persistent Reply Keyboard** (всегда внизу чата):
```
[Status]         [Tasks]
[Scraper Tokens] [Post Accounts]
[Post Texts]     [Post Images]
[Filters]        [Export]
[Auto-Post: ON]  [Pause All / Resume All]
```

**Inline Keyboard** (Main Menu через callback):
```
[Status]          [Tasks]
[Scraper Tokens]  [Post Accounts]
[Post Texts]      [Post Images]
[Filters]         [Export]
[Auto-Post: ON]   [Add Community]
```

**Текстовые команды** (backward-compatible):

| Команда | Описание |
|---------|----------|
| `/start`, `/help`, `/menu` | Показать Control Panel с клавиатурой |
| `/status` | Статистика: uptime, задачи, usernames, токены |
| `/tasks` | Последние 10 задач с иконками статуса |
| `/export <community_id>` | Скачать usernames как .txt |
| `/export_all` | Скачать ВСЕ уникальные usernames |
| `/retry <task_id>` | Перезапустить failed-задачу |
| `/add <community_url>` | Добавить community в очередь без задержки |
| `/token <auth_token>` | Добавить scraper token |
| `/tokens` | Показать summary пула |
| `/deltoken <num>` | Удалить токен по номеру |
| `/filters [mcap/liquidity/chains/age]` | Просмотр/изменение фильтров |

**Callback-обработка** (inline кнопки):

Метод `_handle_callback(callback, session)` обрабатывает около 30 различных callback_data:
- `menu` — возврат в главное меню
- `status`, `tasks` — информационные
- `scraper_tokens` / `scraper_tokens:add` / `scraper_tokens:upload` / `scraper_tokens:clear_invalid` / `scraper_tokens:clear_all` / `scraper_tokens:clear_all_confirm`
- `post_accounts` / `post_accounts:add` / `post_accounts:upload` / `post_accounts:clear_invalid` / `post_accounts:clear_all` / `post_accounts:clear_all_confirm`
- `post_texts` / `post_texts:add` / `post_texts:upload` / `post_texts:clear_all`
- `post_images` / `post_images:upload` / `post_images:clear_all` / `post_images:toggle_photo`
- `filters` / `filters:mcap` / `filters:liquidity` / `filters:chains` / `filters:age`
- `export` / `export:all` / `export:community`
- `toggle_autopost`, `add_community`
- `repost:<uuid_key>` — повторный пост с другого аккаунта

**State Machine** (`_waiting_for`):

Когда пользователь нажимает inline-кнопку вроде "Add Token", бот запоминает состояние `{"waiting_for": "scraper_token"}` для данного chat_id. Следующее текстовое сообщение обрабатывается как ответ.

Поддерживаемые waiting-состояния:
- `scraper_token` — ожидание scraper auth_token
- `scraper_token_file` — ожидание .txt файла с токенами
- `post_account` — ожидание posting auth_token (с авто-извлечением 40-символьного hex)
- `post_account_file` — ожидание .txt файла с posting-аккаунтами
- `post_text` — ожидание текста шаблона твита
- `post_text_file` — ожидание .txt файла с шаблонами
- `post_image` — ожидание фото/документа
- `filter_mcap` / `filter_liquidity` / `filter_chains` / `filter_age`
- `export_community_id` — ожидание community ID для экспорта
- `community_url` — ожидание URL для добавления community

**Как добавить новый хэндлер**:
1. Добавить обработку `callback_data` в `_handle_callback()` (для inline-кнопки) или текстовой команды в `_handle()`
2. Если нужен текстовый ввод — добавить ключ в `_waiting_for` и обработать в `_handle_waiting_input()`
3. Для persistent keyboard — добавить обработку в `_handle_keyboard_button()`

**Порядок обработки сообщения в `run()`**:
1. Callback query (inline кнопки) → `_handle_callback()`
2. Photo upload → `_handle_photo()`
3. Document upload → `_handle_document()`
4. Persistent keyboard button (не начинается с `/`) → `_handle_keyboard_button()`
5. Waiting input (не начинается с `/`) → `_handle_waiting_input()`
6. Slash-команды → `_handle()`

**Метод `run()`**:
- Удаляет webhook (на случай предыдущего использования)
- Long-polling через `getUpdates` с `timeout=30`
- При ошибке — `asyncio.sleep(5)`, повторная попытка
- Перед каждым циклом `_flush_buffer()` — попытка отправить буферизованные сообщения

---

### 2.3. Модуль `database/`

#### 2.3.1. `database/models.py`

Файл: `database/models.py` (37 строк)

Содержит константу `SCHEMA_SQL` — DDL для SQLite. Подробная схема описана в разделе 8.

#### 2.3.2. `database/db.py`

Файл: `database/db.py` (254 строки)

**Класс `Database`** — thread-safe SQLite wrapper (один connection на thread через `threading.local()`).

**Инициализация**:
- Путь к БД: `data/scraper.db` (из `config.DB_PATH`)
- PRAGMA: `journal_mode=WAL`, `foreign_keys=ON`
- Вызывает `_migrate()` — применяет SCHEMA_SQL
- Вызывает `_reset_stuck_tasks()` — сбрасывает `in_progress` задачи в `pending` при старте

**Методы для задач**:

| Метод | Описание |
|-------|----------|
| `create_task(...)` | INSERT с уникальным `community_url`, возвращает task_id или None (дубликат) |
| `get_next_pending_task()` | Самая старая pending задача, где `scrape_after <= now` |
| `mark_task_in_progress(id)` | Ставит статус `in_progress`, фиксирует `started_at` |
| `mark_task_completed(id, count)` | Ставит `completed`, записывает `usernames_count` |
| `mark_task_failed(id, error)` | Ставит `failed`, записывает `error_message` |
| `retry_task(id)` | Сбрасывает failed → pending с `scrape_after = now` |
| `pause_all_pending()` | Ставит все pending в `failed` с ошибкой `auth_token_invalid` |
| `resume_paused_tasks()` | Возвращает задачи, отменённые из-за auth, в `pending` |
| `get_task_by_id(id)` | Получить задачу по ID |
| `get_recent_tasks(limit=10)` | Последние 10 задач |
| `get_stats()` | Количества по статусам + total unique usernames |
| `task_exists(community_url)` | Проверка существования задачи |

**Методы для usernames**:

| Метод | Описание |
|-------|----------|
| `save_usernames(task_id, community_id, usernames)` | Bulk INSERT с пропуском дубликатов (UNIQUE constraint). Возвращает кол-во новых |
| `get_usernames_by_community(community_id)` | Все usernames конкретного community |
| `get_all_unique_usernames()` | DISTINCT usernames из всей БД |

---

### 2.4. Модуль `poster/`

#### 2.4.1. `poster/post_pool.py`

Файл: `poster/post_pool.py` (277 строк)

**Класс `PostPool`** — управление аккаунтами для постинга, шаблонами твитов и изображениями.

**Хранилище**: `data/post_config.json`

**Конфигурация по умолчанию**:
```python
{
    "accounts": [],               # Список posting-аккаунтов
    "current_account_index": 0,   # Текущий аккаунт для ротации
    "tweets": [],                 # Шаблоны твитов
    "use_photo": True,            # Прикреплять фото к постам
    "delay_after_alert_sec": 10,  # Задержка перед постом после алерта
    "enabled": True,              # Глобальный переключатель автопоста
}
```

**Управление аккаунтами**:

| Метод | Описание |
|-------|----------|
| `add_account(auth_token)` | Добавляет аккаунт; при дубликате — реактивирует если invalid |
| `remove_account(index)` | Удаление по 0-based индексу |
| `rotate_account()` | Переключение на следующий valid аккаунт |
| `mark_invalid(index)` | Пометить аккаунт как невалидный |
| `get_current_account()` | Текущий валидный аккаунт (с auto-forward к следующему valid) |
| `clear_invalid_accounts()` | Удалить все невалидные |
| `clear_all_accounts()` | Удалить все |

**Управление шаблонами твитов**:

| Метод | Описание |
|-------|----------|
| `add_tweet(text)` | Добавить шаблон |
| `remove_tweet(index)` | Удалить по индексу |
| `clear_tweets()` | Удалить все |
| `get_random_tweet()` | Случайный шаблон (для постинга) |

Переменные в шаблонах: `{token_name}`, `{token_symbol}`, `{community_url}`

**Управление изображениями**:

| Метод | Описание |
|-------|----------|
| `save_image(filename, data)` | Сохранить в `data/post_images/` |
| `get_random_image_path()` | Случайное изображение |
| `list_images()` | Список файлов (.jpg, .png, .gif, .webp) |
| `clear_images()` | Удалить все |
| `set_use_photo(enabled)` | Включить/выключить фото |

#### 2.4.2. `poster/worker.py`

Файл: `poster/worker.py` (140 строк)

**Функция `post_to_community(community_id, community_url, token_name, token_symbol, post_pool)`** — синхронная функция, вызывается через `asyncio.to_thread()`.

**Процесс постинга** (шаг за шагом):
1. Получить текущий valid аккаунт из `post_pool.get_current_account()`
2. Создать `curl_cffi.requests.Session` с импersonацией Chrome 136
3. Получить `ct0` CSRF-токен через `XPoster.get_ct0()`
4. Создать `XClientTransaction` генератор через `XPoster.get_transaction_id()`
5. Вступить в community через `XPoster.join_community()`
6. Подождать 8-12 секунд (рандомизировано — join нужен для пропагации)
7. Получить случайный шаблон твита, подставить переменные `{token_name}`, `{token_symbol}`, `{community_url}`
8. Добавить 2 случайных emoji из набора для уникализации (избежание ошибки 187 — duplicate tweet)
9. Если `use_photo=True`, загрузить случайное изображение через `XPoster.upload_media()`
10. Опубликовать твит через `XPoster.create_tweet()` с `community_id` и `semantic_annotation_ids`
11. Извлечь `tweet_id` из ответа по пути `data.create_tweet.tweet_results.result.rest_id`

**Формат возвращаемого результата**:
```python
{
    "success": True/False,
    "tweet_id": "1234567890",
    "tweet_url": "https://x.com/i/status/1234567890",
    "error": None / "error message",
    "account_index": 0,
}
```

#### 2.4.3. `poster/x_api.py`

Файл: `poster/x_api.py` (490 строк)

**Класс `XPoster`** — статический класс для работы с GraphQL API X/Twitter.

Использует захардкоженный Bearer-токен X.

**Методы**:

**`get_ct0(session, auth_token)`**:
- POST к `https://twitter.com/i/api/1.1/account/update_profile.json` с `Cookie: auth_token=...`
- Извлекает `ct0` из cookies ответа (CSRF token)
- 3 попытки с retry

**`get_transaction_id()`**:
- GET к `https://x.com` — получение HTML страницы
- Парсинг через BeautifulSoup
- Извлечение URL ondemand-файла через `get_ondemand_file_url()`
- Создание `ClientTransaction` — генератор `x-client-transaction-id`

**`join_community(session, ct0, auth_token, xtid, community_id)`**:
- POST к `https://x.com/i/api/graphql/.../JoinCommunity`
- GraphQL мутация с `communityId` в variables
- Заголовки: Chrome 143 sec-ch-ua, полный набор security headers

**`upload_media(session, ct0, auth_token, xtid, image_path)`**:
- 3-шаговый процесс загрузки через `https://upload.x.com/i/media/upload.json`:
  1. **INIT**: `command=INIT, total_bytes=..., media_type=<detected>, media_category=tweet_image`
  2. **APPEND**: multipart upload с `CurlMime` (бинарные данные изображения)
  3. **FINALIZE**: `command=FINALIZE, media_id=...`
- Тип медиа определяется из расширения файла (.jpg/.jpeg → image/jpeg, .png → image/png и т.д.)
- Возвращает `media_id_string`
- Все 3 шага с retry (3 попытки)

**`create_tweet(session, ct0, auth_token, xtid, community_id, text, media_id)`**:
- POST к `https://x.com/i/api/graphql/.../CreateTweet`
- **Ключевой элемент**: `semantic_annotation_ids` с `group_id: "8"`, `domain_id: "31"`, `entity_id: community_id` — именно это привязывает твит к community, а не к личному таймлайну
- Огромный набор features (30+ boolean-флагов GraphQL)
- 3 попытки с retry

---

### 2.5. Модуль `scraper/`

#### 2.5.1. `scraper/auth.py`

Файл: `scraper/auth.py` (154 строки)

**Функции для работы с сессией Playwright**:

**`inject_auth_token(context, auth_token)`** — добавляет cookie `auth_token` для домена `.x.com` в browser context.

**`check_session_validity(page, wait_timeout=8000)`** — многоуровневая проверка валидности сессии:
1. **URL check**: если URL содержит `/login` или `/i/flow/login` — невалидна
2. **Selector wait**: пробует 5 селекторов (`nav[role="navigation"]`, `[data-testid="AppTabBar_Profile_Link"]`, etc.) с таймаутом
3. **Title check**: если title содержит "It's what's happening" или "Log in" — невалидна
4. **URL heuristic**: `x.com/home` с title "Home" — валидна

**`_save_debug_screenshot(page, name)`** — сохраняет скриншот для отладки.

#### 2.5.2. `scraper/runner.py`

Файл: `scraper/runner.py` (71 строка)

**Standalone CLI entry point**, запускается как `python -m scraper.runner <url> <auth_token> <task_id>`.

1. Вызывает `scrape_community(url, auth_token)`
2. Записывает результат в `data/task_result_{task_id}.json`
3. Exit code: 0 при успехе, 1 при ошибке

#### 2.5.3. `scraper/worker.py`

Файл: `scraper/worker.py` (524 строки)

**Константы**:
- `_SCRAPE_TIMEOUT_SEC = 14 * 60` (14 минут — меньше, чем 15-минутный kill в main.py)
- `_PROFILE_LINK_RE` — regex для X-профилей: `^/([A-Za-z0-9_]{1,15})$`
- `_IGNORE_NAMES` — frozenset системных username'ов (home, explore, notifications, etc.)

**Вспомогательные функции**:

**`_safe_goto(page, url, max_retries=3)`** — навигация с retry:
- Проверяет редирект на homepage (сессия невалидна)
- Проверяет HTTP status >= 400
- Делает скриншоты при ошибках

**`_collect_usernames(page, seen)`** — сбор видимых профильных ссылок:
- `page.query_selector_all("a[href]")` — все ссылки на странице
- Фильтрация через `_PROFILE_LINK_RE` — только `/{username}` формата
- Исключение системных имён из `_IGNORE_NAMES`

**`_get_member_count(page)`** — попытка прочитать число участников через regex по тексту страницы.

**`_scroll_and_collect(page, deadline)`** — главный алгоритм скрапинга:

**Первый проход** (scroll to bottom):
1. Собрать usernames из начального viewport
2. Нажать `End` (прокрутка до конца)
3. Ждать `SCROLL_WAIT_MS` (1500мс по умолчанию)
4. Если `scrollHeight` не изменился — 5 retry с увеличивающимся ожиданием (2с, 3с, 4с, 5с, 6с)
5. Объявить "конец" только после 5 подряд stale retry
6. Лимит: `MAX_PAGEDOWNS` (5000 скроллов)

**Второй проход** (verification pass):
1. `Home` — прокрутка в начало
2. Повторный scroll до конца с 3 stale retry
3. Собирает usernames, пропущенные в первом проходе

**`scrape_community(community_url, auth_token)`** — основная публичная функция:
1. Запуск Playwright Chromium (headless по умолчанию)
2. Viewport: 1280x900, User-Agent Chrome 120
3. Inject `auth_token` cookie
4. Навигация на `x.com` + валидация сессии
5. Навигация на community members page
6. Проверки: login redirect, error pages ("Something went wrong"), deleted community
7. Dismiss cookie banner если есть
8. Чтение ожидаемого числа участников
9. Scroll-and-collect
10. Heuristic: если title == "X" и 0 usernames — `community_deleted`
11. Сравнение coverage (warning если < 80%)
12. Cleanup: close context, browser, stop playwright

Возвращает `(usernames: list[str], error: str | None)`.

---

### 2.6. `token_pool.py`

Файл: `token_pool.py` (202 строки)

**Класс `TokenPool`** — управление пулом auth_token'ов для X/Twitter (используемых scraper'ом).

**Хранилище**: `data/auth_tokens.json`

**Формат записи**:
```json
{
    "tokens": [
        {"value": "abc123...", "added_at": "2024-01-01T00:00:00+00:00", "valid": true}
    ],
    "current_index": 0
}
```

**Миграция при первом запуске** (приоритет):
1. `data/auth_tokens.json` — основной файл
2. `data/auth_token.txt` — legacy single-token файл
3. Переменная окружения `X_AUTH_TOKEN`

**Методы**:

| Метод | Описание |
|-------|----------|
| `get_current()` | Текущий валидный токен (с авто-forward) |
| `mark_invalid(value)` | Пометить конкретный токен невалидным |
| `rotate_next()` | Пометить текущий невалидным, переключиться на следующий |
| `add(value)` | Добавить токен; при дубликате реактивирует если invalid |
| `add_many(values)` | Bulk-добавление, возвращает (new, dups) |
| `delete(index)` | Удалить по 1-based индексу |
| `count_valid()` / `count_total()` | Счётчики |
| `has_valid()` | Есть ли хотя бы один valid |
| `summary()` | Текстовое резюме для Telegram |
| `current_label()` | Короткая метка, например `#2 (abc12345...)` |

---

## 3. Telegram бот подробно

### 3.1. Протокол коммуникации

Бот использует **raw HTTP API** Telegram:
- `getUpdates` с long-polling (timeout=30 секунд)
- `sendMessage` для текстовых сообщений (без parse_mode — plain text)
- `editMessageText` для обновления inline-кнопок
- `answerCallbackQuery` для подтверждения нажатия кнопки
- `sendDocument` для отправки .txt файлов
- `getFile` + скачивание для приёма файлов от пользователя

### 3.2. Авторизация

Метод `_is_authorized(user_id)` — проверка `str(user_id) in TELEGRAM_USER_IDS`. Неавторизованные пользователи получают "Access denied." только при slash-командах.

### 3.3. Как бот получает данные от других модулей

- **Алерты**: через `broadcast()` / `broadcast_with_markup()`, вызываемые из `on_alert()` и `run_auto_post()` в main.py
- **Статус**: через `Database.get_stats()` и `Database.get_recent_tasks()`
- **Токены**: через `TokenPool.summary()`
- **Фильтры**: через `TokenFilter.summary()`
- **Post accounts**: через `PostPool.accounts_summary()`

### 3.4. Как добавить новый хэндлер

1. **Persistent keyboard кнопка**: добавить текст в `_main_keyboard_markup()` и обработчик в `_handle_keyboard_button()`
2. **Inline button**: добавить в соответствующий `markup` dict и обработчик в `_handle_callback()`
3. **Slash-команда**: добавить `elif` в `_handle()`
4. **Text input**: добавить ключ waiting_for и обработать в `_handle_waiting_input()`
5. **File upload**: добавить обработку в `_handle_document()` или `_handle_photo()`

---

## 4. Poster подробно

### 4.1. Процесс постинга в X Community

**Триггер**: функция `on_new_task()` в main.py вызывается при обнаружении нового токена alerter'ом. Если auto-post включён (`PostPool.is_enabled()`), есть valid аккаунты и шаблоны, создаётся `asyncio.create_task(run_auto_post(...))`.

**Полный flow**:
1. `asyncio.sleep(post_pool.delay)` — задержка (default 10 сек)
2. `asyncio.to_thread(post_to_community, ...)` — запуск синхронной функции в thread pool
3. Внутри `post_to_community()`:
   - `curl_cffi.Session(impersonate="chrome136")` — сессия с Chrome fingerprint
   - `XPoster.get_ct0()` — CSRF token через POST на `update_profile.json`
   - `XPoster.get_transaction_id()` — fetch x.com, parse HTML, build ClientTransaction
   - `XPoster.join_community()` — GraphQL JoinCommunity mutation
   - `time.sleep(8-12)` — ожидание пропагации join
   - Template rendering: `{token_name}`, `{token_symbol}`, `{community_url}` + 2 random emoji
   - `XPoster.upload_media()` — если use_photo enabled (INIT → APPEND → FINALIZE)
   - `XPoster.create_tweet()` — GraphQL CreateTweet mutation с `semantic_annotation_ids`
4. Возврат в `run_auto_post()`:
   - Формируется сообщение для Telegram с результатом
   - Создаётся `repost_key` для повторного постинга
   - Отправляется через `bot.broadcast_with_markup()` с inline-кнопкой "Repost with different account"

### 4.2. Формат ответа после поста

**Успех**:
```
Posted in TokenName ($SYM) community!
Link: https://x.com/i/status/1234567890
Account: #0 (abc12345...)
[Repost with different account]
```

**Ошибка**:
```
Failed to post in TokenName ($SYM): error message
Account: #0 (abc12345...)
[Retry with different account]
```

### 4.3. Связь с alerter и scraper

- **Alerter → Poster**: `on_new_task()` в main.py запускает `run_auto_post()` как `asyncio.create_task()` — полностью неблокирующий
- **Poster НЕ зависит от scraper**: постинг и скрапинг — параллельные задачи. Poster работает сразу (с задержкой `delay_after_alert_sec`), scraper — через `SCRAPE_DELAY_MINUTES` (60 мин)
- **Poster использует отдельный пул аккаунтов** (`PostPool`), не связанный со scraper `TokenPool`
- Community URL для poster'а очищается от `/members` suffix, так как шаблоны используют `{community_url}` для ссылки на community page

---

## 5. Полный flow данных

### 5.1. От алерта до финального результата

```
DexScreener API
    |
    v
DexScreenerMonitor._poll()
    | fetch_profiles() -> список профилей
    | fetch_token_data() -> пары для каждого профиля
    | TokenFilter.passes_all() -> фильтрация
    | TokenMemory.mark_alerted() -> дедупликация
    |
    |--- on_alert(text) ---> TelegramBot.broadcast() ---> Telegram users
    |
    +--- on_new_task(members_url, community_id, token_address, ...)
              |
              |--- Database.create_task()
              |      --> SQLite (status='pending', scrape_after=now+60min)
              |
              |--- [if auto-post enabled]:
              |      asyncio.create_task(run_auto_post(...))
              |        |
              |        +-- asyncio.sleep(delay)
              |        +-- asyncio.to_thread(post_to_community, ...)
              |              |
              |              +-- curl_cffi session
              |              +-- XPoster.get_ct0()
              |              +-- XPoster.get_transaction_id()
              |              +-- XPoster.join_community()
              |              +-- time.sleep(8-12s)
              |              +-- PostPool.get_random_tweet() -> template rendering
              |              +-- XPoster.upload_media() [optional]
              |              +-- XPoster.create_tweet()
              |        |
              |        +-- bot.broadcast_with_markup()
              |              --> Telegram (result + repost button)
              |
    Meanwhile, scraper_loop runs continuously:
              |
              +-- Database.get_next_pending_task() (every 60s)
                    |
                    +-- [scrape_after passed]:
                          |
                          +-- TokenPool.get_current() -> auth_token
                          +-- asyncio.create_subprocess_exec(
                          |     "python -m scraper.runner", ...)
                          |     |
                          |     +-- scraper.runner.main()
                          |           |
                          |           +-- scrape_community(url, auth_token)
                          |                 |
                          |                 +-- Playwright Chromium launch
                          |                 +-- inject_auth_token()
                          |                 +-- navigate to x.com, validate session
                          |                 +-- navigate to community/members
                          |                 +-- _scroll_and_collect() (2 passes)
                          |                 +-- return (usernames, error)
                          |     |
                          |     +-- Result JSON: data/task_result_{task_id}.json
                          |
                          +-- [error == "auth_token_invalid"]:
                          |     pool.rotate_next() -> next token
                          |     db.retry_task() -> back to pending
                          |
                          +-- [error == "community_deleted"]:
                          |     db.mark_task_failed()
                          |     bot.broadcast("community deleted")
                          |
                          +-- [success]:
                                db.save_usernames(task_id, community_id, usernames)
                                db.mark_task_completed(task_id, len(usernames))
                                bot.broadcast("Scraped $SYM: N new usernames")
```

### 5.2. Временная шкала

```
T+0s       : DexScreener профиль обнаружен
T+0.1s     : Алерт в Telegram
T+0.1s     : Scrape task создан (scrape_after = T+60min)
T+10s      : Auto-post начинается (если включён)
T+20-30s   : Post опубликован в community
T+60min    : Scraper подбирает задачу
T+60-74min : Scraping завершён (зависит от размера community)
T+74min    : Usernames сохранены в SQLite
```

---

## 6. config.py и .env

### 6.1. Все переменные

Файл: `config.py` (82 строки)

| Переменная | Источник (.env) | По умолчанию | Описание |
|------------|-----------------|--------------|----------|
| `TELEGRAM_BOT_TOKEN` | `TELEGRAM_BOT_TOKEN` | `""` | Токен бота от BotFather |
| `TELEGRAM_USER_IDS` | `TELEGRAM_USER_IDS` | `[]` | Список разрешённых user ID (comma-separated) |
| `X_AUTH_TOKEN` | `X_AUTH_TOKEN` | `""` | auth_token cookie для X (fallback для миграции) |
| `FILTER_CHAINS` | `FILTER_CHAINS` | `["solana"]` | Whitelist блокчейнов |
| `FILTER_MIN_MCAP` | `FILTER_MIN_MCAP` | `10000` | Мин. market cap ($) |
| `FILTER_MAX_MCAP` | `FILTER_MAX_MCAP` | `10000000` | Макс. market cap ($) |
| `FILTER_MIN_LIQUIDITY` | `FILTER_MIN_LIQUIDITY` | `5000` | Мин. ликвидность ($) |
| `FILTER_MIN_AGE_MINUTES` | `FILTER_MIN_AGE_MINUTES` | `5` | Мин. возраст пары (мин) |
| `FILTER_MAX_AGE_HOURS` | `FILTER_MAX_AGE_HOURS` | `24` | Макс. возраст пары (ч) |
| `API_PROFILES_URL` | — | `https://api.dexscreener.com/token-profiles/latest/v1` | URL для latest profiles |
| `API_TOKENS_URL` | — | `https://api.dexscreener.com/latest/dex/tokens` | URL для token data |
| `API_TIMEOUT` | `API_TIMEOUT` | `10` | Таймаут HTTP запроса (сек) |
| `API_MAX_CONSECUTIVE_FAILURES` | — | `5` | Порог для алерта о сбое API |
| `CHECK_INTERVAL` | `CHECK_INTERVAL` | `30` | Интервал polling DexScreener (сек) |
| `SCRAPE_DELAY_MINUTES` | `SCRAPE_DELAY_MINUTES` | `60` | Задержка перед скрапингом (мин) |
| `SCRAPER_HEADLESS` | `SCRAPER_HEADLESS` | `true` | Headless режим Chromium |
| `SCROLL_WAIT_MS` | `SCROLL_WAIT_MS` | `1500` | Пауза между скроллами (мс) |
| `MAX_PAGEDOWNS` | `MAX_PAGEDOWNS` | `5000` | Макс. число скроллов |
| `BASE_DIR` | — | Директория проекта | Корень проекта |
| `DATA_DIR` | — | `BASE_DIR / "data"` | Данные (БД, JSON, скриншоты) |
| `LOG_DIR` | — | `BASE_DIR / "logs"` | Логи |
| `DB_PATH` | — | `DATA_DIR / "scraper.db"` | SQLite БД |
| `MEMORY_FILE` | — | `DATA_DIR / "memory.json"` | Персистенция памяти алертера |
| `FILTERS_FILE` | — | `DATA_DIR / "filters.json"` | Оверрайды фильтров |
| `MEMORY_HOURS` | — | `24` | Время жизни записей в памяти |

---

## 7. token_pool.py — Пул токенов и ротация

### 7.1. Стратегия ротации

- При `auth_token_invalid` от scraper'а, `main.py:scraper_loop()` вызывает `pool.rotate_next()`
- `rotate_next()` помечает текущий токен как `valid=False`, ищет следующий valid по кругу
- Если valid токен найден — задача возвращается в pending (`db.retry_task()`)
- Если все токены мертвы:
  - Устанавливается `_all_tokens_dead = True`
  - Scraper loop засыпает (sleep 30s в цикле)
  - 3 повторных алерта в Telegram с интервалом 60 сек
  - Пользователь может добавить новые токены через бот

### 7.2. Адаптивный таймаут

В scraper_loop реализован адаптивный таймаут для subprocess:
- Base: 15 минут
- При timeout: +5 минут (до максимума 30 минут)
- При успехе: сброс на 15 минут

### 7.3. Файлы хранения

| Файл | Описание |
|------|----------|
| `data/auth_tokens.json` | Основной пул scraper-токенов |
| `data/auth_token.txt` | Legacy (один токен, мигрируется) |
| `data/post_config.json` | Пул posting-аккаунтов + настройки |

---

## 8. Database Schema

Файл: `database/models.py`

### 8.1. Таблица `scrape_tasks`

```sql
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
```

**Возможные статусы**:
- `pending` — ожидает выполнения (scrape_after ещё не наступил, или ждёт очередь)
- `in_progress` — выполняется (при старте приложения сбрасываются в pending)
- `completed` — успешно выполнена
- `failed` — ошибка (хранится в error_message)

**UNIQUE constraint** на `community_url` — предотвращает дубликаты задач.

### 8.2. Таблица `usernames`

```sql
CREATE TABLE IF NOT EXISTS usernames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    community_id TEXT NOT NULL,
    task_id INTEGER NOT NULL,
    scraped_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES scrape_tasks(id),
    UNIQUE(username, community_id)
);
```

**UNIQUE constraint** на `(username, community_id)` — один пользователь не дублируется в рамках одного community. Но один username может быть в нескольких communities.

### 8.3. Индексы

```sql
CREATE INDEX IF NOT EXISTS idx_tasks_status ON scrape_tasks(status, scrape_after);
CREATE INDEX IF NOT EXISTS idx_usernames_community ON usernames(community_id);
CREATE INDEX IF NOT EXISTS idx_usernames_username ON usernames(username);
```

- `idx_tasks_status` — оптимизация запроса `get_next_pending_task()` (фильтр по status + ORDER BY scrape_after)
- `idx_usernames_community` — быстрый экспорт по community
- `idx_usernames_username` — быстрый поиск по username

### 8.4. Pragma настройки

```python
conn.execute("PRAGMA journal_mode=WAL")   # Write-Ahead Logging для concurrent reads
conn.execute("PRAGMA foreign_keys=ON")    # Включение foreign keys
```

### 8.5. Thread safety

`Database` использует `threading.local()` для хранения per-thread SQLite connections с `timeout=30` секунд. Это необходимо, так как scraper работает в subprocess (через `scraper.runner`), а основной event loop — в main thread.
