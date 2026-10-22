"""
shadow_trader.py
AI 그림자 트레이더 — Claude 가 매일 매매 판단을 내리되, 종이 위에서만 체결한다

왜 만들었나
  "AI 에게 매매를 맡기면 더 낫지 않을까"에 답하는 유일한 정직한 방법은 **돈을 걸지 않고
  나란히 달려 보는 것**이다. 규칙 봇과 같은 정보(그날 봇이 기록한 시세), 같은 체결 가정
  (측정된 슬리피지)으로 가상 포트폴리오를 굴려, 몇 달 뒤 수익률·낙폭을 숫자로 비교한다.

안전장치
  - 주문 API 를 import 조차 하지 않는다 (tests/test_ai_lab.py 의 SafetyBeltTest 가 강제).
  - 봇의 설정·잔고·주문에 손대지 않는다. 쓰는 것은 shadow/ 아래 자기 파일뿐이다.
  - 졸업 기준(GRADUATION)을 통과해도 자동으로 하는 일은 없다. 사람이 읽고 정한다.
  - 현금 이상으로 사지 못하고, 보유 이상으로 팔지 못한다 (sanitize).

타이밍
  봇은 22:29 KST 에 그날 시세를 읽고 주문한다. 이 스크립트는 22:35 에 돌며 봇 로그에서
  그 시세를 읽어 **같은 가격**으로 가상 체결한다. 정보 우위도 불리도 없다.

실행
  python shadow_trader.py            오늘 판단 → 가상 체결 → 텔레그램 1줄
  python shadow_trader.py --stdout   텔레그램 대신 화면
  python shadow_trader.py --report   지금까지의 봇 vs 그림자 비교표
  python shadow_trader.py --dry-run  Claude 호출 없이 자료·상태만 확인 (관망으로 기록하지 않음)
"""

import argparse
import csv
import glob
import io
import json
import logging
import os
import pickle
import re
import sys
import time
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import log_redact  # noqa: E402
from ai_client import call_structured, text_of  # noqa: E402
from weekly_audit import load_env  # noqa: E402
from market_calendar import is_trading_day  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, AttributeError):
            pass

SHADOW_DIR  = os.path.join(HERE, "shadow")
STATE_FILE  = os.path.join(SHADOW_DIR, "state.json")
DECISIONS   = os.path.join(SHADOW_DIR, "decisions.csv")
PX_CACHE    = os.path.join(SHADOW_DIR, "px_cache.pkl")
LOG_DIR     = os.path.join(HERE, "logs")
REPORT_DIR  = os.path.join(HERE, "reports")
SHADOW_LOG  = os.path.join(LOG_DIR, "shadow.log")

TICKER        = "TQQQ"
DEFAULT_MODEL = "claude-opus-5"
SLIPPAGE      = 0.003        # 봇이 실측한 실효 슬리피지(수수료 포함) 평균 ≈ +0.3%
HISTORY_DAYS  = 90           # 모델에 보여 주는 일봉 수
TELEGRAM_LIMIT = 3_900

# 졸업 기준 — 통과해도 자동으로 돈을 옮기지 않는다
GRADUATION = {
    "min_days": 60,            # 거래일 수
    "beat_return_by_pt": 0.0,  # 그림자 누적수익률 − 봇 누적수익률 (%p)
    "max_mdd_worse_pt": 0.0,   # 그림자 MDD 가 봇보다 이만큼 넘게 나쁘면 탈락 (0 = 나쁘면 안 됨)
}

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "action":     {"type": "string", "enum": ["buy", "sell", "hold"]},
        "quantity":   {"type": "integer"},
        "confidence": {"type": "number"},
        "regime":     {"type": "string", "enum": ["bull", "bear", "range"]},
        "rationale":  {"type": "string"},
    },
    "required": ["action", "quantity", "confidence", "regime", "rationale"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """당신은 TQQQ(나스닥100 3배 레버리지 ETF) 하나만 거래하는 가상 계좌의 운용자다.
하루에 한 번, 미국 정규장 개장 직후 시세로 매수·매도·관망 중 하나를 정한다.

규칙
- 현금 범위 안에서만 사고, 보유 수량 안에서만 판다. 공매도·레버리지 추가 없음.
- 체결은 제시된 시세에 약 0.3% 비용이 붙는다(매수는 비싸게, 매도는 싸게).
- 판단 근거는 주어진 자료(가격 이력·파생 지표·계좌 상태)로만 세운다. 모르는 것은 모른다고 한다.
- rationale 은 한국어 3줄 이내, 숫자를 인용한다. regime 은 현재 국면에 대한 당신의 판단이다.
- quantity 는 주 단위 정수(0 이상). hold 면 0. confidence 는 0~1 사이 소수.

목표는 몇 달에 걸친 누적수익률과 최대낙폭이다. 매일 거래할 필요는 없다."""

logger = logging.getLogger("shadow_trader")


# ─────────────────────────────────────────
# 자료
# ─────────────────────────────────────────

def bot_snapshot() -> dict:
    """봇의 최신 리포트 행 (현금·보유·평단·총자산·날짜). 없으면 빈 dict"""
    path = os.path.join(REPORT_DIR, "미국장.csv")
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.reader(f) if any(c.strip() for c in r)]
    if len(rows) < 2:
        return {}
    h = rows[0]
    ix = {k: h.index(k) for k in h}
    num = lambda s: float(s.replace(",", "")) if s and s.strip() else 0.0
    snap, last_head = {}, None
    for r in rows[1:]:
        if r[0].startswith("─"):
            continue
        if r[ix["기록시각"]]:
            last_head = r
            snap.update(date=r[ix["기록시각"]][:10], cash=num(r[ix["현금잔고"]]),
                        total=num(r[ix["총자산"]]))
        elif r[ix["잔고종목"]] == TICKER:
            snap.update(qty=int(num(r[ix["보유수량"]])), avg=num(r[ix["평균단가"]]),
                        price=num(r[ix["현재가"]]))
    if last_head is not None and "qty" not in snap:
        snap.update(qty=0, avg=0.0)
    return snap


def todays_bot_price(today: date = None) -> tuple:
    """봇이 오늘 22:29 에 기록한 시세. (가격, 출처) — 없으면 (None, '')"""
    today = today or date.today()
    path = os.path.join(LOG_DIR, f"trade_{today:%Y%m%d}.log")
    if not os.path.exists(path):
        return None, ""
    pat = re.compile(rf"{TICKER} 현재가: \$([0-9,]+\.\d+)")
    found = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = pat.search(line)
            if m:
                found = float(m.group(1).replace(",", ""))
    return (found, "bot-log") if found else (None, "")


def wait_for_bot_price(today: date, timeout_sec: int, poll_sec: int = 30) -> tuple:
    """봇이 아직 시세를 못 적었으면(RC9002 재시도 등) 잠시 기다린다. 끝까지 없으면 (None, "")"""
    deadline = time.monotonic() + max(0, timeout_sec)
    price, source = todays_bot_price(today)
    if price is None and timeout_sec > 0:
        logger.info(f"봇 로그에 오늘 시세가 아직 없음 — 최대 {timeout_sec}초 대기")
    while price is None and time.monotonic() < deadline:
        time.sleep(poll_sec)
        price, source = todays_bot_price(today)
    return price, source


def seconds_until_bot_cycle(today: date, now: datetime = None, after_min: int = 5, cap_min: int = 90) -> int:
    """봇의 오늘 미국장 매매 시각(ET→KST 환산, 서머타임 반영) + after_min 분까지 남은 초.

    작업 스케줄러는 KST 고정 시각에만 깨울 수 있어 11월 서머타임 해제 뒤(23:30)에도 맞도록
    여기서 기다린다. 지났거나 알 수 없으면 0, 최대 cap_min 분."""
    now = now or datetime.now()
    if today != now.date():
        return 0
    try:
        import config_manager as cm
        us, _ = cm.get_market_times(cm.load_config())
    except Exception as e:
        logger.warning(f"매매 시각을 읽지 못해 바로 진행: {e}")
        return 0
    if not us:
        return 0
    hh, mm = (int(x) for x in us.split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(minutes=after_min)
    return int(max(0, min((target - now).total_seconds(), cap_min * 60)))


def live_price_fallback() -> tuple:
    """봇 시세가 끝내 없을 때: yfinance 1분봉 마지막 값. 봇과 다른 가격이므로 출처를 남긴다"""
    try:
        import yfinance as yf
        d = yf.download(TICKER, period="1d", interval="1m", progress=False, auto_adjust=True)
        s = d["Close"].squeeze().dropna()
        if len(s):
            return float(s.iloc[-1]), "yfinance-1m"
    except Exception as e:
        logger.warning(f"1분봉 조회 실패: {e}")
    return None, ""


def price_history(days: int = HISTORY_DAYS, today: date = None):
    """TQQQ·QQQ 일봉 종가 (캐시는 하루). 네트워크 실패 시 캐시라도 돌려준다"""
    import pandas as pd
    today = today or date.today()
    os.makedirs(SHADOW_DIR, exist_ok=True)
    if os.path.exists(PX_CACHE):
        with open(PX_CACHE, "rb") as f:
            cached = pickle.load(f)
        if cached.get("stamp") == today.isoformat():
            return cached["px"]
    try:
        import yfinance as yf
        px = {}
        for t in (TICKER, "QQQ"):
            d = yf.download(t, period="6mo", interval="1d", progress=False, auto_adjust=True)
            px[t] = d["Close"].squeeze().dropna()
        with open(PX_CACHE, "wb") as f:
            pickle.dump({"stamp": today.isoformat(), "px": px}, f)
        return px
    except Exception as e:
        logger.warning(f"시세 이력 조회 실패: {e}")
        if os.path.exists(PX_CACHE):
            with open(PX_CACHE, "rb") as f:
                return pickle.load(f)["px"]
        return {TICKER: pd.Series(dtype=float), "QQQ": pd.Series(dtype=float)}


def derived_stats(series) -> dict:
    """모델이 계산 실수를 하지 않도록 파생 지표는 코드가 준다"""
    s = series.dropna()
    if len(s) < 5:
        return {}
    last = float(s.iloc[-1])
    out = {"last": last}
    for n in (5, 20, 60):
        if len(s) > n:
            out[f"ret_{n}d_pct"] = round((last / float(s.iloc[-1 - n]) - 1) * 100, 2)
    hi = float(s.max())
    out["drawdown_from_high_pct"] = round((last / hi - 1) * 100, 2)
    r = s.pct_change().dropna()
    if len(r) >= 20:
        out["vol_20d_annual_pct"] = round(float(r.tail(20).std()) * (252 ** 0.5) * 100, 1)
    if len(s) >= 20:
        out["ma20"] = round(float(s.tail(20).mean()), 2)
    if len(s) >= 60:
        out["ma60"] = round(float(s.tail(60).mean()), 2)
    return out


# ─────────────────────────────────────────
# 상태
# ─────────────────────────────────────────

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    os.makedirs(SHADOW_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def init_state_from_bot(snap: dict, today: date) -> dict:
    """첫 실행: 봇의 현재 계좌를 그대로 복사해 같은 출발선에서 시작한다"""
    if not snap or "cash" not in snap:
        raise RuntimeError("봇 리포트(reports/미국장.csv)가 없어 출발선을 잡을 수 없습니다")
    total = snap["cash"] + snap.get("qty", 0) * snap.get("price", 0.0)
    return {
        "start_date": today.isoformat(),
        "start_total": total,
        "bot_start_total": total,
        "cash": snap["cash"],
        "qty": snap.get("qty", 0),
        "avg": snap.get("avg", 0.0),
        "peak_total": total,
        "max_dd_pct": 0.0,
        "days": 0,
        "last_date": None,
    }


# ─────────────────────────────────────────
# 판단
# ─────────────────────────────────────────

def build_user_message(state: dict, price: float, source: str, px: dict, today: date) -> str:
    import pandas as pd
    tq = px.get(TICKER)
    qq = px.get("QQQ")
    if tq is not None and len(tq):
        last = tq.index[-1]
        last = last.date() if hasattr(last, "date") else last
        if last < today:                       # 이력은 전날 종가까지 — 오늘 시세를 끝에 붙여 지표를 맞춘다
            tq = pd.concat([tq, pd.Series([price], index=[pd.Timestamp(today)])])
    lines = [f"# {today} 판단 자료", "",
             f"## 오늘 시세 (미국 정규장 개장 직후, 출처 {source}): {TICKER} ${price:.2f}", ""]
    if tq is not None and len(tq):
        lines.append(f"## {TICKER} 파생 지표: {json.dumps(derived_stats(tq), ensure_ascii=False)}")
    if qq is not None and len(qq):
        lines.append(f"## QQQ 파생 지표: {json.dumps(derived_stats(qq), ensure_ascii=False)}")
    if tq is not None and len(tq):
        lines.append(f"## 최근 {min(HISTORY_DAYS, len(tq))}일 {TICKER} 종가 (오래된 → 최근, 마지막은 오늘 시세)")
        tail = tq.tail(HISTORY_DAYS)
        lines.append(", ".join(f"{d.date()}:{float(v):.2f}" for d, v in tail.items()))
    pos_val = state["qty"] * price
    total = state["cash"] + pos_val
    lines += ["", "## 가상 계좌",
              f"현금 ${state['cash']:,.2f} / 보유 {state['qty']}주 (평단 ${state['avg']:.2f}, "
              f"평가 ${pos_val:,.2f}) / 총자산 ${total:,.2f}",
              f"출발 총자산 ${state['start_total']:,.2f} ({state['start_date']}부터 {state['days']}거래일), "
              f"지금까지 {(total / state['start_total'] - 1) * 100:+.2f}%, 최대낙폭 {state['max_dd_pct']:.2f}%",
              f"오늘 매수 가능 최대 {max_affordable(state['cash'], price)}주 / 매도 가능 최대 {state['qty']}주",
              "", "오늘의 판단을 JSON 으로 답하라."]
    return "\n".join(lines)


def max_affordable(cash: float, price: float) -> int:
    return max(0, int(cash / (price * (1 + SLIPPAGE)))) if price > 0 else 0


def sanitize(decision: dict, state: dict, price: float) -> dict:
    """모델 출력을 계좌 제약 안으로 눌러 넣는다. 이상하면 관망"""
    d = dict(decision or {})
    action = str(d.get("action", "hold")).lower()
    try:
        qty = int(d.get("quantity", 0))
    except (TypeError, ValueError):
        qty = 0
    try:
        conf = min(1.0, max(0.0, float(d.get("confidence", 0))))
    except (TypeError, ValueError):
        conf = 0.0
    regime = d.get("regime") if d.get("regime") in ("bull", "bear", "range") else "range"
    rationale = str(d.get("rationale", ""))[:300]
    if action == "buy":
        qty = min(qty, max_affordable(state["cash"], price))
    elif action == "sell":
        qty = min(qty, int(state["qty"]))
    else:
        action, qty = "hold", 0
    if qty <= 0 and action != "hold":
        action, qty = "hold", 0
    return {"action": action, "quantity": qty, "confidence": conf,
            "regime": regime, "rationale": rationale}


def _extract_json(text: str) -> dict:
    """구조화 출력이면 본문이 JSON 그 자체. 아니면 첫 { } 블록을 찾는다"""
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("응답에 JSON 이 없습니다")
    return json.loads(m.group(0))


def decide(client, user_message: str, model: str = DEFAULT_MODEL) -> dict:
    """Claude 에게 판단을 받는다. 거부·오류면 관망"""
    kwargs = dict(
        model=model, max_tokens=4096,
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_message}],
        output_config={"format": {"type": "json_schema", "schema": DECISION_SCHEMA}},
    )
    msg = call_structured(client, kwargs, "\n\n반드시 JSON 객체 하나만 출력하라.")
    if msg.stop_reason == "refusal":
        return {"action": "hold", "quantity": 0, "confidence": 0.0, "regime": "range",
                "rationale": "모델이 응답을 거부해 관망"}
    text = text_of(msg)
    u = msg.usage
    logger.info(f"model={msg.model} in={u.input_tokens} out={u.output_tokens} "
                f"cache_read={getattr(u, 'cache_read_input_tokens', 0)} stop={msg.stop_reason}")
    return _extract_json(text)


# ─────────────────────────────────────────
# 가상 체결 · 기록
# ─────────────────────────────────────────

def apply(state: dict, decision: dict, price: float, today: date, bot_total: float = None,
          source: str = "") -> dict:
    """가상 체결 후 상태 갱신. decisions.csv 에 한 줄 남긴다"""
    cash, qty, avg = state["cash"], state["qty"], state["avg"]
    fill = None
    if decision["action"] == "buy" and decision["quantity"] > 0:
        fill = price * (1 + SLIPPAGE)
        n = decision["quantity"]
        avg = (avg * qty + fill * n) / (qty + n)
        qty += n
        cash -= fill * n
    elif decision["action"] == "sell" and decision["quantity"] > 0:
        fill = price * (1 - SLIPPAGE)
        n = decision["quantity"]
        qty -= n
        cash += fill * n
        if qty == 0:
            avg = 0.0
    total = cash + qty * price
    peak = max(state["peak_total"], total)
    dd = (total / peak - 1) * 100
    state.update(cash=round(cash, 2), qty=qty, avg=round(avg, 4), peak_total=round(peak, 2),
                 max_dd_pct=round(min(state["max_dd_pct"], dd), 2),
                 days=state["days"] + 1, last_date=today.isoformat())

    os.makedirs(SHADOW_DIR, exist_ok=True)
    new = not os.path.exists(DECISIONS)
    with open(DECISIONS, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["날짜", "시세", "출처", "행동", "수량", "체결가", "확신", "국면", "근거",
                        "현금", "보유", "평단", "총자산", "봇총자산"])
        w.writerow([today.isoformat(), f"{price:.2f}", source, decision["action"], decision["quantity"],
                    f"{fill:.2f}" if fill else "", f"{decision['confidence']:.2f}", decision["regime"],
                    decision["rationale"].replace("\n", " "),
                    f"{cash:.2f}", qty, f"{avg:.4f}", f"{total:.2f}",
                    f"{bot_total:.2f}" if bot_total else ""])
    return state


def bot_total_today(snap: dict, price: float, today: date = None) -> float:
    """봇 총자산. 오늘 리포트 행이 있으면 그 값, 없으면 마지막 잔고를 오늘 시세로 평가"""
    if not snap:
        return 0.0
    if snap.get("date") == (today or date.today()).isoformat():
        return snap["total"]
    return snap["cash"] + snap.get("qty", 0) * price


# ─────────────────────────────────────────
# 비교표
# ─────────────────────────────────────────

def _decisions() -> list:
    if not os.path.exists(DECISIONS):
        return []
    with open(DECISIONS, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def report_text(for_audit: bool = False) -> str:
    """봇 vs 그림자 비교. for_audit=True 면 감사 자료용 짧은 형식"""
    state = load_state()
    rows = _decisions()
    if not state or not rows:
        return "## 그림자 트레이더: 아직 판단 기록 없음"
    last = rows[-1]
    sh_total = float(last["총자산"])
    sh_ret = (sh_total / state["start_total"] - 1) * 100
    bot_totals = [float(r["봇총자산"]) for r in rows if r["봇총자산"]]
    bot_ret = (bot_totals[-1] / state["bot_start_total"] - 1) * 100 if bot_totals else float("nan")
    # 봇 MDD 는 기록된 일별 총자산으로 근사
    bot_mdd = 0.0
    if bot_totals:
        peak = bot_totals[0]
        for t in bot_totals:
            peak = max(peak, t)
            bot_mdd = min(bot_mdd, (t / peak - 1) * 100)
    acts = {a: sum(1 for r in rows if r["행동"] == a) for a in ("buy", "sell", "hold")}
    g = GRADUATION
    checks = [
        (f"거래일 {state['days']} ≥ {g['min_days']}", state["days"] >= g["min_days"]),
        (f"수익률 그림자 {sh_ret:+.2f}% vs 봇 {bot_ret:+.2f}%", sh_ret - bot_ret >= g["beat_return_by_pt"]),
        (f"MDD 그림자 {state['max_dd_pct']:.2f}% vs 봇 {bot_mdd:.2f}%", bot_mdd - state["max_dd_pct"] <= g["max_mdd_worse_pt"]),
    ]
    status = "졸업 기준 통과" if all(ok for _, ok in checks) else "졸업 기준 미달"
    lines = [f"## 그림자 트레이더 vs 규칙 봇 ({state['start_date']} ~ {last['날짜']}, {state['days']}거래일)",
             f"그림자: 총자산 ${sh_total:,.2f} ({sh_ret:+.2f}%), MDD {state['max_dd_pct']:.2f}%, "
             f"보유 {state['qty']}주, 결정 매수 {acts['buy']}/매도 {acts['sell']}/관망 {acts['hold']}",
             f"봇    : 총자산 ${bot_totals[-1] if bot_totals else 0:,.2f} ({bot_ret:+.2f}%), MDD {bot_mdd:.2f}%",
             f"{status}: " + " / ".join(("✓ " if ok else "✗ ") + t for t, ok in checks)]
    if not for_audit:
        lines.append("")
        lines.append("최근 결정:")
        for r in rows[-5:]:
            lines.append(f"  {r['날짜']} {r['행동']:4} {r['수량']:>3}주 @ {r['시세']} (확신 {r['확신']}, {r['국면']}) — {r['근거'][:80]}")
    return "\n".join(lines)


# ─────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────

def _setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = log_redact.RedactingFormatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    fh = logging.FileHandler(SHADOW_LOG, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh])


def send(text: str, to_stdout: bool) -> None:
    text = text if len(text) <= TELEGRAM_LIMIT else text[:TELEGRAM_LIMIT - 1] + "…"
    if to_stdout:
        print(text)
        return
    from notifier import _send
    _send(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="GBV 그림자 트레이더")
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--date", help="YYYY-MM-DD (테스트용, 기본 오늘)")
    ap.add_argument("--wait-sec", type=int, default=600,
                    help="봇 로그에 시세가 없을 때 기다리는 최대 초 (기본 600)")
    args = ap.parse_args(argv)
    _setup_logging()
    today = date.fromisoformat(args.date) if args.date else date.today()

    if args.report:
        send(report_text(), args.stdout)
        return 0

    if not is_trading_day("us", today):
        logger.info(f"{today} 미국장 휴장 — 판단 없음")
        return 0

    state = load_state()
    snap = bot_snapshot()
    if not state:
        # 첫 실행이 봇의 매매 뒤라면 그날 봇의 체결까지 물려받고 시작한다 (첫날 하루만의 차이)
        try:
            state = init_state_from_bot(snap, today)
        except RuntimeError as e:
            logger.error(str(e))
            return 2
        if not args.dry_run:
            save_state(state)
        logger.info(f"출발선 설정: 봇 계좌 복사 (현금 ${state['cash']:,.2f}, {state['qty']}주, 총 ${state['start_total']:,.2f})")
    if state.get("last_date") == today.isoformat():
        logger.info("오늘은 이미 판단했음")
        return 0

    if not args.dry_run and todays_bot_price(today)[0] is None:
        wait = seconds_until_bot_cycle(today)
        if wait > 0:
            logger.info(f"봇 매매 시각 5분 뒤까지 {wait // 60}분 대기")
            time.sleep(wait)
    price, source = wait_for_bot_price(today, 0 if args.dry_run else args.wait_sec)
    px = price_history(today=today)
    if price is None:
        price, source = live_price_fallback()
    if price is None:
        tq = px.get(TICKER)
        if tq is None or not len(tq):
            logger.error("오늘 시세를 구하지 못했습니다 (봇 로그·yfinance 모두 없음)")
            return 2
        price, source = float(tq.iloc[-1]), "yfinance-close"
    if source != "bot-log":
        logger.warning(f"봇 로그에 오늘 시세가 없어 {source} 사용: {price:.2f} (봇과 다른 가격)")

    user_message = build_user_message(state, price, source, px, today)
    if args.dry_run:
        print(user_message)
        print("\n(dry-run: Claude 호출·기록 없음)")
        return 0

    load_env()
    log_redact.add_secret(os.environ.get("ANTHROPIC_API_KEY"))
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY 가 없습니다 (.env)")
        return 2
    import anthropic
    model = os.environ.get("GBV_SHADOW_MODEL", DEFAULT_MODEL)
    try:
        raw = decide(anthropic.Anthropic(), user_message, model)
    except anthropic.AuthenticationError:
        logger.error("Anthropic 인증 실패 (401)")
        return 3
    except (anthropic.APIStatusError, anthropic.APIConnectionError, ValueError) as e:
        logger.error(f"판단 실패, 오늘은 관망으로 기록: {e}")
        raw = {"action": "hold", "quantity": 0, "confidence": 0.0, "regime": "range",
               "rationale": f"API 오류로 관망: {str(e)[:80]}"}
    decision = sanitize(raw, state, price)
    bot_total = bot_total_today(snap, price, today)
    state = apply(state, decision, price, today, bot_total, source)
    save_state(state)

    total = state["cash"] + state["qty"] * price
    sh_ret = (total / state["start_total"] - 1) * 100
    bot_ret = (bot_total / state["bot_start_total"] - 1) * 100 if bot_total and state["bot_start_total"] else 0.0
    act = {"buy": "매수", "sell": "매도", "hold": "관망"}[decision["action"]]
    line = (f"🧪 [GBV] 그림자 AI {today:%m-%d}: {act}"
            + (f" {decision['quantity']}주 @ ${price:.2f}" if decision["quantity"] else "")
            + f" (확신 {decision['confidence']:.1f}, {decision['regime']})\n"
            f"누적 그림자 {sh_ret:+.2f}% vs 봇 {bot_ret:+.2f}% · {state['days']}거래일\n"
            f"근거: {decision['rationale']}")
    logger.info(line.replace("\n", " | "))
    send(line, args.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
