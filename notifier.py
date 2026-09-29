"""
notifier.py
텔레그램 알림
"""

import requests
import logging

logger = logging.getLogger(__name__)

# 텔레그램 설정: config.txt의 telegram_bot_token / telegram_chat_id 를 우선 사용
# (config.txt에 없을 때만 아래 값 사용 — 공개 저장소라면 여기엔 입력하지 마세요)
TELEGRAM_BOT_TOKEN = "XXXXXXXXXXXXXXXXXXXXXXXXX"
TELEGRAM_CHAT_ID   = "XXXXXXXXXX"


def get_telegram_settings() -> tuple:
    """(bot_token, chat_id) — config.txt 값 우선, 없으면 이 파일의 상수"""
    try:
        from config_manager import load_config
        config = load_config()
    except Exception:
        config = {}
    token = config.get("TELEGRAM_BOT_TOKEN") or TELEGRAM_BOT_TOKEN
    chat_id = config.get("TELEGRAM_CHAT_ID") or TELEGRAM_CHAT_ID
    return token, str(chat_id).strip()


def _send(message: str):
    """텔레그램 메시지 전송"""
    token, chat_id = get_telegram_settings()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=10)
    except Exception as e:
        logger.error(f"텔레그램 전송 오류: {e}")


def notify_buy(ticker: str, qty: int, price: float):
    """매수 알림"""
    _send(f"✅ 매수 {ticker} {qty}주 @ ${price:.2f}")


def notify_sell(ticker: str, qty: int, price: float):
    """매도 알림"""
    _send(f"✅ 매도 {ticker} {qty}주 @ ${price:.2f}")


def notify_monthly_increase(increases: dict):
    """월 증액 알림"""
    lines = ["📈 [GBV] 월 증액"]
    for ticker, (old, new) in increases.items():
        lines.append(f"{ticker}: ${old:,.2f} → ${new:,.2f}")
    _send("\n".join(lines))


def notify_cycle_complete(market: str, trades: list, holdings: dict,
                          cash: float, total: float, prices: dict,
                          outside_tqqq: int):
    """매매 완료 알림"""
    # 거래 내역
    trade_lines = []
    for t in trades:
        action = t["action"]
        ticker = t["ticker"]
        qty = t["qty"]
        price = t["price"]
        if market == "미국장":
            trade_lines.append(f"{action} {ticker} {qty}주 @ ${price:.2f}")
        else:
            trade_lines.append(f"{action} {ticker} {qty}주 @ ₩{int(price):,}")
    
    # 보유 현황
    holding_lines = []
    for ticker, info in holdings.items():
        qty = info["qty"]
        curr_price = prices.get(ticker, 0)
        value = qty * curr_price
        ratio = value / total * 100 if total > 0 else 0
        
        # TQQQ outside 포함
        if ticker == "TQQQ" and outside_tqqq > 0:
            total_qty = qty + outside_tqqq
            total_value = total_qty * curr_price
            ratio = total_value / total * 100 if total > 0 else 0
            if market == "미국장":
                holding_lines.append(
                    f"{ticker}: {total_qty}주 (${total_value:,.2f} / {ratio:.1f}%, outside={outside_tqqq})"
                )
        else:
            if market == "미국장":
                holding_lines.append(f"{ticker}: {qty}주 (${value:,.2f} / {ratio:.1f}%)")
            else:
                holding_lines.append(f"{ticker}: {qty}주 (₩{int(value):,} / {ratio:.1f}%)")
    
    cash_ratio = cash / total * 100 if total > 0 else 0
    
    message_parts = [
        f"🏁 [GBV] {market} 완료",
        "───────────"
    ]
    
    if trade_lines:
        message_parts.extend(trade_lines)
        message_parts.append("───────────")
    
    if market == "미국장":
        message_parts.append(f"달러잔고: ${cash:,.2f} ({cash_ratio:.1f}%)")
        message_parts.append(f"total: ${total:,.2f}")
    else:
        message_parts.append(f"원화잔고: ₩{int(cash):,} ({cash_ratio:.1f}%)")
        message_parts.append(f"total: ₩{int(total):,}")
    
    if holding_lines:
        message_parts.append("───────────")
        message_parts.extend(holding_lines)
    
    _send("\n".join(message_parts))


def notify_error(error_msg: str):
    """에러 알림"""
    _send(f"❌ [GBV] 에러\n{error_msg}")


def notify_config_changed(key: str, old_value: str, new_value: str):
    """설정 변경 알림"""
    _send(
        f"✅ [GBV] config 변경\n"
        f"키: {key}\n"
        f"이전: {old_value}\n"
        f"변경: {new_value}"
    )
