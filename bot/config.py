"""Загрузка config.yaml, .env и выученных параметров (data/learned_params.json)."""
import copy
import json
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOGS = ROOT / "logs"
DATA.mkdir(exist_ok=True)
LOGS.mkdir(exist_ok=True)
load_dotenv(ROOT / ".env")

# Параметры, которые бот имеет право менять сам в ходе самообучения, и их границы.
# Всё, чего здесь нет, меняется только человеком.
TUNABLE_BOUNDS = {
    "signals.entry_threshold": (0.40, 0.72),   # выше 0.72 бот перестаёт торговать вообще
    "signals.conviction_threshold": (0.70, 0.95),
    "signals.news_weight": (0.60, 1.50),
    "signals.ta_weight": (0.20, 1.00),
    "signals.positioning_weight": (0.10, 0.80),
    "signals.min_news_importance": (3, 7),
    "risk.stop_atr_mult": (1.0, 3.0),
    "risk.trail_after_r": (0.5, 2.0),
    "risk.max_hours_in_trade": (6, 96),
    "risk.risk_per_trade_pct.low": (0.1, 0.5),
    "risk.risk_per_trade_pct.medium": (0.3, 1.0),
    "risk.risk_per_trade_pct.high": (0.5, 2.0),
    "risk.risk_per_trade_pct.conviction": (1.0, 4.0),
}


def _get(d, path, default=None):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _set(d, path, value):
    parts = path.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


class Settings:
    def __init__(self):
        with open(ROOT / "config.yaml", encoding="utf-8") as f:
            self.base = yaml.safe_load(f)
        self.learned_path = DATA / "learned_params.json"
        self.learned = {}
        if self.learned_path.exists():
            try:
                self.learned = json.loads(self.learned_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.learned = {}
        self.cfg = self._merged()

    def _merged(self):
        cfg = copy.deepcopy(self.base)
        for k, v in self.learned.items():
            _set(cfg, k, v)
        return cfg

    def get(self, path, default=None):
        return _get(self.cfg, path, default)

    def set_learned(self, path, value):
        """Применить выученный параметр. Возвращает (ok, сообщение)."""
        if path not in TUNABLE_BOUNDS:
            return False, f"{path}: параметр не входит в список разрешённых для самообучения"
        lo, hi = TUNABLE_BOUNDS[path]
        try:
            value = float(value)
        except (TypeError, ValueError):
            return False, f"{path}: значение {value!r} не число"
        old = self.get(path)
        # один разбор не может сдвинуть параметр больше чем на 20% от текущего значения
        if old not in (None, 0):
            lim = abs(float(old)) * 0.2
            value = max(float(old) - lim, min(float(old) + lim, value))
        clamped = max(lo, min(hi, value))
        if isinstance(lo, int) and isinstance(hi, int):
            clamped = int(round(clamped))
        self.learned[path] = clamped
        self.learned_path.write_text(json.dumps(self.learned, indent=2, ensure_ascii=False), encoding="utf-8")
        self.cfg = self._merged()
        note = "" if clamped == value else f" (обрезано до границ {lo}..{hi})"
        return True, f"{path}: {old} -> {clamped}{note}"

    # --- окружение ---
    @property
    def mode(self):
        return os.getenv("BOT_MODE", "demo").strip().lower()

    @property
    def bybit_key(self):
        return os.getenv("BYBIT_API_KEY", "").strip()

    @property
    def bybit_secret(self):
        return os.getenv("BYBIT_API_SECRET", "").strip()

    @property
    def telegram_token(self):
        return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

    @property
    def telegram_chat_id(self):
        return os.getenv("TELEGRAM_CHAT_ID", "").strip()

    @property
    def has_anthropic(self):
        return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
