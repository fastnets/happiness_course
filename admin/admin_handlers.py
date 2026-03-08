import asyncio
import re
import json
import logging
from datetime import datetime
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import ContextTypes, MessageHandler, filters, CommandHandler, ApplicationHandlerStop

from entity.settings import Settings
from ui import texts
from ui.keyboards import menus

log = logging.getLogger("happines_course")

# State steps
ADMIN_MENU_STEP = "admin_menu"
ADMIN_WIZARD_STEP = "admin_wizard"

# Reply buttons (admin UI)
BTN_LIST = "📋 Список"
BTN_CREATE = "➕ Создать"
BTN_EDIT = "✏️ Редактировать"
BTN_DELETE = "🗑 Удалить"
BTN_RANDOM_Q = "🎲 Рандомная анкета всем"
BTN_ADM_ADD = "➕ Добавить admin"
BTN_ADM_PROMOTE = "👑 Выдать owner"
BTN_ADM_DEMOTE = "🛠 Понизить до admin"
BTN_ADM_REMOVE = "🗑 Удалить из админов"

BTN_YES = "Да"
BTN_NO = "Нет"

# Analytics submenu
BTN_PERIOD_TODAY = "Сегодня"
BTN_PERIOD_7 = "7 дней"
BTN_PERIOD_30 = "30 дней"

# Tickets submenu
BTN_T_OPEN = "🟡 Open"
BTN_T_VIEW = "🔎 Открыть по ID"
BTN_T_REPLY = "💬 Ответить"
BTN_T_CLOSE = "✅ Закрыть"
BTN_JOIN_EVENTS_CHAT = "Вступить в группу с уведомлениями об изменениях в курсе"


def _extract_quest_points(item: dict) -> int:
    """Return quest points from current or legacy field names."""
    try:
        return int(item.get("points") or item.get("points_reply") or 0)
    except Exception:
        return 0


def _short_text(value: object, limit: int = 80) -> str:
    if value is None:
        return "—"
    text = re.sub(r"\s+", " ", str(value).strip())
    if not text:
        return "—"
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _int_text(value: object) -> str:
    if value in (None, ""):
        return "—"
    try:
        return str(int(value))
    except Exception:
        return _short_text(value, limit=24)


def _yes_no(value: object) -> str:
    return "да" if bool(value) else "нет"


def _diff_line(label: str, old_value: object, new_value: object, formatter=None) -> str | None:
    fmt = formatter or _short_text
    old_s = fmt(old_value)
    new_s = fmt(new_value)
    if old_s == new_s:
        return None
    if old_s == "—":
        return f"• {label}: → {new_s}"
    if new_s == "—":
        return f"• {label}: {old_s} →"
    return f"• {label}: {old_s} → {new_s}"


def _format_user_ref(user_id: int, user_row: dict | None = None) -> str:
    uid = int(user_id or 0)
    if uid <= 0:
        return "id=—"
    row = user_row or {}
    username = str(row.get("username") or "").strip().lstrip("@")
    if username:
        return f"@{username} (id={uid})"
    display_name = str(row.get("display_name") or "").strip()
    if display_name:
        return f"{_short_text(display_name, limit=40)} (id={uid})"
    return f"id={uid}"


def _admin_role_label(role: str) -> str:
    role_s = str(role or "").strip().lower()
    if role_s == "owner":
        return "owner"
    if role_s == "admin":
        return "admin"
    return "нет роли"


def _questionnaire_type_label(qtype: str) -> str:
    key = str(qtype or "").strip().lower()
    mapping = {
        "manual": "по дню курса",
        "daily": "ежедневная",
        "broadcast_optional": "рандомная всем (опционально)",
        "broadcast_required": "рассылка всем (обязательно)",
        "broadcast": "рассылка всем",
    }
    return mapping.get(key, key or "не указан")


def kb(rows):
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def kb_yes_no():
    return kb([[KeyboardButton(BTN_YES), KeyboardButton(BTN_NO)], [KeyboardButton(texts.BTN_BACK)]])


def kb_admin_home(is_owner: bool = False):
    rows = [
        [KeyboardButton(texts.ADMIN_LESSONS), KeyboardButton(texts.ADMIN_QUESTS)],
        [KeyboardButton(texts.ADMIN_EXTRA), KeyboardButton(texts.ADMIN_QUESTIONNAIRES)],
        [KeyboardButton(texts.ADMIN_ANALYTICS), KeyboardButton(texts.ADMIN_ACHIEVEMENTS)],
        [KeyboardButton(texts.ADMIN_TICKETS)],
    ]
    if is_owner:
        rows.append([KeyboardButton(texts.ADMIN_ADMINS)])
    rows.append([KeyboardButton(texts.BTN_BACK)])
    return kb(rows)

def kb_admin_actions(include_random: bool = False):
    rows = [
        [KeyboardButton(BTN_LIST), KeyboardButton(BTN_CREATE)],
        [KeyboardButton(BTN_EDIT), KeyboardButton(BTN_DELETE)],
    ]
    if include_random:
        rows.append([KeyboardButton(BTN_RANDOM_Q)])
    rows.append([KeyboardButton(texts.BTN_BACK)])
    return kb(rows)


def kb_admin_analytics():
    return kb(
        [
            [KeyboardButton(BTN_PERIOD_TODAY), KeyboardButton(BTN_PERIOD_7), KeyboardButton(BTN_PERIOD_30)],
            [KeyboardButton(texts.BTN_BACK)],
        ]
    )


def kb_admin_tickets():
    return kb(
        [
            [KeyboardButton(BTN_T_OPEN)],
            [KeyboardButton(BTN_T_VIEW), KeyboardButton(BTN_T_REPLY)],
            [KeyboardButton(BTN_T_CLOSE)],
            [KeyboardButton(texts.BTN_BACK)],
        ]
    )


def kb_admin_admins():
    return kb(
        [
            [KeyboardButton(BTN_LIST), KeyboardButton(BTN_ADM_ADD)],
            [KeyboardButton(BTN_ADM_PROMOTE), KeyboardButton(BTN_ADM_DEMOTE)],
            [KeyboardButton(BTN_ADM_REMOVE)],
            [KeyboardButton(texts.BTN_BACK)],
        ]
    )


def register_admin_handlers(app, settings: Settings, services: dict):
    admin_svc = services.get("admin")
    admin_analytics = services.get("admin_analytics")
    support_svc = services.get("support")
    achievement_svc = services.get("achievement")
    daily_pack = services.get("daily_pack")
    user_svc = services["user"]

    def _is_admin(update: Update) -> bool:
        try:
            uid = update.effective_user.id if update.effective_user else None
            return bool(uid and admin_svc and admin_svc.is_admin(uid))
        except Exception:
            return False

    def _is_owner(update: Update) -> bool:
        try:
            uid = update.effective_user.id if update.effective_user else None
            return bool(uid and admin_svc and hasattr(admin_svc, "is_owner") and admin_svc.is_owner(uid))
        except Exception:
            return False

    state = user_svc.state
    qsvc = services["questionnaire"]
    schedule = services["schedule"]
    lesson_repo = schedule.lesson
    quest_repo = schedule.quest
    extra_repo = getattr(schedule, "extra", None)
    admin_events_chat_id = getattr(settings, "admin_events_chat_id", None)

    # ----------------------------
    # Navigation helpers
    # ----------------------------
    def _set_menu(uid: int, screen: str, extra: dict | None = None):
        payload = {"screen": screen}
        if extra:
            payload.update(extra)
        state.set_state(uid, ADMIN_MENU_STEP, payload)

    def _get_user_row(uid: int) -> dict | None:
        try:
            if not hasattr(user_svc, "users") or not hasattr(user_svc.users, "get_user"):
                return None
            return user_svc.users.get_user(int(uid))
        except Exception:
            return None

    def _user_label(uid: int) -> str:
        return _format_user_ref(uid, _get_user_row(uid))

    def _admin_role_by_uid(uid: int) -> str:
        try:
            if not admin_svc or not hasattr(admin_svc, "admins"):
                return "нет роли"
            if admin_svc.admins.is_owner(int(uid)):
                return "owner"
            if admin_svc.admins.is_admin(int(uid)):
                return "admin"
        except Exception:
            pass
        return "нет роли"

    async def _emit_admin_event(context: ContextTypes.DEFAULT_TYPE, actor_id: int, action: str, details: str = ""):
        if not admin_events_chat_id:
            return
        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        details_s = str(details or "").strip()
        lines = [
            "📣 Админ-событие",
            f"Действие: {action}",
            f"Кто: {_user_label(actor_id)}",
        ]
        if details_s:
            lines.append("Что изменилось:")
            lines.append(details_s)
        lines.append(f"Когда: {now_s}")
        msg = "\n".join(lines)
        try:
            await context.bot.send_message(chat_id=int(admin_events_chat_id), text=msg)
        except Exception:
            log.exception("Failed to send admin event")

    async def _regenerate_daily_pack(trigger: str):
        if not daily_pack:
            return
        try:
            await asyncio.to_thread(
                daily_pack.generate_set_for_today,
                trigger=trigger,
                force=True,
            )
        except Exception:
            # Ошибки должны оставаться только в логах.
            log.exception("Daily pack regenerate failed (trigger=%s)", trigger)

    def _schedule_daily_pack_regenerate(trigger: str):
        try:
            asyncio.create_task(_regenerate_daily_pack(trigger))
        except Exception:
            log.exception("Daily pack regenerate scheduling failed (trigger=%s)", trigger)

    async def _send_events_chat_invite_to_admin(
        context: ContextTypes.DEFAULT_TYPE,
        *,
        target_uid: int,
        granted_role: str,
    ) -> str:
        if not admin_events_chat_id:
            return "⚠️ Не отправил ссылку: ADMIN_EVENTS_CHAT_ID не настроен."
        chat_id = int(admin_events_chat_id)
        uid = int(target_uid)
        role = _admin_role_label(granted_role)

        try:
            member = await context.bot.get_chat_member(chat_id=chat_id, user_id=uid)
            if str(getattr(member, "status", "")).lower() in ("member", "administrator", "creator"):
                return "ℹ️ Пользователь уже состоит в группе уведомлений."
        except Exception:
            # Can't determine membership reliably — continue with invite creation.
            pass

        try:
            invite = await context.bot.create_chat_invite_link(
                chat_id=chat_id,
                name=f"admin-{uid}-{datetime.now().strftime('%Y%m%d%H%M%S')}",
            )
        except Exception:
            log.exception("Failed to create admin events invite link")
            return "⚠️ Не смог создать invite-ссылку в группу уведомлений."

        try:
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton(BTN_JOIN_EVENTS_CHAT, url=str(invite.invite_link))]]
            )
            await context.bot.send_message(
                chat_id=uid,
                text=(
                    f"✅ Тебе выдана роль {role}.\n"
                    "Нажми кнопку ниже, чтобы вступить в группу с уведомлениями."
                ),
                reply_markup=markup,
            )
            return "✅ Инвайт-ссылка в группу уведомлений отправлена пользователю в ЛС."
        except Exception:
            log.exception("Failed to send admin events invite to user")
            return (
                "⚠️ Роль выдана, но не смог отправить ссылку в ЛС. "
                "Пусть пользователь откроет бота и нажмёт /start."
            )

    async def _show_main_menu(update: Update):
        uid = update.effective_user.id
        await update.effective_message.reply_text(
            "Главное меню 👇",
            reply_markup=menus.kb_main(bool(admin_svc and admin_svc.is_admin(uid))),
        )

    async def _show_admin_home(update: Update):
        uid = update.effective_user.id
        _set_menu(uid, "home")
        await update.effective_message.reply_text("🛠 Админка", reply_markup=kb_admin_home(_is_owner(update)))

    async def _show_lessons_menu(update: Update):
        uid = update.effective_user.id
        _set_menu(uid, "lessons")
        await update.effective_message.reply_text("📚 Лекции", reply_markup=kb_admin_actions(False))

    async def _show_quests_menu(update: Update):
        uid = update.effective_user.id
        _set_menu(uid, "quests")
        await update.effective_message.reply_text("📝 Задания", reply_markup=kb_admin_actions(False))

    async def _show_extras_menu(update: Update):
        uid = update.effective_user.id
        _set_menu(uid, "extra")
        await update.effective_message.reply_text("🧩 Доп. материалы", reply_markup=kb_admin_actions(False))

    async def _show_q_menu(update: Update):
        uid = update.effective_user.id
        _set_menu(uid, "questionnaires")
        await update.effective_message.reply_text("📋 Анкеты", reply_markup=kb_admin_actions(True))

    async def _show_achievements_menu(update: Update):
        uid = update.effective_user.id
        _set_menu(uid, "achievements")
        await update.effective_message.reply_text("🏆 Ачивки", reply_markup=kb_admin_actions(False))

    async def _show_admins_menu(update: Update):
        uid = update.effective_user.id
        _set_menu(uid, "admins")
        await update.effective_message.reply_text("👥 Управление админами", reply_markup=kb_admin_admins())

    ACH_METRIC_OPTIONS = [
        ("Баллы", "points"),
        ("Завершенные дни", "done_days"),
        ("Серия дней", "streak"),
        ("Привычки выполнено", "habit_done"),
        ("Привычки пропущено", "habit_skipped"),
        ("Анкет заполнено", "questionnaire_count"),
    ]
    ACH_OPERATOR_OPTIONS = [
        ("не меньше", ">="),
        ("больше", ">"),
        ("равно", "="),
        ("не больше", "<="),
        ("меньше", "<"),
    ]
    OPERATOR_TOKEN = {
        ">=": "ge",
        ">": "gt",
        "=": "eq",
        "<=": "le",
        "<": "lt",
    }

    def _achievement_metrics_hint() -> str:
        lines = ["Доступные метрики:"]
        for i, (label, key) in enumerate(ACH_METRIC_OPTIONS, 1):
            lines.append(f"{i}. {label} ({key})")
        lines.append("Введи номер или название.")
        return "\n".join(lines)

    def _achievement_operators_hint() -> str:
        lines = ["Доступные операторы:"]
        for i, (label, sym) in enumerate(ACH_OPERATOR_OPTIONS, 1):
            lines.append(f"{i}. {label} ({sym})")
        lines.append("Введи номер или оператор.")
        return "\n".join(lines)

    def _metric_label_by_key(metric_key: str) -> str:
        key = (metric_key or "").strip()
        for label, val in ACH_METRIC_OPTIONS:
            if val == key:
                return label
        return key or "-"

    def _operator_label_by_symbol(operator: str) -> str:
        op = (operator or "").strip()
        for label, val in ACH_OPERATOR_OPTIONS:
            if val == op:
                return label
        return op or "-"

    def _parse_metric_key(text: str) -> str | None:
        raw = (text or "").strip()
        if not raw:
            return None
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(ACH_METRIC_OPTIONS):
                return ACH_METRIC_OPTIONS[idx - 1][1]
        low = raw.lower()
        for label, key in ACH_METRIC_OPTIONS:
            if low in (label.lower(), key.lower()):
                return key
        return None

    def _parse_operator_symbol(text: str) -> str | None:
        raw = (text or "").strip()
        if not raw:
            return None
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(ACH_OPERATOR_OPTIONS):
                return ACH_OPERATOR_OPTIONS[idx - 1][1]
        low = raw.lower()
        for label, sym in ACH_OPERATOR_OPTIONS:
            if raw == sym or low == label.lower():
                return sym
        return None

    @staticmethod
    def _slugify_ascii(raw: str) -> str:
        s = (raw or "").strip().lower()
        s = re.sub(r"[^a-z0-9]+", "_", s)
        s = re.sub(r"_+", "_", s).strip("_")
        return s

    def _next_achievement_sort_order() -> int:
        if not achievement_svc:
            return 100
        try:
            rows = achievement_svc.list_rules(limit=500, active_only=None) or []
        except Exception:
            return 100
        max_sort = 0
        for row in rows:
            try:
                max_sort = max(max_sort, int(row.get("sort_order") or 0))
            except Exception:
                pass
        if max_sort <= 0:
            return 10
        return ((max_sort // 10) + 1) * 10

    def _generate_achievement_code(title: str, metric_key: str, operator: str, threshold: int) -> str:
        base_title = _slugify_ascii(title)[:24]
        op_token = OPERATOR_TOKEN.get(operator, "eq")
        tail = f"{metric_key}_{op_token}_{abs(int(threshold))}"
        if base_title:
            base = f"{base_title}_{tail}"
        else:
            base = f"ach_{tail}"
        base = re.sub(r"_+", "_", base).strip("_")
        if len(base) < 3:
            base = "ach_rule"
        base = base[:64].strip("_")

        if not achievement_svc:
            return base
        try:
            rows = achievement_svc.list_rules(limit=1000, active_only=None) or []
            existing = {str(r.get("code") or "").strip().lower() for r in rows}
        except Exception:
            existing = set()
        if base.lower() not in existing:
            return base
        for i in range(2, 1000):
            suffix = f"_{i}"
            cand = base
            if len(cand) + len(suffix) > 64:
                cand = cand[: 64 - len(suffix)].rstrip("_")
            cand = f"{cand}{suffix}"
            if cand.lower() not in existing:
                return cand
        return f"ach_{int(datetime.now().timestamp())}"

    def _parse_yes_no(text: str) -> bool | None:
        value = (text or "").strip().lower()
        if value in ("да", "yes", "y", "1", "true"):
            return True
        if value in ("нет", "no", "n", "0", "false"):
            return False
        return None

    def _achievement_row_line(row: dict, pos: int) -> str:
        state_label = "🟢" if bool(row.get("is_active")) else "⚪️"
        metric_label = _metric_label_by_key(str(row.get("metric_key") or ""))
        operator_label = _operator_label_by_symbol(str(row.get("operator") or ""))
        return (
            f"• №{int(pos)} {state_label} {row.get('code')} | "
            f"{metric_label} {operator_label} {row.get('threshold')} | "
            f"{row.get('icon')} {row.get('title')}"
        )

    def _find_achievement_rule(identifier: str) -> dict | None:
        if not achievement_svc:
            return None
        raw = (identifier or "").strip()
        if not raw:
            return None
        token = raw
        if token.startswith("№"):
            pos_token = token[1:].strip()
            if pos_token.isdigit():
                try:
                    rows = achievement_svc.list_rules(limit=500, active_only=None) or []
                except Exception:
                    return None
                pos = int(pos_token)
                if 1 <= pos <= len(rows):
                    return rows[pos - 1]
            return None
        if token.startswith("#"):
            token = token[1:]
        token = token.strip()
        if token.isdigit():
            try:
                row = achievement_svc.get_rule(int(token))
                if row:
                    return row
            except Exception:
                pass
            # Fallback: allow entering list position number (1..N).
            try:
                rows = achievement_svc.list_rules(limit=500, active_only=None) or []
            except Exception:
                return None
            pos = int(token)
            if 1 <= pos <= len(rows):
                return rows[pos - 1]
            return None
        code = token.lower()
        try:
            rows = achievement_svc.list_rules(limit=500, active_only=None)
        except Exception:
            return None
        for row in rows:
            if str(row.get("code") or "").strip().lower() == code:
                return row
        return None

    async def _show_analytics_menu(update: Update, days: int = 7, show_report: bool = True):
        uid = update.effective_user.id
        safe_days = 7
        try:
            safe_days = int(days)
        except Exception:
            safe_days = 7
        if safe_days not in (1, 7, 30):
            safe_days = 7
        _set_menu(uid, "analytics", {"days": safe_days})
        if not show_report:
            await update.effective_message.reply_text(
                "\U0001F4C8 \u0410\u043d\u0430\u043b\u0438\u0442\u0438\u043a\u0430\n"
                "\u0412\u044b\u0431\u0435\u0440\u0438 \u043f\u0435\u0440\u0438\u043e\u0434:",
                reply_markup=kb_admin_analytics(),
            )
            return
        if not admin_analytics:
            await update.effective_message.reply_text(
                "\u26a0\ufe0f \u0421\u0435\u0440\u0432\u0438\u0441 \u0430\u043d\u0430\u043b\u0438\u0442\u0438\u043a\u0438 "
                "\u043d\u0435 \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0451\u043d.",
                reply_markup=kb_admin_analytics(),
            )
            return
        await update.effective_message.reply_text(
            admin_analytics.statistics_report(safe_days),
            reply_markup=kb_admin_analytics(),
        )

    def _safe_tickets_mode(value: str | None) -> str:
        return "all" if (value or "").strip().lower() == "all" else "open"

    def _safe_tickets_limit(value) -> int:
        try:
            n = int(value)
        except Exception:
            n = 20
        return max(1, min(100, n))

    def _tickets_list_text(rows: list[dict], mode: str, limit: int) -> str:
        mode_label = "только open" if mode == "open" else "все"
        if not rows:
            return f"🆘 Тикеты ({mode_label}, limit={limit})\n\nТикетов не найдено."

        lines = [f"🆘 Тикеты ({mode_label}, limit={limit})", ""]
        lines.extend(_ticket_preview(r) for r in rows)
        return "\n".join(lines)

    async def _send_tickets_list(
        update: Update,
        mode: str = "open",
        limit: int = 20,
        reply_markup=None,
    ):
        if not support_svc:
            await update.effective_message.reply_text("⚠️ Сервис поддержки не подключён.", reply_markup=reply_markup)
            return
        safe_mode = _safe_tickets_mode(mode)
        safe_limit = _safe_tickets_limit(limit)
        rows = support_svc.list_open(limit=safe_limit) if safe_mode == "open" else support_svc.list_all(limit=safe_limit)
        text = _tickets_list_text(rows, safe_mode, safe_limit)
        await update.effective_message.reply_text(text, reply_markup=reply_markup)

    async def _show_tickets_menu(update: Update, mode: str = "open", limit: int = 20):
        uid = update.effective_user.id
        safe_mode = _safe_tickets_mode(mode)
        safe_limit = _safe_tickets_limit(limit)
        _set_menu(uid, "tickets", {"mode": safe_mode, "limit": safe_limit})
        await _send_tickets_list(
            update,
            mode=safe_mode,
            limit=safe_limit,
            reply_markup=kb_admin_tickets(),
        )

    def _ticket_status_label(status: str | None) -> str:
        s = (status or "").strip().lower()
        if s == "open":
            return "🟡 open"
        if s == "closed":
            return "✅ closed"
        return s or "-"

    def _ticket_number(row: dict) -> int:
        try:
            tid = int(row.get("id") or 0)
        except Exception:
            tid = 0
        try:
            num = int(row.get("number") or 0)
        except Exception:
            num = 0
        return num if num > 0 else tid

    def _ticket_preview(row: dict) -> str:
        txt = (row.get("question_text") or "").replace("\n", " ").strip()
        if len(txt) > 70:
            txt = txt[:67] + "..."
        tid = int(row.get("id") or 0)
        tnum = _ticket_number(row)
        return (
            f"• №{tnum} (id={tid}) [{_ticket_status_label(row.get('status'))}] "
            f"user={row.get('user_id')} — {txt}"
        )

    def _ticket_details(row: dict) -> str:
        tid = int(row.get("id") or 0)
        tnum = _ticket_number(row)
        base = [
            f"🆘 Тикет №{tnum} (id={tid})",
            f"Статус: {_ticket_status_label(row.get('status'))}",
            f"user_id: {row.get('user_id')}",
            f"Создан: {row.get('created_at')}",
            "",
            "Сообщение:",
            str(row.get("question_text") or "-"),
        ]
        reply = (row.get("admin_reply") or "").strip()
        if reply:
            base.extend(
                [
                    "",
                    f"Ответ админа ({row.get('admin_id')}):",
                    reply,
                ]
            )
        return "\n".join(base)

    # ----------------------------
    # Entry points
    # ----------------------------
    async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update):
            await update.effective_message.reply_text("⛔️ У вас нет прав для доступа к этой команде.")
            return
        await _show_admin_home(update)

    async def open_admin_from_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update):
            await update.effective_message.reply_text("⛔️ Только для админов.")
            return
        await _show_admin_home(update)
        raise ApplicationHandlerStop

    def _parse_target_uid(context: ContextTypes.DEFAULT_TYPE) -> int | None:
        args = list(getattr(context, "args", []) or [])
        if len(args) != 1:
            return None
        raw = str(args[0]).strip()
        if not re.match(r"^\d+$", raw):
            return None
        uid = int(raw)
        return uid if uid > 0 else None

    def _resolve_user_ref(raw: str) -> tuple[int | None, str | None]:
        val = (raw or "").strip()
        if not val:
            return None, "Укажи @username или user_id."
        if val.startswith("@"):
            uname = val[1:].strip()
            if not uname:
                return None, "Некорректный @username."
            row = user_svc.users.get_by_username(uname)
            if not row:
                return None, "Пользователь с таким @username не найден. Пусть сначала запустит бота /start."
            return int(row.get("id") or 0), None
        if re.match(r"^\d+$", val):
            uid = int(val)
            if uid <= 0:
                return None, "Некорректный user_id."
            row = user_svc.users.get_user(uid)
            if not row:
                return None, "Такого user_id нет в базе. Пусть пользователь сначала запустит бота /start."
            return uid, None
        return None, "Формат: @username или user_id."

    async def _send_admins_list(update: Update, *, reply_markup=None):
        if not admin_svc or not hasattr(admin_svc, "list_admins"):
            await update.effective_message.reply_text("⚠️ Сервис админов недоступен.", reply_markup=reply_markup)
            return
        rows = admin_svc.list_admins() or []
        if not rows:
            await update.effective_message.reply_text("👥 Админы: список пуст.", reply_markup=reply_markup)
            return
        lines = ["👥 Админы:"]
        for r in rows:
            uid = int(r.get("user_id") or 0)
            role = str(r.get("role") or "admin").strip().lower()
            mark = "👑 owner" if role == "owner" else "🛠 admin"
            lines.append(f"• {_user_label(uid)} — {mark}")
        await update.effective_message.reply_text("\n".join(lines), reply_markup=reply_markup)

    async def cmd_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_owner(update):
            await update.effective_message.reply_text("⛔️ Команда доступна только owner.")
            return
        await _send_admins_list(update)

    async def cmd_admin_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_owner(update):
            await update.effective_message.reply_text("⛔️ Команда доступна только owner.")
            return
        target_uid = _parse_target_uid(context)
        if not target_uid:
            await update.effective_message.reply_text("Формат: /admin_add <user_id>")
            return
        before_role = _admin_role_by_uid(target_uid)
        ok, msg = admin_svc.grant_admin(update.effective_user.id, target_uid)
        invite_status = ""
        if ok:
            invite_status = await _send_events_chat_invite_to_admin(
                context,
                target_uid=target_uid,
                granted_role="admin",
            )
            msg = f"{msg}\n{invite_status}"
        await update.effective_message.reply_text(("✅ " if ok else "⚠️ ") + msg)
        if ok:
            after_role = _admin_role_by_uid(target_uid)
            await _emit_admin_event(
                context,
                update.effective_user.id,
                "Выдана роль admin",
                "\n".join(
                    [
                        f"• Пользователь: {_user_label(target_uid)}",
                        f"• Роль: {_admin_role_label(before_role)} → {_admin_role_label(after_role)}",
                        f"• Invite-статус: {invite_status or '—'}",
                    ]
                ),
            )

    async def cmd_admin_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_owner(update):
            await update.effective_message.reply_text("⛔️ Команда доступна только owner.")
            return
        target_uid = _parse_target_uid(context)
        if not target_uid:
            await update.effective_message.reply_text("Формат: /admin_remove <user_id>")
            return
        before_role = _admin_role_by_uid(target_uid)
        ok, msg = admin_svc.remove_admin(update.effective_user.id, target_uid)
        await update.effective_message.reply_text(("✅ " if ok else "⚠️ ") + msg)
        if ok:
            after_role = _admin_role_by_uid(target_uid)
            await _emit_admin_event(
                context,
                update.effective_user.id,
                "Админ удалён",
                "\n".join(
                    [
                        f"• Пользователь: {_user_label(target_uid)}",
                        f"• Роль: {_admin_role_label(before_role)} → {_admin_role_label(after_role)}",
                    ]
                ),
            )

    async def _reply_ticket_and_notify(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        ticket_id: int,
        reply_text: str,
        *,
        reply_markup=None,
    ) -> bool:
        row = support_svc.reply_and_close(
            ticket_id=int(ticket_id),
            admin_id=update.effective_user.id,
            reply_text=reply_text,
        )
        if not row:
            await update.effective_message.reply_text(
                "Не смог закрыть тикет. Возможно, он уже закрыт или не существует.",
                reply_markup=reply_markup,
            )
            return False

        ticket_id_internal = int(row.get("id") or ticket_id or 0)
        ticket_number = _ticket_number(row)
        user_id = int(row.get("user_id") or 0)
        user_msg = f"💬 Ответ поддержки по заявке №{ticket_number}:\n{reply_text}"
        sent_ok = True
        try:
            await context.bot.send_message(chat_id=user_id, text=user_msg)
        except Exception:
            sent_ok = False

        tail = "" if sent_ok else "\n⚠️ Пользователю отправить не удалось (проверь chat доступность)."
        await update.effective_message.reply_text(
            f"✅ Заявка №{ticket_number} (id={ticket_id_internal}) закрыта и обработана.{tail}",
            reply_markup=reply_markup,
        )
        return True

    # ----------------------------
    # Actions (reply-based)
    # ----------------------------
    async def lessons_list(update: Update):
        items = lesson_repo.list_latest(200)
        if not items:
            await update.effective_message.reply_text("📚 Лекции: пока пусто.", reply_markup=kb_admin_actions(False))
            return
        lines = ["📚 *Лекции* (день → баллы)"]
        for it in items:
            lines.append(f"• день *{it['day_index']}* — +{it['points_viewed']} балл(ов) — {it['title']}")
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode="Markdown", reply_markup=kb_admin_actions(False)
        )

    async def lessons_create(update: Update):
        uid = update.effective_user.id
        state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "l_create_day"})
        await update.effective_message.reply_text("➕ Создание лекции\n\nВведи номер дня (целое число), например: 1")

    async def lessons_edit(update: Update):
        uid = update.effective_user.id
        state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "l_edit_day"})
        await update.effective_message.reply_text("✏️ Редактирование лекции\n\nВведи номер существующего дня (целое число), например: 1")

    async def lessons_delete(update: Update):
        uid = update.effective_user.id
        state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "l_delete_day"})
        await update.effective_message.reply_text("🗑 Удаление лекции\n\nВведи номер дня (целое число), например: 1")

    async def quests_list(update: Update):
        items = quest_repo.list_latest(200)
        if not items:
            await update.effective_message.reply_text("📝 Задания: пока пусто.", reply_markup=kb_admin_actions(False))
            return
        lines = ["📝 *Задания* (день → баллы)"]
        for it in items:
            pts = _extract_quest_points(it)
            prompt = (it.get("prompt") or "").replace("\n", " ")
            if len(prompt) > 60:
                prompt = prompt[:57] + "..."
            lines.append(f"• день *{it['day_index']}* — +{pts} балл(ов) — {prompt}")
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode="Markdown", reply_markup=kb_admin_actions(False)
        )

    async def quests_create(update: Update):
        uid = update.effective_user.id
        state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "qst_create_day"})
        await update.effective_message.reply_text("➕ Создание задания\n\nВведи номер дня (целое число), например: 1")

    async def quests_edit(update: Update):
        uid = update.effective_user.id
        state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "qst_edit_day"})
        await update.effective_message.reply_text("✏️ Редактирование задания\n\nВведи номер существующего дня (целое число), например: 1")

    async def quests_delete(update: Update):
        uid = update.effective_user.id
        state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "qst_delete_day"})
        await update.effective_message.reply_text("🗑 Удаление задания\n\nВведи номер дня (целое число), например: 1")

    async def extras_list(update: Update):
        if not extra_repo:
            await update.effective_message.reply_text("⚠️ Репозиторий доп. материалов не подключён.", reply_markup=kb_admin_actions(False))
            return
        items = extra_repo.list_latest(200)
        if not items:
            await update.effective_message.reply_text("🧩 Доп. материалы: пока пусто.", reply_markup=kb_admin_actions(False))
            return
        lines = ["🧩 Доп. материалы (день → баллы)"]
        for it in items:
            txt = (it.get("content_text") or "").replace("\n", " ")
            if len(txt) > 60:
                txt = txt[:57] + "..."
            pts = int(it.get("points") or 0)
            flags = []
            if it.get("link_url"):
                flags.append("link")
            if it.get("photo_file_id"):
                flags.append("photo")
            flags_s = f" ({', '.join(flags)})" if flags else ""
            lines.append(f"• день {it['day_index']} — +{pts} балл(ов){flags_s} — {txt}")
        await update.effective_message.reply_text("\n".join(lines), reply_markup=kb_admin_actions(False))

    async def extras_create(update: Update):
        if not extra_repo:
            await update.effective_message.reply_text("⚠️ Репозиторий доп. материалов не подключён.")
            return
        uid = update.effective_user.id
        state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "ext_create_day"})
        await update.effective_message.reply_text("➕ Создание доп. материала\n\nВведи номер дня (целое число), например: 1")

    async def extras_edit(update: Update):
        if not extra_repo:
            await update.effective_message.reply_text("⚠️ Репозиторий доп. материалов не подключён.")
            return
        uid = update.effective_user.id
        state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "ext_edit_day"})
        await update.effective_message.reply_text("✏️ Редактирование доп. материала\n\nВведи номер существующего дня (целое число), например: 1")

    async def extras_delete(update: Update):
        if not extra_repo:
            await update.effective_message.reply_text("⚠️ Репозиторий доп. материалов не подключён.")
            return
        uid = update.effective_user.id
        state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "ext_delete_day"})
        await update.effective_message.reply_text("🗑 Удаление доп. материала\n\nВведи номер дня (целое число), например: 1")

    async def q_list(update: Update):
        items = qsvc.list_latest(50)
        if not items:
            await update.effective_message.reply_text("📋 Анкеты: пока пусто.", reply_markup=kb_admin_actions(True))
            return
        lines = ["📋 Анкеты (id → день, тип, баллы, диаграммы)"]
        for it in items:
            qid = it["id"]
            qtype = str(it.get("qtype") or "manual")
            qtype_label = _questionnaire_type_label(qtype)
            day = it.get("day_index")
            day_label = str(int(day)) if day is not None else "—"
            pts = int(it.get("points") or 0)
            charts = "да" if it.get("use_in_charts") else "нет"
            q = it.get("question") or ""
            if len(q) > 70:
                q = q[:67] + "..."
            lines.append(f"• {qid} — day={day_label} — {qtype_label} — +{pts} — charts={charts} — {q}")
        await update.effective_message.reply_text(
            "\n".join(lines), reply_markup=kb_admin_actions(True)
        )

    async def q_create(update: Update):
        uid = update.effective_user.id
        state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "q_create_question"})
        await update.effective_message.reply_text("➕ Создание анкеты\n\nВведи вопрос анкеты одним сообщением.")

    async def q_edit(update: Update):
        uid = update.effective_user.id
        state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "q_edit_id"})
        await update.effective_message.reply_text("✏️ Редактирование анкеты\n\nВведи ID анкеты (число).")

    async def q_delete(update: Update):
        uid = update.effective_user.id
        state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "q_delete_id"})
        await update.effective_message.reply_text("🗑 Удаление анкеты\n\nВведи ID анкеты (число).")

    async def q_random(update: Update):
        uid = update.effective_user.id
        state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "qcast_question"})
        await update.effective_message.reply_text("🎲 Рандомная анкета всем\n\nВведи вопрос анкеты одним сообщением.")

    async def achievements_list(update: Update):
        if not achievement_svc:
            await update.effective_message.reply_text(
                "⚠️ Сервис ачивок не подключён.",
                reply_markup=kb_admin_actions(False),
            )
            return
        rows = achievement_svc.list_rules(limit=200, active_only=None)
        if not rows:
            await update.effective_message.reply_text(
                "🏆 Правила ачивок: пока пусто.",
                reply_markup=kb_admin_actions(False),
            )
            return
        lines = ["🏆 Правила ачивок (№, code, условие):", ""]
        lines.extend(_achievement_row_line(r, i + 1) for i, r in enumerate(rows))
        await update.effective_message.reply_text(
            "\n".join(lines),
            reply_markup=kb_admin_actions(False),
        )

    async def achievements_create(update: Update):
        uid = update.effective_user.id
        state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "a_create_title"})
        await update.effective_message.reply_text(
            "➕ Создание ачивки\n\n"
            "Шаг 1/7. Введи название ачивки.",
        )

    async def achievements_edit(update: Update):
        uid = update.effective_user.id
        state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "a_edit_id"})
        await update.effective_message.reply_text(
            "✏️ Редактирование ачивки\n\nВведи code, ID или номер из списка (например №9)."
        )

    async def achievements_delete(update: Update):
        uid = update.effective_user.id
        state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "a_delete_id"})
        await update.effective_message.reply_text(
            "🗑 Удаление ачивки\n\nВведи code, ID или номер из списка (например №9)."
        )

    # ----------------------------
    # Reply-based menu router
    # ----------------------------
    async def admin_menu_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update):
            return

        text = (update.effective_message.text or "").strip()
        if not text or text.startswith("/"):
            return

        uid = update.effective_user.id
        st = state.get_state(uid) or {}

        # If we're in wizard, don't handle anything here.
        # wizard_text (group=-10) will handle Back and input.
        if st.get("step") == ADMIN_WIZARD_STEP:
            return

        # This handler should be active only while admin menu state is active.
        # If state is not admin_menu, let other routers handle the message.
        if st.get("step") != ADMIN_MENU_STEP:
            return

        # Allow re-opening admin home from the main menu button while state is active.
        if text == texts.MENU_ADMIN:
            await _show_admin_home(update)
            raise ApplicationHandlerStop

        # If user clicks any user-side navigation button while admin state is still
        # active, leave admin state and let user handlers process this message.
        user_nav_escape = {
            texts.MENU_DAY,
            texts.MENU_PROGRESS,
            texts.MENU_SETTINGS,
            texts.MENU_HELP,
            texts.DAY_QUOTE,
            texts.DAY_PIC,
            texts.DAY_TIP,
            texts.DAY_BOOK,
            texts.DAY_FILM,
            texts.DAY_MOOD,
            texts.DAY_MATERIALS_NOW,
            texts.PROGRESS_REFRESH,
            texts.SETTINGS_TIME,
            texts.SETTINGS_NAME,
            texts.SETTINGS_TZ,
            texts.SETTINGS_REMINDERS,
            texts.SETTINGS_HABITS,
            texts.SETTINGS_PERSONAL_REMINDERS,
            texts.REMINDERS_HUB_HABITS,
            texts.REMINDERS_HUB_ONCE,
            texts.HABITS_CREATE,
            texts.HABITS_LIST,
            texts.HABITS_EDIT,
            texts.HABITS_DELETE,
            texts.REMINDERS_CREATE,
            texts.REMINDERS_LIST,
            texts.REMINDERS_EDIT,
            texts.REMINDERS_DELETE,
            texts.HELP_NOT_HELPED,
            texts.HELP_CONTACT_ADMIN,
        }
        if text in user_nav_escape:
            state.clear_state(uid)
            return
        payload = st.get("payload_json") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        screen = payload.get("screen")

        # Back inside admin menu:
        #   lessons/quests/questionnaires/analytics -> admin home
        #   home -> main menu
        # (Wizard has its own Back in wizard_text.)
        if text == texts.BTN_BACK:
            screen0 = (screen or "home").lower()

            if screen0 in ("lessons", "quests", "extra", "questionnaires", "analytics", "achievements", "tickets", "admins"):
                await _show_admin_home(update)
                raise ApplicationHandlerStop

            # home (or unknown) -> exit to main menu
            state.clear_state(uid)
            await _show_main_menu(update)
            raise ApplicationHandlerStop

        if screen in ("home", "library"):
            if text == texts.ADMIN_LESSONS:
                await _show_lessons_menu(update); raise ApplicationHandlerStop
            if text == texts.ADMIN_QUESTS:
                await _show_quests_menu(update); raise ApplicationHandlerStop
            if text == texts.ADMIN_EXTRA:
                await _show_extras_menu(update); raise ApplicationHandlerStop
            if text == texts.ADMIN_QUESTIONNAIRES:
                await _show_q_menu(update); raise ApplicationHandlerStop
            if text == texts.ADMIN_ANALYTICS:
                await _show_analytics_menu(update, 7, show_report=False); raise ApplicationHandlerStop
            if text == texts.ADMIN_ACHIEVEMENTS:
                await _show_achievements_menu(update); raise ApplicationHandlerStop
            if text == texts.ADMIN_TICKETS:
                await _show_tickets_menu(update, "open", 20); raise ApplicationHandlerStop
            if text == texts.ADMIN_ADMINS:
                if not _is_owner(update):
                    await update.effective_message.reply_text("⛔️ Раздел доступен только owner.")
                    raise ApplicationHandlerStop
                await _show_admins_menu(update); raise ApplicationHandlerStop

        if screen == "analytics":
            if text == BTN_PERIOD_TODAY:
                await _show_analytics_menu(update, 1); raise ApplicationHandlerStop
            if text == BTN_PERIOD_7:
                await _show_analytics_menu(update, 7); raise ApplicationHandlerStop
            if text == BTN_PERIOD_30:
                await _show_analytics_menu(update, 30); raise ApplicationHandlerStop
        if screen == "lessons":
            if text == BTN_LIST: await lessons_list(update); raise ApplicationHandlerStop
            if text == BTN_CREATE: await lessons_create(update); raise ApplicationHandlerStop
            if text == BTN_EDIT: await lessons_edit(update); raise ApplicationHandlerStop
            if text == BTN_DELETE: await lessons_delete(update); raise ApplicationHandlerStop

        if screen == "quests":
            if text == BTN_LIST: await quests_list(update); raise ApplicationHandlerStop
            if text == BTN_CREATE: await quests_create(update); raise ApplicationHandlerStop
            if text == BTN_EDIT: await quests_edit(update); raise ApplicationHandlerStop
            if text == BTN_DELETE: await quests_delete(update); raise ApplicationHandlerStop

        if screen == "extra":
            if text == BTN_LIST: await extras_list(update); raise ApplicationHandlerStop
            if text == BTN_CREATE: await extras_create(update); raise ApplicationHandlerStop
            if text == BTN_EDIT: await extras_edit(update); raise ApplicationHandlerStop
            if text == BTN_DELETE: await extras_delete(update); raise ApplicationHandlerStop

        if screen == "questionnaires":
            if text == BTN_LIST: await q_list(update); raise ApplicationHandlerStop
            if text == BTN_CREATE: await q_create(update); raise ApplicationHandlerStop
            if text == BTN_EDIT: await q_edit(update); raise ApplicationHandlerStop
            if text == BTN_DELETE: await q_delete(update); raise ApplicationHandlerStop
            if text == BTN_RANDOM_Q: await q_random(update); raise ApplicationHandlerStop

        if screen == "achievements":
            if text == BTN_LIST: await achievements_list(update); raise ApplicationHandlerStop
            if text == BTN_CREATE: await achievements_create(update); raise ApplicationHandlerStop
            if text == BTN_EDIT: await achievements_edit(update); raise ApplicationHandlerStop
            if text == BTN_DELETE: await achievements_delete(update); raise ApplicationHandlerStop

        if screen == "tickets":
            payload0 = payload or {}
            mode = _safe_tickets_mode(payload0.get("mode"))
            limit = _safe_tickets_limit(payload0.get("limit"))

            if text == BTN_T_OPEN:
                await _show_tickets_menu(update, "open", limit); raise ApplicationHandlerStop
            if text == BTN_T_VIEW:
                state.set_state(
                    uid,
                    ADMIN_WIZARD_STEP,
                    {"mode": "t_view_id", "return_mode": mode, "return_limit": limit},
                )
                await update.effective_message.reply_text("Введи ID тикета (число).")
                raise ApplicationHandlerStop
            if text == BTN_T_REPLY:
                state.set_state(
                    uid,
                    ADMIN_WIZARD_STEP,
                    {"mode": "t_reply_id", "return_mode": mode, "return_limit": limit},
                )
                await update.effective_message.reply_text("Введи ID тикета, на который нужно ответить.")
                raise ApplicationHandlerStop
            if text == BTN_T_CLOSE:
                state.set_state(
                    uid,
                    ADMIN_WIZARD_STEP,
                    {"mode": "t_close_id", "return_mode": mode, "return_limit": limit},
                )
                await update.effective_message.reply_text("Введи ID тикета, который нужно закрыть.")
                raise ApplicationHandlerStop

        if screen == "admins":
            if not _is_owner(update):
                await update.effective_message.reply_text("⛔️ Раздел доступен только owner.")
                await _show_admin_home(update)
                raise ApplicationHandlerStop
            if text == BTN_LIST:
                await _send_admins_list(update, reply_markup=kb_admin_admins())
                raise ApplicationHandlerStop
            if text == BTN_ADM_ADD:
                state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "adm_add_target"})
                await update.effective_message.reply_text("Введи @username или user_id для добавления в admin.")
                raise ApplicationHandlerStop
            if text == BTN_ADM_PROMOTE:
                state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "adm_promote_target"})
                await update.effective_message.reply_text("Введи @username или user_id для выдачи роли owner.")
                raise ApplicationHandlerStop
            if text == BTN_ADM_DEMOTE:
                state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "adm_demote_target"})
                await update.effective_message.reply_text("Введи @username или user_id для понижения до admin.")
                raise ApplicationHandlerStop
            if text == BTN_ADM_REMOVE:
                state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "adm_remove_target"})
                await update.effective_message.reply_text("Введи @username или user_id для удаления из админов.")
                raise ApplicationHandlerStop

        # Stop further handlers while admin menu is active.
        raise ApplicationHandlerStop

    # ----------------------------
    # Wizard text (adapted from previous implementation; reply keyboards only)
    # ----------------------------
    async def wizard_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update):
            return
        text = (update.effective_message.text or "").strip()
        if not text or text.startswith("/"):
            return
        uid = update.effective_user.id
        st = state.get_state(uid)

        # ✅ Wizard handler must only run when we are really inside the wizard.
        if not st or st.get("step") != ADMIN_WIZARD_STEP:
            return

        # Allow leaving admin wizard by pressing user-side navigation buttons.
        user_nav_escape = {
            texts.MENU_DAY,
            texts.MENU_PROGRESS,
            texts.MENU_SETTINGS,
            texts.MENU_HELP,
            texts.DAY_QUOTE,
            texts.DAY_PIC,
            texts.DAY_TIP,
            texts.DAY_BOOK,
            texts.DAY_FILM,
            texts.DAY_MOOD,
            texts.DAY_MATERIALS_NOW,
            texts.PROGRESS_REFRESH,
            texts.SETTINGS_TIME,
            texts.SETTINGS_NAME,
            texts.SETTINGS_TZ,
            texts.SETTINGS_REMINDERS,
            texts.SETTINGS_HABITS,
            texts.SETTINGS_PERSONAL_REMINDERS,
            texts.REMINDERS_HUB_HABITS,
            texts.REMINDERS_HUB_ONCE,
            texts.HABITS_CREATE,
            texts.HABITS_LIST,
            texts.HABITS_EDIT,
            texts.HABITS_DELETE,
            texts.REMINDERS_CREATE,
            texts.REMINDERS_LIST,
            texts.REMINDERS_EDIT,
            texts.REMINDERS_DELETE,
            texts.HELP_NOT_HELPED,
            texts.HELP_CONTACT_ADMIN,
        }
        if text in user_nav_escape:
            state.clear_state(uid)
            return

        # Quick jump inside admin while wizard is active.
        if text == texts.MENU_ADMIN:
            state.clear_state(uid)
            await _show_admin_home(update)
            raise ApplicationHandlerStop
        if text == texts.ADMIN_LESSONS:
            state.clear_state(uid)
            await _show_lessons_menu(update)
            raise ApplicationHandlerStop
        if text == texts.ADMIN_QUESTS:
            state.clear_state(uid)
            await _show_quests_menu(update)
            raise ApplicationHandlerStop
        if text == texts.ADMIN_EXTRA:
            state.clear_state(uid)
            await _show_extras_menu(update)
            raise ApplicationHandlerStop
        if text == texts.ADMIN_QUESTIONNAIRES:
            state.clear_state(uid)
            await _show_q_menu(update)
            raise ApplicationHandlerStop
        if text == texts.ADMIN_ANALYTICS:
            state.clear_state(uid)
            await _show_analytics_menu(update, 7, show_report=False)
            raise ApplicationHandlerStop
        if text == texts.ADMIN_ACHIEVEMENTS:
            state.clear_state(uid)
            await _show_achievements_menu(update)
            raise ApplicationHandlerStop
        if text == texts.ADMIN_TICKETS:
            state.clear_state(uid)
            await _show_tickets_menu(update, "open", 20)
            raise ApplicationHandlerStop
        if text == texts.ADMIN_ADMINS:
            if not _is_owner(update):
                await update.effective_message.reply_text("⛔️ Раздел доступен только owner.")
                raise ApplicationHandlerStop
            state.clear_state(uid)
            await _show_admins_menu(update)
            raise ApplicationHandlerStop

        # Allow leaving wizard with Back
        if text == texts.BTN_BACK:
            payload0 = st.get("payload_json") or {}
            if isinstance(payload0, str):
                try:
                    payload0 = json.loads(payload0)
                except Exception:
                    payload0 = {}
            mode0 = (payload0.get("mode") or "").lower()

            state.clear_state(uid)

            # Return to the appropriate admin menu
            if mode0.startswith("l_"):
                await _show_lessons_menu(update)
            elif mode0.startswith("qst_"):
                await _show_quests_menu(update)
            elif mode0.startswith("ext_"):
                await _show_extras_menu(update)
            elif mode0.startswith("q_") or mode0.startswith("qcast_"):
                await _show_q_menu(update)
            elif mode0.startswith("a_"):
                await _show_achievements_menu(update)
            elif mode0.startswith("t_"):
                back_mode = _safe_tickets_mode(payload0.get("return_mode"))
                back_limit = _safe_tickets_limit(payload0.get("return_limit"))
                await _show_tickets_menu(update, back_mode, back_limit)
            elif mode0.startswith("adm_"):
                await _show_admins_menu(update)
            else:
                await _show_admin_home(update)
            raise ApplicationHandlerStop
        payload = st.get("payload_json") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        mode = payload.get("mode")
        mode_s = str(mode or "").lower()
        if not mode_s:
            state.clear_state(uid)
            return

        # Quick action buttons should switch wizard mode and never be treated as step input.
        if mode_s.startswith("l_"):
            if text == BTN_LIST:
                state.clear_state(uid); await _show_lessons_menu(update); await lessons_list(update); raise ApplicationHandlerStop
            if text == BTN_CREATE:
                await lessons_create(update); raise ApplicationHandlerStop
            if text == BTN_EDIT:
                await lessons_edit(update); raise ApplicationHandlerStop
            if text == BTN_DELETE:
                await lessons_delete(update); raise ApplicationHandlerStop

        if mode_s.startswith("qst_"):
            if text == BTN_LIST:
                state.clear_state(uid); await _show_quests_menu(update); await quests_list(update); raise ApplicationHandlerStop
            if text == BTN_CREATE:
                await quests_create(update); raise ApplicationHandlerStop
            if text == BTN_EDIT:
                await quests_edit(update); raise ApplicationHandlerStop
            if text == BTN_DELETE:
                await quests_delete(update); raise ApplicationHandlerStop

        if mode_s.startswith("ext_"):
            if text == BTN_LIST:
                state.clear_state(uid); await _show_extras_menu(update); await extras_list(update); raise ApplicationHandlerStop
            if text == BTN_CREATE:
                await extras_create(update); raise ApplicationHandlerStop
            if text == BTN_EDIT:
                await extras_edit(update); raise ApplicationHandlerStop
            if text == BTN_DELETE:
                await extras_delete(update); raise ApplicationHandlerStop

        if mode_s.startswith("q_") or mode_s.startswith("qcast_"):
            if text == BTN_LIST:
                state.clear_state(uid); await _show_q_menu(update); await q_list(update); raise ApplicationHandlerStop
            if text == BTN_CREATE:
                await q_create(update); raise ApplicationHandlerStop
            if text == BTN_EDIT:
                await q_edit(update); raise ApplicationHandlerStop
            if text == BTN_DELETE:
                await q_delete(update); raise ApplicationHandlerStop
            if text == BTN_RANDOM_Q:
                await q_random(update); raise ApplicationHandlerStop

        if mode_s.startswith("a_"):
            if text == BTN_LIST:
                state.clear_state(uid); await _show_achievements_menu(update); await achievements_list(update); raise ApplicationHandlerStop
            if text == BTN_CREATE:
                await achievements_create(update); raise ApplicationHandlerStop
            if text == BTN_EDIT:
                await achievements_edit(update); raise ApplicationHandlerStop
            if text == BTN_DELETE:
                await achievements_delete(update); raise ApplicationHandlerStop

        if mode_s.startswith("t_"):
            return_mode = _safe_tickets_mode(payload.get("return_mode"))
            return_limit = _safe_tickets_limit(payload.get("return_limit"))
            if text == BTN_T_OPEN:
                state.clear_state(uid); await _show_tickets_menu(update, "open", return_limit); raise ApplicationHandlerStop
            if text == BTN_T_VIEW:
                state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "t_view_id", "return_mode": return_mode, "return_limit": return_limit})
                await update.effective_message.reply_text("Введи ID тикета (число).")
                raise ApplicationHandlerStop
            if text == BTN_T_REPLY:
                state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "t_reply_id", "return_mode": return_mode, "return_limit": return_limit})
                await update.effective_message.reply_text("Введи ID тикета, на который нужно ответить.")
                raise ApplicationHandlerStop
            if text == BTN_T_CLOSE:
                state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "t_close_id", "return_mode": return_mode, "return_limit": return_limit})
                await update.effective_message.reply_text("Введи ID тикета, который нужно закрыть.")
                raise ApplicationHandlerStop

        if mode_s.startswith("adm_"):
            if text == BTN_LIST:
                state.clear_state(uid)
                await _show_admins_menu(update)
                await _send_admins_list(update, reply_markup=kb_admin_admins())
                raise ApplicationHandlerStop
            if text == BTN_ADM_ADD:
                state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "adm_add_target"})
                await update.effective_message.reply_text("Введи @username или user_id для добавления в admin.")
                raise ApplicationHandlerStop
            if text == BTN_ADM_PROMOTE:
                state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "adm_promote_target"})
                await update.effective_message.reply_text("Введи @username или user_id для выдачи роли owner.")
                raise ApplicationHandlerStop
            if text == BTN_ADM_DEMOTE:
                state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "adm_demote_target"})
                await update.effective_message.reply_text("Введи @username или user_id для понижения до admin.")
                raise ApplicationHandlerStop
            if text == BTN_ADM_REMOVE:
                state.set_state(uid, ADMIN_WIZARD_STEP, {"mode": "adm_remove_target"})
                await update.effective_message.reply_text("Введи @username или user_id для удаления из админов.")
                raise ApplicationHandlerStop

        # --- Lessons wizard ---
        if mode in ("l_create_day", "l_edit_day", "l_delete_day"):
            if not re.match(r"^\d+$", text):
                await update.effective_message.reply_text("Нужен номер дня (число)."); raise ApplicationHandlerStop
            day = int(text)
            if mode == "l_delete_day":
                old = lesson_repo.get_by_day(day)
                ok = lesson_repo.delete_day(day)
                if ok:
                    _schedule_daily_pack_regenerate("lesson_deleted")
                state.clear_state(update.effective_user.id)
                await _show_lessons_menu(update)
                await update.effective_message.reply_text("✅ Удалено" if ok else "⚠️ Не найдено")
                if ok:
                    details = [f"• День: {day}"]
                    if old:
                        details.append(f"• Название: {_short_text(old.get('title'))}")
                        details.append(f"• Баллы: {_int_text(old.get('points_viewed'))}")
                        details.append(f"• Видео: {_short_text(old.get('video_url'), limit=120)}")
                    await _emit_admin_event(
                        context,
                        uid,
                        "Удалена лекция",
                        "\n".join(details),
                    )
                return
            if mode == "l_edit_day":
                existing = lesson_repo.get_by_day(day)
                if not existing:
                    await update.effective_message.reply_text("⚠️ Лекция на этом дне не найдена."); raise ApplicationHandlerStop
                payload = {
                    "mode": "l_edit_new_day",
                    "source_day_index": day,
                    "day_index": day,
                    "title": str(existing.get("title") or ""),
                    "description": str(existing.get("description") or ""),
                    "video_url": str(existing.get("video_url") or ""),
                    "points_viewed": int(existing.get("points_viewed") or 0),
                }
                state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
                await update.effective_message.reply_text(
                    f"Текущий день: {day}.\n"
                    "Введи новый номер дня или '-' чтобы оставить как есть."
                )
                return
            existing = lesson_repo.get_by_day(day)
            payload = {"mode": "l_title", "day_index": day}
            state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text(
                f"День {day}. Введи название лекции." + (f"\nТекущее: {existing['title']}" if existing else ""))
            return

        if mode == "l_edit_new_day":
            source_day = int(payload.get("source_day_index") or 0)
            if text.strip() == "-":
                new_day = source_day
            else:
                if not re.match(r"^\d+$", text):
                    await update.effective_message.reply_text("Нужен номер дня (число)."); raise ApplicationHandlerStop
                new_day = int(text)
                if new_day <= 0:
                    await update.effective_message.reply_text("Номер дня должен быть больше нуля."); raise ApplicationHandlerStop
            if new_day != source_day and lesson_repo.get_by_day(new_day):
                await update.effective_message.reply_text("⚠️ На этом дне уже есть лекция. Выбери другой день."); raise ApplicationHandlerStop
            payload["day_index"] = new_day
            payload["mode"] = "l_title"
            state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text(
                f"День {new_day}. Введи название лекции.\n"
                f"Текущее: {payload.get('title') or '—'}\n"
                "Отправь '-' чтобы оставить как есть."
            )
            return

        if mode == "l_title":
            is_edit = payload.get("source_day_index") is not None
            if is_edit and text.strip() == "-":
                payload["title"] = str(payload.get("title") or "")
            else:
                payload["title"] = text
            payload["mode"] = "l_desc"
            state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
            if is_edit:
                await update.effective_message.reply_text(
                    "Введи описание лекции (текст).\n"
                    f"Текущее: {_short_text(payload.get('description'), limit=120)}\n"
                    "Отправь '-' чтобы оставить как есть."
                )
            else:
                await update.effective_message.reply_text("Введи описание лекции (текст).")
            return

        if mode == "l_desc":
            is_edit = payload.get("source_day_index") is not None
            if is_edit and text.strip() == "-":
                payload["description"] = str(payload.get("description") or "")
            else:
                payload["description"] = text
            payload["mode"] = "l_video"
            state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
            if is_edit:
                await update.effective_message.reply_text(
                    "Вставь ссылку на видео (Rutube/YouTube и т.п.).\n"
                    f"Текущая: {_short_text(payload.get('video_url'), limit=120)}\n"
                    "Отправь '-' чтобы оставить как есть."
                )
            else:
                await update.effective_message.reply_text("Вставь ссылку на видео (Rutube/YouTube и т.п.).")
            return

        if mode == "l_video":
            is_edit = payload.get("source_day_index") is not None
            if is_edit and text.strip() == "-":
                payload["video_url"] = str(payload.get("video_url") or "")
            else:
                if not (text.startswith("http://") or text.startswith("https://")):
                    await update.effective_message.reply_text("Нужна ссылка, которая начинается с http:// или https://"); raise ApplicationHandlerStop
                payload["video_url"] = text
            payload["mode"] = "l_points"
            state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
            if is_edit:
                await update.effective_message.reply_text(
                    "Сколько баллов за кнопку «Просмотрено»? (целое число)\n"
                    f"Текущее: {int(payload.get('points_viewed') or 0)}\n"
                    "Отправь '-' чтобы оставить как есть."
                )
            else:
                await update.effective_message.reply_text("Сколько баллов за кнопку «Просмотрено»? (целое число)")
            return

        if mode == "l_points":
            is_edit = payload.get("source_day_index") is not None
            if is_edit and text.strip() == "-":
                points = int(payload.get("points_viewed") or 0)
            else:
                if not re.match(r"^\d+$", text):
                    await update.effective_message.reply_text("Нужно целое число, например: 1"); raise ApplicationHandlerStop
                points = int(text)
            day = int(payload["day_index"])
            source_day = int(payload.get("source_day_index") or day)
            old = lesson_repo.get_by_day(source_day) or {}
            lesson_repo.upsert_lesson(day, payload["title"], payload["description"], payload["video_url"], points)
            if source_day != day:
                lesson_repo.delete_day(source_day)
            _schedule_daily_pack_regenerate("lesson_updated" if old else "lesson_added")
            state.clear_state(update.effective_user.id)
            await _show_lessons_menu(update)
            await update.effective_message.reply_text("✅ Сохранено.")
            details = [f"• День: {source_day} → {day}" if source_day != day else f"• День: {day}"]
            for line in (
                _diff_line("Название", old.get("title"), payload["title"]),
                _diff_line("Описание", old.get("description"), payload["description"], formatter=lambda v: _short_text(v, limit=120)),
                _diff_line("Видео", old.get("video_url"), payload["video_url"], formatter=lambda v: _short_text(v, limit=120)),
                _diff_line("Баллы", old.get("points_viewed"), points, formatter=_int_text),
            ):
                if line:
                    details.append(line)
            if len(details) == 1:
                details.append("• Изменений в полях нет (повторное сохранение).")
            await _emit_admin_event(
                context,
                uid,
                "Создана лекция" if not old else "Обновлена лекция",
                "\n".join(details),
            )
            return

        # --- Quests wizard ---
        if mode in ("qst_create_day", "qst_edit_day", "qst_delete_day"):
            if not re.match(r"^\d+$", text):
                await update.effective_message.reply_text("Нужен номер дня (число)."); raise ApplicationHandlerStop
            day = int(text)
            if mode == "qst_delete_day":
                old = quest_repo.get_by_day(day)
                ok = quest_repo.delete_day(day)
                state.clear_state(update.effective_user.id)
                await _show_quests_menu(update)
                await update.effective_message.reply_text("✅ Удалено" if ok else "⚠️ Не найдено")
                if ok:
                    details = [f"• День: {day}"]
                    if old:
                        details.append(f"• Текст: {_short_text(old.get('prompt'), limit=120)}")
                        details.append(f"• Баллы: {_int_text(old.get('points'))}")
                        details.append(f"• Фото: {_yes_no(bool(old.get('photo_file_id')))}")
                    await _emit_admin_event(
                        context,
                        uid,
                        "Удалено задание",
                        "\n".join(details),
                    )
                return
            if mode == "qst_edit_day":
                existing = quest_repo.get_by_day(day)
                if not existing:
                    await update.effective_message.reply_text("⚠️ Задание на этом дне не найдено."); raise ApplicationHandlerStop
                payload = {
                    "mode": "qst_edit_new_day",
                    "source_day_index": day,
                    "day_index": day,
                    "prompt": str(existing.get("prompt") or ""),
                    "photo_file_id": existing.get("photo_file_id"),
                    "points": int(existing.get("points") or 0),
                }
                state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
                await update.effective_message.reply_text(
                    f"Текущий день: {day}.\n"
                    "Введи новый номер дня или '-' чтобы оставить как есть."
                )
                return
            existing = quest_repo.get_by_day(day)
            payload = {"mode": "qst_prompt", "day_index": day}
            state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text(
                f"День {day}. Введи текст задания." + (f"\nТекущее: {existing['prompt']}" if existing else ""))
            return

        if mode == "qst_edit_new_day":
            source_day = int(payload.get("source_day_index") or 0)
            if text.strip() == "-":
                new_day = source_day
            else:
                if not re.match(r"^\d+$", text):
                    await update.effective_message.reply_text("Нужен номер дня (число)."); raise ApplicationHandlerStop
                new_day = int(text)
                if new_day <= 0:
                    await update.effective_message.reply_text("Номер дня должен быть больше нуля."); raise ApplicationHandlerStop
            if new_day != source_day and quest_repo.get_by_day(new_day):
                await update.effective_message.reply_text("⚠️ На этом дне уже есть задание. Выбери другой день."); raise ApplicationHandlerStop
            payload["day_index"] = new_day
            payload["mode"] = "qst_prompt"
            state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text(
                f"День {new_day}. Введи текст задания.\n"
                f"Текущее: {_short_text(payload.get('prompt'), limit=120)}\n"
                "Отправь '-' чтобы оставить как есть."
            )
            return

        if mode == "qst_prompt":
            is_edit = payload.get("source_day_index") is not None
            if is_edit and text.strip() == "-":
                payload["prompt"] = str(payload.get("prompt") or "")
            else:
                payload["prompt"] = text
            payload["mode"] = "qst_photo"
            state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
            if is_edit:
                await update.effective_message.reply_text(
                    "Прикрепи фото к заданию.\n"
                    f"Текущее фото: {_yes_no(bool(payload.get('photo_file_id')))}\n"
                    "Отправь '-' чтобы оставить как есть или '0' чтобы убрать фото."
                )
            else:
                await update.effective_message.reply_text(
                    "Прикрепи фото к заданию или отправь '-' чтобы оставить задание только текстовым."
                )
            return

        if mode == "qst_photo":
            is_edit = payload.get("source_day_index") is not None
            if text == "-":
                if not is_edit:
                    payload["photo_file_id"] = None
                payload["mode"] = "qst_points"
                state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
                if is_edit:
                    await update.effective_message.reply_text(
                        "Сколько баллов за ответ на задание? (целое число)\n"
                        f"Текущее: {int(payload.get('points') or 0)}\n"
                        "Отправь '-' чтобы оставить как есть."
                    )
                else:
                    await update.effective_message.reply_text("Сколько баллов за ответ на задание? (целое число)")
                return
            if is_edit and text == "0":
                payload["photo_file_id"] = None
                payload["mode"] = "qst_points"
                state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
                await update.effective_message.reply_text(
                    "Сколько баллов за ответ на задание? (целое число)\n"
                    f"Текущее: {int(payload.get('points') or 0)}\n"
                    "Отправь '-' чтобы оставить как есть."
                )
                return
            await update.effective_message.reply_text(
                "Нужна фотография, '-' (оставить как есть) или '0' (убрать фото)."
                if is_edit else
                "Нужна фотография или символ '-' для текстового задания."
            )
            return

        if mode == "qst_points":
            is_edit = payload.get("source_day_index") is not None
            if is_edit and text.strip() == "-":
                points = int(payload.get("points") or 0)
            else:
                if not re.match(r"^\d+$", text):
                    await update.effective_message.reply_text("Нужно целое число, например: 1"); raise ApplicationHandlerStop
                points = int(text)
            day = int(payload["day_index"])
            source_day = int(payload.get("source_day_index") or day)
            old = quest_repo.get_by_day(source_day) or {}
            try:
                quest_repo.upsert_quest(day, points, payload["prompt"], payload.get("photo_file_id"))
                if source_day != day:
                    quest_repo.delete_day(source_day)
            except Exception:
                await update.effective_message.reply_text("⚠️ Упс, ошибка. Попробуй ещё раз.")
                raise ApplicationHandlerStop
            state.clear_state(update.effective_user.id)
            await _show_quests_menu(update)
            await update.effective_message.reply_text("✅ Сохранено.")
            details = [f"• День: {source_day} → {day}" if source_day != day else f"• День: {day}"]
            for line in (
                _diff_line("Текст", old.get("prompt"), payload["prompt"], formatter=lambda v: _short_text(v, limit=120)),
                _diff_line("Баллы", old.get("points"), points, formatter=_int_text),
                _diff_line("Фото", bool(old.get("photo_file_id")), bool(payload.get("photo_file_id")), formatter=_yes_no),
            ):
                if line:
                    details.append(line)
            if len(details) == 1:
                details.append("• Изменений в полях нет (повторное сохранение).")
            await _emit_admin_event(
                context,
                uid,
                "Создано задание" if not old else "Обновлено задание",
                "\n".join(details),
            )
            return

        # --- Extra materials wizard ---
        if mode in ("ext_create_day", "ext_edit_day", "ext_delete_day"):
            if not extra_repo:
                await update.effective_message.reply_text("⚠️ Репозиторий доп. материалов не подключён."); raise ApplicationHandlerStop
            if not re.match(r"^\d+$", text):
                await update.effective_message.reply_text("Нужен номер дня (число)."); raise ApplicationHandlerStop
            day = int(text)
            if mode == "ext_delete_day":
                old = extra_repo.get_by_day(day)
                ok = extra_repo.delete_day(day)
                state.clear_state(update.effective_user.id)
                await _show_extras_menu(update)
                await update.effective_message.reply_text("✅ Удалено" if ok else "⚠️ Не найдено")
                if ok:
                    details = [f"• День: {day}"]
                    if old:
                        details.append(f"• Текст: {_short_text(old.get('content_text'), limit=120)}")
                        details.append(f"• Баллы: {_int_text(old.get('points'))}")
                        details.append(f"• Ссылка: {_short_text(old.get('link_url'), limit=120)}")
                        details.append(f"• Фото: {_yes_no(bool(old.get('photo_file_id')))}")
                    await _emit_admin_event(
                        context,
                        uid,
                        "Удалён доп. материал",
                        "\n".join(details),
                    )
                return
            if mode == "ext_edit_day":
                existing = extra_repo.get_by_day(day)
                if not existing:
                    await update.effective_message.reply_text("⚠️ Доп. материал на этом дне не найден."); raise ApplicationHandlerStop
                payload = {
                    "mode": "ext_edit_new_day",
                    "source_day_index": day,
                    "day_index": day,
                    "content_text": str(existing.get("content_text") or ""),
                    "link_url": existing.get("link_url"),
                    "photo_file_id": existing.get("photo_file_id"),
                    "points": int(existing.get("points") or 0),
                }
                state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
                await update.effective_message.reply_text(
                    f"Текущий день: {day}.\n"
                    "Введи новый номер дня или '-' чтобы оставить как есть."
                )
                return
            existing = extra_repo.get_by_day(day)
            payload = {"mode": "ext_text", "day_index": day}
            state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text(
                f"День {day}. Введи текст доп. материала."
                + (f"\nТекущее: {(existing.get('content_text') or '')[:200]}" if existing else "")
            )
            return

        if mode == "ext_edit_new_day":
            source_day = int(payload.get("source_day_index") or 0)
            if text.strip() == "-":
                new_day = source_day
            else:
                if not re.match(r"^\d+$", text):
                    await update.effective_message.reply_text("Нужен номер дня (число)."); raise ApplicationHandlerStop
                new_day = int(text)
                if new_day <= 0:
                    await update.effective_message.reply_text("Номер дня должен быть больше нуля."); raise ApplicationHandlerStop
            if new_day != source_day and extra_repo.get_by_day(new_day):
                await update.effective_message.reply_text("⚠️ На этом дне уже есть доп. материал. Выбери другой день."); raise ApplicationHandlerStop
            payload["day_index"] = new_day
            payload["mode"] = "ext_text"
            state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text(
                f"День {new_day}. Введи текст доп. материала.\n"
                f"Текущее: {_short_text(payload.get('content_text'), limit=120)}\n"
                "Отправь '-' чтобы оставить как есть."
            )
            return

        if mode == "ext_text":
            is_edit = payload.get("source_day_index") is not None
            if is_edit and text.strip() == "-":
                payload["content_text"] = str(payload.get("content_text") or "")
            else:
                payload["content_text"] = text
            payload["mode"] = "ext_link"
            state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
            if is_edit:
                await update.effective_message.reply_text(
                    "Вставь ссылку (http/https).\n"
                    f"Текущая: {_short_text(payload.get('link_url'), limit=120)}\n"
                    "Отправь '-' чтобы оставить как есть или '0' чтобы убрать ссылку."
                )
            else:
                await update.effective_message.reply_text("Вставь ссылку (http/https) или отправь '-' если без ссылки.")
            return

        if mode == "ext_link":
            is_edit = payload.get("source_day_index") is not None
            if text == "-" and not is_edit:
                payload["link_url"] = None
            elif text == "-" and is_edit:
                payload["link_url"] = payload.get("link_url")
            elif is_edit and text == "0":
                payload["link_url"] = None
            elif text.startswith("http://") or text.startswith("https://"):
                payload["link_url"] = text
            else:
                await update.effective_message.reply_text(
                    "Нужна ссылка http/https, '-' (оставить как есть) или '0' (убрать ссылку)."
                    if is_edit else
                    "Нужна ссылка http/https или '-' для пустого значения."
                )
                raise ApplicationHandlerStop
            payload["mode"] = "ext_photo"
            state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
            if is_edit:
                await update.effective_message.reply_text(
                    "Прикрепи фото.\n"
                    f"Текущее фото: {_yes_no(bool(payload.get('photo_file_id')))}\n"
                    "Отправь '-' чтобы оставить как есть или '0' чтобы убрать фото."
                )
            else:
                await update.effective_message.reply_text("Прикрепи фото или отправь '-' если без фото.")
            return

        if mode == "ext_photo":
            is_edit = payload.get("source_day_index") is not None
            if text == "-":
                if not is_edit:
                    payload["photo_file_id"] = None
                payload["mode"] = "ext_points"
                state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
                if is_edit:
                    await update.effective_message.reply_text(
                        "Сколько баллов за просмотр? (целое число, можно 0)\n"
                        f"Текущее: {int(payload.get('points') or 0)}\n"
                        "Отправь '-' чтобы оставить как есть."
                    )
                else:
                    await update.effective_message.reply_text("Сколько баллов за просмотр? (целое число, можно 0)")
                return
            if is_edit and text == "0":
                payload["photo_file_id"] = None
                payload["mode"] = "ext_points"
                state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
                await update.effective_message.reply_text(
                    "Сколько баллов за просмотр? (целое число, можно 0)\n"
                    f"Текущее: {int(payload.get('points') or 0)}\n"
                    "Отправь '-' чтобы оставить как есть."
                )
                return
            await update.effective_message.reply_text(
                "Нужна фотография, '-' (оставить как есть) или '0' (убрать фото)."
                if is_edit else
                "Нужна фотография или символ '-' для варианта без фото."
            )
            return

        if mode == "ext_points":
            is_edit = payload.get("source_day_index") is not None
            if is_edit and text.strip() == "-":
                points = int(payload.get("points") or 0)
            else:
                if not re.match(r"^\d+$", text):
                    await update.effective_message.reply_text("Нужно целое число, например: 0"); raise ApplicationHandlerStop
                points = int(text)
            day = int(payload["day_index"])
            source_day = int(payload.get("source_day_index") or day)
            old = extra_repo.get_by_day(source_day) or {}
            try:
                extra_repo.upsert(
                    day_index=day,
                    content_text=payload["content_text"],
                    points=points,
                    link_url=payload.get("link_url"),
                    photo_file_id=payload.get("photo_file_id"),
                    is_active=True,
                )
                if source_day != day:
                    extra_repo.delete_day(source_day)
            except Exception:
                await update.effective_message.reply_text("⚠️ Упс, ошибка. Попробуй ещё раз.")
                raise ApplicationHandlerStop
            state.clear_state(update.effective_user.id)
            await _show_extras_menu(update)
            await update.effective_message.reply_text("✅ Сохранено.")
            details = [f"• День: {source_day} → {day}" if source_day != day else f"• День: {day}"]
            for line in (
                _diff_line("Текст", old.get("content_text"), payload["content_text"], formatter=lambda v: _short_text(v, limit=120)),
                _diff_line("Баллы", old.get("points"), points, formatter=_int_text),
                _diff_line("Ссылка", old.get("link_url"), payload.get("link_url"), formatter=lambda v: _short_text(v, limit=120)),
                _diff_line("Фото", bool(old.get("photo_file_id")), bool(payload.get("photo_file_id")), formatter=_yes_no),
            ):
                if line:
                    details.append(line)
            if len(details) == 1:
                details.append("• Изменений в полях нет (повторное сохранение).")
            await _emit_admin_event(
                context,
                uid,
                "Создан доп. материал" if not old else "Обновлён доп. материал",
                "\n".join(details),
            )
            return

        # --- Questionnaire wizard (create/edit/delete) ---
        if mode == "q_create_question":
            payload = {"mode": "q_create_day", "question": text}
            state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text("Для какого дня эта анкета? (целое число, например: 1)")
            return

        if mode == "q_create_day":
            if not re.match(r"^\d+$", text):
                await update.effective_message.reply_text("Нужен номер дня (целое число)."); raise ApplicationHandlerStop
            day = int(text)
            if day <= 0:
                await update.effective_message.reply_text("Номер дня должен быть больше нуля."); raise ApplicationHandlerStop
            payload["day_index"] = day
            payload["mode"] = "q_create_charts"
            state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text("Учитывать в диаграммах? (Да/Нет)", reply_markup=kb_yes_no())
            return

        if mode == "q_create_charts":
            t = text.lower()
            if t not in ("да", "нет"):
                await update.effective_message.reply_text("Нужно 'Да' или 'Нет'.", reply_markup=kb_yes_no()); raise ApplicationHandlerStop
            payload["use_in_charts"] = (t == "да")
            payload["mode"] = "q_create_points"
            state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text("Сколько баллов за прохождение? (целое число)")
            return

        if mode == "q_create_points":
            if not re.match(r"^\d+$", text):
                await update.effective_message.reply_text("Нужно целое число, например: 1"); raise ApplicationHandlerStop
            day_index = int(payload.get("day_index") or 1)
            points = int(text)
            qid = qsvc.create(
                payload["question"],
                "manual",
                bool(payload["use_in_charts"]),
                points,
                update.effective_user.id,
                day_index=day_index,
            )
            item = qsvc.get(qid)
            state.clear_state(update.effective_user.id)
            await _show_q_menu(update)
            await update.effective_message.reply_text(f"✅ Анкета создана. ID={qid}.\nВопрос: {item['question']}")
            await _emit_admin_event(
                context,
                uid,
                "Создание анкеты",
                "\n".join(
                    [
                        f"• ID: {qid}",
                        f"• День: {day_index}",
                        f"• Баллы: {points}",
                        f"• Вопрос: {_short_text(payload.get('question'), limit=120)}",
                        f"• В диаграммах: {_yes_no(payload.get('use_in_charts'))}",
                    ]
                ),
            )
            return

        if mode == "q_edit_id":
            if not re.match(r"^\d+$", text):
                await update.effective_message.reply_text("Нужен ID анкеты (число)."); raise ApplicationHandlerStop
            qid = int(text)
            item = qsvc.get(qid)
            if not item:
                await update.effective_message.reply_text("⚠️ Анкета не найдена."); raise ApplicationHandlerStop
            payload = {
                "mode": "q_edit_question",
                "id": qid,
                "qtype": str(item.get("qtype") or "manual"),
                "day_index": item.get("day_index"),
                "question": str(item.get("question") or ""),
                "use_in_charts": bool(item.get("use_in_charts")),
                "points": int(item.get("points") or 0),
            }
            state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text(
                f"Текущий вопрос:\n{payload['question']}\n\n"
                "Введи новый вопрос или отправь '-' чтобы оставить как есть."
            )
            return

        if mode == "q_edit_question":
            if text.strip() != "-":
                payload["question"] = text
            payload["mode"] = "q_edit_day"
            state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
            current_day = payload.get("day_index")
            day_hint = f"{int(current_day)}" if current_day is not None else "не задан"
            await update.effective_message.reply_text(
                f"Текущий день: {day_hint}\n"
                "Введи новый номер дня (целое число) или '-' чтобы оставить как есть."
            )
            return

        if mode == "q_edit_day":
            if text.strip() == "-":
                day = payload.get("day_index")
            else:
                if not re.match(r"^\d+$", text):
                    await update.effective_message.reply_text("Нужен номер дня (целое число)."); raise ApplicationHandlerStop
                day = int(text)
                if day <= 0:
                    await update.effective_message.reply_text("Номер дня должен быть больше нуля."); raise ApplicationHandlerStop
            payload["day_index"] = day
            payload["mode"] = "q_edit_charts"
            state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text(
                f"Учитывать в диаграммах? (Да/Нет)\nТекущее: {_yes_no(payload.get('use_in_charts'))}\n"
                "Отправь '-' чтобы оставить как есть.",
                reply_markup=kb_yes_no(),
            )
            return

        if mode == "q_edit_charts":
            t = text.lower()
            if t == "-":
                payload["use_in_charts"] = bool(payload.get("use_in_charts"))
            else:
                if t not in ("да", "нет"):
                    await update.effective_message.reply_text("Нужно 'Да' или 'Нет'.", reply_markup=kb_yes_no()); raise ApplicationHandlerStop
                payload["use_in_charts"] = (t == "да")
            payload["mode"] = "q_edit_points"
            state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text(
                f"Сколько баллов? (целое число)\nТекущее: {int(payload.get('points') or 0)}\n"
                "Отправь '-' чтобы оставить как есть."
            )
            return

        if mode == "q_edit_points":
            if text.strip() == "-":
                points = int(payload.get("points") or 0)
            else:
                if not re.match(r"^\d+$", text):
                    await update.effective_message.reply_text("Нужно целое число, например: 1"); raise ApplicationHandlerStop
                points = int(text)
            qid = int(payload["id"])
            old = qsvc.get(qid) or {}
            day_raw = payload.get("day_index")
            day_index = int(day_raw) if day_raw is not None else None
            qsvc.update(
                qid,
                payload["question"],
                str(payload.get("qtype") or "manual"),
                bool(payload["use_in_charts"]),
                points,
                day_index=day_index,
            )
            state.clear_state(update.effective_user.id)
            await _show_q_menu(update)
            await update.effective_message.reply_text("✅ Анкета обновлена.")
            details = [f"• ID: {qid}"]
            for line in (
                _diff_line("Вопрос", old.get("question"), payload["question"], formatter=lambda v: _short_text(v, limit=120)),
                _diff_line("День", old.get("day_index"), day_index, formatter=_int_text),
                _diff_line("Баллы", old.get("points"), points, formatter=_int_text),
                _diff_line("В диаграммах", old.get("use_in_charts"), bool(payload["use_in_charts"]), formatter=_yes_no),
            ):
                if line:
                    details.append(line)
            if len(details) == 1:
                details.append("• Изменений в полях нет (повторное сохранение).")
            await _emit_admin_event(
                context,
                uid,
                "Обновление анкеты",
                "\n".join(details),
            )
            return

        if mode == "q_delete_id":
            if not re.match(r"^\d+$", text):
                await update.effective_message.reply_text("Нужен ID анкеты (число)."); raise ApplicationHandlerStop
            qid = int(text)
            old = qsvc.get(qid)
            ok = qsvc.delete(qid)
            state.clear_state(update.effective_user.id)
            await _show_q_menu(update)
            await update.effective_message.reply_text("✅ Удалено" if ok else "⚠️ Не найдено")
            if ok:
                details = [f"• ID: {qid}"]
                if old:
                    details.append(f"• День: {_int_text(old.get('day_index'))}")
                    details.append(f"• Баллы: {_int_text(old.get('points'))}")
                    details.append(f"• Вопрос: {_short_text(old.get('question'), limit=120)}")
                await _emit_admin_event(
                    context,
                    uid,
                    "Удалена анкета",
                    "\n".join(details),
                )
            return

        # --- Achievements wizard ---
        if mode == "a_create_code":
            # Backward compatibility for previously stored wizard states.
            payload = {"mode": "a_create_desc", "title": text.strip()}
            state.set_state(uid, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text("Шаг 2/7. Введи описание ачивки.")
            return

        if mode == "a_create_title":
            title = text.strip()
            if not title:
                await update.effective_message.reply_text("Название не должно быть пустым."); raise ApplicationHandlerStop
            payload["title"] = title
            payload["mode"] = "a_create_desc"
            state.set_state(uid, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text("Шаг 2/7. Введи описание ачивки.")
            return

        if mode == "a_create_desc":
            description = text.strip()
            if not description:
                await update.effective_message.reply_text("Описание не должно быть пустым."); raise ApplicationHandlerStop
            payload["description"] = description
            payload["mode"] = "a_create_icon"
            state.set_state(uid, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text(
                "Шаг 3/7. Введи иконку (emoji) или '-' для значения по умолчанию 🏅."
            )
            return

        if mode == "a_create_icon":
            payload["icon"] = "🏅" if text.strip() == "-" else (text.strip() or "🏅")
            payload["mode"] = "a_create_metric"
            state.set_state(uid, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text(
                f"Шаг 4/7. Выбери метрику.\n{_achievement_metrics_hint()}"
            )
            return

        if mode == "a_create_metric":
            metric_key = _parse_metric_key(text)
            if not metric_key:
                await update.effective_message.reply_text(
                    f"Не понял метрику.\n{_achievement_metrics_hint()}"
                )
                raise ApplicationHandlerStop
            payload["metric_key"] = metric_key
            payload["mode"] = "a_create_op"
            state.set_state(uid, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text(
                f"Шаг 5/7. Выбери оператор.\n{_achievement_operators_hint()}"
            )
            return

        if mode == "a_create_op":
            operator = _parse_operator_symbol(text)
            if not operator:
                await update.effective_message.reply_text(
                    f"Не понял оператор.\n{_achievement_operators_hint()}"
                )
                raise ApplicationHandlerStop
            payload["operator"] = operator
            payload["mode"] = "a_create_threshold"
            state.set_state(uid, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text("Шаг 6/7. Введи порог (целое число).")
            return

        if mode == "a_create_threshold":
            if not re.match(r"^-?\d+$", text.strip()):
                await update.effective_message.reply_text("Порог должен быть целым числом."); raise ApplicationHandlerStop
            payload["threshold"] = int(text.strip())
            payload["mode"] = "a_create_active"
            state.set_state(uid, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text("Шаг 7/7. Активна? (Да/Нет)")
            return

        if mode == "a_create_active":
            active = _parse_yes_no(text)
            if active is None:
                await update.effective_message.reply_text("Введи 'Да' или 'Нет'."); raise ApplicationHandlerStop
            payload["is_active"] = active
            if not achievement_svc:
                state.clear_state(uid)
                await _show_achievements_menu(update)
                await update.effective_message.reply_text("⚠️ Сервис ачивок не подключён.")
                return
            metric_key = str(payload.get("metric_key") or "").strip()
            operator = str(payload.get("operator") or "").strip()
            threshold = int(payload.get("threshold") or 0)
            code = _generate_achievement_code(
                title=str(payload.get("title") or ""),
                metric_key=metric_key,
                operator=operator,
                threshold=threshold,
            )
            sort_order = _next_achievement_sort_order()
            try:
                row = achievement_svc.create_rule(
                    code=code,
                    title=payload.get("title"),
                    description=payload.get("description"),
                    icon=payload.get("icon"),
                    metric_key=metric_key,
                    operator=operator,
                    threshold=threshold,
                    is_active=payload.get("is_active"),
                    sort_order=sort_order,
                )
            except Exception as e:
                await update.effective_message.reply_text(f"⚠️ Не удалось создать правило: {e}")
                raise ApplicationHandlerStop
            state.clear_state(uid)
            await _show_achievements_menu(update)
            if not row:
                await update.effective_message.reply_text("⚠️ Не удалось создать правило.")
                return
            await update.effective_message.reply_text(
                f"✅ Правило создано: code={str(row.get('code') or '').strip()}"
            )
            return

        if mode == "a_edit_id":
            if not achievement_svc:
                state.clear_state(uid)
                await _show_achievements_menu(update)
                await update.effective_message.reply_text("⚠️ Сервис ачивок не подключён.")
                return
            row = _find_achievement_rule(text)
            if not row:
                await update.effective_message.reply_text("⚠️ Правило не найдено. Введи code, ID или № из списка."); raise ApplicationHandlerStop
            rid = int(row.get("id") or 0)
            payload = {
                "mode": "a_edit_title",
                "id": rid,
                "code": row.get("code"),
                "title": row.get("title"),
                "description": row.get("description"),
                "icon": row.get("icon"),
                "metric_key": row.get("metric_key"),
                "operator": row.get("operator"),
                "threshold": int(row.get("threshold") or 0),
                "is_active": bool(row.get("is_active")),
                "sort_order": int(row.get("sort_order") or 100),
            }
            state.set_state(uid, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text(
                f"Редактируем правило code={payload['code']}.\n"
                f"Текущее название: {payload['title']}\n"
                "Введи новое название или '-' чтобы оставить."
            )
            return

        if mode == "a_edit_code":
            # Backward compatibility for previously stored wizard states.
            if text.strip() != "-":
                payload["code"] = text.strip().lower()
            payload["mode"] = "a_edit_title"
            state.set_state(uid, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text(
                f"Текущее название: {payload['title']}\nВведи новое название или '-' чтобы оставить."
            )
            return

        if mode == "a_edit_title":
            if text.strip() != "-":
                payload["title"] = text.strip()
            payload["mode"] = "a_edit_desc"
            state.set_state(uid, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text("Введи новое описание или '-' чтобы оставить текущее.")
            return

        if mode == "a_edit_desc":
            if text.strip() != "-":
                payload["description"] = text.strip()
            payload["mode"] = "a_edit_icon"
            state.set_state(uid, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text(
                f"Текущая иконка: {payload['icon']}\nВведи новую или '-' чтобы оставить."
            )
            return

        if mode == "a_edit_icon":
            if text.strip() != "-":
                payload["icon"] = text.strip() or payload.get("icon") or "🏅"
            payload["mode"] = "a_edit_metric"
            state.set_state(uid, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text(
                f"Текущая метрика: {_metric_label_by_key(str(payload.get('metric_key') or ''))} "
                f"({payload['metric_key']})\n"
                f"Введи новую или '-' чтобы оставить.\n{_achievement_metrics_hint()}"
            )
            return

        if mode == "a_edit_metric":
            if text.strip() != "-":
                metric_key = _parse_metric_key(text)
                if not metric_key:
                    await update.effective_message.reply_text(
                        f"Не понял метрику.\n{_achievement_metrics_hint()}"
                    )
                    raise ApplicationHandlerStop
                payload["metric_key"] = metric_key
            payload["mode"] = "a_edit_op"
            state.set_state(uid, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text(
                f"Текущий оператор: {_operator_label_by_symbol(payload['operator'])} ({payload['operator']})\n"
                f"Введи новый или '-' чтобы оставить.\n{_achievement_operators_hint()}"
            )
            return

        if mode == "a_edit_op":
            if text.strip() != "-":
                operator = _parse_operator_symbol(text)
                if not operator:
                    await update.effective_message.reply_text(
                        f"Не понял оператор.\n{_achievement_operators_hint()}"
                    )
                    raise ApplicationHandlerStop
                payload["operator"] = operator
            payload["mode"] = "a_edit_threshold"
            state.set_state(uid, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text(
                f"Текущий порог: {payload['threshold']}\nВведи новый или '-' чтобы оставить."
            )
            return

        if mode == "a_edit_threshold":
            if text.strip() != "-":
                if not re.match(r"^-?\d+$", text.strip()):
                    await update.effective_message.reply_text("Порог должен быть целым числом."); raise ApplicationHandlerStop
                payload["threshold"] = int(text.strip())
            payload["mode"] = "a_edit_active"
            state.set_state(uid, ADMIN_WIZARD_STEP, payload)
            active_label = "Да" if payload.get("is_active") else "Нет"
            await update.effective_message.reply_text(
                f"Сейчас активна: {active_label}\nВведи Да/Нет или '-' чтобы оставить."
            )
            return

        if mode == "a_edit_active":
            if text.strip() != "-":
                active = _parse_yes_no(text)
                if active is None:
                    await update.effective_message.reply_text("Введи 'Да', 'Нет' или '-'."); raise ApplicationHandlerStop
                payload["is_active"] = active
            payload["mode"] = "a_edit_sort"
            state.set_state(uid, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text(
                f"Текущий порядок показа: {payload['sort_order']}\n"
                "Введи новый порядок (например 10/20/30) или '-' чтобы оставить."
            )
            return

        if mode == "a_edit_sort":
            if text.strip() != "-":
                if not re.match(r"^-?\d+$", text.strip()):
                    await update.effective_message.reply_text("Порядок должен быть целым числом."); raise ApplicationHandlerStop
                payload["sort_order"] = int(text.strip())
            if not achievement_svc:
                state.clear_state(uid)
                await _show_achievements_menu(update)
                await update.effective_message.reply_text("⚠️ Сервис ачивок не подключён.")
                return
            try:
                row = achievement_svc.update_rule(
                    rule_id=int(payload.get("id")),
                    code=payload.get("code"),
                    title=payload.get("title"),
                    description=payload.get("description"),
                    icon=payload.get("icon"),
                    metric_key=payload.get("metric_key"),
                    operator=payload.get("operator"),
                    threshold=payload.get("threshold"),
                    is_active=payload.get("is_active"),
                    sort_order=payload.get("sort_order"),
                )
            except Exception as e:
                await update.effective_message.reply_text(f"⚠️ Не удалось обновить правило: {e}")
                raise ApplicationHandlerStop
            state.clear_state(uid)
            await _show_achievements_menu(update)
            if not row:
                await update.effective_message.reply_text("⚠️ Правило не найдено.")
                return
            await update.effective_message.reply_text(
                f"✅ Правило обновлено: code={str(row.get('code') or '').strip()}"
            )
            return

        if mode == "a_delete_id":
            if not achievement_svc:
                state.clear_state(uid)
                await _show_achievements_menu(update)
                await update.effective_message.reply_text("⚠️ Сервис ачивок не подключён.")
                return
            row = _find_achievement_rule(text)
            if not row:
                await update.effective_message.reply_text("⚠️ Правило не найдено. Введи code, ID или № из списка."); raise ApplicationHandlerStop
            rid = int(row.get("id") or 0)
            code = str(row.get("code") or "").strip()
            ok = achievement_svc.delete_rule(rid)
            state.clear_state(uid)
            await _show_achievements_menu(update)
            if ok:
                await update.effective_message.reply_text(f"✅ Правило удалено: code={code}")
            else:
                await update.effective_message.reply_text("⚠️ Не найдено")
            return

        # --- Tickets wizard ---
        if mode == "t_view_id":
            if not re.match(r"^\d+$", text):
                await update.effective_message.reply_text("ID должен быть числом."); raise ApplicationHandlerStop
            tid = int(text)
            return_mode = _safe_tickets_mode(payload.get("return_mode"))
            return_limit = _safe_tickets_limit(payload.get("return_limit"))
            _set_menu(uid, "tickets", {"mode": return_mode, "limit": return_limit})
            if not support_svc:
                await update.effective_message.reply_text("⚠️ Сервис поддержки не подключён.", reply_markup=kb_admin_tickets())
                return
            row = support_svc.get(tid)
            if not row:
                await update.effective_message.reply_text("Тикет не найден.", reply_markup=kb_admin_tickets())
                return
            await update.effective_message.reply_text(_ticket_details(row), reply_markup=kb_admin_tickets())
            return

        if mode == "t_reply_id":
            if not re.match(r"^\d+$", text):
                await update.effective_message.reply_text("ID должен быть числом."); raise ApplicationHandlerStop
            if not support_svc:
                _set_menu(uid, "tickets", {"mode": _safe_tickets_mode(payload.get("return_mode")), "limit": _safe_tickets_limit(payload.get("return_limit"))})
                await update.effective_message.reply_text("⚠️ Сервис поддержки не подключён.", reply_markup=kb_admin_tickets())
                return
            tid = int(text)
            row = support_svc.get(tid)
            if not row:
                await update.effective_message.reply_text("Тикет не найден."); raise ApplicationHandlerStop
            payload["ticket_id"] = tid
            payload["ticket_number"] = _ticket_number(row)
            payload["mode"] = "t_reply_text"
            state.set_state(uid, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text(
                f"Заявка №{payload['ticket_number']} (id={tid}).\nВведи текст ответа пользователю."
            )
            return

        if mode == "t_reply_text":
            tid = int(payload.get("ticket_id") or 0)
            reply_text = text.strip()
            if tid <= 0:
                _set_menu(uid, "tickets", {"mode": _safe_tickets_mode(payload.get("return_mode")), "limit": _safe_tickets_limit(payload.get("return_limit"))})
                await update.effective_message.reply_text("⚠️ Потерян ID тикета. Открой действие заново.", reply_markup=kb_admin_tickets())
                return
            if not reply_text:
                await update.effective_message.reply_text("Ответ не должен быть пустым."); raise ApplicationHandlerStop
            return_mode = _safe_tickets_mode(payload.get("return_mode"))
            return_limit = _safe_tickets_limit(payload.get("return_limit"))
            _set_menu(uid, "tickets", {"mode": return_mode, "limit": return_limit})
            await _reply_ticket_and_notify(
                update,
                context,
                tid,
                reply_text,
                reply_markup=kb_admin_tickets(),
            )
            return

        if mode == "t_close_id":
            if not re.match(r"^\d+$", text):
                await update.effective_message.reply_text("ID должен быть числом."); raise ApplicationHandlerStop
            tid = int(text)
            return_mode = _safe_tickets_mode(payload.get("return_mode"))
            return_limit = _safe_tickets_limit(payload.get("return_limit"))
            _set_menu(uid, "tickets", {"mode": return_mode, "limit": return_limit})
            if not support_svc:
                await update.effective_message.reply_text("⚠️ Сервис поддержки не подключён.", reply_markup=kb_admin_tickets())
                return
            row = support_svc.close(tid, update.effective_user.id)
            if not row:
                await update.effective_message.reply_text(
                    "Не смог закрыть тикет. Возможно, он уже закрыт или не существует.",
                    reply_markup=kb_admin_tickets(),
                )
                return
            await update.effective_message.reply_text(
                f"✅ Заявка №{_ticket_number(row)} (id={int(row.get('id') or tid)}) закрыта.",
                reply_markup=kb_admin_tickets(),
            )
            return

        # --- Broadcast random questionnaire to all ---
        if mode == "qcast_question":
            payload = {"mode": "qcast_charts", "question": text}
            state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text("Учитывать в диаграммах? (Да/Нет)", reply_markup=kb_yes_no())
            return

        if mode == "qcast_charts":
            t = text.lower()
            if t not in ("да", "нет"):
                await update.effective_message.reply_text("Нужно 'Да' или 'Нет'.", reply_markup=kb_yes_no()); raise ApplicationHandlerStop
            payload["use_in_charts"] = (t == "да")
            payload["mode"] = "qcast_points"
            state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text("Сколько баллов за прохождение? (целое число)")
            return

        if mode == "qcast_points":
            if not re.match(r"^\d+$", text):
                await update.effective_message.reply_text("Нужно целое число, например: 1"); raise ApplicationHandlerStop
            payload["points"] = int(text)
            payload["mode"] = "qcast_time"
            state.set_state(update.effective_user.id, ADMIN_WIZARD_STEP, payload)
            await update.effective_message.reply_text("Во сколько отправить всем? (ЧЧ:ММ)")
            return

        if mode == "qcast_time":
            if not re.match(r"^\d{1,2}:\d{2}$", text):
                await update.effective_message.reply_text("Формат времени: ЧЧ:ММ"); raise ApplicationHandlerStop
            hh_i, mm_i = [int(x) for x in text.split(":")]
            if not (0 <= hh_i <= 23 and 0 <= mm_i <= 59):
                await update.effective_message.reply_text("Некорректное время."); raise ApplicationHandlerStop
            hhmm = f"{hh_i:02d}:{mm_i:02d}"

            # Admin "random broadcast" questionnaire is optional for users.
            qid = qsvc.create(
                payload["question"],
                "broadcast_optional",
                bool(payload["use_in_charts"]),
                int(payload["points"]),
                update.effective_user.id,
            )
            created = schedule.schedule_questionnaire_broadcast(qid, hhmm, optional=True)
            state.clear_state(update.effective_user.id)
            await _show_q_menu(update)
            await update.effective_message.reply_text(f"✅ Запланировано. Анкета ID={qid}. Получателей: {created}")
            await _emit_admin_event(
                context,
                uid,
                "Запланирована рассылка анкеты",
                "\n".join(
                    [
                        f"• ID: {qid}",
                        f"• Время отправки: {hhmm}",
                        f"• Получателей: {created}",
                        "• Тип: broadcast_optional",
                    ]
                ),
            )
            return

        # --- Admins owner wizard ---
        if mode in ("adm_add_target", "adm_remove_target", "adm_promote_target", "adm_demote_target"):
            if not _is_owner(update):
                state.clear_state(uid)
                await _show_admin_home(update)
                await update.effective_message.reply_text("⛔️ Раздел доступен только owner.")
                return
            target_uid, err = _resolve_user_ref(text)
            if err:
                await update.effective_message.reply_text(err)
                raise ApplicationHandlerStop
            before_role = _admin_role_by_uid(target_uid)
            if mode == "adm_add_target":
                ok, msg = admin_svc.grant_admin(uid, int(target_uid))
                invite_status = ""
                if ok:
                    invite_status = await _send_events_chat_invite_to_admin(
                        context,
                        target_uid=int(target_uid),
                        granted_role="admin",
                    )
                    msg = f"{msg}\n{invite_status}"
                state.clear_state(uid)
                await _show_admins_menu(update)
                await update.effective_message.reply_text(("✅ " if ok else "⚠️ ") + msg, reply_markup=kb_admin_admins())
                if ok:
                    after_role = _admin_role_by_uid(target_uid)
                    await _emit_admin_event(
                        context,
                        uid,
                        "Выдана роль admin",
                        "\n".join(
                            [
                                f"• Пользователь: {_user_label(target_uid)}",
                                f"• Роль: {_admin_role_label(before_role)} → {_admin_role_label(after_role)}",
                                f"• Invite-статус: {invite_status or '—'}",
                            ]
                        ),
                    )
                return
            if mode == "adm_remove_target":
                ok, msg = admin_svc.remove_admin(uid, int(target_uid))
                state.clear_state(uid)
                await _show_admins_menu(update)
                await update.effective_message.reply_text(("✅ " if ok else "⚠️ ") + msg, reply_markup=kb_admin_admins())
                if ok:
                    after_role = _admin_role_by_uid(target_uid)
                    await _emit_admin_event(
                        context,
                        uid,
                        "Админ удалён",
                        "\n".join(
                            [
                                f"• Пользователь: {_user_label(target_uid)}",
                                f"• Роль: {_admin_role_label(before_role)} → {_admin_role_label(after_role)}",
                            ]
                        ),
                    )
                return
            if mode == "adm_promote_target":
                ok, msg = admin_svc.grant_owner(uid, int(target_uid))
                action = "Выдана роль owner"
                invite_status = ""
                if ok:
                    invite_status = await _send_events_chat_invite_to_admin(
                        context,
                        target_uid=int(target_uid),
                        granted_role="owner",
                    )
                    msg = f"{msg}\n{invite_status}"
            else:
                ok, msg = admin_svc.demote_owner_to_admin(uid, int(target_uid))
                action = "Понижение owner до admin"
                invite_status = ""
            state.clear_state(uid)
            await _show_admins_menu(update)
            await update.effective_message.reply_text(("✅ " if ok else "⚠️ ") + msg, reply_markup=kb_admin_admins())
            if ok:
                after_role = _admin_role_by_uid(target_uid)
                await _emit_admin_event(
                    context,
                    uid,
                    action,
                    "\n".join(
                        [
                            f"• Пользователь: {_user_label(target_uid)}",
                            f"• Роль: {_admin_role_label(before_role)} → {_admin_role_label(after_role)}",
                            f"• Invite-статус: {invite_status or '—'}",
                        ]
                    ),
                )
            return

        raise ApplicationHandlerStop

    async def wizard_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _is_admin(update):
            return
        uid = update.effective_user.id
        st = state.get_state(uid)
        if not st or st.get("step") != ADMIN_WIZARD_STEP:
            return

        payload = st.get("payload_json") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        mode = payload.get("mode")
        if mode not in ("qst_photo", "ext_photo"):
            return

        photos = update.effective_message.photo or []
        if not photos:
            await update.effective_message.reply_text("Не удалось прочитать фото. Попробуй отправить ещё раз.")
            raise ApplicationHandlerStop

        payload["photo_file_id"] = photos[-1].file_id
        payload["mode"] = "qst_points" if mode == "qst_photo" else "ext_points"
        state.set_state(uid, ADMIN_WIZARD_STEP, payload)
        if mode == "qst_photo":
            await update.effective_message.reply_text("Фото принято. Сколько баллов за ответ на задание? (целое число)")
        else:
            await update.effective_message.reply_text("Фото принято. Сколько баллов за просмотр? (целое число, можно 0)")
        raise ApplicationHandlerStop

    # ----------------------------
    # Register handlers
    # ----------------------------
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("admins", cmd_admins))
    app.add_handler(CommandHandler("admin_add", cmd_admin_add))
    app.add_handler(CommandHandler("admin_remove", cmd_admin_remove))
    app.add_handler(MessageHandler(filters.Regex(rf"^{re.escape(texts.MENU_ADMIN)}$"), open_admin_from_menu))
    # Admin menu navigation (reply buttons)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_menu_pick), group=-11)
    # Wizard input
    app.add_handler(MessageHandler(filters.PHOTO, wizard_photo), group=-10)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_text), group=-10)
