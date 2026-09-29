"""
main.py
GBV 자동매매 봇 메인 실행

실행: python main.py
"""

import logging
import time
import os
import threading
from datetime import datetime

from config_manager import (
    load_config, get_market_times, get_trading_enabled
)
from broker import get_broker, validate_api_info, describe_broker
from strategy import run_us_strategy, run_kr_strategy
import trade_state
from market_calendar import is_trading_day, holiday_name
from notifier import notify_error, _send
from telegram_handler import start_polling

# ─────────────────────────────────────────
# 로그 설정
# ─────────────────────────────────────────
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.FileHandler(
            os.path.join(LOG_DIR, f"trade_{datetime.now().strftime('%Y%m%d')}.log"),
            encoding="utf-8"
        ),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

LOOP_INTERVAL_SEC = 30   # 매매 시간 체크 주기
TIME_TOLERANCE_MIN = 1   # 매매 시간 ±허용 범위


def _is_target_time(time_str: str, offset_min: int = 0) -> bool:
    """
    현재 시간이 목표 시간인지 확인
    offset_min: 양수면 미래, 음수면 과거 시간과 비교 (1시간 전 = -60)
    """
    now = datetime.now()
    h, m = map(int, time_str.strip().split(":"))
    now_min = now.hour * 60 + now.minute
    target_min = h * 60 + m + offset_min
    return abs(now_min - target_min) <= TIME_TOLERANCE_MIN


def _is_weekday() -> bool:
    """평일인지 확인"""
    return datetime.now().weekday() < 5


def _notify_pre_trade(api, market: str):
    """매매 1시간 전 계좌 현황 텔레그램 전송"""
    try:
        from config_manager import get_outside_tqqq
        
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        
        if market == "미국장":
            holdings, cash = api.get_us_balance()
            config = load_config()
            outside_tqqq = get_outside_tqqq(config)
            
            total = cash
            prices = {}
            for ticker, info in holdings.items():
                try:
                    price = api.get_us_price(ticker)
                    prices[ticker] = price
                    qty = info["qty"]
                    # TQQQ는 outside 포함
                    if ticker == "TQQQ" and outside_tqqq > 0:
                        total += (qty + outside_tqqq) * price
                    else:
                        total += qty * price
                except Exception:
                    pass
            
            cash_ratio = cash / total * 100 if total > 0 else 0
            holding_lines = []
            for ticker, info in holdings.items():
                price = prices.get(ticker, 0)
                qty = info["qty"]
                # TQQQ는 outside 포함
                if ticker == "TQQQ" and outside_tqqq > 0:
                    total_qty = qty + outside_tqqq
                    value = total_qty * price
                    ratio = value / total * 100 if total > 0 else 0
                    holding_lines.append(f"{ticker}: {total_qty}주 (${value:,.2f} / {ratio:.1f}%, outside={outside_tqqq})")
                else:
                    value = qty * price
                    ratio = value / total * 100 if total > 0 else 0
                    holding_lines.append(f"{ticker}: {qty}주 (${value:,.2f} / {ratio:.1f}%)")
            
            _send(
                f"⏰ [GBV] 미국장 매매 1시간 전\n"
                f"time: {now}\n"
                f"───────────\n"
                f"달러잔고: ${cash:,.2f} ({cash_ratio:.1f}%)\n"
                f"total: ${total:,.2f}\n"
                f"───────────\n"
                + "\n".join(holding_lines or ["no holdings"])
            )
            
        elif market == "국내장":
            holdings, cash = api.get_kr_balance()
            total = cash
            prices = {}
            for ticker, info in holdings.items():
                try:
                    price = api.get_kr_price(ticker)
                    prices[ticker] = price
                    total += info["qty"] * price
                except Exception:
                    pass
            
            cash_ratio = cash / total * 100 if total > 0 else 0
            holding_lines = []
            for ticker, info in holdings.items():
                price = prices.get(ticker, 0)
                name = api.get_kr_name(ticker)
                qty = info["qty"]
                value = qty * price
                ratio = value / total * 100 if total > 0 else 0
                holding_lines.append(f"{name}({ticker}): {qty}주 (₩{int(value):,} / {ratio:.1f}%)")
            
            _send(
                f"⏰ [GBV] 국내장 매매 1시간 전\n"
                f"time: {now}\n"
                f"───────────\n"
                f"원화잔고: ₩{int(cash):,} ({cash_ratio:.1f}%)\n"
                f"total: ₩{int(total):,}\n"
                f"───────────\n"
                + "\n".join(holding_lines or ["no holdings"])
            )
        
        logger.info(f"{market} 매매 1시간 전 현황 전송 완료")
        
    except Exception as e:
        logger.error(f"매매 전 현황 전송 실패: {e}", exc_info=True)


def main():
    logger.info("=" * 60)
    logger.info("  GBV 자동매매 봇 시작")
    logger.info("=" * 60)
    
    # ── config 및 API 초기화 ──
    config = load_config()
    error = validate_api_info(config)
    if error:
        logger.error(error)
        return
    
    api = get_broker(config)
    logger.info(f"{describe_broker(config)} 초기화 완료")
    
    # ── 텔레그램 백그라운드 시작 ──
    stop_event = threading.Event()
    telegram_thread = threading.Thread(
        target=start_polling,
        args=(stop_event,),
        daemon=True
    )
    telegram_thread.start()
    logger.info("텔레그램 폴링 시작")
    
    # ── 매매 플래그 및 설정 ──
    # '오늘 매매했는지'는 trade_state.json 파일에 기록 (재시작해도 유지)
    for market, label in (("us", "미국장"), ("kr", "국내장")):
        if trade_state.traded_today(market):
            logger.info(f"{label}: 오늘 이미 매매함 (trade_state.json) → 오늘은 다시 매매하지 않음")
    us_pre_notified_today = None
    kr_pre_notified_today = None
    holiday_notified = {}     # {market: 날짜} 휴장 안내는 하루 한 번만
    prev_us_time = None
    prev_kr_time = None
    
    logger.info("매매 대기 중...")
    
    try:
        while True:
            # ── 평일 체크 ──
            if not _is_weekday():
                time.sleep(LOOP_INTERVAL_SEC)
                continue
            
            # ── 날짜 체크 ──
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            
            # ── config 재로드 ──
            config = load_config()
            trading_enabled = get_trading_enabled(config)
            us_time, kr_time = get_market_times(config)
            
            # ── 매매 시간 변경 감지 ──
            if prev_us_time and us_time != prev_us_time:
                trade_state.clear_today("us")
                logger.info(f"미국장 매매 시간 변경: {prev_us_time} → {us_time} (플래그 리셋)")
                _send(
                    f"⏰ [GBV] 매매 시간 변경\n"
                    f"미국장: {prev_us_time} → {us_time}\n"
                    f"※ 매매 플래그 리셋됨"
                )
            
            if prev_kr_time and kr_time != prev_kr_time:
                trade_state.clear_today("kr")
                logger.info(f"국내장 매매 시간 변경: {prev_kr_time} → {kr_time} (플래그 리셋)")
                _send(
                    f"⏰ [GBV] 매매 시간 변경\n"
                    f"국내장: {prev_kr_time} → {kr_time}\n"
                    f"※ 매매 플래그 리셋됨"
                )
            
            prev_us_time = us_time
            prev_kr_time = kr_time
            
            # ── 매매 비활성화 체크 ──
            if not trading_enabled:
                time.sleep(LOOP_INTERVAL_SEC)
                continue
            
            # ── 휴장일 확인 (거래소 휴장일이면 그 시장은 매매하지 않음) ──
            us_open = is_trading_day("us")
            kr_open = is_trading_day("kr")
            for market, label, open_, t in (("us", "미국장", us_open, us_time),
                                            ("kr", "국내장", kr_open, kr_time)):
                if not open_ and _is_target_time(t) and holiday_notified.get(market) != today_str:
                    name = holiday_name(market)
                    logger.info(f"{label} 휴장일({name}) → 매매하지 않음")
                    _send(f"🏖 [GBV] 오늘은 {label} 휴장일({name})이라 매매하지 않습니다.")
                    holiday_notified[market] = today_str
            
            # ── 미국장 매매 1시간 전 알림 ──
            if us_open and _is_target_time(us_time, offset_min=-60) and us_pre_notified_today != today_str:
                logger.info("미국장 매매 1시간 전 현황 전송")
                _notify_pre_trade(api, "미국장")
                us_pre_notified_today = today_str
            
            # ── 국내장 매매 1시간 전 알림 ──
            if kr_open and _is_target_time(kr_time, offset_min=-60) and kr_pre_notified_today != today_str:
                logger.info("국내장 매매 1시간 전 현황 전송")
                _notify_pre_trade(api, "국내장")
                kr_pre_notified_today = today_str
            
            # ── 미국장 매매 ──
            if us_open and _is_target_time(us_time) and not trade_state.traded_today("us"):
                logger.info(f"미국장 매매 시간 도달: {us_time}")
                try:
                    run_us_strategy(api)
                    trade_state.mark_traded("us")
                except Exception as e:
                    logger.error(f"미국장 매매 중 에러: {e}", exc_info=True)
                    notify_error(f"미국장 매매 에러:\n{str(e)}")
            
            # ── 국내장 매매 ──
            if kr_open and _is_target_time(kr_time) and not trade_state.traded_today("kr"):
                logger.info(f"국내장 매매 시간 도달: {kr_time}")
                try:
                    run_kr_strategy(api)
                    trade_state.mark_traded("kr")
                except Exception as e:
                    logger.error(f"국내장 매매 중 에러: {e}", exc_info=True)
                    notify_error(f"국내장 매매 에러:\n{str(e)}")
            
            time.sleep(LOOP_INTERVAL_SEC)
    
    except KeyboardInterrupt:
        logger.info("사용자 종료 요청")
        stop_event.set()
        telegram_thread.join(timeout=5)
        logger.info("프로그램 종료")


if __name__ == "__main__":
    main()
