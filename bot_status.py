"""
bot_status.py
텔레그램 /health · /log 와 매일 생존 신호에 쓰는 봇 상태 정보

- 봇 시작 시각 (가동 시간)
- 마지막 오류 로그 (ERROR 이상, 로그 핸들러로 수집)
- 시장별 오늘 매매 여부와 다음 매매 시각
- 최근 로그 파일 끝부분
"""

import glob
import logging
import os
import re
import threading
from collections import deque
from datetime import date, datetime, timedelta

import log_redact

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
TELEGRAM_LIMIT = 3900          # 텔레그램 메시지 최대 4096자 (여유분 제외)

_started_at = datetime.now()
_lock = threading.Lock()
_errors = deque(maxlen=5)      # (시각, 메시지)


def mark_started():
    global _started_at
    _started_at = datetime.now()


def uptime_text(now: datetime = None) -> str:
    delta = (now or datetime.now()) - _started_at
    days, rem = divmod(int(delta.total_seconds()), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    text = f"{hours}시간 {minutes}분"
    return f"{days}일 {text}" if days else text


class LastErrorHandler(logging.Handler):
    """ERROR 이상 로그를 최근 5개까지 기억"""

    def __init__(self):
        super().__init__(level=logging.ERROR)

    def emit(self, record):
        try:
            # 포매터를 거치지 않는 경로라 여기서 따로 가린다. 안 그러면
            # 폴링 오류에 섞인 봇 토큰이 /health 응답으로 텔레그램에 실려 나간다.
            message = log_redact.redact(record.getMessage()).splitlines()[0][:300]
            with _lock:
                _errors.append((datetime.fromtimestamp(record.created), message))
        except Exception:
            pass


def install_error_handler():
    root = logging.getLogger()
    if not any(isinstance(h, LastErrorHandler) for h in root.handlers):
        root.addHandler(LastErrorHandler())


def last_error():
    with _lock:
        return _errors[-1] if _errors else None


def clear_errors():
    with _lock:
        _errors.clear()


# ─────────────────────────────────────────
# 다음 매매 시각
# ─────────────────────────────────────────

def next_trade_time(market: str, time_str: str, traded_today: bool, now: datetime = None):
    """다음 매매 예정 시각 (datetime) — 휴장일·주말은 건너뜀. 14일 안에 없으면 None"""
    from market_calendar import is_trading_day
    now = now or datetime.now()
    try:
        h, m = map(int, time_str.strip().split(":"))
    except ValueError:
        return None
    for offset in range(15):
        d = now.date() + timedelta(days=offset)
        if not is_trading_day(market, d):
            continue
        at = datetime(d.year, d.month, d.day, h, m)
        if offset == 0 and (traded_today or now > at + timedelta(minutes=1)):
            continue
        return at
    return None


def _when_text(at: datetime, now: datetime) -> str:
    if at is None:
        return "없음 (2주 안에 거래일 없음)"
    days = (at.date() - now.date()).days
    label = {0: "오늘", 1: "내일"}.get(days, at.strftime("%m/%d"))
    weekday = "월화수목금토일"[at.weekday()]
    return f"{label}({weekday}) {at.strftime('%H:%M')}"


# ─────────────────────────────────────────
# /health 메시지
# ─────────────────────────────────────────

def health_text(now: datetime = None, title: str = "🩺 [GBV] 봇 상태") -> str:
    from config_manager import (load_config, get_market_times, get_trading_enabled,
                                get_kiwoom_mode, get_dry_run, get_all_us_tickers,
                                get_all_kr_tickers)
    from market_calendar import holiday_name
    import trade_state

    now = now or datetime.now()
    config = load_config()
    us_time, kr_time = get_market_times(config)
    mode = get_kiwoom_mode(config)

    lines = [
        title,
        f"time: {now.strftime('%Y-%m-%d %H:%M')}",
        f"가동 시간: {uptime_text(now)} (시작 {_started_at.strftime('%m/%d %H:%M')})",
        f"모드: {'실전' if mode == 'real' else '모의투자'}"
        + (" / 연습(dry_run)" if get_dry_run(config) else ""),
        f"매매: {'켜짐' if get_trading_enabled(config) else '꺼짐 (trading_enabled=false)'}",
        "───────────",
    ]

    for market, label, t, tickers in (
        ("us", "미국장", us_time, get_all_us_tickers(config)),
        ("kr", "국내장", kr_time, get_all_kr_tickers(config)),
    ):
        if not t:
            lines.append(f"{label}: 매매 시각 미설정 → 매매 안 함")
            continue
        if not tickers:
            lines.append(f"{label}: 종목 없음")
            continue
        done = trade_state.traded_today(market)
        holiday = holiday_name(market, now.date()) or ("주말" if now.weekday() >= 5 else "")
        today = "✅ 오늘 매매함" if done else (f"🏖 오늘 휴장({holiday})" if holiday else "⏳ 오늘 아직")
        nxt = _when_text(next_trade_time(market, t, done, now), now)
        lines.append(f"{label}: {today} / 다음 {nxt}")

    lines.append("───────────")
    err = last_error()
    if err:
        at, msg = err
        lines.append(f"마지막 오류: {at.strftime('%m/%d %H:%M')}\n{msg}")
    else:
        lines.append("마지막 오류: 없음 ✅")
    return "\n".join(lines)


# ─────────────────────────────────────────
# /log
# ─────────────────────────────────────────

def latest_log_file():
    files = glob.glob(os.path.join(LOG_DIR, "trade_*.log"))
    return max(files, key=os.path.getmtime) if files else None


# "2026-09-30 01:08:44,455 [INFO] kiwoom_api - x"        → "01:08:44 I kiwoom_api x"
# "2026-09-30 02:55:37,183 [INFO] [10808] main - x"      → "02:55:37 I main x"
# PID 는 로그 파일에만 두고 텔레그램에는 안 보낸다. 길이 제한이 빠듯하다.
_LOG_LINE = re.compile(
    r"^\d{4}-\d\d-\d\d "        # 날짜 (버림)
    r"(\d\d:\d\d:\d\d),\d+ "  # 시각
    r"\[(\w+)\] "                 # 레벨
    r"(?:\[\d+\] )?"              # PID (옛 로그에는 없다)
    r"([\w.]+) - "                  # 모듈
    r"(.*)$"                         # 메시지
)


def tail_log(lines: int = 20, errors_only: bool = False) -> str:
    """최근 로그 파일 끝부분 (텔레그램 길이 제한에 맞춰 자름)"""
    path = latest_log_file()
    if not path:
        return "로그 파일이 없습니다"
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        rows = f.read().splitlines()
    if errors_only:
        rows = [r for r in rows if "[ERROR]" in r or "[WARNING]" in r or "[CRITICAL]" in r]
    picked = rows[-lines:] if lines > 0 else []
    short = []
    for r in picked:
        m = _LOG_LINE.match(r)
        if m:
            clock, level, module, msg = m.groups()
            short.append(f"{clock} {level[:1]} {module} {msg}")
        else:
            short.append(r)          # 트레이스백 등 형식이 다른 줄은 그대로
    header = f"📄 {os.path.basename(path)} 최근 {len(short)}줄" + (" (경고·오류만)" if errors_only else "")
    body = "\n".join(short) or "(해당 로그 없음)"
    if len(header) + len(body) + 1 > TELEGRAM_LIMIT:
        body = "…\n" + body[-(TELEGRAM_LIMIT - len(header) - 3):]
    return header + "\n" + body
