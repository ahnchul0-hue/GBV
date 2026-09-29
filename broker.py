"""
broker.py
키움증권 API 객체 생성 및 공유

strategy.py / main.py / telegram_handler.py는 이 모듈의 get_broker()로
같은 KiwoomAPI 객체를 받아 쓴다 (토큰·요청 간격 공유).
"""

import logging
import threading

from config_manager import load_config, get_api_info, get_kiwoom_mode

logger = logging.getLogger(__name__)

_shared = None
_shared_lock = threading.Lock()


def validate_api_info(config: dict) -> str:
    """필수 설정 확인. 문제가 있으면 오류 메시지, 없으면 빈 문자열"""
    app_key, app_secret, _ = get_api_info(config)
    if not app_key or not app_secret:
        return "config.txt에 app_key, app_secret을 입력하세요"
    if get_kiwoom_mode(config) not in ("real", "demo"):
        return "kiwoom_mode는 real 또는 demo 여야 합니다"
    return ""


def create_broker(config: dict = None):
    """config에 맞는 KiwoomAPI 객체 새로 생성"""
    config = config or load_config()
    error = validate_api_info(config)
    if error:
        raise ValueError(error)

    from kiwoom_api import KiwoomAPI
    app_key, app_secret, account_no = get_api_info(config)
    return KiwoomAPI(app_key, app_secret, account_no, mode=get_kiwoom_mode(config))


def get_broker(config: dict = None):
    """프로세스 전체에서 공유하는 KiwoomAPI 객체 (메인 루프 + 텔레그램 스레드)"""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = create_broker(config)
        return _shared


def describe_broker(config: dict) -> str:
    return f"키움 REST API ({get_kiwoom_mode(config)})"
