import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message
from telethon import TelegramClient, errors, functions, types as tg_types

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
SESSIONS_DIR = ROOT_DIR / "sessions"
DATA_DIR.mkdir(exist_ok=True)
SESSIONS_DIR.mkdir(exist_ok=True)

ACCOUNTS_FILE = DATA_DIR / "accounts.json"
BOT_TOKEN = "8604995011:AAFjPpG0hdz8ryfx5rZKpSjQarxuJo_7AHU"
CLICKER_TARGET = "patrickstarsrobot"
CLICK_INTERVAL_MINUTES = 6
CLICK_DELAY_SECONDS = 1
ADMIN_IDS = []

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

user_states: Dict[int, Dict[str, Optional[str]]] = {}


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


class AccountManager:
    def __init__(self):
        self.accounts = self._load_accounts()

    def _load_accounts(self) -> Dict[str, Dict]:
        if ACCOUNTS_FILE.exists():
            try:
                return json.loads(ACCOUNTS_FILE.read_text())
            except json.JSONDecodeError:
                return {}
        return {}

    def _save_accounts(self):
        ACCOUNTS_FILE.write_text(json.dumps(self.accounts, indent=2, ensure_ascii=False))

    def list_accounts(self):
        return list(self.accounts.values())

    def get_account(self, phone: str):
        return self.accounts.get(phone)

    def _session_path(self, phone: str) -> Path:
        clean = phone.replace("+", "").replace("@", "")
        return SESSIONS_DIR / f"{clean}.session"

    def add_pending_account(self, phone: str, api_id: int, api_hash: str):
        self.accounts[phone] = {
            "phone": phone,
            "api_id": api_id,
            "api_hash": api_hash,
            "phone_code_hash": None,
            "created_at": _now_iso(),
            "status": "pending_code",
            "last_click_at": None,
            "click_count": 0,
            "next_click_at": None,
            "authorized": False,
        }
        self._save_accounts()

    async def send_code(self, phone: str):
        account = self.get_account(phone)
        if not account:
            raise ValueError("Hisob topilmadi")
        session = self._session_path(phone)
        client = TelegramClient(session, account["api_id"], account["api_hash"])
        await client.connect()
        try:
            result = await client.send_code_request(phone)
            account["phone_code_hash"] = getattr(result, "phone_code_hash", None)
            self._save_accounts()
        finally:
            await client.disconnect()

    def _format_next_click(self, seconds: int) -> str:
        return (datetime.utcnow() + timedelta(seconds=seconds)).replace(microsecond=0).isoformat() + "Z"

    def _parse_cooldown_seconds(self, text: str) -> Optional[int]:
        if not text:
            return None
        match = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
        if not match:
            return None
        parts = [int(p) for p in match.groups() if p is not None]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        return parts[0] * 60 + parts[1]

    def _parse_math_captcha(self, text: str) -> Optional[float]:
        if not text:
            return None
        normalized = text.replace("×", "*").replace("x", "*").replace("X", "*")
        normalized = normalized.replace("÷", "/").replace(":", "/")
        normalized = re.sub(r"[^0-9+\-*/().= ]", " ", normalized)
        expr_match = re.search(r"(\d+[ \t]*[+\-*/][ \t]*\d+)", normalized)
        if not expr_match:
            return None
        expr = expr_match.group(1)
        try:
            result = eval(expr, {"__builtins__": None}, {})
            return float(result)
        except Exception:
            return None

    def _find_button_by_answer(self, buttons, answer: float):
        answer_text = str(int(answer)) if answer.is_integer() else str(answer)
        for row in buttons:
            for button in row:
                text = (getattr(button, "text", None) or "").strip()
                if not text:
                    continue
                normalized = text.replace(" ", "").replace("=", "")
                if normalized == answer_text:
                    return button
                if answer_text in normalized:
                    return button
        return None

    async def _solve_robot_check(self, client, last_message):
        text = (last_message.message or "").lower()
        if "проверка на робота" not in text and "чтобы получить награду" not in text and "сумму чисел" not in text:
            return None
        answer = self._parse_math_captcha(text)
        if answer is None:
            return None
        button = self._find_button_by_answer(last_message.buttons, answer)
        if not button:
            return None
        await client(functions.messages.GetBotCallbackAnswerRequest(
            peer=CLICKER_TARGET,
            msg_id=last_message.id,
            data=button.data,
        ))
        await asyncio.sleep(1)
        return True

    async def complete_login(self, phone: str, code: str, password: Optional[str] = None):
        account = self.get_account(phone)
        if not account:
            raise ValueError("Hisob topilmadi")
        session = self._session_path(phone)
        client = TelegramClient(session, account["api_id"], account["api_hash"])
        await client.connect()
        try:
            if not await client.is_user_authorized():
                try:
                    await client.sign_in(phone, code=code, phone_code_hash=account.get("phone_code_hash"))
                except errors.SessionPasswordNeededError:
                    if not password:
                        raise
                    await client.sign_in(password=password)
            account["authorized"] = True
            account["status"] = "active"
            account["authorized_at"] = _now_iso()
            self._save_accounts()
        finally:
            await client.disconnect()

    async def perform_click(self, account: Dict) -> str:
        phone = account["phone"]
        session = self._session_path(phone)
        client = TelegramClient(session, account["api_id"], account["api_hash"])
        await client.connect()
        try:
            if not await client.is_user_authorized():
                return "not_authorized"

            # Try to find an existing message with buttons first (avoid re-sending /start)
            messages = await client.get_messages(CLICKER_TARGET, limit=24)
            button_message = None
            for msg in messages:
                if msg.buttons:
                    button_message = msg
                    break

            # If no button message found, send /start and fetch recent messages
            if not button_message:
                await client.send_message(CLICKER_TARGET, "/start")
                await asyncio.sleep(2)
                messages = await client.get_messages(CLICKER_TARGET, limit=12)
                for msg in messages:
                    if msg.buttons:
                        button_message = msg
                        break

            if button_message and button_message.buttons:
                # Try to solve robot-check math captcha if it appears
                solved = await self._solve_robot_check(client, button_message)
                if solved:
                    follow_up = await client.get_messages(CLICKER_TARGET, limit=8)
                    combined = "\n".join((m.message or "") for m in follow_up).lower()
                    if "ты получил(а) 0.10 ⭐️" in combined or "ты получил(а) 0.10" in combined:
                        account["last_click_at"] = _now_iso()
                        account["click_count"] = account.get("click_count", 0) + 1
                        account["next_click_at"] = self._format_next_click(CLICK_INTERVAL_MINUTES * 60 + CLICK_DELAY_SECONDS)
                        self._save_accounts()
                        return "success"
                    if "не так быстро" in combined or "подожди" in combined:
                        cooldown = self._parse_cooldown_seconds(combined)
                        if cooldown is not None:
                            account["next_click_at"] = self._format_next_click(cooldown)
                            self._save_accounts()
                            return f"cooldown:{cooldown}"
                        account["next_click_at"] = self._format_next_click(CLICK_INTERVAL_MINUTES * 60 + CLICK_DELAY_SECONDS)
                        self._save_accounts()
                        return "cooldown"

                target_button = None
                for row in button_message.buttons:
                    for button in row:
                        button_text = (getattr(button, "text", None) or "").lower()
                        button_data = getattr(button, "data", None)
                        if not button_data:
                            continue
                        if any(keyword in button_text for keyword in ["клик", "clicker", "заработать звезды", "⭐️", "ok", "да"]):
                            target_button = button
                            break
                    if target_button:
                        break
                if not target_button:
                    for row in button_message.buttons:
                        for button in row:
                            if getattr(button, "data", None):
                                target_button = button
                                break
                        if target_button:
                            break
                if target_button:
                    await client(functions.messages.GetBotCallbackAnswerRequest(
                        peer=CLICKER_TARGET,
                        msg_id=button_message.id,
                        data=target_button.data,
                    ))
                    await asyncio.sleep(1)
                    account["last_click_at"] = _now_iso()
                    account["click_count"] = account.get("click_count", 0) + 1
                    self._save_accounts()

                    follow_up = await client.get_messages(CLICKER_TARGET, limit=8)
                    combined = "\n".join((m.message or "") for m in follow_up).lower()
                    if "ты получил(а) 0.10 ⭐️" in combined or "ты получил(а) 0.10" in combined:
                        account["next_click_at"] = self._format_next_click(CLICK_INTERVAL_MINUTES * 60 + CLICK_DELAY_SECONDS)
                        self._save_accounts()
                        return "success"
                    if "не так быстро" in combined or "подожди" in combined:
                        cooldown = self._parse_cooldown_seconds(combined)
                        if cooldown is not None:
                            account["next_click_at"] = self._format_next_click(cooldown)
                            self._save_accounts()
                            return f"cooldown:{cooldown}"
                        account["next_click_at"] = self._format_next_click(CLICK_INTERVAL_MINUTES * 60 + CLICK_DELAY_SECONDS)
                        self._save_accounts()
                        return "cooldown"
                    account["next_click_at"] = self._format_next_click(CLICK_INTERVAL_MINUTES * 60 + CLICK_DELAY_SECONDS)
                    self._save_accounts()
                    return f"button_clicked: {target_button.text}"

            account["next_click_at"] = self._format_next_click(CLICK_INTERVAL_MINUTES * 60 + CLICK_DELAY_SECONDS)
            self._save_accounts()
            return "no_click_button"
        finally:
            await client.disconnect()

    def build_summary(self):
        lines = []
        for account in self.list_accounts():
            lines.append(
                f"{account['phone']}: status={account.get('status')} "
                f"authorized={account.get('authorized')} clicks={account.get('click_count')} "
                f"last={account.get('last_click_at') or 'never'} "
                f"next={account.get('next_click_at') or 'ready'}"
            )
        return "\n".join(lines) if lines else "Hech qanday hisob yo'q."


manager = AccountManager()


def is_admin(user_id: int) -> bool:
    if not ADMIN_IDS:
        return True
    return user_id in ADMIN_IDS


def format_account_balance(account: Dict, per_click: float = 0.10) -> str:
    clicks = account.get("click_count", 0)
    balance = clicks * per_click
    return (
        f"{account['phone']} | ✅ | balans: {balance:.2f} "
        f"({clicks} ta bosildi; har bir click 0.10)"
    )


async def notify_admins(message: str):
    if not ADMIN_IDS:
        return
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, message)
        except Exception as exc:
            logger.error("Admin notification failed for %s: %s", admin_id, exc)


async def send_menu(chat_id: int):
    text = (
        "Botga xush kelibsiz!\n\n"
        "Buyruqlar:\n"
        "/add_account - yangi hisob qo'shish\n"
        "/list_accounts - hisoblarni ko'rish\n"
        "/stats - umumiy statistika\n"
        "/click_now - hozir click sinovini bajarish\n"
    )
    await bot.send_message(chat_id, text)


async def handle_start(message: Message):
    if not is_admin(message.from_user.id):
        await message.reply("Sizga ruxsat yo'q.")
        return
    await send_menu(message.chat.id)


async def handle_add_account(message: Message):
    if not is_admin(message.from_user.id):
        return
    user_states[message.from_user.id] = {"step": "phone"}
    await message.reply("Telefon raqamingizni xalqaro formatda + bilan kiriting:")


async def handle_list_accounts(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.reply(manager.build_summary())


async def handle_stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    accounts = manager.list_accounts()
    active = sum(1 for a in accounts if a.get("authorized"))
    total_clicks = sum(a.get("click_count", 0) for a in accounts)
    text = (
        f"Umumiy hisoblar: {len(accounts)}\n"
        f"Faol hisoblar: {active}\n"
        f"Jami clicklar: {total_clicks}\n"
        f"\nHisoblar:\n{manager.build_summary()}"
    )
    await message.reply(text)


async def handle_manual_click(message: Message):
    if not is_admin(message.from_user.id):
        return
    results = []
    for account in manager.list_accounts():
        if not account.get("authorized"):
            continue
        result = await manager.perform_click(account)
        if result == "success":
            results.append(format_account_balance(account))
        else:
            results.append(f"{account['phone']}: {result}")
    if not results:
        await message.reply("Faol hisoblar topilmadi.")
    else:
        await message.reply("\n".join(results))


async def ask_api_id(message: Message, state: Dict):
    state["step"] = "api_id"
    await message.reply("API ID ni kiriting:")


async def ask_api_hash(message: Message, state: Dict):
    state["step"] = "api_hash"
    await message.reply("API hash ni kiriting:")


async def ask_code(message: Message, state: Dict):
    state["step"] = "code"
    await message.reply("Telegramga yuborilgan kodni kiriting:")


async def ask_password(message: Message, state: Dict):
    state["step"] = "password"
    await message.reply("Agar 2-bosqichli parol talab etilsa, uni kiriting. Aks holda /skip deb yozing:")


async def process_text(message: Message):
    if message.from_user.id not in user_states:
        return
    state = user_states[message.from_user.id]
    step = state.get("step")
    text = message.text.strip()

    if step == "phone":
        phone_text = text
        if not phone_text.startswith("+"):
            await message.reply("Iltimos, telefon raqamini + bilan boshlang.")
            return
        state["phone"] = phone_text
        await ask_api_id(message, state)
        return
    if step == "api_id":
        try:
            state["api_id"] = int(text)
        except ValueError:
            await message.reply("API ID butun raqam bo'lishi kerak.")
            return
        await ask_api_hash(message, state)
        return
    if step == "api_hash":
        state["api_hash"] = text
        manager.add_pending_account(state["phone"], state["api_id"], state["api_hash"])
        await message.reply("Kod yuborildi. Iltimos, Telegramdan kodni kiriting.")
        try:
            await manager.send_code(state["phone"])
        except Exception as exc:
            await message.reply(f"Kod yuborishda xatolik yuz berdi: {exc}")
            user_states.pop(message.from_user.id, None)
            return
        await ask_code(message, state)
        return
    if step == "code":
        state["code"] = text
        try:
            await manager.complete_login(state["phone"], state["code"])
            account = manager.get_account(state["phone"])
            await message.reply("Hisob muvaffaqiyatli qo'shildi va avtorizatsiya qilindi.")
            if account:
                try:
                    result = await manager.perform_click(account)
                    await message.reply(f"Click natijasi: {result}")
                except Exception as exc:
                    await message.reply(f"Click bajarishda xatolik: {exc}")
            user_states.pop(message.from_user.id, None)
        except errors.SessionPasswordNeededError:
            await ask_password(message, state)
        except Exception as exc:
            await message.reply(f"Avtorizatsiya xatosi: {exc}")
            user_states.pop(message.from_user.id, None)
        return
    if step == "password":
        if text.lower() == "/skip":
            state["password"] = None
        else:
            state["password"] = text
        try:
            await manager.complete_login(state["phone"], state.get("code"), state.get("password"))
            await message.reply("Hisob muvaffaqiyatli qo'shildi va avtorizatsiya qilindi.")
        except Exception as exc:
            await message.reply(f"Avtorizatsiya xatosi: {exc}")
        user_states.pop(message.from_user.id, None)
        return


async def click_scheduler():
    while True:
        now = datetime.utcnow()
        for account in manager.list_accounts():
            if not account.get("authorized"):
                continue
            next_click_at = account.get("next_click_at")
            if next_click_at:
                try:
                    next_dt = datetime.fromisoformat(next_click_at.rstrip("Z"))
                    if next_dt > now:
                        continue
                except ValueError:
                    pass
            try:
                result = await manager.perform_click(account)
                logger.info("%s -> %s", account["phone"], result)
            except Exception as exc:
                logger.error("Click failed for %s: %s", account["phone"], exc)
        await asyncio.sleep(10)


async def main():
    global bot
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        raise RuntimeError("Bot tokenni main.py ichida BOT_TOKEN ga yozing.")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    dp.message.register(handle_start, Command(commands=["start"]))
    dp.message.register(handle_add_account, Command(commands=["add_account"]))
    dp.message.register(handle_list_accounts, Command(commands=["list_accounts"]))
    dp.message.register(handle_stats, Command(commands=["stats"]))
    dp.message.register(handle_manual_click, Command(commands=["click_now"]))
    dp.message.register(process_text)

    await bot.delete_webhook(drop_pending_updates=True)
    scheduler = asyncio.create_task(click_scheduler())
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
