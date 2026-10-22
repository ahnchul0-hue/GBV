"""
ai_researcher.py
AI 리서처 — 규칙 변형을 제안하게 하고, 백테스트로 걸러, 메모만 남긴다

AI 가 알파를 만들 수 있는 곳은 매 순간의 판단이 아니라 규칙의 발명이다. 그런데 발명은
검증과 짝을 이뤄야 한다. 그래서 Claude 는 코드를 쓰지 않고 rules_lab.RuleParams 의
숫자만 제안한다(최대 5개). 모든 제안은 같은 엔진으로 세 구간에 돌려,
위기 구간 셋(코로나 2020, 2022 하락, 2022-23 회복)을 모두 이기고 자금부족이 없는 것만 '통과'로 적는다.
최근 12개월은 참고로만 보여 준다 — 한 구간에 맞춘 과최적화를 걸러내기 위해서다.

통과한 제안이 있어도 설정을 바꾸지 않는다. research/ 에 메모를 쓰고 텔레그램으로
요약을 보낸다. 적용은 사람이 /set 으로 한다 (큰 변경은 confirm 을 요구한다).

실행
  python ai_researcher.py            제안 → 검증 → 메모 → 텔레그램
  python ai_researcher.py --stdout   텔레그램 대신 화면
  python ai_researcher.py --dry-run  Claude 호출 없이 기준 규칙 성적표만
"""

import argparse
import io
import json
import logging
import os
import pickle
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import log_redact  # noqa: E402
from ai_client import call_structured, text_of  # noqa: E402
from rules_lab import (RuleParams, BOUNDS, evaluate,  # noqa: E402
                       PASS_MIN_RETURN_GAIN_PT, PASS_MAX_MDD_WORSE_PT)
from weekly_audit import load_env  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, AttributeError):
            pass

RESEARCH_DIR  = os.path.join(HERE, "research")
PX_CACHE      = os.path.join(RESEARCH_DIR, "px.pkl")
LOG_DIR       = os.path.join(HERE, "logs")
RESEARCH_LOG  = os.path.join(LOG_DIR, "research.log")
REPORT_DIR    = os.path.join(HERE, "reports")

TICKER        = "TQQQ"
DEFAULT_MODEL = "claude-opus-5"
MAX_PROPOSALS = 5
TELEGRAM_LIMIT = 3_900
# 통과 판정에 쓰는 구간. '2022 하락'(순수 하락 해)이 있어야 노출만 늘린 제안이 걸러진다 —
# 회복까지 포함한 구간만 보면 TQQQ 비중을 키운 모든 제안이 이기기 때문이다 (2026-10-22 첫 실행에서 확인).
CRISIS = ("코로나 2020", "2022 하락", "2022-23 회복")

logger = logging.getLogger("ai_researcher")

PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name":       {"type": "string"},
                    "hypothesis": {"type": "string"},
                    "params": {
                        "type": "object",
                        "properties": {
                            "base_ratio":       {"type": "number"},
                            "monthly_rate":     {"type": "number"},
                            "pause_below_cash": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                            "bump_drawdown":    {"anyOf": [{"type": "number"}, {"type": "null"}]},
                            "bump_pct":         {"anyOf": [{"type": "number"}, {"type": "null"}]},
                            "cap_ratio":        {"anyOf": [{"type": "number"}, {"type": "null"}]},
                        },
                        "required": ["base_ratio", "monthly_rate", "pause_below_cash",
                                     "bump_drawdown", "bump_pct", "cap_ratio"],
                        "additionalProperties": False,
                    },
                },
                "required": ["name", "hypothesis", "params"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["proposals"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """당신은 TQQQ 밸류 애버리징 전략의 리서처다. 코드는 쓰지 않는다.
주어진 파라미터 메뉴 안에서 '현재 규칙을 위기 구간에서 이길' 변형을 최대 5개 제안한다 (6개 이상은 잘린다).

파라미터 메뉴와 허용 범위
- base_ratio: 기준금/자본 비율
- monthly_rate: 월 증액률
- pause_below_cash: 현금이 기준금의 N배 미만이면 그달 증액 중지 (null = 끔)
- bump_drawdown + bump_pct: 고점 대비 낙폭이 D 이상이면 기준금을 P 만큼 한 번 올림 (둘 다 주거나 둘 다 null)
- cap_ratio: 기준금 ≤ (현금+평가액)×C (null = 끔)

규칙
- 각 제안에 한 문장 가설을 단다: '어느 구간에서 왜 이길 것인가'.
- 지난 메모에서 탈락한 유형을 그대로 반복하지 않는다.
- 범위 밖 값은 자동 탈락이니 쓰지 않는다. 극단값으로 한 구간만 이기는 제안은 통과하지 못한다.
- 위기 구간에서 현금이 바닥나면(자금부족) 그 제안은 어떤 수익률이든 탈락이다.
- 통과하려면 '2022 하락'(순수 하락 해)에서도 기준보다 덜 잃어야 한다. 노출만 키운 제안은 거기서 떨어진다."""


# ─────────────────────────────────────────
# 자료
# ─────────────────────────────────────────

def prices(today: date = None):
    """TQQQ 일봉 (2019~). 캐시는 하루"""
    import pandas as pd
    today = today or date.today()
    os.makedirs(RESEARCH_DIR, exist_ok=True)
    if os.path.exists(PX_CACHE):
        with open(PX_CACHE, "rb") as f:
            c = pickle.load(f)
        if c.get("stamp") == today.isoformat():
            return c["px"]
    import yfinance as yf
    d = yf.download(TICKER, start="2019-01-01", end=today.isoformat(), progress=False, auto_adjust=True)
    px = d["Close"].squeeze().dropna()
    with open(PX_CACHE, "wb") as f:
        pickle.dump({"stamp": today.isoformat(), "px": px}, f)
    return px


def periods(today: date = None) -> dict:
    today = today or date.today()
    return {
        "코로나 2020":  ("2020-01-01", "2020-12-31"),
        "2022 하락":    ("2022-01-01", "2022-12-31"),
        "2022-23 회복": ("2022-01-01", "2023-12-31"),
        "최근 12개월":  ((today - timedelta(days=365)).isoformat(), today.isoformat()),
    }


def baseline_from_config() -> tuple:
    """(RuleParams, capital). 기준금/자본 비율은 봇 리포트의 최신 총자산으로 잡는다"""
    import config_manager as cm
    import risk_table
    cfg = cm.load_config()
    base = cm.get_base_value(cfg, TICKER)
    rate = cm.get_monthly_rate(cfg, TICKER)
    capital = risk_table.latest_total_assets("미국장") or 100_000.0
    ratio = min(max(base / capital, BOUNDS["base_ratio"][0]), BOUNDS["base_ratio"][1])
    return RuleParams(base_ratio=round(ratio, 4), monthly_rate=rate).validate(), capital


def last_memo_summary() -> str:
    files = sorted(f for f in os.listdir(RESEARCH_DIR) if f.endswith(".md")) if os.path.isdir(RESEARCH_DIR) else []
    if not files:
        return "(지난 메모 없음)"
    with open(os.path.join(RESEARCH_DIR, files[-1]), encoding="utf-8") as f:
        text = f.read()
    return text[-2500:]


# ─────────────────────────────────────────
# 제안
# ─────────────────────────────────────────

def results_table(evaluated: list, period_names: list) -> str:
    head = f"{'규칙':44} " + " ".join(f"{p:>22}" for p in period_names)
    lines = [head]
    for row in evaluated:
        cells = []
        for p in period_names:
            r = row["results"].get(p)
            cells.append(f"{r['수익률%']:+6.1f}% / MDD {r['MDD%']:6.1f}%" if r else "실행불가".rjust(22))
        mark = "" if row["passed"] is None else ("  ✓ 통과" if row["passed"] else "  ✗")
        lines.append(f"{row['label'][:44]:44} " + " ".join(cells) + mark)
        for p, (ok, why) in row["judge"].items():
            if not ok:
                lines.append(f"{'':44}   - {p}: {why}")
    return "\n".join(lines)


def build_user_message(evaluated_baseline: list, period_names: list, last_memo: str) -> str:
    bounds = "\n".join(f"- {k}: {lo} ~ {hi}" for k, (lo, hi) in BOUNDS.items())
    return (f"# 현재 규칙 성적표\n{results_table(evaluated_baseline, period_names)}\n\n"
            f"# 허용 범위\n{bounds}\n\n"
            f"# 지난 메모 (끝부분)\n{last_memo}\n\n"
            f"최대 {MAX_PROPOSALS}개를 JSON 으로 제안하라.")


def propose(client, user_message: str, model: str = DEFAULT_MODEL) -> list:
    kwargs = dict(
        model=model, max_tokens=4096,
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_message}],
        output_config={"format": {"type": "json_schema", "schema": PROPOSAL_SCHEMA}},
    )
    msg = call_structured(client, kwargs, "\n\n반드시 {\"proposals\": [...]} JSON 하나만 출력하라.")
    if msg.stop_reason == "refusal":
        logger.warning("모델이 제안을 거부")
        return []
    text = text_of(msg)
    u = msg.usage
    logger.info(f"model={msg.model} in={u.input_tokens} out={u.output_tokens} stop={msg.stop_reason}")
    try:
        data = json.loads(text)
    except ValueError:
        import re
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {"proposals": []}
    return list(data.get("proposals", []))[:MAX_PROPOSALS]


def parse_proposals(raw: list) -> tuple:
    """(유효한 [(name, hypothesis, RuleParams)], 탈락 [(name, 이유)])"""
    good, bad = [], []
    for item in raw:
        name = str(item.get("name", "?"))[:40]
        hyp = str(item.get("hypothesis", ""))[:200]
        try:
            good.append((name, hyp, RuleParams.from_dict(item.get("params") or {})))
        except (ValueError, TypeError) as e:
            bad.append((name, str(e)))
    return good, bad


# ─────────────────────────────────────────
# 메모
# ─────────────────────────────────────────

def write_memo(today: date, baseline: RuleParams, capital: float, evaluated: list,
               names: dict, bad: list, period_names: list) -> str:
    os.makedirs(RESEARCH_DIR, exist_ok=True)
    path = os.path.join(RESEARCH_DIR, f"{today.isoformat()}.md")
    passed = [r for r in evaluated[1:] if r["passed"]]
    lines = [f"# AI 리서처 메모 {today}", "",
             f"기준: {baseline.label()} / 자본 ${capital:,.0f} / 통과 조건: 위기 구간({', '.join(CRISIS)}) 모두 "
             f"수익률 +{PASS_MIN_RETURN_GAIN_PT}%p 이상, MDD {PASS_MAX_MDD_WORSE_PT}%p 넘는 악화 없음, 자금부족 0일",
             "", "```", results_table(evaluated, period_names), "```", ""]
    for r in evaluated[1:]:
        n, h = names.get(id(r["params"]), ("", ""))
        lines.append(f"- **{n}** — {h}")
        lines.append(f"  - {r['label']}")
    if bad:
        lines.append("")
        lines.append("범위 밖 등으로 탈락한 제안:")
        lines.extend(f"- {n}: {why}" for n, why in bad)
    lines += ["", f"**통과 {len(passed)}건 / 제안 {len(evaluated) - 1}건.** "
              + ("설정은 바꾸지 않았다. 적용하려면 /set (큰 변경은 confirm)." if passed
                 else "현재 규칙이 그대로 가장 낫다.")]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def summary_text(today: date, evaluated: list, names: dict, bad: list) -> str:
    passed = [r for r in evaluated[1:] if r["passed"]]
    base = evaluated[0]["results"]
    head = f"🔬 [GBV] AI 리서처 {today:%m-%d}: 제안 {len(evaluated) - 1}건 중 통과 {len(passed)}건"
    lines = [head, "───────────",
             "기준 " + " / ".join(f"{p} {r['수익률%']:+.1f}%(MDD {r['MDD%']:.0f}%)" for p, r in base.items())]
    for r in evaluated[1:]:
        n, _ = names.get(id(r["params"]), ("", ""))
        res = " / ".join(f"{p} {v['수익률%']:+.1f}%" if v else f"{p} -" for p, v in r["results"].items())
        lines.append(("✓ " if r["passed"] else "✗ ") + f"{n}: {res}")
        if not r["passed"]:
            why = "; ".join(w for ok, w in r["judge"].values() if not ok)
            lines.append(f"   {why[:120]}")
    if bad:
        lines.append(f"범위 밖 탈락 {len(bad)}건")
    lines.append("설정은 바꾸지 않았습니다. 메모: research/")
    text = "\n".join(lines)
    return text if len(text) <= TELEGRAM_LIMIT else text[:TELEGRAM_LIMIT - 1] + "…"


# ─────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────

def _setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = log_redact.RedactingFormatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    fh = logging.FileHandler(RESEARCH_LOG, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="GBV AI 리서처")
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    _setup_logging()
    today = date.today()

    px = prices(today)
    baseline, capital = baseline_from_config()
    per = periods(today)
    names = list(per)
    base_eval = evaluate(px, capital, baseline, [], per, CRISIS)
    logger.info(f"기준 규칙 {baseline.label()} / 자본 ${capital:,.0f}")
    if args.dry_run:
        print(results_table(base_eval, names))
        return 0

    load_env()
    log_redact.add_secret(os.environ.get("ANTHROPIC_API_KEY"))
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY 가 없습니다 (.env)")
        return 2
    import anthropic
    model = os.environ.get("GBV_RESEARCH_MODEL", DEFAULT_MODEL)
    try:
        raw = propose(anthropic.Anthropic(), build_user_message(base_eval, names, last_memo_summary()), model)
    except anthropic.AuthenticationError:
        logger.error("Anthropic 인증 실패 (401)")
        return 3
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        logger.error(f"제안 실패: {e}")
        return 4

    good, bad = parse_proposals(raw)
    logger.info(f"제안 {len(raw)}건 → 유효 {len(good)}건, 탈락 {len(bad)}건")
    cands = [p for _, _, p in good]
    evaluated = evaluate(px, capital, baseline, cands, per, CRISIS)
    name_map = {id(p): (n, h) for n, h, p in good}
    path = write_memo(today, baseline, capital, evaluated, name_map, bad, names)
    logger.info(f"메모 저장: {path}")
    text = summary_text(today, evaluated, name_map, bad)
    if args.stdout:
        print(text)
    else:
        from notifier import _send
        _send(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
