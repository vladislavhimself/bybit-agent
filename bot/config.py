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
    "signals.max_extension_atr": (1.5, 3.5),   # по теневым сделкам фильтра позднего входа
    "risk.breakeven_r": (0.5, 1.5),             # еженедельный перерасчёт на истории (bot/walkforward.py)
    "risk.symbol_cooldown_hours": (1, 24),
    "signals.range_shorts": (0, 1),
    "entry.pullback_atr": (0.2, 0.8),          # по теневым сделкам неисполненных откатов
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


MARKET_FILE = DATA / "market.txt"


def _deep_merge(dst, src):
    for k, v in (src or {}).items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)
    return dst


def active_market(default="crypto"):
    """Активный рынок: data/market.txt (меняется командой /market), иначе переменная MARKET, иначе из config.yaml."""
    try:
        v = MARKET_FILE.read_text(encoding="utf-8").strip().lower()
        if v:
            return v
    except OSError:
        pass
    return os.getenv("MARKET", "").strip().lower() or default


class Settings:
    def __init__(self):
        with open(ROOT / "config.yaml", encoding="utf-8") as f:
            self.base = yaml.safe_load(f)
        self.market = active_market(self.base.get("market", "crypto"))
        if self.market != "crypto":
            # настройки рынка накладываются на общие: универсум, источники, риск, валюта
            _deep_merge(self.base, (self.base.get("markets") or {}).get(self.market) or {})
        self.base["market"] = self.market
        suffix = "" if self.market == "crypto" else f"_{self.market}"
        self.db_path = DATA / f"bot{suffix}.db"
        self.learned_path = DATA / f"learned_params{suffix}.json"
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

    # Параметры, которые подбирает еженедельный перерасчёт на истории (bot/walkforward.py): разбору моделью по пяти
    # сделкам они закрыты — именно он опустил порог силы до 0.45 (PF 1.03 на 7 годах против 1.30 при 0.55)
    WF_OWNED = ("signals.entry_threshold", "signals.ta_weight", "risk.stop_atr_mult", "risk.breakeven_r",
                "signals.max_extension_atr", "risk.symbol_cooldown_hours", "signals.range_shorts", "risk.max_hours_in_trade")

    def set_learned(self, path, value, max_step=0.2, source="review"):
        """Применить выученный параметр. Возвращает (ok, сообщение)."""
        if path not in TUNABLE_BOUNDS:
            return False, f"{path}: параметр не входит в список разрешённых для самообучения"
        if source == "review" and path in self.WF_OWNED and (self.get("learning.walkforward") or {}).get("enabled"):
            return False, f"{path}: параметр подбирается еженедельным перерасчётом на истории, разбору не доступен"
        lo, hi = TUNABLE_BOUNDS[path]
        try:
            value = float(value)
        except (TypeError, ValueError):
            return False, f"{path}: значение {value!r} не число"
        old = self.get(path)
        # один разбор не может сдвинуть параметр больше чем на 20% от текущего значения
        if old not in (None, 0) and max_step:
            lim = abs(float(old)) * max_step
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

    def tinvest_token(self, mode):
        return os.getenv("TINVEST_TOKEN" if mode == "live" else "TINVEST_SANDBOX_TOKEN", "").strip()

    @property
    def has_anthropic(self):
        return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
