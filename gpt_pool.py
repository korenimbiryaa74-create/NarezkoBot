#!/usr/bin/env python3
"""Пул аккаунтов ChatGPT: на каждый аккаунт — свой профиль Chrome.

Зачем: бесплатный аккаунт быстро выжимается (лимит на генерации, потом
сессия вообще умирает), поэтому аккаунты надо менять. Куки при этом
трогать не нужно: после первого ручного входа они лежат в профиле
(user-data-dirj/Default/Network/Cookies) и живут ~90 дней.

Структура:
    gpt_accounts/
      acc_01/
        profile/        # user-data-dir этого аккаунта
        cookies.json    # бэкап, обновляется сам после каждого запроса
        state.json      # когда упёрся в лимит / умер, счётчик ошибок
"""
import json
import os
import time

ACCOUNTS_DIR = "gpt_accounts"
DEFAULT_LIMIT_HOURS = 3      # сколько молчим после лимита на генерации
DEFAULT_DEAD_HOURS = 6       # сколько молчим после смерти сессии


class Account:
    """Один аккаунт = одна папка с профилем Chrome."""

    def __init__(self, root: str, name: str):
        self.name = name
        self.dir = os.path.join(root, name)
        self.profile = os.path.join(self.dir, "profile")
        self.cookies = os.path.join(self.dir, "cookies.json")
        self.state_path = os.path.join(self.dir, "state.json")
        self.state = self._load_state()

    def _load_state(self) -> dict:
        base = {
            "dead_until": 0.0,
            "limit_until": 0.0,
            "last_used": 0.0,
            "last_ok": 0.0,
            "fails": 0,
            "note": "",
        }
        if os.path.isfile(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as fh:
                    base.update(json.load(fh))
            except Exception:                          # noqa: BLE001
                pass
        return base

    def save(self):
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.state_path)

    def ensure_dirs(self):
        os.makedirs(self.profile, exist_ok=True)

    # ------------------------------------------------------------- статус
    def freeze_left(self) -> float:
        """Сколько ещё секунд аккаунт недоступен. 0 = свободен."""
        now = time.time()
        return max(0.0,
                   max(self.state.get("dead_until", 0),
                       self.state.get("limit_until", 0)) - now)

    def free(self) -> bool:
        return self.freeze_left() <= 0

    def mark_limit(self, hours: float = DEFAULT_LIMIT_HOURS, note: str = ""):
        self.state["limit_until"] = time.time() + hours * 3600
        self.state["fails"] = int(self.state.get("fails", 0)) + 1
        self.state["note"] = note or "лимит генераций"
        self.save()

    def mark_dead(self, hours: float = DEFAULT_DEAD_HOURS, note: str = ""):
        self.state["dead_until"] = time.time() + hours * 3600
        self.state["fails"] = int(self.state.get("fails", 0)) + 1
        self.state["note"] = note or "сессия мертва"
        self.save()

    def mark_ok(self):
        self.state["last_ok"] = time.time()
        self.state["fails"] = 0
        self.state["note"] = ""
        self.save()

    def touch(self):
        self.state["last_used"] = time.time()
        self.save()

    # -------------------------------------------------------------- вывод
    def _left_str(self) -> str:
        left = self.freeze_left()
        if left <= 0:
            return ""
        h, m = int(left // 3600), int((left % 3600) // 60)
        return f"{h}ч {m:02d}м" if h else f"{m}м"

    def line(self) -> str:
        if self.freeze_left() > 0:
            kind = "умер" if self.state.get("dead_until", 0) > time.time() \
                else "лимит"
            return f"⏳ <code>{self.name}</code> — {kind} ({self._left_str()})"
        if self.state.get("last_ok"):
            ago = (time.time() - self.state["last_ok"]) / 3600
            when = f"{ago:.1f} ч назад" if ago < 48 else f"{ago/24:.0f} дн назад"
            return f"✅ <code>{self.name}</code> — работал {when}"
        return f"❔ <code>{self.name}</code> — ещё ни разу не проверен"


class AccountPool:
    """Набор аккаунтов с ротацией."""

    def __init__(self, base_dir: str, port_base: int = 9300,
                 timeout: int = 240, headless: bool = False):
        self.base_dir = os.path.join(base_dir, ACCOUNTS_DIR)
        self.port_base = port_base
        self.timeout = timeout
        self.headless = headless
        os.makedirs(self.base_dir, exist_ok=True)
        self._cache = {}
        self._ports = {}

    # ------------------------------------------------------------- учёт
    def names(self) -> list:
        out = []
        for n in sorted(os.listdir(self.base_dir)):
            if n.startswith("acc_") and \
                    os.path.isdir(os.path.join(self.base_dir, n)):
                out.append(n)
        return out

    def get(self, name: str) -> Account:
        if name not in self._cache:
            self._cache[name] = Account(self.base_dir, name)
        return self._cache[name]

    def accounts(self) -> list:
        return [self.get(n) for n in self.names()]

    def create(self, name: str = None) -> Account:
        """Новая папка под очередной аккаунт."""
        if not name:
            used = self.names()
            i = 1
            while f"acc_{i:02d}" in used:
                i += 1
            name = f"acc_{i:02d}"
        acc = self.get(name)
        acc.ensure_dirs()
        acc.save()
        return acc

    def port_for(self, acc: Account) -> int:
        """Стабильный CDP-порт на аккаунт, чтобы окна не конфликтовали."""
        if acc.name not in self._ports:
            try:
                idx = self.names().index(acc.name)
            except ValueError:
                idx = len(self._ports)
            self._ports[acc.name] = self.port_base + idx
        return self._ports[acc.name]

    # ---------------------------------------------------------- ротация
    def pick(self) -> Account:
        """Свободный аккаунт, который дольше всех не использовали.

        Если все заморожены — всё равно отдаём того, у кого раньше всех
        кончится заморозка: пусть запрос встанет в очередь с понятной
        ошибкой, а не молча пропадёт.
        """
        accs = self.accounts()
        if not accs:
            return None
        free = [a for a in accs if a.free()]
        if free:
            return min(free, key=lambda a: a.state.get("last_used", 0))
        return min(accs, key=lambda a: a.freeze_left())

    def order(self) -> list:
        """Все аккаунты в порядке опроса: сначала свободные, потом resto."""
        accs = self.accounts()
        free = sorted([a for a in accs if a.free()],
                      key=lambda a: a.state.get("last_used", 0))
        busy = sorted([a for a in accs if not a.free()],
                      key=lambda a: a.freeze_left())
        return free + busy

    # ----------------------------------------------------------- отчёт
    def status_text(self) -> str:
        accs = self.accounts()
        if not accs:
            return ("<b>👥 Аккаунты ChatGPT</b>\n\n"
                    "Пока ни одного. Нажми «➕ Добавить аккаунт» — откроется "
                    "окно Chrome, залогинься там, и профиль сохранится.")
        free = sum(1 for a in accs if a.free())
        head = (f"<b>👥 Аккаунты ChatGPT</b>\n\n"
                f"Всего: {len(accs)}, свободно: {free}\n\n")
        return head + "\n".join(a.line() for a in accs)


def is_limit_error(text: str) -> bool:
    """Похоже ли сообщение об ошибке на лимит генераций."""
    t = (text or "").lower()
    return any(s in t for s in (
        "лимит", "limit", "reached your", "too many",
        "попробуйте позже", "try again later", "usage",
        " quotas", "rate limit",
    ))


def is_dead_error(text: str) -> bool:
    """Похоже ли на мёртвую сессию."""
    t = (text or "").lower()
    return any(s in t for s in (
        "сессия chatgpt истекла", "сессия истекла",
        "не залогинились", "куки протухли",
        "session has expired", "log in again",
    ))
