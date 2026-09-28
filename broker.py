"""
broker.py
config.txt의 broker 설정에 따라 증권사 API 객체 생성

broker = kiwoom  → KiwoomAPI (기본값)
broker = kis     → KisAPI

두 클래스는 같은 메서드(get_us_price, get_us_balance, buy_us, sell_us,
get_kr_price, get_kr_balance, get_kr_name, buy_kr, sell_kr, get_usd_krw_rate)를
제공하므로 strategy.py는 증권사와 무관하게 동작한다.
"""

import logging
import threading

from config_manager import load_config, get_api_info, get_broker_name, get_kiwoom_mode

logger = logging.getLogger(__name__)

SUPPORTED_BROKERS = ("kiwoom", "kis")

_shared = None
_shared_lock = threading.Lock()


def validate_api_info(config: dict) -> str:
    """필수 설정 확인. 문제가 있으면 오류 메시지, 없으면 빈 문자열"""
    broker = get_broker_name(config)
    app_key, app_secret, account_no = get_api_info(config)
    if broker not in SUPPORTED_BROKERS:
        return f"broker 설정은 {', '.join(SUPPORTED_BROKERS)} 중 하나여야 합니다 (현재: {broker})"
    if not app_key or not app_secret:
        return "config.txt에 app_key, app_secret을 입력하세요"
    if broker == "kis" and not account_no:
        return "KIS는 config.txt에 account_no가 필요합니다"
    return ""


def create_broker(config: dict = None):
    """config에 맞는 증권사 API 객체 새로 생성"""
    config = config or load_config()
    error = validate_api_info(config)
    if error:
        raise ValueError(error)

    broker = get_broker_name(config)
    app_key, app_secret, account_no = get_api_info(config)

    if broker == "kis":
        from kis_api import KisAPI
        return KisAPI(app_key, app_secret, account_no)

    from kiwoom_api import KiwoomAPI
    return KiwoomAPI(app_key, app_secret, account_no, mode=get_kiwoom_mode(config))


def get_broker(config: dict = None):
    """프로세스 전체에서 공유하는 증권사 API 객체 (메인 루프 + 텔레그램 스레드)"""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = create_broker(config)
        return _shared


def describe_broker(config: dict) -> str:
    broker = get_broker_name(config)
    if broker == "kiwoom":
        return f"키움 REST API ({get_kiwoom_mode(config)})"
    return "KIS API"
