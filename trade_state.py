"""
trade_state.py
시장별 '오늘 매매했음' 기록을 파일에 저장

봇을 매매 시간 중에 재시작해도 같은 날 같은 시장을 다시 매매하지 않도록
메모리 플래그 대신 파일(trade_state.json)에 날짜를 남긴다.

형식: {"us": "2026-09-29", "kr": "2026-09-29"}
"""

import json
import logging
import os
import threading
from datetime import date

logger = logging.getLogger(__name__)

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_state.json")
MARKETS = ("us", "kr")

_lock = threading.Lock()


def _load() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as e:
        logger.error(f"매매 기록 파일 읽기 실패 ({STATE_FILE}): {e}")
        return {}


def _save(data: dict):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, STATE_FILE)   # 쓰는 도중 꺼져도 파일이 깨지지 않게


def _check(market: str) -> str:
    if market not in MARKETS:
        raise ValueError(f"market은 {MARKETS} 중 하나여야 합니다: {market!r}")
    return market


def traded_today(market: str) -> bool:
    """오늘 이 시장 매매(주문 시도 포함)를 했는지"""
    with _lock:
        return _load().get(_check(market)) == date.today().isoformat()


def mark_traded(market: str):
    """오늘 이 시장 매매했음을 기록 (주문 직전·사이클 종료 시 호출)"""
    with _lock:
        data = _load()
        today = date.today().isoformat()
        if data.get(_check(market)) == today:
            return
        data[market] = today
        try:
            _save(data)
        except OSError as e:
            logger.error(f"매매 기록 파일 저장 실패 ({STATE_FILE}): {e}")


def clear_today(market: str):
    """오늘 기록 삭제 (매매 시간을 바꿔 같은 날 다시 매매하려는 경우)"""
    with _lock:
        data = _load()
        if data.pop(_check(market), None) is not None:
            try:
                _save(data)
            except OSError as e:
                logger.error(f"매매 기록 파일 저장 실패 ({STATE_FILE}): {e}")
