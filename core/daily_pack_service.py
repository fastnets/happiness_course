from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from entity.repositories.daily_pack_repo import DailyPackRepo

logger = logging.getLogger("happines_course")


class DailyPackService:
    """Generates and stores a daily content pack based on the latest lesson topic.

    Requirements:
    - Once per UTC day at 00:00 UTC, generate a new pack (quote, tip, image, film, book).
    - If a new lesson is added during the day, generate a new pack using the new lesson topic
      and supersede the previous pack for that same UTC date.

    Stores generated items in DB and image bytes on disk (path stored in payload_json).
    """

    def __init__(self, db, settings, ai_service, schedule_service):
        self.settings = settings
        self.ai = ai_service
        self.schedule = schedule_service
        self.repo = DailyPackRepo(db)

        self.images_dir = Path(getattr(settings, "generated_dir", "generated")) / "daily_images"
        self.images_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def utc_date_today() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _latest_lesson_topic(self) -> Dict[str, Optional[str]]:
        lesson = self.schedule.lesson.get_latest() if getattr(self.schedule, "lesson", None) else None
        if not lesson:
            return {"day_index": None, "title": None, "description": None, "topic": "Курс на счастье"}

        day_index = int(lesson.get("day_index") or 0)
        title = (lesson.get("title") or "").strip()
        desc = (lesson.get("description") or "").strip()
        topic = title or "Тема дня"
        return {"day_index": day_index, "title": title, "description": desc, "topic": topic}

    def _context_block(self, lesson_ctx: Dict[str, Optional[str]]) -> str:
        title = lesson_ctx.get("title") or ""
        desc = lesson_ctx.get("description") or ""
        day_index = lesson_ctx.get("day_index")

        parts = ["Курс: Курс на счастье"]
        if day_index:
            parts.append(f"День лекции: {day_index}")
        if title:
            parts.append(f"Тема лекции: {title}")
        if desc:
            desc_short = desc.strip()
            if len(desc_short) > 800:
                desc_short = desc_short[:800] + "…"
            parts.append(f"Описание/тезисы: {desc_short}")
        return "\n".join(parts)

    def generate_set_for_today(self, *, trigger: str, force: bool = True) -> Optional[int]:
        """Generate a new pack for today's UTC date.

        If force=False, will skip generation if any set already exists for the date.
        """
        utc_date = self.utc_date_today()

        # ВАЖНО: у вас в repo параметр keyword-only, поэтому только так:
        if (not force) and self.repo.has_any_set_for_date(utc_date=utc_date):
            return None

        lesson_ctx = self._latest_lesson_topic()
        topic = lesson_ctx.get("topic") or "Курс на счастье"
        lesson_day_index = lesson_ctx.get("day_index")

        set_id = self.repo.create_set(
            utc_date=utc_date,
            lesson_day_index=lesson_day_index,
            topic=topic,
            trigger=trigger,
        )

        try:
            ctx = self._context_block(lesson_ctx)
            self._generate_items(set_id=set_id, utc_date=utc_date, ctx=ctx)
            self.repo.mark_ready(set_id=set_id)
            self.repo.supersede_other_ready(utc_date=utc_date, keep_set_id=set_id)
            return set_id
        except Exception:
            self.repo.mark_failed(set_id=set_id)
            raise

    def get_today_pack(self) -> Optional[Dict]:
        """Returns active pack for today's UTC date."""
        utc_date = self.utc_date_today()
        s = self.repo.get_active_set(utc_date=utc_date)
        if not s:
            return None
        items = self.repo.get_items_for_set(set_id=int(s["id"]))
        return {"set": s, "items": items}

    # -------------------------
    # Generation
    # -------------------------
    def _gen_text(self, system: str, user: str) -> str:
        if not self.ai or not getattr(self.ai, "enabled", lambda: False)():
            return ""
        out = self.ai._chat(system, user)
        return (out or "").strip()

    def _generate_items(self, *, set_id: int, utc_date: str, ctx: str):
        # 1) Quote
        q_system = "Ты — редактор вдохновляющих материалов. Пиши по-русски, ясно и без пафоса."
        q_user = (
            f"{ctx}\n\n"
            "Сформируй «Цитату дня» по теме лекции.\n"
            "Правила:\n"
            "- 1–2 коротких предложения.\n"
            "- если это не дословная проверяемая цитата, укажи: «Автор: Автор неизвестен».\n"
            "Верни строго в формате:\n"
            "Цитата: ...\n"
            "Автор: ..."
        )
        quote = self._gen_text(q_system, q_user) or (
            "Цитата: Маленькие шаги делают большие перемены.\n"
            "Автор: Автор неизвестен"
        )
        self.repo.upsert_item(set_id=set_id, kind="quote", title=None, content_text=quote, payload={"utc_date": utc_date})

        # 2) Tip
        t_system = "Ты — практичный коуч по благополучию. Пиши коротко, конкретно и без морализаторства."
        t_user = (
            f"{ctx}\n\n"
            "Сформируй «Совет дня» по теме лекции.\n"
            "Формат строго:\n"
            "Совет: (1 предложение)\n"
            "3 шага:\n"
            "1) ...\n2) ...\n3) ...\n"
            "Вопрос: (1 строка)"
        )
        tip = self._gen_text(t_system, t_user) or (
            "Совет: Сделай одну осознанную паузу на 10 секунд.\n"
            "3 шага:\n"
            "1) Заметь дыхание\n"
            "2) Выдохни медленно\n"
            "3) Назови чувство\n"
            "Вопрос: Что меняется после паузы?"
        )
        self.repo.upsert_item(set_id=set_id, kind="tip", title=None, content_text=tip, payload={"utc_date": utc_date})

        # 3) Image (prompt + caption + optional saved jpg)
        i_system = "Ты — редактор коротких подписей к изображениям для wellbeing-курса."
        i_user = (
            f"{ctx}\n\n"
            "Сформируй подпись к «Картинке дня». Верни только одну строку.\n"
            "Формат строго:\n"
            "- одна короткая строка на русском, до 140 символов.\n"
            "- без префиксов, без эмодзи и без упоминания темы/дня."
        )
        image_block = self._gen_text(i_system, i_user) or "Найди гармонию внутри себя."

        # Убираем метки, чтобы оставить только текст
        image_block = image_block.replace("🖼️ Промпт:", "").replace("✍️ Подпись:", "").replace("❓ Вопрос:", "").strip()

        m = re.search(r"Промпт:\s*(.+)", image_block)
        short_prompt = (m.group(1).strip() if m else "Минимализм, тёплый свет, спокойное настроение, без текста.")
        short_prompt = short_prompt.replace("\n", " ").strip()[:350]

        img_path = None
        if self.ai and getattr(self.ai, "generate_image_bytes", None):
            try:
                self.images_dir.mkdir(parents=True, exist_ok=True)

                gen_prompt = (
                    "Нарисуй минималистичную иллюстрацию для телеграм-курса «Курс на счастье».\n"
                    f"{ctx}\n"
                    "Стиль: минимализм, теплый мягкий свет, спокойная атмосфера.\n"
                    "Критично: НИКАКОГО текста. Запрещены буквы, слова, цифры, логотипы, водяные знаки, подписи и интерфейсные элементы. Это не постер и не обложка.\n"
                    f"Сцена: {short_prompt}"
                )

                img_bytes = self.ai.generate_image_bytes(gen_prompt)
                if img_bytes:
                    filename = f"{utc_date}_set{set_id}.jpg"
                    path = self.images_dir / filename
                    path.write_bytes(img_bytes)
                    img_path = str(path)
                else:
                    logger.warning("Daily image bytes is None (set_id=%s, utc_date=%s)", set_id, utc_date)
            except Exception:
                logger.exception("Daily image generation failed (set_id=%s, utc_date=%s)", set_id, utc_date)

        self.repo.upsert_item(
            set_id=set_id,
            kind="image",
            title=None,
            content_text=image_block,
            payload={"utc_date": utc_date, "image_path": img_path},
        )

        # 4) Film
        f_system = "Ты — редактор кинорекомендаций. Предлагай только реально существующие фильмы."
        f_user = (
            f"{ctx}\n\n"
            "Подбери «Фильм дня» по теме лекции.\n"
            "Правила:\n"
            "- Не выдумывай названия и годы.\n"
            "- Если не уверен — дай до 3 вариантов.\n"
            "Верни строго в формате:\n"
            "Фильм дня: Название (год)\n"
            "Почему подходит: (2–3 коротких предложения)\n"
            "3 вопроса после просмотра: 1)... 2)... 3)..."
        )
        film = self._gen_text(f_system, f_user) or (
            "Фильм дня: The Secret Life of Walter Mitty (2013)\n"
            "Почему подходит: Про маленькие шаги и возвращение вкуса к жизни.\n"
            "3 вопроса после просмотра: 1) Что герой понял? 2) Какой шаг сделаю я? 3) Что поддержит меня?"
        )
        self.repo.upsert_item(set_id=set_id, kind="film", title=None, content_text=film, payload={"utc_date": utc_date})

        # 5) Book
        b_system = "Ты — редактор книжных рекомендаций. Предлагай только реально существующие книги."
        b_user = (
            f"{ctx}\n\n"
            "Подбери «Книгу дня» по теме лекции (нон-фикшн/психология/саморазвитие).\n"
            "Правила:\n"
            "- Не выдумывай названия и авторов.\n"
            "- Если не уверен — дай до 3 вариантов.\n"
            "Верни строго в формате:\n"
            "Книга дня: Название — Автор\n"
            "Почему подходит: (2–3 коротких предложения)\n"
            "Мини-задание после чтения: (1 строка)"
        )
        book = self._gen_text(b_system, b_user) or (
            "Книга дня: Атомные привычки — Джеймс Клир\n"
            "Почему подходит: Про маленькие шаги и устойчивые изменения.\n"
            "Мини-задание после чтения: выбери 1 привычку и уменьшай до 2 минут."
        )
        self.repo.upsert_item(set_id=set_id, kind="book", title=None, content_text=book, payload={"utc_date": utc_date})
