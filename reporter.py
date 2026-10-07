"""
reporter.py
매매 완료 후 CSV 리포트 저장
- 미국장/국내장 각각 하나의 파일에 누적 저장
- reports/미국장.csv, reports/국내장.csv
"""

import csv
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

REPORT_DIR = os.path.join(os.path.dirname(__file__), "reports")

# 가격 컬럼 이름이 "주문기준가"인 이유: 주문 API 는 접수만 돌려주고 체결가는 주지 않는다.
# 실제 체결 결과는 다음 잔고 조회의 평균단가(avg_price)에 반영되므로 그쪽을 손익 기준으로 쓴다.
HEADER = [
    "기록시각", "현금잔고", "총자산",
    "원화환산총자산", "환율", "TQQQ기준금",
    "매매구분", "종목", "수량", "주문기준가",
    "잔고종목", "보유수량", "평균단가", "현재가", "현재가치", "평가손익", "손익률",
]

def _ensure_dir():
    os.makedirs(REPORT_DIR, exist_ok=True)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _archive_if_old_layout(filename: str) -> bool:
    """컬럼 구성이 바뀌었으면 옛 파일을 따로 보관한다

    같은 파일에 컬럼 수가 다른 행을 이어 붙이면 열이 어긋나 읽기 어려워진다.
    Returns: 새로 헤더를 써야 하는지
    """
    if not os.path.exists(filename):
        return True
    try:
        with open(filename, newline="", encoding="utf-8-sig") as f:
            first = next(csv.reader(f), None)
    except OSError as e:
        logger.warning(f"리포트 헤더 확인 실패({filename}): {e}")
        return False
    if first == HEADER:
        return False
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    old = f"{os.path.splitext(filename)[0]}_{stamp}.csv"
    try:
        os.replace(filename, old)
        logger.info(f"리포트 컬럼이 바뀌어 옛 파일을 보관했습니다: {os.path.basename(old)}")
    except OSError as e:
        logger.warning(f"옛 리포트 보관 실패: {e}")
        return False
    return True


def save_report(market: str, trades: list, holdings: dict,
                cash: float, total_assets: float,
                tqqq_base: float, prices: dict, currency: str,
                total_assets_krw: float = 0.0, usd_krw_rate: float = 0.0):
    _ensure_dir()
    unit     = "$" if currency == "USD" else "₩"
    filename = os.path.join(REPORT_DIR, f"{market}.csv")
    need_header = _archive_if_old_layout(filename)

    with open(filename, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)

        if need_header:
            writer.writerow(HEADER)

        now_str    = _now()
        cash_fmt   = f"{cash:,.2f}"          if currency == "USD" else f"{int(cash):,}"
        assets_fmt = f"{total_assets:,.2f}"  if currency == "USD" else f"{int(total_assets):,}"
        krw_fmt    = f"{int(total_assets_krw):,}" if total_assets_krw > 0 else ""
        rate_fmt   = f"{usd_krw_rate:,.2f}"  if usd_krw_rate > 0 else ""
        base_fmt   = f"{tqqq_base:,.2f}"     if tqqq_base > 0 else ""

        # 매매 내역
        if trades:
            for i, t in enumerate(trades):
                price_fmt = f"{t['price']:,.2f}" if currency == "USD" else f"{int(t['price']):,}"
                head = ([now_str, cash_fmt, assets_fmt, krw_fmt, rate_fmt, base_fmt]
                        if i == 0 else [""] * 6)
                writer.writerow(head
                                + [t["action"], t["ticker"], t["qty"], price_fmt]
                                + [""] * 7)
        else:
            writer.writerow([now_str, cash_fmt, assets_fmt, krw_fmt, rate_fmt, base_fmt,
                             "거래없음", "", "", ""] + [""] * 7)

        # 잔고 현황 (평균단가가 있으면 평가손익까지)
        for ticker, info in holdings.items():
            qty   = info["qty"]
            curr  = prices.get(ticker, 0)
            avg   = info.get("avg_price") or 0
            value = qty * curr
            cost  = qty * avg
            money = (lambda v: f"{v:,.2f}") if currency == "USD" else (lambda v: f"{int(v):,}")
            pnl_fmt = rate_pnl = ""
            if avg > 0 and curr > 0:
                pnl_fmt  = f"{value - cost:+,.2f}" if currency == "USD" else f"{int(value - cost):+,}"
                rate_pnl = f"{(curr / avg - 1) * 100:+.2f}%"
            writer.writerow([""] * 10 + [
                ticker, qty, money(avg) if avg > 0 else "",
                money(curr), money(value), pnl_fmt, rate_pnl,
            ])

        # 구분선
        writer.writerow(["─" * 10] + [""] * (len(HEADER) - 1))

    logger.info(f"리포트 저장: {filename}")
    return filename
