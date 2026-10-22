"""
weekly_audit.py
주 1회, 지난 7일치 로그·리포트를 Claude 에 보내 이상 징후를 짚고 텔레그램으로 받는다.

원칙
  - 주문 경로와 무관하다. main.py 는 이 파일을 import 하지 않고, 이 파일은 주문 API 를 부르지 않는다.
  - 읽기 전용이다. 설정·잔고·주문에 손대지 않는다. 쓰는 것은 logs/audit.log 뿐이다.
  - 비밀값을 보내지 않는다. config 는 키·토큰·계좌번호를 뺀 요약만 넣는다.
  - 실패해도 조용히 끝난다. 이 스크립트가 죽어도 봇 운영에는 아무 영향이 없다.
  - 예측·추천을 시키지 않는다. "규칙이 아직 유효한가"만 묻는다.

실행
  python weekly_audit.py            지난 7일 감사 → 텔레그램 발송
  python weekly_audit.py --stdout   화면에만 출력 (텔레그램 안 보냄)
  python weekly_audit.py --days 3   기간 변경

설정 (.env)
  ANTHROPIC_API_KEY=...             필수
  GBV_AUDIT_MODEL=claude-opus-5     선택. 비용을 낮추려면 claude-sonnet-5 등
"""

import argparse
import csv
import glob
import io
import json
import logging
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import log_redact  # noqa: E402

# 윈도우 콘솔(cp949)은 이모지를 못 찍어 --stdout 에서 죽는다. 콘솔 출력만 UTF-8 로 돌린다.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, AttributeError):
            pass

ENV_PATH    = os.path.join(HERE, ".env")
LOG_DIR     = os.path.join(HERE, "logs")
REPORT_DIR  = os.path.join(HERE, "reports")
AUDIT_LOG   = os.path.join(LOG_DIR, "audit.log")
STATE_FILE  = os.path.join(HERE, "trade_state.json")

DEFAULT_MODEL   = "claude-opus-5"
MAX_CONTEXT     = 60_000      # 모델에 보내는 문자 수 상한 (초과분은 오래된 쪽부터 버린다)
TELEGRAM_LIMIT  = 3_900
MAX_REPORT_ROWS = 40

logger = logging.getLogger("weekly_audit")

# ─────────────────────────────────────────
# .env
# ─────────────────────────────────────────

def load_env(path: str = ENV_PATH) -> dict:
    """KEY=value 파일을 읽어 os.environ 에 넣는다 (이미 있는 값은 덮지 않는다)

    메모장이 붙이는 BOM, 값 양끝의 따옴표, 주석·빈 줄을 처리한다.
    python-dotenv 를 쓰지 않는 이유: 의존성 하나를 아끼고 동작을 눈에 보이게 두기 위해서다.
    """
    loaded = {}
    if not os.path.exists(path):
        return loaded
    data = open(path, "rb").read()
    if data[:3] == bytes([0xEF, 0xBB, 0xBF]):
        data = data[3:]
    for line in data.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)
            loaded[key] = value
    return loaded


# ─────────────────────────────────────────
# 자료 수집 (전부 읽기 전용)
# ─────────────────────────────────────────

def _day_files(days: int, today: date = None) -> list:
    today = today or date.today()
    wanted = {(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(days)}
    files = []
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "trade_*.log"))):
        stamp = os.path.basename(path)[6:14]
        if stamp in wanted:
            files.append(path)
    return files


def collect_logs(days: int, today: date = None) -> str:
    """경고·오류 전부 + 매매 사이클 줄 + 날짜별 집계"""
    keep = re.compile(r"\[(WARNING|ERROR|CRITICAL)\]|매매 시간 도달|매매 시작|매매 완료|GBV매수|GBV매도|"
                      r"접수|실효 체결가|미체결|거래 없음|휴장|잠금 획득|봇 시작|프로그램 종료|시간 변경")
    out = []
    for path in _day_files(days, today):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError as e:
            out.append(f"[{os.path.basename(path)}: 읽기 실패 {e}]")
            continue
        levels = {}
        for l in lines:
            m = re.search(r"\[(INFO|WARNING|ERROR|CRITICAL)\]", l)
            if m:
                levels[m.group(1)] = levels.get(m.group(1), 0) + 1
        out.append(f"## {os.path.basename(path)}  "
                   + ", ".join(f"{k} {v}" for k, v in sorted(levels.items())))
        for l in lines:
            if keep.search(l):
                # 날짜·PID 는 빼고 시각부터 (토큰 절약)
                out.append(re.sub(r"^\d{4}-\d\d-\d\d (\d\d:\d\d:\d\d),\d+ \[(\w+)\] \[\d+\] ",
                                  r"\1 \2 ", l))
    return "\n".join(out) if out else "(해당 기간 로그 없음)"


REPORT_GLOSSARY = ("컬럼 설명: 주문기준가=수량을 정한 시세 / 실효체결가=주문 전후 현금차÷수량(수수료 포함) / "
                   "슬리피지=비용 기준이라 +면 불리(매수는 비싸게, 매도는 싸게 체결), -면 유리")


def collect_reports(max_rows: int = MAX_REPORT_ROWS) -> str:
    """현재 리포트 + 보관본

    컬럼이 바뀌면 reporter 가 옛 파일을 <시장>_<시각>.csv 로 밀어내므로, 현재 파일만 보면
    그 전 거래가 사라진다. 10-18 감사가 10-13 거래를 '기록 누락'으로 오인한 원인이다.
    """
    out = [REPORT_GLOSSARY]
    for name in ("미국장", "국내장"):
        files = sorted(glob.glob(os.path.join(REPORT_DIR, f"{name}_*.csv")))   # 이름에 시각이 있어 시간순
        live = os.path.join(REPORT_DIR, f"{name}.csv")
        if os.path.exists(live):
            files.append(live)                                                  # 현재 파일이 가장 최근
        for path in files:
            with open(path, newline="", encoding="utf-8-sig") as f:
                rows = [r for r in csv.reader(f) if any(c.strip() for c in r)]
            if not rows:
                continue
            header, body = rows[0], [r for r in rows[1:] if not r[0].startswith("─")]
            tag = "현재" if path == live else "보관본"
            out.append(f"## reports/{os.path.basename(path)} ({tag}, 최근 {min(len(body), max_rows)}행 / 전체 {len(body)}행)")
            out.append(",".join(header))
            out.extend(",".join(r) for r in body[-max_rows:])
    return "\n".join(out) if len(out) > 1 else "(리포트 없음)"


def collect_recent_commits(days: int = 7) -> str:
    """최근 커밋. 설정·동작 변화가 코드 변경과 나란히 보이면 '드리프트'와 '조치됨'을 가릴 수 있다"""
    try:
        r = subprocess.run(["git", "log", f"--since={days} days ago", "--format=%ad %s", "--date=short"],
                           cwd=HERE, capture_output=True, timeout=10)
        text = r.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return ""                                   # git 이 없으면 이 절만 빠진다
    return "## 최근 코드 변경 (git log)\n" + (text or "(없음)")


def collect_state_and_config() -> str:
    out = []
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            out.append("## trade_state.json\n" + f.read().strip())
    except OSError:
        out.append("## trade_state.json\n(없음)")
    try:
        import config_manager as cm
        cfg = cm.load_config()
        us_raw, kr_raw = cm.get_market_times_raw(cfg)
        lines = [
            f"kiwoom_mode={cm.get_kiwoom_mode(cfg)}",
            f"dry_run={cm.get_dry_run(cfg)}",
            f"trading_enabled={cm.get_trading_enabled(cfg)}",
            f"us_market_time={cm.describe_market_time(us_raw)}",
            f"kr_market_time={cm.describe_market_time(kr_raw)}",
            f"outside_tqqq={cm.get_outside_tqqq(cfg)}",
        ]
        for t, base in cm.get_all_us_tickers(cfg).items():
            lines.append(f"{t}: base={base:,.0f} current={cm.get_base_value(cfg, t):,.2f} "
                         f"monthly_rate={cm.get_monthly_rate(cfg, t)}")
        for t, base in cm.get_all_kr_tickers(cfg).items():
            lines.append(f"{t}: base={base:,.0f} current={cm.get_base_value(cfg, t):,.2f} "
                         f"monthly_rate={cm.get_monthly_rate(cfg, t)}")
        out.append("## config (비밀값 제외)\n" + "\n".join(lines))
    except Exception as e:   # config 가 깨져도 감사는 계속한다
        out.append(f"## config\n(읽기 실패: {e})")
    return "\n".join(out)


def collect_shadow() -> str:
    """그림자 트레이더 vs 봇 비교표 (브랜치 claude/ai-lab). 없으면 그 사실만 적는다"""
    try:
        import shadow_trader
        return shadow_trader.report_text(for_audit=True)
    except Exception as e:                 # 모듈이 없거나 기록이 없어도 감사는 계속한다
        return f"## 그림자 트레이더 비교: 없음 ({type(e).__name__})"


def collect_context(days: int = 7, today: date = None) -> str:
    today = today or date.today()
    parts = [
        f"# GBV 주간 감사 자료  (생성 {datetime.now():%Y-%m-%d %H:%M}, 기간 {days}일)",
        collect_state_and_config(),
        collect_recent_commits(days),
        collect_reports(),
        collect_shadow(),
        collect_logs(days, today),
    ]
    text = log_redact.redact("\n\n".join(parts))
    if len(text) > MAX_CONTEXT:
        # 로그가 맨 뒤라 오래된 로그부터 잘려 나간다. 머리말은 남긴다.
        text = text[:MAX_CONTEXT // 4] + "\n…(중간 생략)…\n" + text[-(MAX_CONTEXT * 3 // 4):]
    return text


# ─────────────────────────────────────────
# Claude 호출
# ─────────────────────────────────────────

SYSTEM_PROMPT = """당신은 TQQQ 밸류 애버리징 자동매매 봇(GBV)의 감사관이다.
주문 권한이 없고, 시장을 예측하지 않으며, 매수·매도·비중에 대한 조언을 하지 않는다.
주어진 로그·리포트·설정만 보고 다음 네 가지를 판단한다.

1. 세션 누락: 매매가 있어야 할 거래일에 '매매 시작'이 없는 날이 있는가. 주말·휴장은 제외.
2. 오류: WARNING/ERROR 중 일시적 네트워크 끊김(자동 복구)이 아닌 것. 반복·패턴·새로운 유형.
3. 체결 품질: 실효체결가·슬리피지의 추세. 슬리피지는 비용 기준이라 매수·매도 모두 +면 불리한 체결이다 — 부호를 가격 방향으로 읽지 않는다. 특히 매매 시각이 바뀐 전후 비교.
4. 설정 드리프트: 기준금·매매 시각·종목·월증액 변경 흔적과 그 시점. 의도된 것으로 보이는지.
5. 그림자 AI (있을 때만): 봇 대비 누적수익률·MDD 차이를 한 줄로. 평가·권고는 하지 않는다.

규칙
- 각 항목은 근거 숫자(날짜·시각·금액·건수)를 인용한다. 근거 없는 추정은 "확인 불가"로 적는다.
- 문제 없으면 "이상 없음"이라고 짧게 쓴다. 억지로 지적하지 않는다.
- 전체 1200자 이내, 한국어, 항목당 1~3문장. 마지막 줄에 '요약: 정상 / 주의 / 조치 필요' 중 하나.
- '최근 코드 변경'에 해당 수정이 보이면 그 문제는 "조치됨"으로 적고 '조치 필요'에서 뺀다.
  어떤 기록(예: 실효체결가)이 기능 추가 날짜 이전에 없는 것은 누락이 아니다.
- 투자 판단, 수익률 평가, 전략 변경 권고는 하지 않는다."""


def run_audit(client, context: str, model: str = DEFAULT_MODEL) -> str:
    """Claude 에 자료를 보내고 본문 텍스트를 돌려준다"""
    with client.beta.messages.stream(
        model=model,
        max_tokens=4096,                           # 1200자 답이라 충분하다
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",                       # 정책 거부 시 서버가 대체 모델로 이어 준다
        system=[{"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}}],   # 고정 프롬프트는 캐시
        messages=[{"role": "user", "content": context}],
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        detail = getattr(msg, "stop_details", None)
        return f"(감사관이 응답을 거부했습니다: {getattr(detail, 'category', None)})"
    text = "\n".join(b.text for b in msg.content if b.type == "text").strip()
    usage = msg.usage
    logger.info(f"model={msg.model} in={usage.input_tokens} out={usage.output_tokens} "
                f"cache_read={getattr(usage, 'cache_read_input_tokens', 0)} stop={msg.stop_reason}")
    return text or "(빈 응답)"


def send_result(text: str, days: int, to_stdout: bool) -> None:
    head = f"🔍 [GBV] 주간 감사 (최근 {days}일)\n───────────\n"
    body = text
    if len(head) + len(body) > TELEGRAM_LIMIT:
        body = body[:TELEGRAM_LIMIT - len(head) - 1] + "…"
    message = head + body
    if to_stdout:
        print(message)
        return
    from notifier import _send
    _send(message)


# ─────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────

def _setup_logging() -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = log_redact.RedactingFormatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    fh = logging.FileHandler(AUDIT_LOG, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="GBV 주간 감사")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--stdout", action="store_true", help="텔레그램 대신 화면에 출력")
    args = parser.parse_args(argv)

    _setup_logging()
    load_env()
    log_redact.add_secret(os.environ.get("ANTHROPIC_API_KEY"))
    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error("ANTHROPIC_API_KEY 가 없습니다. .env 를 확인하세요 (.env.example 참고)")
        return 2

    import anthropic
    model = os.environ.get("GBV_AUDIT_MODEL", DEFAULT_MODEL)
    context = collect_context(args.days)
    logger.info(f"자료 {len(context):,}자 수집, 모델 {model}")

    try:
        text = run_audit(anthropic.Anthropic(), context, model)
    except anthropic.AuthenticationError:
        logger.error("Anthropic 인증 실패 (401): 키가 틀렸거나 폐기됐습니다")
        return 3
    except anthropic.RateLimitError as e:
        logger.error(f"Anthropic 사용량 제한 (429): retry-after={e.response.headers.get('retry-after')}")
        return 4
    except anthropic.APIStatusError as e:
        logger.error(f"Anthropic API 오류 {e.status_code}: {e.message}")
        return 5
    except anthropic.APIConnectionError as e:
        logger.error(f"Anthropic 연결 실패: {e}")
        return 6

    send_result(text, args.days, args.stdout)
    logger.info("감사 결과 발송 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
