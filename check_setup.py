"""
check_setup.py
설치·연결 상태 점검 (주문은 절대 보내지 않음)

실행: python check_setup.py
  - 봇(main.py)이 켜져 있으면 먼저 끄고 실행하세요 (텔레그램 조회가 겹치면 409 오류).

점검 순서
  1) 파이썬·패키지
  2) config.txt 값 (키는 길이·앞 4자리만 표시)
  3) 텔레그램: 봇 토큰 확인 → 채팅 ID 확인(없으면 찾아 줌) → 테스트 메시지 전송
  4) 키움: 접근토큰 발급 → 국내·미국 잔고 조회 (조회만)
"""

import sys

OK, FAIL, WARN = "✅", "❌", "⚠️"
results = []


def report(status, title, detail=""):
    results.append((status, title))
    print(f"{status} {title}")
    if detail:
        for line in str(detail).splitlines():
            print(f"     {line}")


def mask(value: str) -> str:
    value = value or ""
    if not value:
        return "(비어 있음)"
    return f"길이 {len(value)}, 앞 4자리 '{value[:4]}'"


def is_placeholder(value: str) -> bool:
    return not value or value.upper().startswith("XXX") or value.startswith("<")


# ─────────────────────────────────────────
# 1) 파이썬·패키지
# ─────────────────────────────────────────
def check_python():
    print("\n[1] 파이썬·패키지")
    report(OK, f"Python {sys.version.split()[0]}")
    missing = []
    for module, pip_name in (("requests", "requests"), ("telebot", "pyTelegramBotAPI"),
                             ("holidays", "holidays")):
        try:
            __import__(module)
        except ImportError:
            missing.append(pip_name)
    if missing:
        report(FAIL, "필요한 패키지가 없습니다: " + ", ".join(missing),
               "pip install -r requirements.txt  (또는 pip install " + " ".join(missing) + ")")
        return False
    report(OK, "필요한 패키지 설치됨 (requests, pyTelegramBotAPI, holidays)")
    return True


# ─────────────────────────────────────────
# 2) config.txt
# ─────────────────────────────────────────
def check_config():
    print("\n[2] config.txt")
    import config_manager as cm
    try:
        config = cm.load_config()
    except FileNotFoundError:
        report(FAIL, f"config.txt가 없습니다: {cm.CONFIG_PATH}")
        return None
    except UnicodeDecodeError:
        report(FAIL, "config.txt를 읽지 못했습니다 (UTF-8로 저장하세요)")
        return None

    mode = cm.get_kiwoom_mode(config)
    report(OK if mode in ("real", "demo") else FAIL,
           f"kiwoom_mode = {mode}  ({'실전' if mode == 'real' else '모의투자' if mode == 'demo' else '잘못된 값'})")
    report(OK, f"dry_run = {'true (연습 모드: 주문 안 보냄)' if cm.get_dry_run(config) else 'false (실제 주문)'}")
    report(OK if cm.get_trading_enabled(config) else WARN,
           f"trading_enabled = {'true' if cm.get_trading_enabled(config) else 'false (매매 꺼짐)'}")

    app_key, app_secret, _ = cm.get_api_info(config)
    for name, value in (("app_key", app_key), ("app_secret", app_secret)):
        if is_placeholder(value):
            report(FAIL, f"{name}가 입력되지 않았습니다 ({mask(value)})")
        else:
            report(OK, f"{name}: {mask(value)}")

    us_time, kr_time = cm.get_market_times(config)
    report(OK, f"매매 시각: 미국장 {us_time} / 국내장 {kr_time}")
    us, kr = cm.get_all_us_tickers(config), cm.get_all_kr_tickers(config)
    if us or kr:
        items = [f"{t} ${v:,.0f}" for t, v in us.items()] + [f"{t} ₩{v:,.0f}" for t, v in kr.items()]
        report(OK, "GBV 종목: " + ", ".join(items))
    else:
        report(WARN, "GBV 종목이 없습니다 (예: TQQQ = 1000)")
    return config


# ─────────────────────────────────────────
# 3) 텔레그램
# ─────────────────────────────────────────
def check_telegram():
    print("\n[3] 텔레그램")
    import requests
    from notifier import get_telegram_settings
    token, chat_id = get_telegram_settings()

    if is_placeholder(token):
        report(FAIL, "telegram_bot_token이 없습니다",
               "텔레그램 @BotFather → /newbot → 받은 토큰을 config.txt에 입력:\n"
               "telegram_bot_token = 123456789:ABC...")
        return False

    base = f"https://api.telegram.org/bot{token}"
    try:
        me = requests.get(f"{base}/getMe", timeout=10).json()
    except Exception as e:
        report(FAIL, f"텔레그램 서버에 접속하지 못했습니다: {e}")
        return False
    if not me.get("ok"):
        report(FAIL, "봇 토큰이 틀렸습니다 (텔레그램이 거절)",
               f"응답: {me.get('description')}\n@BotFather에서 토큰을 다시 복사하세요 (/mybots → API Token)")
        return False
    bot_name = me["result"].get("username")
    report(OK, f"봇 토큰 정상: @{bot_name}")

    if is_placeholder(chat_id):
        report(FAIL, "telegram_chat_id가 없습니다")
        _find_chat_ids(requests, base, bot_name)
        return False

    try:
        res = requests.post(f"{base}/sendMessage", timeout=10, json={
            "chat_id": chat_id,
            "text": "🔧 [GBV] 설치 점검 테스트 메시지입니다. 이 메시지가 보이면 텔레그램 연결 성공!",
        }).json()
    except Exception as e:
        report(FAIL, f"테스트 메시지 전송 실패: {e}")
        return False
    if not res.get("ok"):
        report(FAIL, f"테스트 메시지 전송 실패: {res.get('description')}",
               f"텔레그램에서 @{bot_name} 에게 먼저 아무 메시지나 보냈는지 확인하세요.\n"
               "채팅 ID가 맞는지도 확인하세요 (아래는 봇이 받은 최근 채팅 목록).")
        _find_chat_ids(requests, base, bot_name)
        return False
    report(OK, f"테스트 메시지 전송 성공 (chat_id {chat_id}) → 텔레그램을 확인하세요")
    return True


def _find_chat_ids(requests, base, bot_name):
    """봇이 받은 최근 메시지에서 채팅 ID 찾기"""
    try:
        data = requests.get(f"{base}/getUpdates", timeout=10).json()
    except Exception as e:
        print(f"     채팅 목록 조회 실패: {e}")
        return
    if not data.get("ok"):
        desc = str(data.get("description"))
        if "Conflict" in desc or data.get("error_code") == 409:
            print("     봇(main.py)이 켜져 있어서 조회할 수 없습니다. 봇을 끄고 다시 실행하세요.")
        else:
            print(f"     채팅 목록 조회 실패: {desc}")
        return
    chats = {}
    for update in data.get("result", []):
        msg = update.get("message") or update.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            name = chat.get("username") or chat.get("first_name") or chat.get("title") or ""
            chats[chat["id"]] = name
    if not chats:
        print(f"     봇이 받은 메시지가 없습니다. 텔레그램에서 @{bot_name} 에게 '안녕'을 보내고 다시 실행하세요.")
        return
    print("     봇에게 메시지를 보낸 채팅 (본인 것을 config.txt에 입력):")
    for cid, name in chats.items():
        print(f"       telegram_chat_id = {cid}    ({name})")


# ─────────────────────────────────────────
# 4) 키움
# ─────────────────────────────────────────
_KEY_HINT = ("App Key/Secret Key가 틀렸거나 kiwoom_mode(real/demo)와 키 종류가 다릅니다.\n"
             "모의투자 키 → kiwoom_mode = demo, 실전 키 → kiwoom_mode = real\n"
             "포털에서 복사 버튼으로 다시 붙여 넣고, 재발급했다면 새 키를 쓰세요.")
_MODE_HINT = "투자구분(실전/모의)이 앱키와 다릅니다 → kiwoom_mode를 확인하세요."
_IP_HINT = ("지정단말기(허용 IP) 인증에 실패했습니다 → 키움 OpenAPI 포털에 이 PC의 공인 IP를 등록하세요.\n"
            "(공인 IP 확인: 네이버에서 '내 IP' 검색)")
KIWOOM_HINTS = {  # 키움 인증 오류 코드 분류
    "8001": _KEY_HINT, "8002": _KEY_HINT, "8011": _KEY_HINT, "8012": _KEY_HINT,
    "8030": _MODE_HINT, "8031": _MODE_HINT,
    "8010": _IP_HINT, "8040": _IP_HINT, "8050": _IP_HINT, "8103": _IP_HINT,
}


def check_kiwoom(config):
    print("\n[4] 키움 REST API (조회만, 주문 안 함)")
    import config_manager as cm
    app_key, app_secret, account_no = cm.get_api_info(config)
    if is_placeholder(app_key) or is_placeholder(app_secret):
        report(FAIL, "app_key/app_secret이 없어 건너뜁니다")
        return False
    from kiwoom_api import KiwoomAPI
    mode = cm.get_kiwoom_mode(config)
    try:
        api = KiwoomAPI(app_key, app_secret, account_no, mode=mode)
    except Exception as e:
        text = str(e)
        hint = next((h for code, h in KIWOOM_HINTS.items() if f"{code}:" in text or f"({code})" in text), "")
        report(FAIL, f"접근토큰 발급 실패 ({mode})", f"{text}\n{hint}".strip())
        return False
    report(OK, f"접근토큰 발급 성공 ({mode}, {api.base_url})")

    ok = True
    for label, func, unit in (("국내", api.get_kr_balance, "₩"), ("미국", api.get_us_balance, "$")):
        try:
            holdings, cash = func()
            items = ", ".join(f"{t} {h['qty']}주" for t, h in holdings.items()) or "보유 종목 없음"
            report(OK, f"{label} 잔고 조회 성공: 주문가능 {unit}{cash:,.2f} / {items}")
        except Exception as e:
            ok = False
            report(WARN, f"{label} 잔고 조회 실패: {e}",
                   "모의투자에서는 해외주식 조회가 막혀 있을 수 있습니다." if label == "미국" and mode == "demo" else "")
    return ok


def main():
    # Git Bash/UCRT64 등에서 콘솔 인코딩이 cp949여도 이모지 때문에 멈추지 않게
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    print("=" * 56)
    print("  GBV 설치 점검 (주문은 보내지 않습니다)")
    print("=" * 56)
    if not check_python():
        return
    config = check_config()
    check_telegram()
    if config is not None:
        check_kiwoom(config)

    fails = [t for s, t in results if s == FAIL]
    warns = [t for s, t in results if s == WARN]
    print("\n" + "=" * 56)
    if fails:
        print(f"{FAIL} 고칠 것 {len(fails)}개:")
        for t in fails:
            print(f"   - {t}")
    else:
        print(f"{OK} 필수 항목 모두 정상! 이제 python main.py 로 봇을 켜세요.")
    if warns:
        print(f"{WARN} 참고 {len(warns)}개: " + " / ".join(warns))
    print("=" * 56)


if __name__ == "__main__":
    main()
