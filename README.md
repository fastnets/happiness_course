# happiness_course

Telegram-бот "Курс на счастье" с дневным контентом, заданиями, анкетами, напоминаниями и админкой.

## Что умеет бот

### Для пользователя
- Дневной маршрут: лекция, задание, анкета, доп. материалы.
- Пак дня: цитата, картинка, совет, книга, фильм.
- Возврат к пропущенным материалам в один клик.
- Трекер настроения (оценка + график за 7/30 дней).
- Привычки с отметками "выполнено/пропущено" и начислением баллов.
- Персональные разовые напоминания.
- Прогресс, серия, ачивки, недельная динамика.
- Поддержка через тикеты (если FAQ не помог).

### Для админа
- CRUD для лекций, заданий, анкет, доп. материалов и ачивок.
- Рандомная рассылка анкеты всем пользователям.
- Тикеты поддержки: просмотр, ответ, закрытие.
- Аналитика за 1/7/30 дней.
- Управление ролями `owner/admin`.

## Архитектура
- `handlers` — Telegram-роутинг и UI.
- `services` — бизнес-логика.
- `repositories` — SQL и доступ к БД.
- `scheduling` + `outbox_jobs` — планирование и доставка.

Проект рассчитан на идемпотентную доставку и защиту от двойных начислений баллов.

## Технологии
- Python 3.11+
- python-telegram-bot 21.x
- PostgreSQL
- `psycopg` + `python-dotenv`

## Быстрый старт (локально)
1. Создай виртуальное окружение:
   - Windows: `python -m venv .venv`
   - Linux/macOS: `python3 -m venv .venv`
2. Активируй окружение:
   - Windows (PowerShell): `.venv\Scripts\Activate.ps1`
   - Linux/macOS: `source .venv/bin/activate`
3. Установи зависимости: `pip install -r requirements.txt`
4. Создай конфиг: скопируй `.env.example` в `.env`
5. Заполни минимум:
   - `BOT_TOKEN`
   - `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`
6. Запусти бота: `python main.py`

## Запуск на VM (Ubuntu)
```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git postgresql postgresql-contrib

git clone https://github.com/fastnets/happiness_course.git
cd happiness_course

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# отредактируй .env

python main.py
```

## Проверка качества
- Запуск всех тестов: `python -m pytest -q`

## Обязательные env-переменные
- `BOT_TOKEN`
- `DB_HOST`
- `DB_PORT`
- `DB_NAME`
- `DB_USER`
- `DB_PASSWORD`

## Опциональные env-переменные
- `OWNER_TG_ID`, `ADMIN_TG_IDS`, `ADMIN_EVENTS_CHAT_ID`
- `DEFAULT_TIMEZONE`, параметры quiet hours/reminders
- AI-блок `GIGACHAT_*` (если нужен AI-фидбек)

## Полезно для разработки
- Схема БД и миграции применяются при старте (`db.init_schema()`).
- Если `GIGACHAT_*` не заполнены, бот работает без AI-функций.
