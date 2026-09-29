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


def _fmt_price(price: float, is_us: bool) -> str:
    return f"${price:,.2f}" if is_us else f"₩{int(price):,}"


def notify_buy(ticker: str, qty: int, price: float, is_us: bool = True):
    """매수 주문 접수 알림"""
    _send(f"✅ 매수 주문 {ticker} {qty}주 @ {_fmt_price(price, is_us)}")


def notify_sell(ticker: str, qty: int, price: float, is_us: bool = True):
    """매도 주문 접수 알림"""
    _send(f"✅ 매도 주문 {ticker} {qty}주 @ {_fmt_price(price, is_us)}")


def notify_order_failed(action: str, ticker: str, qty: int, price: float, is_us: bool, reason: str = ""):
    """주문 거절·실패 알림"""
    _send(
        f"❌ [GBV] {action} 주문 실패 {ticker} {qty}주 @ {_fmt_price(price, is_us)}\n"
        f"사유: {reason or '알 수 없음 (로그 확인)'}\n"
        f"※ 오늘은 다시 주문하지 않습니다. 잔고·주문가능금액을 확인하세요."
    )


def notify_monthly_increase(increases: dict, is_us: bool = True):
    """월 증액 알림"""
    lines = ["📈 [GBV] 월 증액"]
    for ticker, (old, new) in increases.items():
        lines.append(f"{ticker}: {_fmt_price(old, is_us)} → {_fmt_price(new, is_us)}")
    _send("\n".join(lines))


def notify_cycle_complete(market: str, trades: list, holdings: dict,
                          cash: float, total: float, prices: dict,
                          outside_tqqq: int):
    """매매 완료 알림 (거래가 없으면 보내지 않는다)

    거래가 한 건도 없으면 잔고·보유 현황만 적힌 '완료' 메시지가 되는데,
    매일 같은 내용이라 알림으로서 쓸모가 없고 진짜 체결 알림을 묻는다.
    계좌가 비어 있는 경우(잔고 0·보유 없음)도 자연히 여기에 걸린다.
    사이클이 돌았다는 사실은 로그에 남으므로 확인에는 지장이 없다.
    """
    if not trades:
        logger.info(f"{market} 거래 없음 → 완료 알림 생략")
        return

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
