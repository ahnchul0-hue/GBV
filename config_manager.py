"""
config_manager.py
config.txt 읽기/쓰기

주요 기능:
- 종목별 월 증액률 관리
- current_base 자동 초기화
"""

import os
import logging
from datetime import date

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.txt")
logger = logging.getLogger(__name__)

_RESERVED_KEYS = {
    "APP_KEY", "APP_SECRET", "ACCOUNT_NO", "BROKER", "KIWOOM_MODE",  # BROKER: 이전 설정 호환
    "US_MARKET_TIME", "KR_MARKET_TIME",
    "OUTSIDE_TQQQ", "TRADING_ENABLED", "LAST_INCREASED_MONTH",
    "LAST_INCREASED_MONTH_US", "LAST_INCREASED_MONTH_KR",
    "DRY_RUN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"
}


def _normalize(key: str) -> str:
    """키를 대문자로 정규화"""
    return key.strip().upper().replace(" ", "")


def load_config() -> dict:
    """config 파일 로드"""
    config = {}
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = _normalize(key)
            value = value.strip()
            if "#" in value:
                value = value[:value.index("#")].strip()
            config[key] = value
    return config


def get_api_info(config: dict) -> tuple:
    """(app_key, app_secret, account_no)"""
    return (
        config.get("APP_KEY", ""),
        config.get("APP_SECRET", ""),
        config.get("ACCOUNT_NO", "")
    )


def get_kiwoom_mode(config: dict) -> str:
    """키움 실전/모의 구분 (real / demo), 기본값 real"""
    return config.get("KIWOOM_MODE", "real").strip().lower() or "real"


def normalize_kr_ticker(ticker: str) -> str:
    """국내 종목코드 정규화: 'A252670' → '252670' (증권사 잔고 키와 맞추기 위함)"""
    code = str(ticker).strip().upper()
    if len(code) == 7 and code[0] in "AJQ" and code[1:].isalnum():
        return code[1:]
    return code


def get_market_times(config: dict) -> tuple:
    """(us_market_time, kr_market_time)"""
    return (
        config.get("US_MARKET_TIME", "19:00"),
        config.get("KR_MARKET_TIME", "09:05")
    )


def get_all_us_tickers(config: dict) -> dict:
    """
    미국 종목 전체 기준금
    Returns: {ticker: base_value}
    """
    result = {}
    for key, value in config.items():
        # 순수 알파벳 (예약어 제외)
        if (key.isalpha() and 
            key not in _RESERVED_KEYS and 
            not key.endswith("_MONTHLY_RATE") and
            not key.endswith("_CURRENT_BASE")):
            try:
                result[key] = float(value)
            except ValueError:
                pass
    return result


def get_all_kr_tickers(config: dict) -> dict:
    """
    국내 종목 전체 기준금
    숫자만 OR 영숫자 혼합 (알파벳만은 제외, 8자 이하)
    Returns: {ticker: base_value}
    """
    result = {}
    for key, value in config.items():
        # 숫자만 OR (영숫자 혼합 AND 알파벳만 아님 AND 8자 이하)
        if key.isdigit() or (key.isalnum() and not key.isalpha() and len(key) <= 8):
            try:
                result[key] = float(value)
            except ValueError:
                pass
    return result


def get_base_value(config: dict, ticker: str) -> float:
    """
    종목의 current_base 조회
    없으면 기본 base 값으로 자동 초기화
    """
    current_key = f"{ticker.upper()}_CURRENT_BASE"
    base_key = ticker.upper()
    
    current = config.get(current_key, "")
    base = config.get(base_key, "0")
    
    if not current or current == "0":
        # 자동 초기화
        _set_value(current_key.lower(), base)
        logger.info(f"{ticker} current_base 자동 초기화: {base}")
        return float(base)
    
    return float(current)


def get_monthly_rate(config: dict, ticker: str) -> float:
    """
    종목의 월 증액률 조회
    없으면 0 반환 (자동 생성하지 않음)
    """
    key = f"{ticker.upper()}_MONTHLY_RATE"
    return float(config.get(key, "0"))


def get_outside_tqqq(config: dict) -> int:
    """다른 계좌 TQQQ 수량"""
    return int(config.get("OUTSIDE_TQQQ", 0))


def get_dry_run(config: dict) -> bool:
    """dry_run = true 이면 주문을 보내지 않고 기록·알림만 (봇 재시작 후 적용)"""
    return config.get("DRY_RUN", "false").strip().lower() == "true"


def get_trading_enabled(config: dict) -> bool:
    """매매 활성화 여부"""
    return config.get("TRADING_ENABLED", "true").strip().lower() == "true"


def update_base(ticker: str, new_base: float):
    """종목의 current_base 업데이트"""
    current_key = f"{ticker.lower()}_current_base"
    _set_value(current_key, f"{new_base:.2f}")
    logger.info(f"{ticker} current_base 업데이트: ${new_base:.2f}")


def update_monthly_increase_date(market: str):
    """월 증액 날짜 업데이트 (market: "us" / "kr" 시장별로 따로 기록)"""
    today_str = date.today().strftime("%Y-%m")
    _set_value(f"last_increased_month_{market}", today_str)
    logger.info(f"월 증액 날짜 업데이트 ({market}): {today_str}")


def get_last_increased_month(config: dict, market: str) -> str:
    """
    시장별 마지막 증액 월
    시장별 기록이 아직 없으면 예전 공용 기록(last_increased_month)을 사용
    (업그레이드 직후 같은 달에 두 번 증액되는 것을 막기 위함)
    """
    key = f"LAST_INCREASED_MONTH_{market.upper()}"
    if key in config:
        return config.get(key, "")
    return config.get("LAST_INCREASED_MONTH", "")


def _set_value(key: str, value: str):
    """config 파일의 특정 키 값 변경"""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()
    
    def _normalize_key(k: str) -> str:
        return k.strip().lower().replace(" ", "").replace("_", "")

    target = _normalize_key(key)
    updated = False
    new_lines = []
    
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        
        k = _normalize_key(stripped.partition("=")[0])
        if k == target:
            new_lines.append(f"{key} = {value}\n")
            updated = True
        else:
            new_lines.append(line)
    
    if not updated:
        # 파일 마지막 줄에 줄바꿈이 없으면 추가
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        new_lines.append(f"{key} = {value}\n")
    
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


def _delete_keys(ticker: str):
    """종목 관련 키 전체 삭제 (base, monthly_rate, current_base)"""
    def _normalize_key(k: str) -> str:
        return k.strip().lower().replace(" ", "").replace("_", "")

    targets = {
        _normalize_key(ticker),
        _normalize_key(f"{ticker}_monthly_rate"),
        _normalize_key(f"{ticker}_current_base"),
    }

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        lines = f.readlines()

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        k = _normalize_key(stripped.partition("=")[0])
        if k not in targets:
            new_lines.append(line)

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    logger.info(f"{ticker} 관련 키 삭제 완료")
