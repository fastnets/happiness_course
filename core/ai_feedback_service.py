import os
import json
import time
import uuid
import asyncio
import ssl
import logging
import urllib.parse
import urllib.request
import urllib.error
import re
from typing import Optional, Dict, Any

logger = logging.getLogger("happines_course")


class AiFeedbackService:
    """
    AI feedback service using GigaChat.
    Поддерживает:
    - текстовые ответы
    - генерацию изображений (text2image)
    """

    _IMG_RE = re.compile(r"<img[^>]*\s+src=['\"]([^'\"]+)['\"]", re.IGNORECASE)

    def __init__(self):
        self.basic = (os.getenv("GIGACHAT_BASIC", "") or "").strip()
        if (self.basic.startswith('"') and self.basic.endswith('"')) or (
            self.basic.startswith("'") and self.basic.endswith("'")
        ):
            self.basic = self.basic[1:-1].strip()

        self.scope = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_B2B")
        self.model = os.getenv("GIGACHAT_MODEL", "GigaChat-2-Max")
        self.oauth_url = os.getenv(
            "GIGACHAT_OAUTH_URL",
            "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
        )
        self.chat_url = os.getenv(
            "GIGACHAT_CHAT_URL",
            "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
        )
        self.timeout_sec = float(os.getenv("GIGACHAT_TIMEOUT_SEC", "30"))
        self.verify_ssl = os.getenv("GIGACHAT_VERIFY_SSL", "1") != "0"

        self._token: Optional[str] = None
        self._token_exp_ts: float = 0.0

    # -------------------------------------------------
    # Base helpers
    # -------------------------------------------------
    def enabled(self) -> bool:
        ok = bool(self.basic)
        if not ok:
            logger.debug("GigaChat disabled: GIGACHAT_BASIC is empty")
        return ok

    def _ssl_context(self) -> Optional[ssl.SSLContext]:
        if self.verify_ssl:
            return None
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _ensure_token(self) -> Optional[str]:
        if not self.enabled():
            return None

        now = time.time()
        if self._token and now < (self._token_exp_ts - 10):
            return self._token

        data = urllib.parse.urlencode({"scope": self.scope}).encode("utf-8")
        req = urllib.request.Request(
            self.oauth_url,
            data=data,
            headers={
                "Authorization": "Basic " + self.basic,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "RqUID": str(uuid.uuid4()),
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec, context=self._ssl_context()) as r:
                raw = r.read().decode("utf-8", errors="ignore")
                obj = json.loads(raw)
        except Exception as e:
            logger.error("[GigaChat] OAuth failed: %s", e)
            return None

        token = (obj.get("access_token") or "").strip()
        if not token:
            logger.error("[GigaChat] OAuth: no access_token. raw=%s", str(obj)[:300])
            return None

        # expires_in обычно в секундах
        exp = obj.get("expires_in", 1800)
        try:
            exp_sec = float(exp)
        except Exception:
            exp_sec = 1800.0

        self._token = token
        self._token_exp_ts = time.time() + exp_sec
        return token

    def _refresh_token(self) -> Optional[str]:
        self._token = None
        self._token_exp_ts = 0.0
        return self._ensure_token()

    # -------------------------------------------------
    # TEXT CHAT
    # -------------------------------------------------
    def _chat(self, system: str, user: str) -> Optional[str]:
        token = self._ensure_token()
        if not token:
            return None

        def do_req(bearer: str) -> Optional[Dict[str, Any]]:
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.7,
                "profanity_check": True,
            }
            req = urllib.request.Request(
                self.chat_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {bearer}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_sec, context=self._ssl_context()) as r:
                    return json.loads(r.read().decode("utf-8", errors="ignore"))
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    return {"__http401__": True}
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="ignore")
                except Exception:
                    pass
                logger.error("[GigaChat] Chat HTTPError=%s body=%s", e.code, body[:500])
                return None
            except Exception as e:
                logger.error("[GigaChat] Chat failed: %s", e)
                return None

        obj = do_req(token)
        if obj and obj.get("__http401__"):
            token2 = self._refresh_token()
            if not token2:
                return None
            obj = do_req(token2)

        if not obj:
            return None

        try:
            return (obj["choices"][0]["message"]["content"] or "").strip() or None
        except Exception:
            return None

    # -------------------------------------------------
    # IMAGE GENERATION
    # -------------------------------------------------
    def _files_base_url(self) -> str:
        # корректно даже если поменяется домен, главное чтобы путь был .../chat/completions
        if "/chat/completions" in self.chat_url:
            return self.chat_url.split("/chat/completions")[0] + "/files"
        # fallback
        return "https://gigachat.devices.sberbank.ru/api/v1/files"

    def generate_image_bytes(self, prompt: str) -> Optional[bytes]:
        """
        Генерирует изображение через GigaChat и возвращает bytes.
        Возвращает None, если:
          - сервис не отдал <img src="...">
          - скачивание файла не удалось
        """
        token = self._ensure_token()
        if not token:
            return None

        prompt = (prompt or "").strip()

        def do_chat(bearer: str) -> Optional[Dict[str, Any]]:
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": "Ты — художник-минималист. Нарисуй изображение. Без текста на изображении."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.7,
                "profanity_check": True,
                "function_call": "auto",
            }
            req = urllib.request.Request(
                self.chat_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {bearer}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_sec, context=self._ssl_context()) as r:
                    return json.loads(r.read().decode("utf-8", errors="ignore"))
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    return {"__http401__": True}
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="ignore")
                except Exception:
                    pass
                logger.error("[GigaChat] Image chat HTTPError=%s body=%s", e.code, body[:500])
                return None
            except Exception as e:
                logger.error("[GigaChat] Image chat failed: %s", e)
                return None

        obj = do_chat(token)
        if obj and obj.get("__http401__"):
            token2 = self._refresh_token()
            if not token2:
                return None
            token = token2
            obj = do_chat(token)

        if not obj:
            return None

        content = ""
        try:
            content = (obj.get("choices", [{}])[0].get("message", {}) or {}).get("content", "") or ""
            content = content.strip()
        except Exception:
            content = ""

        m = self._IMG_RE.search(content or "")
        if not m:
            # ЛОГ важен — тут станет понятно, что приходит от GigaChat
            logger.warning("[GigaChat] No <img src> in response. content_preview=%s", (content or "")[:400])
            return None

        file_id = m.group(1).strip()
        file_url = f"{self._files_base_url()}/{file_id}/content"

        def try_download(method: str) -> Optional[bytes]:
            data = b"" if method == "POST" else None
            req = urllib.request.Request(
                file_url,
                data=data,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/octet-stream",
                },
                method=method,
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_sec, context=self._ssl_context()) as r:
                    bts = r.read()
                    return bts if bts else None
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="ignore")
                except Exception:
                    pass
                logger.error("[GigaChat] File download %s HTTPError=%s body=%s", method, e.code, body[:500])
                return None
            except Exception as e:
                logger.error("[GigaChat] File download %s failed: %s", method, e)
                return None

        # 1) пробуем GET (часто работает)
        data = try_download("GET")
        if data:
            return data

        # 2) fallback на POST
        data = try_download("POST")
        return data

    async def fallback_reply(
        self,
        user_name: str,
        user_text: str,
        history: Optional[list[dict[str, str]]] = None,
    ) -> Optional[str]:
        """Generic fallback reply for free-text messages outside active flows."""

        user_text = (user_text or "").strip()
        if not user_text:
            return None

        system = (
            "Ты — ассистент Telegram-бота «Курс на счастье» "
            "(курс по благополучию и здоровым привычкам). "
            "Отвечай на языке пользователя и естественно продолжай текущий диалог. "
            "Сначала прямо и конкретно ответь на последнее сообщение пользователя. "
            "Не представляйся заново и не здоровайся в каждом ответе. "
            "Если контекст уже передан, не говори, что не видишь прошлые сообщения. "
            "Избегай шаблонных повторов и длинных списков не по теме. "
            "Пиши кратко (1-4 предложения), по делу и в контексте текущей темы. "
            "Если данных всё ещё недостаточно, задай один короткий уточняющий вопрос. "
            "Не выдумывай факты и не обещай действий вне чата."
        )

        history_lines: list[str] = []
        for row in (history or []):
            role = (row or {}).get("role")
            content = str((row or {}).get("content") or "").strip()
            if not content:
                continue
            speaker = "User" if role == "user" else "Assistant"
            history_lines.append(f"{speaker}: {content[:500]}")

        history_block = ""
        if history_lines:
            history_block = "Recent dialog:\n" + "\n".join(history_lines[-20:]) + "\n\n"

        uname = (user_name or "").strip()
        if uname:
            user = f"{history_block}User: {uname}\nMessage: {user_text}"
        else:
            user = f"{history_block}User message: {user_text}"

        def _call() -> Optional[str]:
            return self._chat(system=system, user=user)

        return await asyncio.to_thread(_call)

    # -------------------------------------------------
    # PUBLIC API used by handlers
    # -------------------------------------------------
    def generate_followup_question(self, quest_text: str, user_answer: str) -> Optional[str]:
        """Backward-compatible sync API.

        Used by:
        - /admin AI test
        - learning_handlers as a fallback

        Returns a short supportive feedback + 1 follow-up question.
        """
        quest_text = (quest_text or "").strip()
        user_answer = (user_answer or "").strip()

        system = (
            "Ты — доброжелательный коуч по счастью и осознанности. "
            "Твоя задача: дать краткую поддерживающую обратную связь по ответу пользователя "
            "и задать ОДИН уточняющий вопрос, чтобы помочь продвинуться дальше. "
            "Пиши по-русски, 2–6 коротких предложений, без оценочных суждений."
        )

        user = (
            "ЗАДАНИЕ:\n"
            f"{quest_text or '(задание не найдено)'}\n\n"
            "ОТВЕТ ПОЛЬЗОВАТЕЛЯ:\n"
            f"{user_answer or '(пусто)'}\n\n"
            "Сначала 1–3 предложения поддержки/обратной связи, затем один вопрос."
        )

        return self._chat(system=system, user=user)

    async def feedback_for_quest_answer(
        self,
        user_name: str,
        day_index: int,
        quest_text: str,
        answer_text: str,
    ) -> Optional[str]:
        """Async API expected by learning_handlers.

        Runs blocking HTTP in a thread to avoid blocking the event loop.
        """

        def _call() -> Optional[str]:
            prefix = (user_name or "").strip()
            if prefix:
                # добавим персонализацию в текст задания
                qt = f"День {day_index}. {quest_text}"
                ua = f"{prefix}: {answer_text}"
                return self.generate_followup_question(qt, ua)
            return self.generate_followup_question(f"День {day_index}. {quest_text}", answer_text)

        return await asyncio.to_thread(_call)

    async def followup_after_user_reply(
        self,
        user_name: str,
        day_index: int,
        quest_text: str,
        first_answer: str,
        ai_message_1: str,
        user_followup: str,
    ) -> Optional[str]:
        """Continue the dialog after user's follow-up message."""

        system = (
            "Ты — доброжелательный коуч. Продолжай короткий диалог по заданию. "
            "Отвечай по-русски. Дай поддержку и 1 следующий вопрос или 1 маленькое практическое действие. "
            "Не будь многословным (до ~8 предложений)."
        )

        user = (
            f"КОНТЕКСТ (день {day_index}):\n"
            f"Задание: {quest_text}\n\n"
            f"Первый ответ пользователя: {first_answer}\n\n"
            f"Твой прошлый ответ: {ai_message_1}\n\n"
            f"Новое сообщение пользователя: {user_followup}\n\n"
            "Продолжи диалог: короткий ответ + один вопрос/следующий шаг."
        )

        def _call() -> Optional[str]:
            return self._chat(system=system, user=user)

        return await asyncio.to_thread(_call)
