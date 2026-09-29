"""
market_calendar.py
시장별 거래일(휴장일 제외) 판단

- 국내장(kr): 한국거래소 휴장일 (설·추석, 노동절, 연말 휴장일 등)
- 미국장(us): 뉴욕증권거래소 휴장일 (추수감사절, 독립기념일 등)

미국장 매매 시각(한국 저녁)은 미국 날짜로도 같은 날이므로 한국 날짜 그대로 판단한다.
holidays 패키지가 없으면 주말만 쉬는 것으로 판단한다 (기존 동작).
"""

import logging
from datetime import date

logger = logging.getLogger(__name__)

_EXCHANGES = {"kr": "XKRX", "us": "XNYS"}
_cache: dict = {}

try:
    import holidays as _holidays
except ImportError:          # pragma: no cover - 설치 안 된 환경용
    _holidays = None
    logger.warning("holidays 패키지가 없어 휴장일을 확인하지 못합니다 (pip install -r requirements.txt)")


def _holiday_set(market: str, year: int):
    if _holidays is None:
        return {}
    key = (market, year)
    if key not in _cache:
        _cache[key] = _holidays.financial_holidays(_EXCHANGES[market], years=year)
    return _cache[key]


def holiday_name(market: str, d: date = None) -> str:
    """휴장일이면 이름(예: '추석'), 아니면 빈 문자열"""
    if market not in _EXCHANGES:
        raise ValueError(f"market은 {tuple(_EXCHANGES)} 중 하나여야 합니다: {market!r}")
    d = d or date.today()
    return str(_holiday_set(market, d.year).get(d, "") or "")


def is_trading_day(market: str, d: date = None) -> bool:
    """주말·휴장일이 아니면 True"""
    d = d or date.today()
    if d.weekday() >= 5:
        return False
    return not holiday_name(market, d)

