import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from entity.settings import Settings
from event_bus import callbacks as cb
from questionnaires.questionnaire_handlers import q_buttons
from static.faq import FAQ
from ui import texts
from ui.keyboards import menus


log = logging.getLogger("happines_course")


# ===== User steps stored in user_state (via UserService) =====
STEP_WAIT_NAME = "wait_name"
STEP_WAIT_TIME = "wait_time"
STEP_ENROLL_TIME = "enroll_time"
STEP_PD_CONSENT = "pd_consent"
STEP_WAIT_TZ = "wait_timezone"

# Habits wizard
STEP_HABIT_WAIT_TITLE = "habit_wait_title"
STEP_HABIT_WAIT_TIME = "habit_wait_time"
STEP_HABIT_WAIT_FREQ = "habit_wait_freq"

# Habits management (reply-menu driven)
STEP_HABIT_PICK_FOR_EDIT = "habit_pick_for_edit"
STEP_HABIT_EDIT_MENU = "habit_edit_menu"
STEP_HABIT_EDIT_TITLE = "habit_edit_title"
STEP_HABIT_EDIT_TIME = "habit_edit_time"
STEP_HABIT_EDIT_FREQ = "habit_edit_freq"
STEP_HABIT_PICK_FOR_DELETE = "habit_pick_for_delete"
STEP_HABIT_DELETE_CONFIRM = "habit_delete_confirm"

# Personal reminders wizard/management
STEP_PR_WAIT_TEXT = "pr_wait_text"
STEP_PR_WAIT_DATETIME = "pr_wait_datetime"
STEP_PR_PICK_FOR_EDIT = "pr_pick_for_edit"
STEP_PR_EDIT_MENU = "pr_edit_menu"
STEP_PR_EDIT_TEXT = "pr_edit_text"
STEP_PR_EDIT_DATETIME = "pr_edit_datetime"
STEP_PR_PICK_FOR_DELETE = "pr_pick_for_delete"
STEP_PR_DELETE_CONFIRM = "pr_delete_confirm"

# Support
STEP_SUPPORT_WAIT_TEXT = "support_wait_text"

HELP_FAQ_PREFIX = "help:faq:"
HELP_FAQ_LIST = "help:faq:list"
HELP_ESCALATE = "help:escalate"
HELP_ESCALATE_TEXT = "📨 Направить вопрос администрации"
ADMIN_TICKET_OPEN_PREFIX = "admin_ticket:open:"
ADMIN_TICKET_REPLY_PREFIX = "admin_ticket:reply:"
MOOD_MENU_CB = "mood:menu"
MOOD_RATE_CB = "mood:rate"
MOOD_SET_PREFIX = "mood:set:"
MOOD_CHART_PREFIX = "mood:chart:"


def _faq_items() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in FAQ or []:
        try:
            q, a = item
            q_s = str(q).strip()
            a_s = str(a).strip()
            if q_s and a_s:
                out.append((q_s, a_s))
        except Exception:
            continue
    return out


def register_user_handlers(app, settings: Settings, services: dict):
    user_svc = services["user"]
    analytics = services["analytics"]
    schedule = services["schedule"]
    learning = services.get("learning")
    daily = services.get("daily_pack")
    qsvc = services.get("questionnaire")
    admin_svc = services.get("admin")
    achievement_svc = services.get("achievement")
    habit_svc = services.get("habit")
    habit_schedule = services.get("habit_schedule")
    pr_svc = services.get("personal_reminder")
    pr_schedule = services.get("personal_reminder_schedule")
    support_svc = services.get("support")
    mood_svc = services.get("mood")

    def _is_admin(uid: int) -> bool:
        try:
            return bool(admin_svc and admin_svc.is_admin(uid))
        except Exception:
            return False

    def _parse_hhmm(raw: str) -> str | None:
        """Strict HH:MM validation (00-23 / 00-59). Returns normalized string or None."""

        s = (raw or "").strip()
        if not re.fullmatch(r"\d{2}:\d{2}", s):
            return None
        try:
            hh_s, mm_s = s.split(":", 1)
            hh = int(hh_s)
            mm = int(mm_s)
        except Exception:
            return None
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        return f"{hh:02d}:{mm:02d}"

    def _extract_numeric_id(raw: str) -> int | None:
        s = (raw or "").strip()
        if s.startswith("#"):
            s = s[1:]
        if not s.isdigit():
            return None
        try:
            hid = int(s)
            return hid if hid > 0 else None
        except Exception:
            return None

    def _parse_user_datetime(raw: str) -> str | None:
        s = (raw or "").strip()
        try:
            dt = datetime.strptime(s, "%d.%m.%Y %H:%M")
        except Exception:
            return None
        return dt.strftime("%d.%m.%Y %H:%M")

    def _format_start_local(uid: int, start_at_val) -> str:
        tz_name = user_svc.get_timezone(uid) or settings.default_timezone
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo(settings.default_timezone)
        dt = start_at_val
        if isinstance(dt, str):
            try:
                dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            except Exception:
                return "-"
        try:
            return dt.astimezone(tz).strftime("%d.%m.%Y %H:%M")
        except Exception:
            return "-"

    def _progress_text(uid: int) -> str:
        if analytics and hasattr(analytics, "progress_report"):
            try:
                return analytics.progress_report(uid)
            except Exception:
                pass
        prof = analytics.profile(uid)
        return f"📊 Мой прогресс\nБаллы: {prof['points']}\nДней завершено: {prof['done_days']}"

    def _evaluate_achievements(uid: int) -> list[dict]:
        if not achievement_svc:
            return []
        try:
            return achievement_svc.evaluate(uid, user_svc.get_timezone(uid))
        except Exception:
            return []

    def _achievement_lines(rows: list[dict]) -> str | None:
        if not rows:
            return None
        header = "🏆 Новая ачивка!" if len(rows) == 1 else "🏆 Новые ачивки!"
        lines = [header]
        for row in rows:
            icon = (row.get("icon") or "🏅").strip() or "🏅"
            title = (row.get("title") or "Ачивка").strip()
            description = (row.get("description") or "").strip()
            line = f"• {icon} {title}"
            if description:
                line += f" — {description}"
            lines.append(line)
        return "\n".join(lines)

    def _admin_ids() -> list[int]:
        try:
            if not admin_svc or not getattr(admin_svc, "admins", None):
                return []
            ids = admin_svc.admins.list_user_ids() or []
            return [int(x) for x in ids]
        except Exception:
            return []

    def _ticket_for_admin(ticket: dict, u) -> str:
        username = f"@{u.username}" if getattr(u, "username", None) else "-"
        name = (u.first_name or u.full_name or "").strip() or "Без имени"
        text = (ticket.get("question_text") or "").strip()
        tid = int(ticket.get("id") or 0)
        tnum = int(ticket.get("number") or tid or 0)
        uid = int(ticket.get("user_id") or 0)
        return (
            f"🆘 Новый тикет №{tnum} (id={tid})\n"
            f"Пользователь: {name} ({username})\n"
            f"user_id: {uid}\n\n"
            f"Сообщение:\n{text}\n\n"
            "Взаимодействуйте через админ-панель:\n"
            "🛠 Админка -> 🆘 Тикеты"
        )

    def _ticket_admin_markup(ticket: dict) -> InlineKeyboardMarkup:
        tid = int(ticket.get("id") or 0)
        tnum = int(ticket.get("number") or tid or 0)
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🛠 Открыть тикеты", callback_data=f"{ADMIN_TICKET_OPEN_PREFIX}{tid}")],
                [InlineKeyboardButton(f"💬 Ответить по №{tnum}", callback_data=f"{ADMIN_TICKET_REPLY_PREFIX}{tid}")],
            ]
        )

    def _faq_list_markup() -> InlineKeyboardMarkup:
        rows = []
        for idx, (q_text, _ans) in enumerate(_faq_items()):
            rows.append([InlineKeyboardButton(q_text, callback_data=f"{HELP_FAQ_PREFIX}{idx}")])
        rows.append([InlineKeyboardButton(HELP_ESCALATE_TEXT, callback_data=HELP_ESCALATE)])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _faq_answer_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(HELP_ESCALATE_TEXT, callback_data=HELP_ESCALATE)],
                [InlineKeyboardButton("⬅️ К вопросам", callback_data=HELP_FAQ_LIST)],
            ]
        )

    @staticmethod
    def _mood_menu_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✍️ Отметить настроение", callback_data=MOOD_RATE_CB)],
                [
                    InlineKeyboardButton("📈 График 7 дн.", callback_data=f"{MOOD_CHART_PREFIX}7"),
                    InlineKeyboardButton("📈 График 30 дн.", callback_data=f"{MOOD_CHART_PREFIX}30"),
                ],
            ]
        )

    @staticmethod
    def _mood_rate_markup() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("1", callback_data=f"{MOOD_SET_PREFIX}1"),
                    InlineKeyboardButton("2", callback_data=f"{MOOD_SET_PREFIX}2"),
                    InlineKeyboardButton("3", callback_data=f"{MOOD_SET_PREFIX}3"),
                    InlineKeyboardButton("4", callback_data=f"{MOOD_SET_PREFIX}4"),
                    InlineKeyboardButton("5", callback_data=f"{MOOD_SET_PREFIX}5"),
                ],
                [InlineKeyboardButton("⬅️ Назад", callback_data=MOOD_MENU_CB)],
            ]
        )

    def _parse_reminder_nav_data(data: str) -> dict | None:
        d = (data or "").strip()
        if not d:
            return None
        if d == cb.REMINDER_NAV_NEXT:
            return {"mode": "next"}
        if not d.startswith(cb.REMINDER_NAV_PREFIX):
            return None

        raw = d[len(cb.REMINDER_NAV_PREFIX):]
        parts = raw.split(":")
        if len(parts) < 2:
            return None

        kind = parts[0].strip()
        if kind not in ("lesson", "quest", "questionnaire"):
            return None
        if not parts[1].isdigit():
            return None

        item = {"mode": "item", "kind": kind, "day_index": int(parts[1])}
        if kind == "questionnaire":
            if len(parts) < 3 or (not parts[2].isdigit()):
                return None
            item["questionnaire_id"] = int(parts[2])
        return item

    def _first_pending_material(uid: int) -> dict | None:
        if not learning or not qsvc:
            return None

        day_now = max(1, int(schedule.current_day_index(uid)))
        for d in range(1, day_now + 1):
            lesson = schedule.lesson.get_by_day(d)
            if lesson and (not learning.has_viewed_lesson(uid, d)):
                return {"kind": "lesson", "day_index": d}

            quest = schedule.quest.get_by_day(d)
            if quest and (not learning.has_quest_answer(uid, d)):
                return {"kind": "quest", "day_index": d}

            for row in qsvc.list_for_day(d, qtypes=("manual", "daily")):
                qid = int(row["id"])
                if not qsvc.has_response(uid, qid):
                    return {"kind": "questionnaire", "day_index": d, "questionnaire_id": qid}
        return None

    def _collect_pending_materials(uid: int) -> tuple[list[str], int | None, int | None, tuple[int, int] | None]:
        if not learning or not qsvc:
            return [], None, None, None

        pending: list[str] = []
        first_lesson_day: int | None = None
        first_quest_day: int | None = None
        first_questionnaire: tuple[int, int] | None = None

        day_now = max(1, int(schedule.current_day_index(uid)))
        for d in range(1, day_now + 1):
            lesson = schedule.lesson.get_by_day(d)
            if lesson and (not learning.has_viewed_lesson(uid, d)):
                pending.append(f"• 📚 День {d}: лекция — не отмечена «Просмотрено»")
                if first_lesson_day is None:
                    first_lesson_day = d

            quest = schedule.quest.get_by_day(d)
            if quest and (not learning.has_quest_answer(uid, d)):
                pending.append(f"• 📝 День {d}: задание — нет ответа")
                if first_quest_day is None:
                    first_quest_day = d

            for row in qsvc.list_for_day(d, qtypes=("manual", "daily")):
                qid = int(row["id"])
                if not qsvc.has_response(uid, qid):
                    pending.append(f"• 📋 День {d}: анкета — нет ответа")
                    if first_questionnaire is None:
                        first_questionnaire = (d, qid)
                    break

        return pending, first_lesson_day, first_quest_day, first_questionnaire

    def _remember_material_message(uid: int, item: dict, message_id: int):
        try:
            repo = getattr(schedule, "material_messages", None)
            if not repo:
                return
            kind = str(item.get("kind") or "")
            day_index = int(item.get("day_index") or 0)
            content_id = int(item.get("questionnaire_id") or 0) if kind == "questionnaire" else 0
            if day_index <= 0 or message_id <= 0:
                return
            repo.upsert(
                user_id=uid,
                day_index=day_index,
                kind=kind,
                content_id=content_id,
                message_id=message_id,
            )
        except Exception:
            pass

    def _stored_material_message_id(uid: int, item: dict) -> int | None:
        try:
            repo = getattr(schedule, "material_messages", None)
            if not repo:
                return None
            kind = str(item.get("kind") or "")
            day_index = int(item.get("day_index") or 0)
            if day_index <= 0 or kind not in ("lesson", "quest", "questionnaire"):
                return None

            row = None
            if kind == "questionnaire":
                qid = int(item.get("questionnaire_id") or 0)
                if qid > 0:
                    row = repo.get_message(uid, day_index, kind, content_id=qid)
                if not row:
                    row = repo.get_latest_message(uid, day_index, kind)
            else:
                row = repo.get_latest_message(uid, day_index, kind)

            if not row:
                return None
            mid = int(row.get("message_id") or 0)
            return mid if mid > 0 else None
        except Exception:
            return None

    async def _resend_material(uid: int, item: dict, context: ContextTypes.DEFAULT_TYPE) -> bool:
        kind = str(item.get("kind") or "")
        day_index = int(item.get("day_index") or 0)
        if day_index <= 0:
            return False

        if kind == "lesson":
            lesson = schedule.lesson.get_by_day(day_index)
            if not lesson:
                return False
            pts = int(lesson.get("points_viewed") or 0)
            viewed_cb = schedule.make_viewed_cb(day_index, pts)
            kb_i = InlineKeyboardMarkup(
                [[InlineKeyboardButton("Просмотрено", callback_data=viewed_cb)]]
            )
            title = lesson.get("title") or f"День {day_index}"
            desc = lesson.get("description") or ""
            video = lesson.get("video_url") or ""
            msg_text = f"📚 Лекция дня {day_index}\n{title}\n\n{desc}"
            if video:
                msg_text += f"\n\n🎥 {video}"
            msg = await context.bot.send_message(
                chat_id=uid,
                text=msg_text,
                reply_markup=kb_i,
            )
            _remember_material_message(uid, item, int(msg.message_id))
            return True

        if kind == "quest":
            quest = schedule.quest.get_by_day(day_index)
            if not quest:
                return False
            reply_cb = f"{cb.QUEST_REPLY_PREFIX}{day_index}"
            kb_i = InlineKeyboardMarkup(
                [[InlineKeyboardButton("✍️ Ответить на задание", callback_data=reply_cb)]]
            )
            qtext = (
                f"📝 Задание дня {day_index}:\n{quest['prompt']}\n\n"
                "Нажми кнопку ниже, чтобы продолжить, или просто ответь сообщением в чат."
            )
            photo_file_id = quest.get("photo_file_id")
            if photo_file_id:
                try:
                    msg = await context.bot.send_photo(
                        chat_id=uid,
                        photo=photo_file_id,
                        caption=qtext,
                        reply_markup=kb_i,
                    )
                except Exception:
                    msg = await context.bot.send_message(chat_id=uid, text=qtext, reply_markup=kb_i)
            else:
                msg = await context.bot.send_message(chat_id=uid, text=qtext, reply_markup=kb_i)
            _remember_material_message(uid, item, int(msg.message_id))
            learning.state.set_state(
                uid,
                "last_quest",
                {
                    "day_index": day_index,
                    "points": int(quest.get("points") or 1),
                    "prompt": quest.get("prompt"),
                },
            )
            return True

        if kind == "questionnaire" and qsvc:
            qid = int(item.get("questionnaire_id") or 0)
            if qid <= 0:
                for row in qsvc.list_for_day(day_index, qtypes=("manual", "daily")):
                    candidate = int(row["id"])
                    if not qsvc.has_response(uid, candidate):
                        qid = candidate
                        break
            if qid <= 0:
                return False
            qrow = qsvc.get(qid)
            if not qrow:
                return False
            msg = await context.bot.send_message(
                chat_id=uid,
                text=f"📋 Анкета\n\n{qrow['question']}",
                reply_markup=q_buttons(qid),
            )
            item_with_qid = {"kind": "questionnaire", "day_index": day_index, "questionnaire_id": qid}
            _remember_material_message(uid, item_with_qid, int(msg.message_id))
            return True

        return False

    async def reminder_nav_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        data = (q.data or "").strip()
        parsed = _parse_reminder_nav_data(data)
        if not parsed:
            return

        await q.answer()
        uid = q.from_user.id

        if parsed.get("mode") == "next":
            item = _first_pending_material(uid)
            if not item:
                await context.bot.send_message(chat_id=uid, text="✅ Пропущенных материалов нет.")
                return
        else:
            item = parsed

        message_id = _stored_material_message_id(uid, item)
        if message_id:
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text="↩️ Возвращаю к нужному сообщению выше.",
                    reply_to_message_id=message_id,
                )
                return
            except Exception:
                pass

        resent = await _resend_material(uid, item, context)
        if resent:
            await context.bot.send_message(
                chat_id=uid,
                text="Старая точка в чате не найдена — отправил материал заново.",
            )
        else:
            await context.bot.send_message(
                chat_id=uid,
                text="⚠️ Не смог открыть материал. Попробуй через «🗓 Мой день».",
            )

    async def _start_support_ticket_flow(update: Update):
        uid = update.effective_user.id
        user_svc.set_step(uid, STEP_SUPPORT_WAIT_TEXT, {})
        await update.effective_message.reply_text(
            "Опиши проблему одним сообщением.\n"
            "Я создам тикет и передам его администратору в раздел «🆘 Тикеты».",
            reply_markup=menus.kb_back_only(),
        )

    # ----------------------------
    # /start
    # ----------------------------
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        display_name = u.first_name or u.full_name or (u.username or "")
        user_svc.ensure_user(u.id, u.username, display_name)

        if not user_svc.has_pd_consent(u.id):
            user_svc.set_step(u.id, STEP_PD_CONSENT, {})
            text = (
                "👋 Привет! Перед началом мне нужно твоё согласие на обработку персональных данных.\n\n"
                "Я храню минимальные данные (TG id, имя/ник, настройки расписания) только для работы курса.\n"
                "Если не согласен — я не смогу продолжить."
            )
            await update.effective_message.reply_text(text, reply_markup=menus.kb_consent())
            return

        prof = analytics.profile(u.id)

        # Deep-link support: /start gol_<day> or /start goq_<day>
        if prof.get("enrolled") and context.args:
            payload = (context.args[0] or "").strip()
            m = re.match(r"^(go[ql])_(\d+)$", payload)
            if m:
                kind = m.group(1)  # goq / gol
                day_index = int(m.group(2))
                if kind == "gol":
                    lesson = schedule.lesson.get_by_day(day_index)
                    if lesson:
                        pts = int(lesson.get("points_viewed") or 0)
                        viewed_cb = schedule.make_viewed_cb(day_index, pts)
                        kb_i = InlineKeyboardMarkup(
                            [[InlineKeyboardButton("Просмотрено", callback_data=viewed_cb)]]
                        )
                        title = lesson.get("title") or f"День {day_index}"
                        desc = lesson.get("description") or ""
                        video = lesson.get("video_url") or ""
                        msg = f"📚 Лекция дня {day_index}\n{title}\n\n{desc}"
                        if video:
                            msg += f"\n\n🎥 {video}"
                        await update.effective_message.reply_text(
                            msg, reply_markup=kb_i
                        )
                else:
                    quest = schedule.quest.get_by_day(day_index)
                    if quest:
                        reply_cb = f"{cb.QUEST_REPLY_PREFIX}{day_index}"
                        kb_i = InlineKeyboardMarkup(
                            [[InlineKeyboardButton("✍️ Ответить на задание", callback_data=reply_cb)]]
                        )
                        qtext = (
                            f"📝 Задание дня {day_index}:\n{quest['prompt']}\n\n"
                            "Нажми кнопку ниже, чтобы продолжить, или просто ответь сообщением в чат."
                        )
                        photo_file_id = quest.get("photo_file_id")
                        if photo_file_id:
                            try:
                                await update.effective_message.reply_photo(
                                    photo=photo_file_id,
                                    caption=qtext,
                                    reply_markup=kb_i,
                                )
                            except Exception:
                                await update.effective_message.reply_text(qtext, reply_markup=kb_i)
                        else:
                            await update.effective_message.reply_text(qtext, reply_markup=kb_i)
                await update.effective_message.reply_text(
                    "Главное меню 👇", reply_markup=menus.kb_main(_is_admin(u.id))
                )
                return

        if not prof.get("enrolled"):
            if not user_svc.get_timezone(u.id):
                user_svc.set_step(u.id, STEP_WAIT_TZ, {})
                await update.effective_message.reply_text(
                    "🕒 Сначала выбери свой часовой пояс:", reply_markup=menus.kb_timezone()
                )
                return

            user_svc.set_step(u.id, STEP_ENROLL_TIME, {})
            await update.effective_message.reply_text(
                "⏰ Выбери удобное время получения материалов (ЧЧ:ММ):",
                reply_markup=menus.kb_enroll_time(),
            )
            return

        await update.effective_message.reply_text(
            f"Привет, {prof['display_name']}! 👋\n"
            f"Баллы: {prof['points']}\n"
            "\nВыбери раздел в меню ниже 👇",
            reply_markup=menus.kb_main(_is_admin(u.id)),
        )

    # ----------------------------
    # /enroll
    # ----------------------------
    async def enroll_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not user_svc.has_pd_consent(uid):
            user_svc.set_step(uid, STEP_PD_CONSENT, {})
            await update.effective_message.reply_text(
                "👋 Перед записью нужно согласие на обработку персональных данных.",
                reply_markup=menus.kb_consent(),
            )
            return

        if not user_svc.get_timezone(uid):
            user_svc.set_step(uid, STEP_WAIT_TZ, {})
            await update.effective_message.reply_text(
                "🕒 Сначала выбери часовой пояс:", reply_markup=menus.kb_timezone()
            )
            return

        user_svc.set_step(uid, STEP_ENROLL_TIME, {})
        await update.effective_message.reply_text(
            "📝 Запись на «Курс на счастье»\n\nВо сколько удобно получать ежедневные материалы?",
            reply_markup=menus.kb_enroll_time(),
        )

    # ----------------------------
    # Callback handlers (consent / timezone / enroll time)
    # ----------------------------
    async def consent_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        choice = q.data.split(":", 1)[-1]
        if choice != "yes":
            user_svc.set_pd_consent(q.from_user.id, False)
            user_svc.set_step(q.from_user.id, None)
            await q.edit_message_text(
                "Понял. Без согласия я не могу продолжить работу. Если передумаешь — нажми /start"
            )
            try:
                await q.message.reply_text("", reply_markup=ReplyKeyboardRemove())
            except Exception:
                pass
            return

        user_svc.set_pd_consent(q.from_user.id, True)
        if not user_svc.get_timezone(q.from_user.id):
            user_svc.set_step(q.from_user.id, STEP_WAIT_TZ, {})
            await q.edit_message_text(
                "✅ Спасибо! Теперь выбери часовой пояс — это нужно, чтобы материалы приходили вовремя."
            )
            await q.message.reply_text("Выбери часовой пояс:", reply_markup=menus.kb_timezone())
            return

        user_svc.set_step(q.from_user.id, STEP_ENROLL_TIME, {})
        await q.edit_message_text(
            "✅ Спасибо! Остался один шаг — выбери удобное время получения материалов (ЧЧ:ММ)."
        )
        await q.message.reply_text("Выбери время:", reply_markup=menus.kb_enroll_time())

    async def tz_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        val = q.data.split(":", 1)[-1]
        if val == "custom":
            user_svc.set_step(q.from_user.id, STEP_WAIT_TZ, {})
            await q.edit_message_text(
                "Ок. Введи часовой пояс в формате IANA, например Europe/Moscow, Asia/Yekaterinburg."
            )
            return

        try:
            ZoneInfo(val)
        except Exception:
            await q.edit_message_text("Не смог распознать часовой пояс. Выбери из списка или нажми «Другое».")
            return

        user_svc.set_timezone(q.from_user.id, val)
        st = user_svc.get_step(q.from_user.id) or {}
        payload = st.get("payload_json") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        after_tz = payload.get("after_tz")

        # If user initiated "change_time" but had no tz yet, continue to time input
        if after_tz == "change_time":
            user_svc.set_step(q.from_user.id, STEP_WAIT_TIME, {})
            await q.edit_message_text("✅ Часовой пояс сохранён. Теперь введи новое время (ЧЧ:ММ).")
            await q.message.reply_text("Введи время:", reply_markup=menus.kb_back_only())
            return

        prof = analytics.profile(q.from_user.id)
        if prof.get("enrolled"):
            user_svc.set_step(q.from_user.id, None)
            await q.edit_message_text("✅ Часовой пояс сохранён.")
            await q.message.reply_text(
                "Главное меню 👇", reply_markup=menus.kb_main(_is_admin(q.from_user.id))
            )
            return

        user_svc.set_step(q.from_user.id, STEP_ENROLL_TIME, {})
        await q.edit_message_text("✅ Часовой пояс сохранён. Теперь выбери время доставки материалов.")
        await q.message.reply_text("Выбери время:", reply_markup=menus.kb_enroll_time())

    async def enroll_time_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        value = q.data.replace(cb.ENROLL_TIME_PREFIX, "", 1)

        if value == "custom":
            user_svc.set_step(q.from_user.id, STEP_ENROLL_TIME, {"custom": True})
            await q.edit_message_text("Ок. Введи время в формате ЧЧ:ММ (например 09:30).")
            return

        user_svc.enroll_user(q.from_user.id, value)
        user_svc.set_step(q.from_user.id, None)
        await q.edit_message_text(f"✅ Записал! Время доставки: {value}")
        await q.message.reply_text(
            "Главное меню 👇", reply_markup=menus.kb_main(_is_admin(q.from_user.id))
        )

    # ----------------------------
    # Habits callbacks (frequency selection, done/skip, manage)
    # ----------------------------
    async def habit_freq_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        if not habit_svc:
            await q.edit_message_text("❌ HabitService не подключён.")
            return

        freq = q.data.split(":", 2)[-1]
        st = user_svc.get_step(q.from_user.id) or {}
        payload = st.get("payload_json") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}

        title = (payload.get("title") or "").strip()
        remind_time = (payload.get("remind_time") or "").strip()
        if not title or not remind_time:
            user_svc.set_step(q.from_user.id, None)
            await q.edit_message_text("⚠️ Не смог завершить создание (нет данных). Попробуй ещё раз.")
            return

        habit_id = habit_svc.create(q.from_user.id, title, remind_time, freq)
        user_svc.set_step(q.from_user.id, None)

        # Plan occurrences/outbox for the next days right away.
        try:
            if habit_schedule:
                habit_schedule.schedule_due_jobs()
        except Exception:
            pass

        await q.edit_message_text(f"✅ Привычка создана!\n\n#{habit_id} — {title}\n⏰ {remind_time} · 📅 {freq}")
        await q.message.reply_text("Меню привычек 👇", reply_markup=menus.kb_habits())

    async def habit_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        if not habit_svc:
            await q.edit_message_text("❌ HabitService не подключён.")
            return
        try:
            occ_id = int(q.data.split(":")[-1])
        except Exception:
            return

        ok = habit_svc.mark_done(q.from_user.id, occ_id)
        if ok:
            pts = habit_svc.bonus_points()
            await q.edit_message_text(f"✅ Отлично! Засчитано. +{pts} балл(ов) 🎉")
            ach_text = _achievement_lines(_evaluate_achievements(q.from_user.id))
            if ach_text:
                await q.message.reply_text(ach_text)
        else:
            await q.edit_message_text("⚠️ Уже было отмечено или недоступно.")

    async def habit_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        if not habit_svc:
            await q.edit_message_text("❌ HabitService не подключён.")
            return
        try:
            occ_id = int(q.data.split(":")[-1])
        except Exception:
            return
        ok = habit_svc.mark_skipped(q.from_user.id, occ_id)
        if ok:
            await q.edit_message_text("Ок, пропуск записал.")
        else:
            await q.edit_message_text("⚠️ Уже было отмечено или недоступно.")

    async def habit_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        if not habit_svc:
            await q.edit_message_text("❌ HabitService не подключён.")
            return
        try:
            hid = int(q.data.split(":")[-1])
        except Exception:
            return
        ok = habit_svc.toggle(q.from_user.id, hid)
        if ok:
            await q.edit_message_text("✅ Обновил. Открой «Мои привычки» ещё раз, чтобы увидеть актуальный статус.")
            try:
                if habit_schedule:
                    habit_schedule.schedule_due_jobs()
            except Exception:
                pass
        else:
            await q.edit_message_text("⚠️ Не нашёл привычку.")

    async def habit_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        if not habit_svc:
            await q.edit_message_text("❌ HabitService не подключён.")
            return
        try:
            hid = int(q.data.split(":")[-1])
        except Exception:
            return
        ok = habit_svc.delete(q.from_user.id, hid)
        if ok:
            await q.edit_message_text("🗑 Удалил. Открой «Мои привычки» ещё раз.")
        else:
            await q.edit_message_text("⚠️ Не нашёл привычку.")

    async def help_faq_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        data = (q.data or "").strip()

        if data == HELP_FAQ_LIST:
            await q.edit_message_text("❓ Помощь\n\nВыбери вопрос:", reply_markup=_faq_list_markup())
            return

        if data == HELP_ESCALATE:
            user_svc.set_step(q.from_user.id, STEP_SUPPORT_WAIT_TEXT, {})
            await q.edit_message_text(
                "📨 Направить вопрос администрации\n\n"
                "Опиши проблему одним сообщением в этот чат.\n"
                "Я создам тикет и передам администратору.",
            )
            try:
                await q.message.reply_text("Жду твоё описание 👇", reply_markup=menus.kb_back_only())
            except Exception:
                pass
            return

        if not data.startswith(HELP_FAQ_PREFIX):
            return

        raw_idx = data.replace(HELP_FAQ_PREFIX, "", 1)
        if not raw_idx.isdigit():
            await q.answer("Не смог распознать вопрос.", show_alert=False)
            return

        idx = int(raw_idx)
        items = _faq_items()
        if idx < 0 or idx >= len(items):
            await q.answer("Этот пункт устарел. Открой помощь заново.", show_alert=False)
            return

        q_text, a_text = items[idx]
        msg = (
            f"❓ {q_text}\n\n"
            f"{a_text}\n\n"
            f"Если это не решило вопрос, нажми «{HELP_ESCALATE_TEXT}»."
        )
        await q.edit_message_text(msg, reply_markup=_faq_answer_markup())

    async def admin_ticket_quick_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        data = (q.data or "").strip()
        uid = q.from_user.id
        if not _is_admin(uid):
            await q.answer("Только для админов.", show_alert=True)
            return
        await q.answer()

        if data.startswith(ADMIN_TICKET_OPEN_PREFIX):
            raw = data.replace(ADMIN_TICKET_OPEN_PREFIX, "", 1)
            tid = int(raw) if raw.isdigit() else 0
            user_svc.set_step(uid, "admin_menu", {"screen": "tickets", "mode": "open", "limit": 20})
            await q.message.reply_text(
                "🆘 Быстрый переход к тикетам.\n"
                "Открой: 🛠 Админка -> 🆘 Тикеты.\n"
                f"ID заявки: {tid if tid > 0 else '-'}",
                reply_markup=menus.kb_main(True),
            )
            return

        if data.startswith(ADMIN_TICKET_REPLY_PREFIX):
            raw = data.replace(ADMIN_TICKET_REPLY_PREFIX, "", 1)
            if not raw.isdigit():
                await q.message.reply_text("Не смог распознать ID тикета.")
                return
            tid = int(raw)
            if not support_svc:
                await q.message.reply_text("⚠️ Сервис поддержки не подключён.")
                return
            row = support_svc.get(tid)
            if not row:
                await q.message.reply_text("Тикет не найден.")
                return
            status = str(row.get("status") or "").strip().lower()
            if status != "open":
                await q.message.reply_text("Тикет уже закрыт.")
                return
            tnum = int(row.get("number") or tid or 0)
            user_svc.set_step(
                uid,
                "admin_wizard",
                {
                    "mode": "t_reply_text",
                    "ticket_id": tid,
                    "ticket_number": tnum,
                    "return_mode": "open",
                    "return_limit": 20,
                },
            )
            await q.message.reply_text(
                f"💬 Быстрый ответ по заявке №{tnum} (id={tid}).\n"
                "Введи текст ответа одним сообщением."
            )
            return

    async def mood_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        data = (q.data or "").strip()
        if not data.startswith("mood:"):
            return
        await q.answer()
        uid = q.from_user.id

        if not mood_svc:
            await context.bot.send_message(chat_id=uid, text="❌ Трекер настроения временно недоступен.")
            return

        if data == MOOD_MENU_CB:
            await q.edit_message_text(
                "😊 Настроение\n\nВыбери действие:",
                reply_markup=_mood_menu_markup(),
            )
            return

        if data == MOOD_RATE_CB:
            await q.edit_message_text(
                "Оцени настроение за сегодня (1-5):",
                reply_markup=_mood_rate_markup(),
            )
            return

        if data.startswith(MOOD_SET_PREFIX):
            raw = data.replace(MOOD_SET_PREFIX, "", 1)
            if raw not in ("1", "2", "3", "4", "5"):
                await q.answer("Некорректная оценка", show_alert=False)
                return
            score = int(raw)
            row = mood_svc.set_today(uid, score)
            if not row:
                await context.bot.send_message(chat_id=uid, text="⚠️ Не смог сохранить настроение.")
                return
            await q.edit_message_text(
                f"✅ Сохранил настроение за сегодня: {score}/5",
                reply_markup=_mood_menu_markup(),
            )
            return

        if data.startswith(MOOD_CHART_PREFIX):
            raw = data.replace(MOOD_CHART_PREFIX, "", 1)
            days = 7
            try:
                days = int(raw)
            except Exception:
                days = 7
            if days not in (7, 30):
                days = 7
            await context.bot.send_message(chat_id=uid, text=mood_svc.chart_text(uid, days))
            return

    # ----------------------------
    # Text input steps (name / time / tz / custom enroll time)
    # ----------------------------
    async def on_step_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        uid = u.id
        text = (update.effective_message.text or "").strip()

        # If another feature is waiting for free-form text (quest answer, AI chat,
        # questionnaire comment, admin wizard, etc.), don't intercept it here.
        # We only handle explicit menu navigation buttons.
        st_any = user_svc.get_step(uid)
        user_steps = {
            STEP_WAIT_NAME,
            STEP_WAIT_TIME,
            STEP_ENROLL_TIME,
            STEP_PD_CONSENT,
            STEP_WAIT_TZ,
            STEP_HABIT_WAIT_TITLE,
            STEP_HABIT_WAIT_TIME,
            STEP_HABIT_WAIT_FREQ,
            STEP_HABIT_PICK_FOR_EDIT,
            STEP_HABIT_EDIT_MENU,
            STEP_HABIT_EDIT_TITLE,
            STEP_HABIT_EDIT_TIME,
            STEP_HABIT_EDIT_FREQ,
            STEP_HABIT_PICK_FOR_DELETE,
            STEP_HABIT_DELETE_CONFIRM,
            STEP_PR_WAIT_TEXT,
            STEP_PR_WAIT_DATETIME,
            STEP_PR_PICK_FOR_EDIT,
            STEP_PR_EDIT_MENU,
            STEP_PR_EDIT_TEXT,
            STEP_PR_EDIT_DATETIME,
            STEP_PR_PICK_FOR_DELETE,
            STEP_PR_DELETE_CONFIRM,
            STEP_SUPPORT_WAIT_TEXT,
        }
        nav_texts = {
            texts.MENU_DAY,
            texts.MENU_PROGRESS,
            texts.MENU_SETTINGS,
            texts.MENU_HELP,
            texts.HELP_NOT_HELPED,
            texts.HELP_CONTACT_ADMIN,
            texts.MENU_ADMIN,
            texts.BTN_BACK,
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
            texts.SETTINGS_HABITS,
            texts.SETTINGS_PERSONAL_REMINDERS,
            texts.HABITS_CREATE,
            texts.HABITS_LIST,
            texts.HABITS_EDIT,
            texts.HABITS_DELETE,
            texts.REMINDERS_CREATE,
            texts.REMINDERS_LIST,
            texts.REMINDERS_EDIT,
            texts.REMINDERS_DELETE,
        }
        if st_any and st_any.get("step") == "wait_q_comment":
            if text == texts.BTN_BACK:
                user_svc.set_step(uid, None)
                await update.effective_message.reply_text(
                    "Ок, вернул в главное меню.",
                    reply_markup=menus.kb_main(_is_admin(uid)),
                )
                raise ApplicationHandlerStop
            if text in nav_texts:
                await update.effective_message.reply_text(
                    "Сначала напиши короткий комментарий к анкете одним сообщением.",
                )
                raise ApplicationHandlerStop
            return
        if st_any and st_any.get("step") and st_any.get("step") not in user_steps:
            if text in nav_texts:
                # User explicitly navigates away — cancel the pending flow.
                try:
                    user_svc.set_step(uid, None)
                except Exception:
                    pass
            else:
                return

        step = user_svc.get_step(uid) or {}
        cur = step.get("step")
        if not cur:
            return

        if text == texts.BTN_BACK:
            user_svc.set_step(uid, None)
            # If user is inside habits flow, return to habits menu; otherwise go to main.
            if cur and str(cur).startswith("habit_"):
                await update.effective_message.reply_text(
                    "Меню привычек 👇",
                    reply_markup=menus.kb_habits(),
                )
            elif cur and str(cur).startswith("pr_"):
                await update.effective_message.reply_text(
                    "Меню напоминаний 👇",
                    reply_markup=menus.kb_personal_reminders(),
                )
            else:
                await update.effective_message.reply_text(
                    "Главное меню 👇", reply_markup=menus.kb_main(_is_admin(uid))
                )
            raise ApplicationHandlerStop

        if cur == STEP_WAIT_NAME:
            name = text[:64]
            user_svc.update_display_name(uid, name)
            user_svc.set_step(uid, None)
            await update.effective_message.reply_text(
                f"✅ Ок, буду звать тебя: {name}",
                reply_markup=menus.kb_main(_is_admin(uid)),
            )
            raise ApplicationHandlerStop

        if cur in (STEP_WAIT_TIME, STEP_ENROLL_TIME):
            hhmm = _parse_hhmm(text)
            if not hhmm:
                await update.effective_message.reply_text(
                    "Формат времени: ЧЧ:ММ (например 09:30). Часы 00–23, минуты 00–59."
                )
                raise ApplicationHandlerStop

            if cur == STEP_WAIT_TIME:
                user_svc.update_delivery_time(uid, hhmm)
                user_svc.set_step(uid, None)
                await update.effective_message.reply_text(
                    f"✅ Время обновлено: {hhmm}",
                    reply_markup=menus.kb_main(_is_admin(uid)),
                )
                raise ApplicationHandlerStop

            # enroll time
            user_svc.enroll_user(uid, hhmm)
            user_svc.set_step(uid, None)
            await update.effective_message.reply_text(
                f"✅ Записал! Время доставки: {hhmm}",
                reply_markup=menus.kb_main(_is_admin(uid)),
            )
            raise ApplicationHandlerStop

        if cur == STEP_WAIT_TZ:
            try:
                ZoneInfo(text)
            except Exception:
                await update.effective_message.reply_text(
                    "Не похоже на IANA timezone. Пример: Europe/Moscow, Asia/Yekaterinburg."
                )
                raise ApplicationHandlerStop
            user_svc.set_timezone(uid, text)
            user_svc.set_step(uid, None)
            await update.effective_message.reply_text(
                f"✅ Часовой пояс сохранён: {text}",
                reply_markup=menus.kb_main(_is_admin(uid)),
            )
            raise ApplicationHandlerStop

        # ----------------------------
        # Habits wizard (title -> time -> frequency)
        # ----------------------------
        if cur == STEP_HABIT_WAIT_TITLE:
            title = (text or "").strip()
            if not title:
                await update.effective_message.reply_text("Название не должно быть пустым. Напиши ещё раз.")
                raise ApplicationHandlerStop
            user_svc.set_step(uid, STEP_HABIT_WAIT_TIME, {"title": title})
            await update.effective_message.reply_text(
                "⏰ Во сколько напоминать? Введи время в формате ЧЧ:ММ (например 09:30).",
                reply_markup=menus.kb_back_only(),
            )
            raise ApplicationHandlerStop

        if cur == STEP_HABIT_WAIT_TIME:
            hhmm = _parse_hhmm(text)
            if not hhmm:
                await update.effective_message.reply_text(
                    "Неверное время. Формат: ЧЧ:ММ (например 09:30). Часы 00–23, минуты 00–59. Попробуй ещё раз."
                )
                raise ApplicationHandlerStop
            payload = step.get("payload_json") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            payload["remind_time"] = hhmm
            user_svc.set_step(uid, STEP_HABIT_WAIT_FREQ, payload)
            await update.effective_message.reply_text(
                "📅 Выбери периодичность:",
                reply_markup=menus.kb_habit_frequency(),
            )
            raise ApplicationHandlerStop

        if cur == STEP_HABIT_WAIT_FREQ:
            # Frequency is chosen via inline buttons; text input isn't expected.
            await update.effective_message.reply_text(
                "Нажми кнопку с периодичностью 👇",
                reply_markup=menus.kb_habit_frequency(),
            )
            raise ApplicationHandlerStop

        # ----------------------------
        # Habits management (reply-menu)
        # ----------------------------
        if cur == STEP_HABIT_PICK_FOR_EDIT:
            if not habit_svc:
                user_svc.set_step(uid, None)
                await update.effective_message.reply_text("❌ HabitService не подключён.", reply_markup=menus.kb_habits())
                raise ApplicationHandlerStop
            hid = _extract_numeric_id(text)
            if not hid:
                await update.effective_message.reply_text("Введи номер привычки (например 1 или #1).")
                raise ApplicationHandlerStop
            h = habit_svc.habits.get(hid)
            if not h or int(h.get("user_id")) != int(uid):
                await update.effective_message.reply_text("Не нашёл такую привычку у тебя. Введи номер из списка.")
                raise ApplicationHandlerStop
            user_svc.set_step(uid, STEP_HABIT_EDIT_MENU, {"habit_id": int(hid)})
            await update.effective_message.reply_text(
                f"✏️ Изменяем привычку #{hid}: {h.get('title')}",
                reply_markup=menus.kb_habit_edit_menu(),
            )
            raise ApplicationHandlerStop

        if cur == STEP_HABIT_EDIT_MENU:
            payload = step.get("payload_json") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            hid = int(payload.get("habit_id") or 0)
            if text == texts.HABIT_EDIT_NAME:
                user_svc.set_step(uid, STEP_HABIT_EDIT_TITLE, {"habit_id": hid})
                await update.effective_message.reply_text(
                    "Введи новое название привычки:",
                    reply_markup=menus.kb_back_only(),
                )
                raise ApplicationHandlerStop
            if text == texts.HABIT_EDIT_TIME:
                user_svc.set_step(uid, STEP_HABIT_EDIT_TIME, {"habit_id": hid})
                await update.effective_message.reply_text(
                    "Введи новое время (ЧЧ:ММ):",
                    reply_markup=menus.kb_back_only(),
                )
                raise ApplicationHandlerStop
            if text == texts.HABIT_EDIT_FREQ:
                user_svc.set_step(uid, STEP_HABIT_EDIT_FREQ, {"habit_id": hid})
                await update.effective_message.reply_text(
                    "Выбери новую периодичность:",
                    reply_markup=menus.kb_habit_frequency_reply(),
                )
                raise ApplicationHandlerStop

            await update.effective_message.reply_text(
                "Выбери, что изменить: название / время / периодичность.",
                reply_markup=menus.kb_habit_edit_menu(),
            )
            raise ApplicationHandlerStop

        if cur == STEP_HABIT_EDIT_TITLE:
            if not habit_svc:
                user_svc.set_step(uid, None)
                await update.effective_message.reply_text("❌ HabitService не подключён.", reply_markup=menus.kb_habits())
                raise ApplicationHandlerStop
            new_title = (text or "").strip()
            if not new_title:
                await update.effective_message.reply_text("Название не должно быть пустым. Напиши ещё раз.")
                raise ApplicationHandlerStop
            payload = step.get("payload_json") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            hid = int(payload.get("habit_id") or 0)
            ok = habit_svc.update_title(uid, hid, new_title)
            user_svc.set_step(uid, None)
            await update.effective_message.reply_text(
                "✅ Название обновлено." if ok else "❌ Не смог обновить (проверь номер привычки).",
                reply_markup=menus.kb_habits(),
            )
            raise ApplicationHandlerStop

        if cur == STEP_HABIT_EDIT_TIME:
            if not habit_svc:
                user_svc.set_step(uid, None)
                await update.effective_message.reply_text("❌ HabitService не подключён.", reply_markup=menus.kb_habits())
                raise ApplicationHandlerStop
            hhmm = _parse_hhmm(text)
            if not hhmm:
                await update.effective_message.reply_text(
                    "Неверное время. Формат: ЧЧ:ММ (например 09:30). Часы 00–23, минуты 00–59. Попробуй ещё раз.",
                    reply_markup=menus.kb_back_only(),
                )
                raise ApplicationHandlerStop
            payload = step.get("payload_json") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            hid = int(payload.get("habit_id") or 0)
            ok = habit_svc.update_time(uid, hid, hhmm)
            if ok and habit_schedule:
                # Cancelled jobs/occurrences are handled inside HabitService; now re-plan.
                habit_schedule.schedule_due_jobs()
            user_svc.set_step(uid, None)
            await update.effective_message.reply_text(
                f"✅ Время обновлено: {hhmm}" if ok else "❌ Не смог обновить (проверь номер привычки).",
                reply_markup=menus.kb_habits(),
            )
            raise ApplicationHandlerStop

        if cur == STEP_HABIT_EDIT_FREQ:
            if not habit_svc:
                user_svc.set_step(uid, None)
                await update.effective_message.reply_text("❌ HabitService не подключён.", reply_markup=menus.kb_habits())
                raise ApplicationHandlerStop
            m = {
                "Ежедневно": "daily",
                "По будням": "weekdays",
                "По выходным": "weekends",
            }
            freq = m.get((text or "").strip())
            if not freq:
                await update.effective_message.reply_text(
                    "Выбери периодичность кнопкой ниже 👇",
                    reply_markup=menus.kb_habit_frequency_reply(),
                )
                raise ApplicationHandlerStop
            payload = step.get("payload_json") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            hid = int(payload.get("habit_id") or 0)
            ok = habit_svc.update_frequency(uid, hid, freq)
            if ok and habit_schedule:
                habit_schedule.schedule_due_jobs()
            user_svc.set_step(uid, None)
            await update.effective_message.reply_text(
                "✅ Периодичность обновлена." if ok else "❌ Не смог обновить (проверь номер привычки).",
                reply_markup=menus.kb_habits(),
            )
            raise ApplicationHandlerStop

        if cur == STEP_HABIT_PICK_FOR_DELETE:
            if not habit_svc:
                user_svc.set_step(uid, None)
                await update.effective_message.reply_text("❌ HabitService не подключён.", reply_markup=menus.kb_habits())
                raise ApplicationHandlerStop
            hid = _extract_numeric_id(text)
            if not hid:
                await update.effective_message.reply_text("Введи номер привычки (например 1 или #1).")
                raise ApplicationHandlerStop
            h = habit_svc.habits.get(hid)
            if not h or int(h.get("user_id")) != int(uid):
                await update.effective_message.reply_text("Не нашёл такую привычку у тебя. Введи номер из списка.")
                raise ApplicationHandlerStop
            user_svc.set_step(uid, STEP_HABIT_DELETE_CONFIRM, {"habit_id": int(hid)})
            await update.effective_message.reply_text(
                f"🗑 Удалить привычку #{hid}: {h.get('title')}?",
                reply_markup=menus.kb_yes_no(),
            )
            raise ApplicationHandlerStop

        if cur == STEP_HABIT_DELETE_CONFIRM:
            payload = step.get("payload_json") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            hid = int(payload.get("habit_id") or 0)
            if text == texts.NO:
                user_svc.set_step(uid, None)
                await update.effective_message.reply_text("Ок, не удаляю.", reply_markup=menus.kb_habits())
                raise ApplicationHandlerStop
            if text != texts.YES:
                await update.effective_message.reply_text("Нажми «Да» или «Нет».", reply_markup=menus.kb_yes_no())
                raise ApplicationHandlerStop
            ok = False
            try:
                ok = bool(habit_svc and habit_svc.delete(uid, hid))
            except Exception:
                ok = False
            if ok and habit_schedule:
                habit_schedule.schedule_due_jobs()
            user_svc.set_step(uid, None)
            await update.effective_message.reply_text(
                "✅ Привычка удалена." if ok else "❌ Не смог удалить (проверь номер привычки).",
                reply_markup=menus.kb_habits(),
            )
            raise ApplicationHandlerStop

        # ----------------------------
        # Personal reminders wizard/management
        # ----------------------------
        if cur == STEP_PR_WAIT_TEXT:
            if not pr_svc:
                user_svc.set_step(uid, None)
                await update.effective_message.reply_text(
                    "❌ Сервис напоминаний не подключён.",
                    reply_markup=menus.kb_settings(),
                )
                raise ApplicationHandlerStop
            val = (text or "").strip()
            if not val:
                await update.effective_message.reply_text("Текст напоминания не должен быть пустым. Напиши ещё раз.")
                raise ApplicationHandlerStop
            user_svc.set_step(uid, STEP_PR_WAIT_DATETIME, {"text": val})
            await update.effective_message.reply_text(
                "📅 Введи дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ\nНапример: 21.02.2026 09:30",
                reply_markup=menus.kb_back_only(),
            )
            raise ApplicationHandlerStop

        if cur == STEP_PR_WAIT_DATETIME:
            dt = _parse_user_datetime(text)
            if not dt:
                await update.effective_message.reply_text(
                    "Неверный формат. Введи дату и время как ДД.ММ.ГГГГ ЧЧ:ММ.",
                    reply_markup=menus.kb_back_only(),
                )
                raise ApplicationHandlerStop
            payload = step.get("payload_json") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            reminder_id = pr_svc.create(
                user_id=uid,
                text=payload.get("text") or "",
                start_local=dt,
            )
            if reminder_id and pr_schedule:
                pr_schedule.schedule_due_jobs()
            user_svc.set_step(uid, None)
            await update.effective_message.reply_text(
                f"✅ Напоминание создано: #{reminder_id}" if reminder_id else "❌ Не смог создать напоминание.",
                reply_markup=menus.kb_personal_reminders(),
            )
            raise ApplicationHandlerStop

        if cur == STEP_PR_PICK_FOR_EDIT:
            if not pr_svc:
                user_svc.set_step(uid, None)
                await update.effective_message.reply_text(
                    "❌ Сервис напоминаний не подключён.",
                    reply_markup=menus.kb_personal_reminders(),
                )
                raise ApplicationHandlerStop
            rid = _extract_numeric_id(text)
            if not rid:
                await update.effective_message.reply_text("Введи номер напоминания (например 1 или #1).")
                raise ApplicationHandlerStop
            r = pr_svc.get_owned(uid, rid)
            if not r:
                await update.effective_message.reply_text("Не нашёл такое напоминание у тебя. Введи номер из списка.")
                raise ApplicationHandlerStop
            user_svc.set_step(uid, STEP_PR_EDIT_MENU, {"reminder_id": rid})
            await update.effective_message.reply_text(
                f"✏️ Изменяем напоминание #{rid}: {r.get('text')}",
                reply_markup=menus.kb_personal_reminder_edit_menu(),
            )
            raise ApplicationHandlerStop

        if cur == STEP_PR_EDIT_MENU:
            payload = step.get("payload_json") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            rid = int(payload.get("reminder_id") or 0)
            if text == texts.REMINDER_EDIT_TEXT:
                user_svc.set_step(uid, STEP_PR_EDIT_TEXT, {"reminder_id": rid})
                await update.effective_message.reply_text(
                    "Введи новый текст напоминания:",
                    reply_markup=menus.kb_back_only(),
                )
                raise ApplicationHandlerStop
            if text == texts.REMINDER_EDIT_DATETIME:
                user_svc.set_step(uid, STEP_PR_EDIT_DATETIME, {"reminder_id": rid})
                await update.effective_message.reply_text(
                    "Введи новую дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ:",
                    reply_markup=menus.kb_back_only(),
                )
                raise ApplicationHandlerStop
            await update.effective_message.reply_text(
                "Выбери, что изменить: текст или дата и время.",
                reply_markup=menus.kb_personal_reminder_edit_menu(),
            )
            raise ApplicationHandlerStop

        if cur == STEP_PR_EDIT_TEXT:
            payload = step.get("payload_json") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            rid = int(payload.get("reminder_id") or 0)
            val = (text or "").strip()
            if not val:
                await update.effective_message.reply_text("Текст не должен быть пустым. Напиши ещё раз.")
                raise ApplicationHandlerStop
            ok = bool(pr_svc and pr_svc.update_text(uid, rid, val))
            user_svc.set_step(uid, None)
            await update.effective_message.reply_text(
                "✅ Текст обновлён." if ok else "❌ Не смог обновить напоминание.",
                reply_markup=menus.kb_personal_reminders(),
            )
            raise ApplicationHandlerStop

        if cur == STEP_PR_EDIT_DATETIME:
            payload = step.get("payload_json") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            rid = int(payload.get("reminder_id") or 0)
            dt = _parse_user_datetime(text)
            if not dt:
                await update.effective_message.reply_text(
                    "Неверный формат. Введи как ДД.ММ.ГГГГ ЧЧ:ММ.",
                    reply_markup=menus.kb_back_only(),
                )
                raise ApplicationHandlerStop
            ok = bool(pr_svc and pr_svc.update_datetime(uid, rid, dt))
            if ok and pr_schedule:
                pr_schedule.schedule_due_jobs()
            user_svc.set_step(uid, None)
            await update.effective_message.reply_text(
                "✅ Дата и время обновлены." if ok else "❌ Не смог обновить напоминание.",
                reply_markup=menus.kb_personal_reminders(),
            )
            raise ApplicationHandlerStop

        if cur == STEP_PR_PICK_FOR_DELETE:
            if not pr_svc:
                user_svc.set_step(uid, None)
                await update.effective_message.reply_text(
                    "❌ Сервис напоминаний не подключён.",
                    reply_markup=menus.kb_personal_reminders(),
                )
                raise ApplicationHandlerStop
            rid = _extract_numeric_id(text)
            if not rid:
                await update.effective_message.reply_text("Введи номер напоминания (например 1 или #1).")
                raise ApplicationHandlerStop
            r = pr_svc.get_owned(uid, rid)
            if not r:
                await update.effective_message.reply_text("Не нашёл такое напоминание у тебя. Введи номер из списка.")
                raise ApplicationHandlerStop
            user_svc.set_step(uid, STEP_PR_DELETE_CONFIRM, {"reminder_id": rid})
            await update.effective_message.reply_text(
                f"🗑 Удалить напоминание #{rid}: {r.get('text')}?",
                reply_markup=menus.kb_yes_no(),
            )
            raise ApplicationHandlerStop

        if cur == STEP_PR_DELETE_CONFIRM:
            payload = step.get("payload_json") or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            rid = int(payload.get("reminder_id") or 0)
            if text == texts.NO:
                user_svc.set_step(uid, None)
                await update.effective_message.reply_text(
                    "Ок, не удаляю.",
                    reply_markup=menus.kb_personal_reminders(),
                )
                raise ApplicationHandlerStop
            if text != texts.YES:
                await update.effective_message.reply_text(
                    "Нажми «Да» или «Нет».",
                    reply_markup=menus.kb_yes_no(),
                )
                raise ApplicationHandlerStop
            ok = bool(pr_svc and pr_svc.delete(uid, rid))
            if ok and pr_schedule:
                pr_schedule.schedule_due_jobs()
            user_svc.set_step(uid, None)
            await update.effective_message.reply_text(
                "✅ Напоминание удалено." if ok else "❌ Не смог удалить напоминание.",
                reply_markup=menus.kb_personal_reminders(),
            )
            raise ApplicationHandlerStop

        if cur == STEP_SUPPORT_WAIT_TEXT:
            issue = (text or "").strip()
            if len(issue) < 3:
                await update.effective_message.reply_text(
                    "Опиши проблему чуть подробнее (минимум 3 символа).",
                    reply_markup=menus.kb_back_only(),
                )
                raise ApplicationHandlerStop

            if not support_svc:
                user_svc.set_step(uid, None)
                await update.effective_message.reply_text(
                    "⚠️ Сервис поддержки временно недоступен.",
                    reply_markup=menus.kb_main(_is_admin(uid)),
                )
                raise ApplicationHandlerStop

            ticket = support_svc.create_ticket(uid, issue)
            user_svc.set_step(uid, None)
            if not ticket:
                await update.effective_message.reply_text(
                    "⚠️ Не удалось создать тикет. Попробуй ещё раз чуть позже.",
                    reply_markup=menus.kb_main(_is_admin(uid)),
                )
                raise ApplicationHandlerStop

            tid = int(ticket.get("id") or 0)
            tnum = int(ticket.get("number") or tid or 0)
            is_author_admin = _is_admin(uid)
            recipient_ids = [int(aid) for aid in _admin_ids() if int(aid) != int(uid)]

            if recipient_ids:
                await update.effective_message.reply_text(
                    f"✅ Принято. Заявка №{tnum} передана администратору.\n"
                    "Когда будет ответ, я пришлю его сюда.",
                    reply_markup=menus.kb_main(is_author_admin),
                )
            elif is_author_admin:
                await update.effective_message.reply_text(
                    f"✅ Принято. Заявка №{tnum} сохранена.\n"
                    "ℹ️ Других администраторов в системе нет, поэтому уведомление не отправлено.\n"
                    "Тикет доступен в разделе «🛠 Админка -> 🆘 Тикеты».",
                    reply_markup=menus.kb_main(is_author_admin),
                )
            else:
                await update.effective_message.reply_text(
                    f"✅ Принято. Заявка №{tnum} сохранена.\n"
                    "ℹ️ Сейчас нет доступных администраторов для мгновенного уведомления.",
                    reply_markup=menus.kb_main(is_author_admin),
                )

            admin_text = _ticket_for_admin(ticket, u)
            for admin_id in recipient_ids:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=admin_text,
                        reply_markup=_ticket_admin_markup(ticket),
                    )
                except Exception:
                    log.exception("Failed to send support ticket notification to admin_id=%s", admin_id)
            raise ApplicationHandlerStop

    # ----------------------------
    # Main navigation (ReplyKeyboard)
    # ----------------------------
    async def on_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        u = update.effective_user
        uid = u.id
        text = (update.effective_message.text or "").strip()

        # Exit AI chat mode on navigation
        if learning and text in (
            texts.MENU_DAY,
            texts.MENU_PROGRESS,
            texts.MENU_SETTINGS,
            texts.MENU_HELP,
            texts.HELP_NOT_HELPED,
            texts.MENU_ADMIN,
            texts.BTN_BACK,
            texts.SETTINGS_HABITS,
            texts.SETTINGS_PERSONAL_REMINDERS,
            texts.HABITS_CREATE,
            texts.HABITS_LIST,
            texts.HABITS_EDIT,
            texts.HABITS_DELETE,
            texts.REMINDERS_CREATE,
            texts.REMINDERS_LIST,
            texts.REMINDERS_EDIT,
            texts.REMINDERS_DELETE,
        ):
            try:
                learning.state.clear_state(uid)
            except Exception:
                pass

        # Ensure user exists
        display_name = u.first_name or u.full_name or (u.username or "")
        user_svc.ensure_user(uid, u.username, display_name)

        # Onboarding gate
        if not user_svc.has_pd_consent(uid):
            user_svc.set_step(uid, STEP_PD_CONSENT, {})
            await update.effective_message.reply_text(
                "👋 Перед началом нужно согласие на обработку персональных данных.",
                reply_markup=menus.kb_consent(),
            )
            raise ApplicationHandlerStop

        if not user_svc.get_timezone(uid):
            # If user clicked "change time" before setting tz, remember it
            after_tz = "change_time" if text == texts.SETTINGS_TIME else None
            user_svc.set_step(uid, STEP_WAIT_TZ, {"after_tz": after_tz} if after_tz else {})
            await update.effective_message.reply_text(
                "🕒 Выбери часовой пояс (это обязательно, чтобы материалы приходили вовремя):",
                reply_markup=menus.kb_timezone(),
            )
            raise ApplicationHandlerStop

        prof = analytics.profile(uid)

        # If another feature is waiting for free-form text (quest answer / AI chat,
        # questionnaire comment, admin wizard, etc.), don't intercept it here.
        # Only explicit menu navigation buttons are handled by this router.
        st_any = user_svc.get_step(uid)
        user_steps = {
            STEP_WAIT_NAME,
            STEP_WAIT_TIME,
            STEP_ENROLL_TIME,
            STEP_PD_CONSENT,
            STEP_WAIT_TZ,
            STEP_HABIT_WAIT_TITLE,
            STEP_HABIT_WAIT_TIME,
            STEP_HABIT_WAIT_FREQ,
            STEP_HABIT_PICK_FOR_EDIT,
            STEP_HABIT_EDIT_MENU,
            STEP_HABIT_EDIT_TITLE,
            STEP_HABIT_EDIT_TIME,
            STEP_HABIT_EDIT_FREQ,
            STEP_HABIT_PICK_FOR_DELETE,
            STEP_HABIT_DELETE_CONFIRM,
            STEP_PR_WAIT_TEXT,
            STEP_PR_WAIT_DATETIME,
            STEP_PR_PICK_FOR_EDIT,
            STEP_PR_EDIT_MENU,
            STEP_PR_EDIT_TEXT,
            STEP_PR_EDIT_DATETIME,
            STEP_PR_PICK_FOR_DELETE,
            STEP_PR_DELETE_CONFIRM,
            STEP_SUPPORT_WAIT_TEXT,
        }
        nav_texts = {
            texts.MENU_DAY,
            texts.MENU_PROGRESS,
            texts.MENU_SETTINGS,
            texts.MENU_HELP,
            texts.HELP_NOT_HELPED,
            texts.HELP_CONTACT_ADMIN,
            texts.MENU_ADMIN,
            texts.BTN_BACK,
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
            texts.SETTINGS_HABITS,
            texts.SETTINGS_PERSONAL_REMINDERS,
            texts.HABITS_CREATE,
            texts.HABITS_LIST,
            texts.HABITS_EDIT,
            texts.HABITS_DELETE,
            texts.REMINDERS_CREATE,
            texts.REMINDERS_LIST,
            texts.REMINDERS_EDIT,
            texts.REMINDERS_DELETE,
        }
        if st_any and st_any.get("step") == "wait_q_comment":
            if text == texts.BTN_BACK:
                user_svc.set_step(uid, None)
                await update.effective_message.reply_text(
                    "Ок, вернул в главное меню.",
                    reply_markup=menus.kb_main(_is_admin(uid)),
                )
                raise ApplicationHandlerStop
            if text in nav_texts:
                await update.effective_message.reply_text(
                    "Сначала напиши короткий комментарий к анкете одним сообщением.",
                )
                raise ApplicationHandlerStop
            return
        if st_any and st_any.get("step") and st_any.get("step") not in user_steps and text not in nav_texts:
            return
        if st_any and st_any.get("step") and st_any.get("step") not in user_steps and text in nav_texts:
            # User explicitly navigates away — cancel the pending flow.
            try:
                user_svc.set_step(uid, None)
            except Exception:
                pass

        # Global back
        if text == texts.BTN_BACK:
            await update.effective_message.reply_text(
                "Главное меню 👇", reply_markup=menus.kb_main(_is_admin(uid))
            )
            raise ApplicationHandlerStop

        if text == texts.MENU_DAY:
            if not prof.get("enrolled"):
                user_svc.set_step(uid, STEP_ENROLL_TIME, {})
                await update.effective_message.reply_text(
                    "⏰ Сначала выбери время доставки материалов:",
                    reply_markup=menus.kb_enroll_time(),
                )
                raise ApplicationHandlerStop
            day_index = schedule.current_day_index(uid)
            await update.effective_message.reply_text(
                f"🗓 Мой день\nКурс: Курс на счастье\nДень: {day_index}\nВремя: {prof['delivery_time']}\n\nВыбери материал:",
                reply_markup=menus.kb_day(),
            )
            raise ApplicationHandlerStop

        if text == texts.MENU_PROGRESS:
            _evaluate_achievements(uid)
            await update.effective_message.reply_text(
                _progress_text(uid),
                reply_markup=menus.kb_progress(),
            )
            raise ApplicationHandlerStop

        if text == texts.MENU_SETTINGS:
            time_text = prof["delivery_time"] if prof.get("enrolled") else "не указано (нужна запись /enroll)"
            await update.effective_message.reply_text(
                f"⚙️ Настройки\nИмя: {prof['display_name']}\nВами указанное время: {time_text}\n\nВыбери действие:",
                reply_markup=menus.kb_settings(),
            )
            raise ApplicationHandlerStop

        if text == texts.MENU_HELP:
            await update.effective_message.reply_text("❓ Помощь\n\nВыбери вопрос:", reply_markup=_faq_list_markup())
            raise ApplicationHandlerStop

        if text in (texts.HELP_NOT_HELPED, texts.HELP_CONTACT_ADMIN):
            await _start_support_ticket_flow(update)
            raise ApplicationHandlerStop

        # Day submenu
        if text == texts.DAY_MOOD:
            if not mood_svc:
                await update.effective_message.reply_text("❌ Трекер настроения временно недоступен.")
                raise ApplicationHandlerStop
            await update.effective_message.reply_text(
                "😊 Настроение\n\nВыбери действие:",
                reply_markup=_mood_menu_markup(),
            )
            raise ApplicationHandlerStop

        if text == texts.DAY_MATERIALS_NOW:
            pending, first_lesson_day, first_quest_day, first_questionnaire = _collect_pending_materials(uid)
            if not pending:
                await update.effective_message.reply_text(
                    "✅ Пропущенных материалов нет.",
                    reply_markup=menus.kb_day(),
                )
                raise ApplicationHandlerStop

            text_msg = (
                "🔎 Пропущенные материалы\n\n"
                "Нашёл незавершённые пункты:\n"
                + "\n".join(pending)
                + "\n\nНажми кнопку ниже, чтобы быстро перейти к нужному материалу."
            )
            buttons = []
            if first_lesson_day is not None:
                buttons.append(
                    [
                        InlineKeyboardButton(
                            f"📚 Открыть незавершенную лекцию (день {first_lesson_day})",
                            callback_data=f"{cb.REMINDER_NAV_PREFIX}lesson:{first_lesson_day}",
                        )
                    ]
                )
            if first_quest_day is not None:
                buttons.append(
                    [
                        InlineKeyboardButton(
                            f"📝 Открыть незавершенное задание (день {first_quest_day})",
                            callback_data=f"{cb.REMINDER_NAV_PREFIX}quest:{first_quest_day}",
                        )
                    ]
                )
            if first_questionnaire is not None:
                q_day, qid = first_questionnaire
                buttons.append(
                    [
                        InlineKeyboardButton(
                            f"📋 Открыть незавершенную анкету (день {q_day})",
                            callback_data=f"{cb.REMINDER_NAV_PREFIX}questionnaire:{q_day}:{qid}",
                        )
                    ]
                )
            buttons.append(
                [
                    InlineKeyboardButton(
                        "➡️ Продолжить по порядку",
                        callback_data=cb.REMINDER_NAV_NEXT,
                    )
                ]
            )

            await update.effective_message.reply_text(
                text_msg,
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            raise ApplicationHandlerStop

        if text in (texts.DAY_QUOTE, texts.DAY_PIC, texts.DAY_TIP, texts.DAY_BOOK, texts.DAY_FILM):
            if not daily:
                await update.effective_message.reply_text("❌ DailyPackService не подключён.", reply_markup=menus.kb_day())
                raise ApplicationHandlerStop

            kind_map = {
                texts.DAY_PIC: "image",
                texts.DAY_TIP: "tip",
                texts.DAY_BOOK: "book",
                texts.DAY_FILM: "film",
                texts.DAY_QUOTE: "quote",
            }
            kind = kind_map[text]

            pack = daily.get_today_pack()
            if not pack:
                await asyncio.to_thread(daily.generate_set_for_today, trigger="on_demand", force=False)
                pack = daily.get_today_pack()

            if not pack:
                await update.effective_message.reply_text(
                    "⚠️ Пакет дня пока не готов. Попробуй ещё раз через минуту.",
                    reply_markup=menus.kb_day(),
                )
                raise ApplicationHandlerStop

            item = next((x for x in pack["items"] if x.get("kind") == kind), None)
            if not item:
                await update.effective_message.reply_text("⚠️ Элемент не найден.", reply_markup=menus.kb_day())
                raise ApplicationHandlerStop

            if kind == "image":
                payload = item.get("payload_json") or {}
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        payload = {}
                photo_file_id = payload.get("photo_file_id")
                img_path = payload.get("image_path")

                # 1) Prefer Telegram file_id (fastest + no local storage).
                if photo_file_id:
                    try:
                        await update.effective_message.reply_photo(photo=photo_file_id)
                    except Exception as e:
                        await update.effective_message.reply_text(f"⚠️ Не смог отправить картинку по file_id: {e}")

                # 2) Fallback: send local file, then cache file_id back to DB for the next time.
                elif img_path:
                    try:
                        with open(img_path, "rb") as f:
                            msg = await update.effective_message.reply_photo(photo=f)
                        try:
                            if daily and hasattr(daily, "repo") and item.get("id") and msg and getattr(msg, "photo", None):
                                fid = msg.photo[-1].file_id
                                await asyncio.to_thread(
                                    daily.repo.set_item_photo_file_id,
                                    item_id=int(item["id"]),
                                    photo_file_id=fid,
                                )
                        except Exception:
                            pass
                    except Exception as e:
                        await update.effective_message.reply_text(f"⚠️ Не смог открыть/отправить файл картинки: {img_path}\n{e}")

                # 3) If neither is present — we just send the text (no warning).

            await update.effective_message.reply_text(item["content_text"], reply_markup=menus.kb_day())
            raise ApplicationHandlerStop

        # Progress submenu
        if text == texts.PROGRESS_REFRESH:
            _evaluate_achievements(uid)
            await update.effective_message.reply_text(
                _progress_text(uid),
                reply_markup=menus.kb_progress(),
            )
            raise ApplicationHandlerStop

        # Settings submenu
        if text == texts.SETTINGS_HABITS:
            await update.effective_message.reply_text(
                "✅ Мои привычки\n\nВыбери действие:",
                reply_markup=menus.kb_habits(),
            )
            raise ApplicationHandlerStop

        if text == texts.HABITS_CREATE:
            user_svc.set_step(uid, STEP_HABIT_WAIT_TITLE, {})
            await update.effective_message.reply_text(
                "➕ Создаём привычку!\n\nКак она называется?",
                reply_markup=menus.kb_back_only(),
            )
            raise ApplicationHandlerStop

        if text == texts.HABITS_LIST:
            if not habit_svc:
                await update.effective_message.reply_text("❌ HabitService не подключён.", reply_markup=menus.kb_habits())
                raise ApplicationHandlerStop
            habits = habit_svc.list_for_user(uid)
            if not habits:
                await update.effective_message.reply_text("У тебя пока нет привычек. Нажми «Создать привычку».", reply_markup=menus.kb_habits())
                raise ApplicationHandlerStop

            lines = []
            for h in habits:
                st = "🟢" if h.get("is_active") else "⚪️"
                lines.append(f"{st} #{h['id']} — {h['title']} — {h['remind_time']} — {h['frequency']}")

            await update.effective_message.reply_text(
                "📋 Твои привычки:\n\n" + "\n".join(lines) + "\n\nДля изменения или удаления — выбери действие в меню привычек.",
                reply_markup=menus.kb_habits(),
            )
            raise ApplicationHandlerStop

        if text == texts.HABITS_EDIT:
            if not habit_svc:
                await update.effective_message.reply_text("❌ HabitService не подключён.", reply_markup=menus.kb_habits())
                raise ApplicationHandlerStop
            habits = habit_svc.list_for_user(uid)
            if not habits:
                await update.effective_message.reply_text("У тебя пока нет привычек. Нажми «Создать привычку».", reply_markup=menus.kb_habits())
                raise ApplicationHandlerStop
            lines = []
            for h in habits:
                lines.append(f"#{h['id']} — {h['title']} — {h['remind_time']} — {h['frequency']}")
            user_svc.set_step(uid, STEP_HABIT_PICK_FOR_EDIT, {})
            await update.effective_message.reply_text(
                "✏️ Выбери привычку для изменения.\n\n" + "\n".join(lines) + "\n\nВведи номер (например 1 или #1):",
                reply_markup=menus.kb_back_only(),
            )
            raise ApplicationHandlerStop

        if text == texts.HABITS_DELETE:
            if not habit_svc:
                await update.effective_message.reply_text("❌ HabitService не подключён.", reply_markup=menus.kb_habits())
                raise ApplicationHandlerStop
            habits = habit_svc.list_for_user(uid)
            if not habits:
                await update.effective_message.reply_text("У тебя пока нет привычек. Нажми «Создать привычку».", reply_markup=menus.kb_habits())
                raise ApplicationHandlerStop
            lines = []
            for h in habits:
                lines.append(f"#{h['id']} — {h['title']} — {h['remind_time']} — {h['frequency']}")
            user_svc.set_step(uid, STEP_HABIT_PICK_FOR_DELETE, {})
            await update.effective_message.reply_text(
                "🗑 Выбери привычку для удаления.\n\n" + "\n".join(lines) + "\n\nВведи номер (например 1 или #1):",
                reply_markup=menus.kb_back_only(),
            )
            raise ApplicationHandlerStop

        if text == texts.SETTINGS_PERSONAL_REMINDERS:
            await update.effective_message.reply_text(
                "🔔 Мои напоминания\n\nВыбери действие:",
                reply_markup=menus.kb_personal_reminders(),
            )
            raise ApplicationHandlerStop

        if text == texts.REMINDERS_CREATE:
            if not pr_svc:
                await update.effective_message.reply_text(
                    "❌ Сервис напоминаний не подключён.",
                    reply_markup=menus.kb_settings(),
                )
                raise ApplicationHandlerStop
            user_svc.set_step(uid, STEP_PR_WAIT_TEXT, {})
            await update.effective_message.reply_text(
                "➕ Создаём напоминание!\n\nВведи текст напоминания.",
                reply_markup=menus.kb_back_only(),
            )
            raise ApplicationHandlerStop

        if text == texts.REMINDERS_LIST:
            if not pr_svc:
                await update.effective_message.reply_text(
                    "❌ Сервис напоминаний не подключён.",
                    reply_markup=menus.kb_personal_reminders(),
                )
                raise ApplicationHandlerStop
            reminders = pr_svc.list_for_user(uid)
            if not reminders:
                await update.effective_message.reply_text(
                    "У тебя пока нет персональных напоминаний. Нажми «Создать напоминание».",
                    reply_markup=menus.kb_personal_reminders(),
                )
                raise ApplicationHandlerStop

            lines = []
            for r in reminders:
                st = "🟢" if r.get("is_active") else "⚪️"
                rid = int(r.get("id") or 0)
                txt = (r.get("text") or "").strip()
                start_local = _format_start_local(uid, r.get("start_at"))
                lines.append(f"{st} #{rid} — {txt} — {start_local}")
            await update.effective_message.reply_text(
                "📋 Твои напоминания:\n\n" + "\n".join(lines),
                reply_markup=menus.kb_personal_reminders(),
            )
            raise ApplicationHandlerStop

        if text == texts.REMINDERS_EDIT:
            if not pr_svc:
                await update.effective_message.reply_text(
                    "❌ Сервис напоминаний не подключён.",
                    reply_markup=menus.kb_personal_reminders(),
                )
                raise ApplicationHandlerStop
            reminders = pr_svc.list_for_user(uid)
            if not reminders:
                await update.effective_message.reply_text(
                    "У тебя пока нет персональных напоминаний. Нажми «Создать напоминание».",
                    reply_markup=menus.kb_personal_reminders(),
                )
                raise ApplicationHandlerStop
            lines = []
            for r in reminders:
                rid = int(r.get("id") or 0)
                txt = (r.get("text") or "").strip()
                start_local = _format_start_local(uid, r.get("start_at"))
                lines.append(f"#{rid} — {txt} — {start_local}")
            user_svc.set_step(uid, STEP_PR_PICK_FOR_EDIT, {})
            await update.effective_message.reply_text(
                "✏️ Выбери напоминание для изменения.\n\n"
                + "\n".join(lines)
                + "\n\nВведи номер (например 1 или #1):",
                reply_markup=menus.kb_back_only(),
            )
            raise ApplicationHandlerStop

        if text == texts.REMINDERS_DELETE:
            if not pr_svc:
                await update.effective_message.reply_text(
                    "❌ Сервис напоминаний не подключён.",
                    reply_markup=menus.kb_personal_reminders(),
                )
                raise ApplicationHandlerStop
            reminders = pr_svc.list_for_user(uid)
            if not reminders:
                await update.effective_message.reply_text(
                    "У тебя пока нет персональных напоминаний. Нажми «Создать напоминание».",
                    reply_markup=menus.kb_personal_reminders(),
                )
                raise ApplicationHandlerStop
            lines = []
            for r in reminders:
                rid = int(r.get("id") or 0)
                txt = (r.get("text") or "").strip()
                start_local = _format_start_local(uid, r.get("start_at"))
                lines.append(f"#{rid} — {txt} — {start_local}")
            user_svc.set_step(uid, STEP_PR_PICK_FOR_DELETE, {})
            await update.effective_message.reply_text(
                "🗑 Выбери напоминание для удаления.\n\n"
                + "\n".join(lines)
                + "\n\nВведи номер (например 1 или #1):",
                reply_markup=menus.kb_back_only(),
            )
            raise ApplicationHandlerStop

        if text == texts.SETTINGS_TZ:
            user_svc.set_step(uid, STEP_WAIT_TZ, {})
            await update.effective_message.reply_text("🕒 Выбери часовой пояс:", reply_markup=menus.kb_timezone())
            raise ApplicationHandlerStop

        if text == texts.SETTINGS_TIME:
            user_svc.set_step(uid, STEP_WAIT_TIME, {})
            await update.effective_message.reply_text(
                "Введи новое время (ЧЧ:ММ), оно применится ко всем будущим рассылкам.",
                reply_markup=menus.kb_back_only(),
            )
            raise ApplicationHandlerStop

        if text == texts.SETTINGS_NAME:
            user_svc.set_step(uid, STEP_WAIT_NAME, {})
            await update.effective_message.reply_text(
                "Как тебя называть? Напиши имя сообщением.",
                reply_markup=menus.kb_back_only(),
            )
            raise ApplicationHandlerStop

        # Unknown text
        await update.effective_message.reply_text(
            "Выбери пункт меню 👇", reply_markup=menus.kb_main(_is_admin(uid))
        )
        raise ApplicationHandlerStop

    # ----------------------------
    # Register handlers
    # ----------------------------
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("enroll", enroll_cmd))

    app.add_handler(CallbackQueryHandler(consent_pick, pattern=r"^consent:(yes|no)$"))
    app.add_handler(CallbackQueryHandler(tz_pick, pattern=r"^tz:.*"))
    app.add_handler(CallbackQueryHandler(enroll_time_pick, pattern=r"^" + re.escape(cb.ENROLL_TIME_PREFIX)))
    app.add_handler(
        CallbackQueryHandler(
            reminder_nav_pick,
            pattern=r"^remnav:(next|lesson:\d+|quest:\d+|questionnaire:\d+:\d+)$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            mood_pick,
            pattern=r"^mood:(menu|rate|set:[1-5]|chart:(7|30))$",
        )
    )
    app.add_handler(CallbackQueryHandler(help_faq_pick, pattern=r"^help:(faq:\d+|faq:list|escalate)$"))
    app.add_handler(CallbackQueryHandler(admin_ticket_quick_pick, pattern=r"^admin_ticket:(open|reply):\d+$"))

    # Habits
    app.add_handler(CallbackQueryHandler(habit_freq_pick, pattern=r"^habit:freq:(daily|weekdays|weekends)$"))
    app.add_handler(CallbackQueryHandler(habit_done, pattern=r"^habit:done:\d+$"))
    app.add_handler(CallbackQueryHandler(habit_skip, pattern=r"^habit:skip:\d+$"))
    app.add_handler(CallbackQueryHandler(habit_toggle, pattern=r"^habit:toggle:\d+$"))
    app.add_handler(CallbackQueryHandler(habit_delete, pattern=r"^habit:delete:\d+$"))

    # Steps must run BEFORE the menu router
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_step_text), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_menu_text), group=1)
