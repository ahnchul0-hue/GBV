"""
broker.py
키움증권 API 객체 생성 및 공유

strategy.py / main.py / telegram_handler.py는 이 모듈의 get_broker()로
같은 KiwoomAPI 객체를 받아 쓴다 (토큰·요청 간격 공유).
"""

import logging
import threading

from config_manager import load_config, get_api_info, get_kiwoom_mode, get_dry_run

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


class DryRunBroker:
    """
    주문만 가로채는 껍데기: 시세·잔고 조회는 진짜 키움 API로 하고
    buy_*/sell_* 는 주문을 보내지 않은 채 로그·텔레그램만 남긴다.
    """

    _ORDER_METHODS = ("buy_us", "sell_us", "buy_kr", "sell_kr")

    def __init__(self, api):
        self._api = api

    def __getattr__(self, name):
        if name in self._ORDER_METHODS:
            return lambda ticker, qty, *args, **kwargs: self._fake_order(name, ticker, qty, args, kwargs)
        return getattr(self._api, name)

    def _fake_order(self, name, ticker, qty, args, kwargs):
        from notifier import _send
        side = "매수" if name.startswith("buy") else "매도"
        price = kwargs.get("price", args[0] if args else None)
        detail = f" @ {price}" if price else ""
        logger.info(f"[DRY-RUN] {side} {ticker} {qty}주{detail} (주문 안 보냄)")
        _send(f"🧪 [GBV DRY-RUN] {side} {ticker} {qty}주{detail}\n※ 연습 모드라 실제 주문은 보내지 않았습니다")
        return True


def create_broker(config: dict = None):
    """config에 맞는 KiwoomAPI 객체 새로 생성"""
    config = config or load_config()
    error = validate_api_info(config)
    if error:
        raise ValueError(error)

    from kiwoom_api import KiwoomAPI
    app_key, app_secret, account_no = get_api_info(config)
    api = KiwoomAPI(app_key, app_secret, account_no, mode=get_kiwoom_mode(config))
    if get_dry_run(config):
        logger.warning("dry_run = true → 주문을 보내지 않는 연습 모드로 실행합니다")
        return DryRunBroker(api)
    return api


def get_broker(config: dict = None):
    """프로세스 전체에서 공유하는 KiwoomAPI 객체 (메인 루프 + 텔레그램 스레드)"""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = create_broker(config)
        return _shared


def describe_broker(config: dict) -> str:
    dry = ", DRY-RUN: 주문 안 보냄" if get_dry_run(config) else ""
    return f"키움 REST API ({get_kiwoom_mode(config)}{dry})"
