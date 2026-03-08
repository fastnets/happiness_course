from telegram import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup

from ui import texts
from event_bus import callbacks as cb


def kb_main(is_admin: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(texts.MENU_DAY), KeyboardButton(texts.MENU_PROGRESS)],
        [KeyboardButton(texts.MENU_SETTINGS), KeyboardButton(texts.MENU_HELP)],
    ]
    if is_admin:
        rows.append([KeyboardButton(texts.MENU_ADMIN)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def kb_day() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(texts.DAY_QUOTE)],
        [KeyboardButton(texts.DAY_PIC), KeyboardButton(texts.DAY_TIP)],
        [KeyboardButton(texts.DAY_BOOK), KeyboardButton(texts.DAY_FILM)],
        [KeyboardButton(texts.DAY_MOOD)],
        [KeyboardButton(texts.DAY_MATERIALS_NOW)],
        [KeyboardButton(texts.BTN_BACK)],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def kb_progress() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(texts.BTN_BACK)]],
        resize_keyboard=True,
    )


def kb_settings() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(texts.SETTINGS_TIME), KeyboardButton(texts.SETTINGS_TZ)],
            [KeyboardButton(texts.SETTINGS_NAME)],
            [KeyboardButton(texts.SETTINGS_REMINDERS)],
            [KeyboardButton(texts.BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def kb_reminders_hub() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(texts.REMINDERS_HUB_HABITS)],
            [KeyboardButton(texts.REMINDERS_HUB_ONCE)],
            [KeyboardButton(texts.BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def kb_habits() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(texts.HABITS_CREATE)],
            [KeyboardButton(texts.HABITS_LIST)],
            [KeyboardButton(texts.HABITS_EDIT)],
            [KeyboardButton(texts.HABITS_DELETE)],
            [KeyboardButton(texts.BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def kb_habit_edit_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(texts.HABIT_EDIT_NAME), KeyboardButton(texts.HABIT_EDIT_TIME)],
            [KeyboardButton(texts.HABIT_EDIT_FREQ)],
            [KeyboardButton(texts.BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def kb_personal_reminders() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(texts.REMINDERS_CREATE)],
            [KeyboardButton(texts.REMINDERS_LIST)],
            [KeyboardButton(texts.REMINDERS_EDIT)],
            [KeyboardButton(texts.REMINDERS_DELETE)],
            [KeyboardButton(texts.BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def kb_personal_reminder_edit_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(texts.REMINDER_EDIT_TEXT), KeyboardButton(texts.REMINDER_EDIT_DATETIME)],
            [KeyboardButton(texts.BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def kb_habit_frequency_reply() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("Ежедневно")],
            [KeyboardButton("По будням")],
            [KeyboardButton("По выходным")],
            [KeyboardButton(texts.BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def kb_habit_frequency():
    rows = [
        [InlineKeyboardButton("Ежедневно", callback_data="habit:freq:daily")],
        [InlineKeyboardButton("По будням", callback_data="habit:freq:weekdays")],
        [InlineKeyboardButton("По выходным", callback_data="habit:freq:weekends")],
    ]
    return InlineKeyboardMarkup(rows)


def kb_back_only() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton(texts.BTN_BACK)]], resize_keyboard=True)


def kb_admin_home() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(texts.ADMIN_LESSONS), KeyboardButton(texts.ADMIN_QUESTS)],
            [KeyboardButton(texts.ADMIN_QUESTIONNAIRES), KeyboardButton(texts.ADMIN_ANALYTICS)],
            [KeyboardButton(texts.ADMIN_ACHIEVEMENTS), KeyboardButton(texts.ADMIN_TICKETS)],
            [KeyboardButton(texts.BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def kb_admin_crud() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(texts.CRUD_LIST), KeyboardButton(texts.CRUD_CREATE)],
            [KeyboardButton(texts.CRUD_EDIT), KeyboardButton(texts.CRUD_DELETE)],
            [KeyboardButton(texts.BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def kb_admin_questionnaires() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(texts.CRUD_LIST), KeyboardButton(texts.CRUD_CREATE)],
            [KeyboardButton(texts.CRUD_EDIT), KeyboardButton(texts.CRUD_DELETE)],
            [KeyboardButton(texts.Q_RANDOM_ALL)],
            [KeyboardButton(texts.BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def kb_yes_no() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(texts.YES), KeyboardButton(texts.NO)], [KeyboardButton(texts.BTN_BACK)]],
        resize_keyboard=True,
    )


def kb_consent() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Согласен", callback_data="consent:yes")],
            [InlineKeyboardButton("❌ Не согласен", callback_data="consent:no")],
        ]
    )


def kb_enroll_time():
    times = ["07:00", "09:00", "12:00", "18:00", "21:00"]
    rows = [[InlineKeyboardButton(t, callback_data=f"{cb.ENROLL_TIME_PREFIX}{t}")] for t in times]
    rows.append([InlineKeyboardButton("Другое (ввести)", callback_data=f"{cb.ENROLL_TIME_PREFIX}custom")])
    return InlineKeyboardMarkup(rows)


def kb_timezone():
    rows = [
        [InlineKeyboardButton("Москва (по умолчанию, UTC+3)", callback_data="tz:Europe/Moscow")],
        [InlineKeyboardButton("Екатеринбург (UTC+5)", callback_data="tz:Asia/Yekaterinburg")],
        [InlineKeyboardButton("Владивосток (UTC+10)", callback_data="tz:Asia/Vladivostok")],
        [InlineKeyboardButton("Другое (ввести IANA)", callback_data="tz:custom")],
    ]
    return InlineKeyboardMarkup(rows)
