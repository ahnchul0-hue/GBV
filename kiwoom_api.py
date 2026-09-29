"""
kiwoom_api.py
키움증권 REST API 연동
- 접근토큰 자동 발급/갱신 (au10001), 파일 캐시
- 국내/미국 주식 현재가, 잔고, 주문, 환율

공식 스펙: https://openapi.kiwoom.com
          https://github.com/Kiwoom-Securities/Kiwoom-REST-API

참고
- 모든 TR이 POST + JSON body, TR 코드는 'api-id' 헤더로 지정
- 계좌번호를 요청에 넣지 않음 (앱키/토큰에 계좌가 묶여 있음)
- 업무 오류도 HTTP 200 + return_code != 0 으로 내려옴
- 가격 필드에 전일 대비 부호가 붙음 ("+201.4700", "-74100") → abs() 필요
- 운영(real)과 모의투자(demo)의 앱키가 서로 다름
"""

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

BASE_URLS = {
    "real": "https://api.kiwoom.com",
    "demo": "https://mockapi.kiwoom.com",
}
TOKEN_DIR = os.path.dirname(os.path.abspath(__file__))
KST = timezone(timedelta(hours=9))

REQUEST_INTERVAL_SEC = 0.25          # 요청 간 최소 간격 (공식 예제는 0.2초)
REQUEST_TIMEOUT_SEC = 15
TOKEN_REFRESH_MARGIN = timedelta(minutes=10)
RATE_LIMIT_RETRIES = 3
MAX_PAGES = 20                       # 연속조회 최대 페이지

AUTH_RETRY_CODES = {8005, 8031, 8103}   # 토큰 재발급 후 1회 재시도
RATE_LIMIT_CODES = {1700, 1701, 1702}   # 유량 초과 → 대기 후 재시도

US_EXCHANGES = ("ND", "NY", "NA")       # NASDAQ, NYSE(Arca 포함), AMEX

_EMBEDDED_CODE_RE = re.compile(r"\[(\d{3,5}):")

# 토큰/요청 간격은 인스턴스가 여러 개여도(메인 루프 + 텔레그램 스레드) 공유
_token_lock = threading.RLock()
_token_cache: dict = {}
_throttle_lock = threading.Lock()
_last_request_at = 0.0


class KiwoomError(Exception):
    def __init__(self, api_id: str, return_code, message: str):
        super().__init__(f"키움 API 오류 ({return_code}): {message} (api-id={api_id})")
        self.api_id = api_id
        self.return_code = return_code
        self.message = message


# ─────────────────────────────────────────
# 값 파싱 유틸
# ─────────────────────────────────────────

def _num(value, default: float = 0.0) -> float:
    """키움 숫자 문자열 → float
    예: "000000000012550", "-00000000196888", "+201.4700", "1,507.70", "50%", ""
    """
    s = str(value if value is not None else "").strip().replace(",", "").replace("%", "")
    if s in ("", "+", "-", "."):
        return default
    try:
        return float(s)
    except ValueError:
        return default


def _price(value) -> float:
    """부호 붙은 가격 → 양수 가격 (부호는 전일 대비 방향일 뿐)"""
    return abs(_num(value))


def _return_code(data: dict):
    """return_code는 int 또는 "0", "0000" 같은 숫자 문자열로 온다 (일부 TR은 returnCode)"""
    value = data.get("return_code", data.get("returnCode"))
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text or not text.lstrip("-").isdigit():
        return None
    return int(text)


def _embedded_code(return_msg):
    """return_msg 안에 '[8005:Token이 유효하지 않습니다]' 형태로 실제 코드가 들어오는 경우"""
    match = _EMBEDDED_CODE_RE.search(str(return_msg or ""))
    return int(match.group(1)) if match else None


def normalize_kr_code(ticker: str) -> str:
    """국내 종목코드 정규화
    'A252670' → '252670' (계좌 조회 응답의 접두어 A/J/Q 제거)
    '005930_NX' → '005930' (거래소 접미어 제거)
    """
    code = str(ticker).strip().upper()
    code = code.split("_", 1)[0]
    if len(code) == 7 and code[0] in "AJQ":
        code = code[1:]
    return code


class KiwoomAPI:

    # 국내 주문 거래소 (KRX / NXT / SOR)
    KR_EXCHANGE = "KRX"

    _kr_name_cache: dict = {}
    _stex_cache: dict = {}

    def __init__(self, app_key: str, app_secret: str, account_no: str = "",
                 mode: str = "real"):
        mode = str(mode or "real").strip().lower()
        if mode in ("mock", "모의", "모의투자"):
            mode = "demo"
        if mode not in BASE_URLS:
            raise ValueError(f"kiwoom_mode는 real 또는 demo 여야 합니다: {mode!r}")

        self.app_key    = app_key
        self.app_secret = app_secret
        self.account_no = account_no  # 요청에는 쓰지 않음 (표시/확인용)
        self.mode       = mode
        self.base_url   = BASE_URLS[mode]
        self.token_file = os.path.join(TOKEN_DIR, f"kiwoom_token_{mode}.json")
        self.session    = requests.Session()
        self.access_token = None
        self.token_expired_at = None
        self._load_or_issue_token()

    # ─────────────────────────────────────────
    # 토큰 관리
    # ─────────────────────────────────────────

    def _fingerprint(self) -> str:
        raw = f"{self.mode}:{self.app_key}:{self.app_secret}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _token_valid(self, expired_at) -> bool:
        return expired_at is not None and datetime.now(KST) < expired_at - TOKEN_REFRESH_MARGIN

    def _load_or_issue_token(self):
        """메모리 → 파일 → 신규 발급 순으로 유효한 토큰 확보"""
        with _token_lock:
            key = self._fingerprint()
            cached = _token_cache.get(key)
            if cached and self._token_valid(cached[1]):
                self.access_token, self.token_expired_at = cached
                return

            if os.path.exists(self.token_file):
                try:
                    with open(self.token_file, "r") as f:
                        data = json.load(f)
                    expired_at = datetime.fromisoformat(data["expired_at"])
                    if data.get("fingerprint") == key and self._token_valid(expired_at):
                        self.access_token     = data["access_token"]
                        self.token_expired_at = expired_at
                        _token_cache[key] = (self.access_token, expired_at)
                        logger.info("기존 키움 토큰 로드 성공")
                        return
                except (OSError, ValueError, KeyError) as e:
                    logger.warning(f"키움 토큰 캐시 읽기 실패, 재발급: {e}")

            self._issue_token()

    def _issue_token(self):
        """au10001 접근토큰 발급 (초당 약 2회 제한 → 1700이면 잠시 후 재시도)"""
        with _token_lock:
            body = {
                "grant_type": "client_credentials",
                "appkey":     self.app_key,
                "secretkey":  self.app_secret,
            }
            data = None
            for attempt in range(RATE_LIMIT_RETRIES + 1):
                res = self.session.post(
                    f"{self.base_url}/oauth2/token",
                    headers={"Content-Type": "application/json;charset=UTF-8"},
                    json=body,
                    timeout=REQUEST_TIMEOUT_SEC,
                )
                try:
                    data = res.json()
                except ValueError:
                    data = {}
                code = _return_code(data)
                if code in RATE_LIMIT_CODES and attempt < RATE_LIMIT_RETRIES:
                    time.sleep(0.6 * (attempt + 1))
                    continue
                break

            code = _return_code(data)
            if res.status_code >= 400 or code not in (None, 0) or not data.get("token"):
                raise KiwoomError(
                    "au10001", code if code is not None else res.status_code,
                    str(data.get("return_msg") or "토큰 발급 실패")
                )

            self.access_token = data["token"]
            # expires_dt: 'YYYYMMDDHHMMSS' (KST 기준)
            try:
                expired_at = datetime.strptime(str(data["expires_dt"]), "%Y%m%d%H%M%S").replace(tzinfo=KST)
            except (KeyError, ValueError):
                expired_at = datetime.now(KST) + timedelta(hours=23)
            self.token_expired_at = expired_at

            key = self._fingerprint()
            _token_cache[key] = (self.access_token, expired_at)
            try:
                with open(self.token_file, "w") as f:
                    json.dump({
                        "access_token": self.access_token,
                        "expired_at":   expired_at.isoformat(),
                        "fingerprint":  key,
                    }, f)
                os.chmod(self.token_file, 0o600)
            except OSError as e:
                logger.warning(f"키움 토큰 파일 저장 실패: {e}")
            logger.info(f"새 키움 접근토큰 발급 완료 ({self.mode})")

    def _reissue_token(self):
        with _token_lock:
            _token_cache.pop(self._fingerprint(), None)
            self._issue_token()

    def _ensure_token(self):
        if not self._token_valid(self.token_expired_at):
            self._load_or_issue_token()

    # ─────────────────────────────────────────
    # 공통 요청
    # ─────────────────────────────────────────

    @staticmethod
    def _throttle():
        global _last_request_at
        with _throttle_lock:
            wait = REQUEST_INTERVAL_SEC - (time.monotonic() - _last_request_at)
            if wait > 0:
                time.sleep(wait)
            _last_request_at = time.monotonic()

    def _request(self, api_id: str, path: str, body: dict,
                 cont_key: str = None) -> tuple:
        """
        TR 1회 호출. (data, response_headers) 반환
        - 토큰 만료(401, 8005/8031/8103) → 재발급 후 1회 재시도
        - 유량 초과(429, 1700~1702) → 대기 후 재시도 (서버가 처리하지 않은 요청이라 주문도 안전)
        - 타임아웃/네트워크 오류는 재시도하지 않음 (주문 중복 방지)
        """
        auth_retried = False
        rate_retries = 0
        while True:
            self._ensure_token()
            headers = {
                "Content-Type":  "application/json;charset=UTF-8",
                "api-id":        api_id,
                "authorization": f"Bearer {self.access_token}",
            }
            if cont_key:
                headers["cont-yn"]  = "Y"
                headers["next-key"] = cont_key

            self._throttle()
            res = self.session.post(
                f"{self.base_url}{path}",
                headers=headers,
                json=body,
                timeout=REQUEST_TIMEOUT_SEC,
            )

            if res.status_code == 401 and not auth_retried:
                auth_retried = True
                self._reissue_token()
                continue

            try:
                data = res.json()
            except ValueError:
                data = {}
            if not isinstance(data, dict):
                data = {}

            code     = _return_code(data)
            embedded = _embedded_code(data.get("return_msg"))

            if not auth_retried and (code in AUTH_RETRY_CODES or embedded in AUTH_RETRY_CODES):
                auth_retried = True
                self._reissue_token()
                continue

            if res.status_code == 429 or code in RATE_LIMIT_CODES or embedded in RATE_LIMIT_CODES:
                if rate_retries < RATE_LIMIT_RETRIES:
                    rate_retries += 1
                    time.sleep(1.0 * rate_retries)
                    continue

            if res.status_code >= 400 or code != 0:
                # return_code가 없거나 JSON이 아닌 응답(점검 페이지 등)도 실패로 처리한다.
                # 성공으로 보면 잔고가 '보유 0주'로 읽혀 전량 매수가 나갈 수 있다.
                if code is None and res.status_code < 400:
                    message = "응답에 return_code가 없거나 JSON 형식이 아닙니다"
                else:
                    message = str(data.get("return_msg") or f"HTTP {res.status_code}")
                raise KiwoomError(api_id, embedded or code or res.status_code, message)
            return data, res.headers

    def _call(self, api_id: str, path: str, body: dict) -> dict:
        """단건 TR (주문 포함). 연속조회하지 않는다."""
        data, _ = self._request(api_id, path, body)
        return data

    def _call_list(self, api_id: str, path: str, body: dict, list_key: str) -> tuple:
        """연속조회(cont-yn/next-key)를 따라가며 list_key 행을 모두 모은다.
        반환: (rows, first_page_data)
        """
        rows, first, cont_key = [], None, None
        for _ in range(MAX_PAGES):
            data, headers = self._request(api_id, path, body, cont_key)
            if first is None:
                first = data
            rows.extend(data.get(list_key) or [])
            if str(headers.get("cont-yn", "")).upper() != "Y" or not headers.get("next-key"):
                break
            cont_key = headers.get("next-key")
        else:
            logger.warning(f"{api_id} 연속조회 {MAX_PAGES}페이지 초과 - 이후 데이터 생략")
        return rows, first or {}

    def get_account_no(self) -> str:
        """ka00001 현재 토큰의 계좌번호 (10자리)"""
        data = self._call("ka00001", "/api/dostk/acnt", {})
        return str(data.get("acctNo", "")).strip()

    # ─────────────────────────────────────────
    # 국내 주식
    # ─────────────────────────────────────────

    def _kr_quote(self, ticker: str) -> dict:
        """ka10001 주식기본정보 (현재가/종목명/상·하한가)"""
        data = self._call("ka10001", "/api/dostk/stkinfo", {"stk_cd": normalize_kr_code(ticker)})
        name = str(data.get("stk_nm") or "").strip()
        if name:
            self._kr_name_cache[ticker] = name
            self._kr_name_cache[normalize_kr_code(ticker)] = name
        return data

    def get_kr_name(self, ticker: str) -> str:
        """국내 종목명 조회 (캐시 적용)"""
        if ticker in self._kr_name_cache:
            return self._kr_name_cache[ticker]
        try:
            self._kr_quote(ticker)
            return self._kr_name_cache.get(ticker, ticker)
        except Exception:
            return ticker

    def get_kr_price(self, ticker: str) -> float:
        """국내 주식 현재가"""
        data  = self._kr_quote(ticker)
        price = _price(data.get("cur_prc"))
        if price <= 0:
            raise KiwoomError("ka10001", None, f"{ticker} 현재가 없음: {data.get('cur_prc')!r}")
        return price

    def get_kr_balance(self) -> tuple:
        """
        국내 잔고 조회
        반환: (holdings_dict, cash_krw)
        holdings_dict = {ticker(6자리): {"qty": int, "avg_price": float}}
        """
        rows, _ = self._call_list(
            "kt00018", "/api/dostk/acnt",
            {"qry_tp": "1", "dmst_stex_tp": self.KR_EXCHANGE},
            "acnt_evlt_remn_indv_tot"
        )
        agg = {}
        for item in rows:
            ticker = normalize_kr_code(item.get("stk_cd", ""))
            qty    = int(_num(item.get("rmnd_qty")))
            if not ticker or qty <= 0:
                continue
            a = agg.setdefault(ticker, {"qty": 0, "pur_amt": 0.0, "pur_pric": 0.0})
            a["qty"]     += qty       # 같은 종목이 여러 행(신용구분 등)으로 올 수 있음
            a["pur_amt"] += _num(item.get("pur_amt"))
            a["pur_pric"] = _num(item.get("pur_pric"))
            name = str(item.get("stk_nm") or "").strip()
            if name:
                self._kr_name_cache[ticker] = name

        holdings = {}
        for ticker, a in agg.items():
            avg = a["pur_amt"] / a["qty"] if a["pur_amt"] else a["pur_pric"]
            holdings[ticker] = {"qty": a["qty"], "avg_price": avg}

        # ── 주문가능금액: kt00001 100%종목주문가능금액 (미수 없는 현금 매수가능액) ──
        cash = 0
        try:
            data = self._call("kt00001", "/api/dostk/acnt", {"qry_tp": "3"})
            value = data.get("100stk_ord_alow_amt")
            if value in (None, ""):
                value = data.get("d2_entra")  # D+2 추정예수금
            cash = int(_num(value))
            logger.info(f"원화 주문가능금액: ₩{cash:,}")
        except Exception as e:
            logger.error(f"원화 주문가능금액 조회 실패: {e}")

        return holdings, cash

    def get_kr_drwg_amt(self) -> int:
        """원화 출금가능금액 조회 (kt00001 pymn_alow_amt)"""
        try:
            data = self._call("kt00001", "/api/dostk/acnt", {"qry_tp": "3"})
            return int(_num(data.get("pymn_alow_amt")))
        except Exception as e:
            logger.error(f"원화 출금가능금액 조회 실패: {e}")
        return 0

    def get_kr_unfilled(self) -> list:
        """
        국내 미체결 주문 (ka10075, 전체 종목·통합 거래소)
        반환: [{"ticker", "side"("매수"/"매도"), "qty", "remaining", "price", "ord_no"}]
        """
        rows, _ = self._call_list(
            "ka10075", "/api/dostk/acnt",
            {"all_stk_tp": "0", "trde_tp": "0", "stk_cd": "", "stex_tp": "0"},
            "oso"
        )
        result = []
        for item in rows:
            remaining = int(_num(item.get("oso_qty")))
            if remaining <= 0:
                continue
            side_text = str(item.get("io_tp_nm") or "")
            result.append({
                "ticker": normalize_kr_code(item.get("stk_cd") or ""),
                "side": "매도" if "매도" in side_text else "매수",
                "qty": int(_num(item.get("ord_qty"))),
                "remaining": remaining,
                "price": _price(item.get("ord_pric")),
                "ord_no": str(item.get("ord_no") or "").strip(),
            })
        return result

    def _get_kr_tick_size(self, price: int) -> int:
        """국내주식 호가단위 (KRX 2023-01-25 개편 기준)
        ETF/ETN 호가단위(2천원 미만 1원, 이상 5원)의 배수이기도 해서 ETF에도 유효하다.
        """
        if price < 2000:       return 1
        elif price < 5000:     return 5
        elif price < 20000:    return 10
        elif price < 50000:    return 50
        elif price < 200000:   return 100
        elif price < 500000:   return 500
        else:                  return 1000

    def _kr_order(self, api_id: str, label: str, ticker: str, qty: int,
                  price: int, factor: float) -> bool:
        code = normalize_kr_code(ticker)
        upper = lower = 0.0
        try:
            quote = self._kr_quote(code)
            if price <= 0:
                price = int(_price(quote.get("cur_prc")))
            upper = _price(quote.get("upl_pric"))
            lower = _price(quote.get("lst_pric"))
        except Exception as e:
            if price <= 0:
                raise
            logger.warning(f"{ticker} 상/하한가 조회 실패, 제한 없이 주문: {e}")
        if price <= 0:
            raise KiwoomError(api_id, None, f"{ticker} 현재가 없음")

        raw_price   = int(price * factor)
        tick        = self._get_kr_tick_size(raw_price)
        order_price = (raw_price // tick) * tick  # 호가단위 내림
        # 상·하한가를 벗어나면 거부되므로 가격제한폭 안으로 맞춤
        if upper > 0 and order_price > upper:
            order_price = int(upper)
        if lower > 0 and order_price < lower:
            order_price = int(lower)

        data = self._call(api_id, "/api/dostk/ordr", {
            "dmst_stex_tp": self.KR_EXCHANGE,
            "stk_cd":       code,
            "ord_qty":      str(int(qty)),
            "ord_uv":       str(order_price),
            "trde_tp":      "0",   # 0: 보통(지정가)
            "cond_uv":      "",
        })
        ord_no = str(data.get("ord_no") or "").strip()
        if not ord_no:
            raise KiwoomError(api_id, _return_code(data), f"주문번호 없음: {data.get('return_msg')}")
        pct = f"{(factor - 1) * 100:+.0f}%"
        logger.info(f"국내 {label} 접수: {ticker} {qty}주 ₩{order_price:,} (현재가 {pct}, 주문번호 {ord_no})")
        return True

    def buy_kr(self, ticker: str, qty: int, price: int = 0) -> bool:
        """국내 주식 매수 - 지정가 (현재가 +3%, 호가단위·상한가 적용)"""
        try:
            return self._kr_order("kt10000", "매수", ticker, qty, price, 1.03)
        except Exception as e:
            logger.error(f"국내 매수 실패 ({ticker}): {e}")
            self.last_order_error = str(e)
            return False

    def sell_kr(self, ticker: str, qty: int, price: int = 0) -> bool:
        """국내 주식 매도 - 지정가 (현재가 -3%, 호가단위·하한가 적용)"""
        try:
            return self._kr_order("kt10001", "매도", ticker, qty, price, 0.97)
        except Exception as e:
            logger.error(f"국내 매도 실패 ({ticker}): {e}")
            self.last_order_error = str(e)
            return False

    # ─────────────────────────────────────────
    # 거래소 코드 자동 감지
    # ─────────────────────────────────────────

    def get_excd(self, ticker: str) -> tuple:
        """
        ticker의 거래소 코드 자동 조회 (usa10098)
        반환: (stex_tp, stex_tp) - (시세용, 주문용). 키움은 코드 하나로 둘 다 사용
        예: TQQQ → ("ND", "ND"), SOXL(NYSE Arca) → ("NY", "NY")
        """
        ticker = ticker.strip().upper()
        if ticker in self._stex_cache:
            stex = self._stex_cache[ticker]
            return stex, stex

        try:
            data = self._call("usa10098", "/api/us/stkinfo", {"stk_cd": ticker})
            for row in data.get("list") or []:
                stex = str(row.get("stex_tp") or "").strip().upper()
                if str(row.get("stk_cd") or "").strip().upper() == ticker and stex in US_EXCHANGES:
                    self._stex_cache[ticker] = stex
                    logger.info(f"{ticker} 거래소 자동 감지: {stex}")
                    return stex, stex
        except Exception as e:
            logger.warning(f"{ticker} 거래소구분 조회(usa10098) 실패, 시세로 탐색: {e}")

        # 대체: 거래소별로 시세 조회 시도
        for stex in US_EXCHANGES:
            try:
                data = self._call("usa20100", "/api/us/mrkcond", {"stex_tp": stex, "stk_cd": ticker})
                if _price(data.get("cur_prc")) > 0 or _price(data.get("base_close_pric")) > 0:
                    self._stex_cache[ticker] = stex
                    logger.info(f"{ticker} 거래소 탐색 성공: {stex}")
                    return stex, stex
            except Exception:
                continue

        # 거래소를 추측해서 잘못 주문하는 것보다 실패가 안전
        raise KiwoomError("usa10098", None, f"{ticker} 거래소 감지 실패")

    # ─────────────────────────────────────────
    # 해외(미국) 주식
    # ─────────────────────────────────────────

    def _us_quote(self, ticker: str) -> dict:
        stex, _ = self.get_excd(ticker)
        return self._call("usa20100", "/api/us/mrkcond", {"stex_tp": stex, "stk_cd": ticker.strip().upper()})

    def get_us_price(self, ticker: str) -> float:
        """미국 주식 현재가 (달러) - 거래소 자동 감지
        키움 cur_prc는 장전/장후 체결도 반영된 최종 체결가다. 체결이 없으면 전일종가 사용.
        """
        data  = self._us_quote(ticker)
        price = _price(data.get("cur_prc")) or _price(data.get("base_close_pric"))
        if price <= 0:
            raise KiwoomError("usa20100", None, f"{ticker} 현재가 없음: {data.get('cur_prc')!r}")
        return price

    def get_us_extended_price(self, ticker: str) -> float:
        """미국 주식 시간외 현재가 (키움은 별도 TR 없이 cur_prc에 시간외 체결 반영)"""
        return self.get_us_price(ticker)

    def get_us_balance(self) -> tuple:
        """
        해외 잔고 조회 (ust21070 원장잔고, 전체 거래소 통합)
        반환: (holdings_dict, cash_usd)
        """
        rows, _ = self._call_list(
            "ust21070", "/api/us/acnt",
            {"stex_tp": "", "stk_cd": ""},
            "result_list"
        )
        holdings = {}
        ref_ticker, ref_price = None, 0.0
        for item in rows:
            crnc = str(item.get("crnc_code") or "USD").strip().upper()
            if crnc not in ("", "USD"):
                continue
            ticker = str(item.get("stk_cd") or "").strip().upper()
            qty    = int(_num(item.get("poss_qty")))   # 보유수량
            if not ticker or qty <= 0:
                continue
            prev = holdings.get(ticker)
            if prev:
                prev["qty"] += qty
            else:
                holdings[ticker] = {"qty": qty, "avg_price": _num(item.get("frgn_stk_book_uv"))}
            if ref_ticker is None and _price(item.get("now_pric")) > 0:
                ref_ticker, ref_price = ticker, _price(item.get("now_pric"))

        cash_usd = self._get_us_orderable_cash(ref_ticker, ref_price)
        return holdings, cash_usd

    def _get_us_orderable_cash(self, ref_ticker: str = None, ref_price: float = 0.0) -> float:
        """
        달러 주문가능현금 (ust31490)
        = ord_alowa(주문가능현금) + min_tdy_rebuy_alowa(금일재사용) + min_pred_rebuy_alowa(전일재사용)
        - 증거금(미수)·원화 환산분이 섞이지 않은 순수 달러 현금 (달러/원화 완전 분리)
        - 재사용금액(미결제 매도대금)을 더해 매도 당일 총자산이 줄어 보이지 않게 함
        - ust31490은 종목·가격이 필수라 보유 종목(없으면 AAPL)을 기준으로 조회
        - 모의투자는 ust31490 자체를 지원하지 않으므로(RC9000) 시도하지 않고
          바로 ust21160 으로 간다. 시도하면 거래소 감지·시세 조회까지 함께 버려진다.
        - 실패 시(권한 없음 등) ust21160 결제 반영 외화예수금으로 대체
        """
        if self.mode == "demo":
            return self._get_us_deposit_cash()
        try:
            ticker = ref_ticker or "AAPL"
            price  = ref_price if ref_price > 0 else self.get_us_price(ticker)
            stex, _ = self.get_excd(ticker)
            data = self._call("ust31490", "/api/us/ordr", {
                "stex_tp": stex,
                "stk_cd":  ticker,
                "uv":      f"{price:.2f}",
            })
            cash = (_num(data.get("ord_alowa"))
                    + _num(data.get("min_tdy_rebuy_alowa"))
                    + _num(data.get("min_pred_rebuy_alowa")))
            logger.info(f"달러 주문가능금액: ${cash:,.2f}")
            return cash
        except Exception as e:
            logger.warning(f"달러 주문가능금액(ust31490) 조회 실패, 예수금 상세로 대체: {e}")
        return self._get_us_deposit_cash()

    def _get_us_deposit_cash(self) -> float:
        """ust21160 결제 반영 외화예수금 (ust31490 대체용)"""
        try:
            data = self._call("ust21160", "/api/us/acnt", {})
            # dN_usd_fx_entr는 누적(D1 = D0 + D1 정산금 ...)이라 마지막 값이 미결제 매수·매도를 모두 반영
            for key in ("d4_usd_fx_entr", "d3_usd_fx_entr", "d2_usd_fx_entr",
                        "d1_usd_fx_entr", "d0_usd_fx_entr"):
                if str(data.get(key) or "").strip():
                    cash = _num(data.get(key))
                    logger.info(f"달러 예수금({key}): ${cash:,.2f}")
                    return cash
        except Exception as e:
            logger.error(f"달러 주문가능금액 조회 실패: {e}")
        return 0.0

    def _us_deposit_row(self) -> dict:
        """ust21110 해외주식 예수금 중 USD 행"""
        rows, _ = self._call_list("ust21110", "/api/us/acnt", {}, "result_list")
        for item in rows:
            if str(item.get("crnc_code") or "").strip().upper() == "USD":
                return item
        return {}

    def get_us_drwg_amt(self) -> float:
        """달러 출금가능금액 조회 (ust21110 fc_pymn_alowa)"""
        try:
            return _num(self._us_deposit_row().get("fc_pymn_alowa"))
        except Exception as e:
            logger.error(f"달러 출금가능금액 조회 실패: {e}")
        return 0.0

    def _us_order(self, api_id: str, label: str, ticker: str, qty: int,
                  price: float, factor: float) -> bool:
        ticker = ticker.strip().upper()
        stex, _ = self.get_excd(ticker)
        if price <= 0:
            price = self.get_us_price(ticker)
        order_price = round(price * factor, 2)
        body = {
            "stex_tp": stex,
            "stk_cd":  ticker,
            "ord_qty": str(int(qty)),
            "trde_tp": "00",            # 00: 지정가
            "ord_uv":  f"{order_price:.2f}",
        }
        if api_id == "ust20001":
            body["stop_pric"] = ""
        data = self._call(api_id, "/api/us/ordr", body)
        ord_no = str(data.get("ord_no") or "").strip()
        if not ord_no:
            raise KiwoomError(api_id, _return_code(data), f"주문번호 없음: {data.get('return_msg')}")
        pct = f"{(factor - 1) * 100:+.0f}%"
        logger.info(f"해외 {label} 접수: {ticker} {qty}주 ({stex}) ${order_price:.2f} (현재가 {pct}, 주문번호 {ord_no})")
        return True

    def buy_us(self, ticker: str, qty: int, price: float = 0) -> bool:
        """미국 주식 매수 - 지정가 (현재가 +3%, 거래소 자동 감지)"""
        try:
            return self._us_order("ust20000", "매수", ticker, qty, price, 1.03)
        except Exception as e:
            logger.error(f"해외 매수 실패 ({ticker}): {e}")
            self.last_order_error = str(e)
            return False

    def sell_us(self, ticker: str, qty: int, price: float = 0) -> bool:
        """미국 주식 매도 - 지정가 (현재가 -3%, 거래소 자동 감지)"""
        try:
            return self._us_order("ust20001", "매도", ticker, qty, price, 0.97)
        except Exception as e:
            logger.error(f"해외 매도 실패 ({ticker}): {e}")
            self.last_order_error = str(e)
            return False

    def get_us_unfilled(self) -> list:
        """
        미국 미체결 주문 (ust21050, 오늘 주문·전체 거래소)
        반환: [{"ticker", "side"("매수"/"매도"), "qty", "remaining", "price", "ord_no"}]
        """
        rows, _ = self._call_list(
            "ust21050", "/api/us/acnt",
            {"ord_dt": "", "slby_tp": "0", "stex_tp": "", "stk_cd": ""},
            "result_list"
        )
        result = []
        for item in rows:
            remaining = int(_num(item.get("ord_remnq")))
            if remaining <= 0 or str(item.get("ord_cntr_tp") or "10").strip() == "12":   # 12: 취소주문
                continue
            result.append({
                "ticker": str(item.get("stk_cd") or "").strip().upper(),
                "side": "매도" if str(item.get("slby_tp") or "").strip() == "1" else "매수",
                "qty": int(_num(item.get("ord_qty"))),
                "remaining": remaining,
                "price": _price(item.get("ord_uv")),
                "ord_no": str(item.get("ord_no") or "").strip(),
            })
        return result

    # ─────────────────────────────────────────
    # 환율 조회
    # ─────────────────────────────────────────

    def get_usd_krw_rate(self) -> float:
        """달러/원 기준환율 조회 (ust21120 exrt_tp=0 crnc_rt, 실패 시 시세의 base_exrt)"""
        try:
            rows, _ = self._call_list(
                "ust21120", "/api/us/acnt",
                {"cmsn_incl_tp": "0", "exrt_tp": "0"},
                "result_list"
            )
            for item in rows:
                if str(item.get("crnc_code") or "").strip().upper() == "USD":
                    rate = _num(item.get("crnc_rt"))
                    if rate > 0:
                        logger.info(f"USD/KRW 환율: {rate:,.2f}")
                        return rate
        except Exception as e:
            logger.warning(f"기준환율(ust21120) 조회 실패, 시세 환율로 대체: {e}")
        try:
            ticker = next(iter(self._stex_cache), "AAPL")
            rate = _num(self._us_quote(ticker).get("base_exrt"))
            if rate > 0:
                logger.info(f"USD/KRW 환율(시세): {rate:,.2f}")
                return rate
        except Exception as e:
            logger.error(f"환율 조회 실패: {e}")
        return 0.0
