"""
kis_api.py
한국투자증권 KIS Developers API 연동
- 액세스 토큰 자동 발급/갱신
- 국내/해외 주식 현재가, 잔고, 주문
"""

import requests
import json
import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

BASE_URL = "https://openapi.koreainvestment.com:9443"
TOKEN_FILE = os.path.join(os.path.dirname(__file__), "kis_token.json")


class KisAPI:

    def __init__(self, app_key: str, app_secret: str, account_no: str):
        self.app_key    = app_key
        self.app_secret = app_secret
        self.account_no = account_no  # 예: "12345678-01"
        self.access_token = None
        self.token_expired_at = None
        self._load_or_issue_token()

    # ─────────────────────────────────────────
    # 토큰 관리
    # ─────────────────────────────────────────

    def _load_or_issue_token(self):
        """저장된 토큰 로드, 없거나 만료 시 새로 발급"""
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, "r") as f:
                data = json.load(f)
            expired_at = datetime.fromisoformat(data["expired_at"])
            if datetime.now() < expired_at - timedelta(minutes=10):
                self.access_token    = data["access_token"]
                self.token_expired_at = expired_at
                logger.info("기존 토큰 로드 성공")
                return
        self._issue_token()

    def _issue_token(self):
        url = f"{BASE_URL}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey":     self.app_key,
            "appsecret":  self.app_secret
        }
        res = requests.post(url, json=body)
        res.raise_for_status()
        data = res.json()
        self.access_token     = data["access_token"]
        expires_in            = int(data.get("expires_in", 86400))
        self.token_expired_at = datetime.now() + timedelta(seconds=expires_in)
        # 저장
        with open(TOKEN_FILE, "w") as f:
            json.dump({
                "access_token": self.access_token,
                "expired_at":   self.token_expired_at.isoformat()
            }, f)
        logger.info("새 액세스 토큰 발급 완료")

    def _ensure_token(self):
        if not self.access_token or datetime.now() >= self.token_expired_at - timedelta(minutes=10):
            self._issue_token()

    def _headers(self, tr_id: str, extra: dict = None) -> dict:
        self._ensure_token()
        h = {
            "Content-Type":  "application/json",
            "authorization": f"Bearer {self.access_token}",
            "appkey":        self.app_key,
            "appsecret":     self.app_secret,
            "tr_id":         tr_id,
        }
        if extra:
            h.update(extra)
        return h

    def _get(self, path: str, tr_id: str, params: dict) -> dict:
        res = requests.get(
            f"{BASE_URL}{path}",
            headers=self._headers(tr_id),
            params=params
        )
        res.raise_for_status()
        data = res.json()
        if data.get("rt_cd") != "0":
            raise Exception(f"KIS API 오류: {data.get('msg1')} (tr_id={tr_id})")
        return data

    def _post(self, path: str, tr_id: str, body: dict) -> dict:
        res = requests.post(
            f"{BASE_URL}{path}",
            headers=self._headers(tr_id),
            json=body
        )
        res.raise_for_status()
        data = res.json()
        if data.get("rt_cd") != "0":
            raise Exception(f"KIS API 오류: {data.get('msg1')} (tr_id={tr_id})")
        return data

    # ─────────────────────────────────────────
    # 계좌번호 파싱
    # ─────────────────────────────────────────

    def _account_prefix(self) -> str:
        """계좌번호 앞 8자리"""
        return self.account_no.replace("-", "")[:8]

    def _account_suffix(self) -> str:
        """계좌번호 뒤 2자리"""
        return self.account_no.replace("-", "")[8:10]

    # ─────────────────────────────────────────
    # 국내 주식
    # ─────────────────────────────────────────

    _kr_name_cache: dict = {}

    def get_kr_name(self, ticker: str) -> str:
        """국내 종목명 조회 (캐시 적용)"""
        if ticker in self._kr_name_cache:
            return self._kr_name_cache[ticker]
        try:
            data = self._get(
                "/uapi/domestic-stock/v1/quotations/inquire-price",
                "FHKST01010100",
                {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker}
            )
            name = data["output"].get("hts_kor_isnm", ticker)
            self._kr_name_cache[ticker] = name
            return name
        except Exception:
            return ticker

    def get_kr_price(self, ticker: str) -> float:
        """국내 주식 현재가"""
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            "FHKST01010100",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker}
        )
        # 종목명도 캐시에 저장
        name = data["output"].get("hts_kor_isnm", ticker)
        self._kr_name_cache[ticker] = name
        return float(data["output"]["stck_prpr"])

    def get_kr_balance(self) -> tuple:
        """
        국내 잔고 조회
        반환: (holdings_dict, cash_krw)
        holdings_dict = {ticker: {"qty": int, "avg_price": float}}
        """
        data = self._get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            "TTTC8434R",
            {
                "CANO":          self._account_prefix(),
                "ACNT_PRDT_CD":  self._account_suffix(),
                "AFHR_FLPR_YN":  "N",
                "OFL_YN":        "N",
                "INQR_DVSN":     "02",
                "UNPR_DVSN":     "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN":     "01",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": ""
            }
        )
        holdings = {}
        for item in data.get("output1", []):
            ticker = item["pdno"]
            qty    = int(item["hldg_qty"])
            avg    = float(item["pchs_avg_pric"])
            if qty > 0:
                holdings[ticker] = {"qty": qty, "avg_price": avg}

        # ── 주문가능금액은 TTTC8908R (nrcvb_buy_amt) ──
        cash = 0
        try:
            # 아무 종목이나 사용 (첫 번째 보유 종목 또는 기본값)
            sample_ticker = list(holdings.keys())[0] if holdings else "005930"
            data2 = self._get(
                "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
                "TTTC8908R",
                {
                    "CANO":              self._account_prefix(),
                    "ACNT_PRDT_CD":      self._account_suffix(),
                    "PDNO":              sample_ticker,
                    "ORD_UNPR":          "0",
                    "ORD_DVSN":          "01",
                    "CMA_EVLU_AMT_ICLD_YN": "N",
                    "OVRS_ICLD_YN":      "N"
                }
            )
            cash = int(float(data2["output"].get("nrcvb_buy_amt", 0)))
            logger.info(f"원화 주문가능금액: ₩{cash:,}")
        except Exception as e:
            logger.error(f"원화 주문가능금액 조회 실패: {e}")

        return holdings, cash

    def get_kr_drwg_amt(self) -> int:
        """원화 출금가능금액 조회 (TTTC8434R dnca_tot_amt)"""
        try:
            data = self._get(
                "/uapi/domestic-stock/v1/trading/inquire-balance",
                "TTTC8434R",
                {
                    "CANO":          self._account_prefix(),
                    "ACNT_PRDT_CD":  self._account_suffix(),
                    "AFHR_FLPR_YN":  "N",
                    "OFL_YN":        "N",
                    "INQR_DVSN":     "02",
                    "UNPR_DVSN":     "01",
                    "FUND_STTL_ICLD_YN": "N",
                    "FNCG_AMT_AUTO_RDPT_YN": "N",
                    "PRCS_DVSN":     "01",
                    "CTX_AREA_FK100": "",
                    "CTX_AREA_NK100": ""
                }
            )
            return int(float(data["output2"][0].get("dnca_tot_amt", 0)))
        except Exception as e:
            logger.error(f"원화 출금가능금액 조회 실패: {e}")
        return 0

    def _get_kr_tick_size(self, price: int) -> int:
        """국내주식 호가단위 반환 (한국거래소 기준)"""
        if price < 1000:       return 1
        elif price < 5000:     return 5
        elif price < 10000:    return 10
        elif price < 50000:    return 50
        elif price < 100000:   return 100
        elif price < 500000:   return 500
        else:                  return 1000

    def buy_kr(self, ticker: str, qty: int, price: int = 0) -> bool:
        """국내 주식 매수 - 지정가 (현재가 +3%, 호가단위 적용)"""
        try:
            if price <= 0:
                price = int(self.get_kr_price(ticker))
            raw_price  = int(price * 1.03)
            tick       = self._get_kr_tick_size(raw_price)
            order_price = (raw_price // tick) * tick  # 호가단위 내림
            self._post(
                "/uapi/domestic-stock/v1/trading/order-cash",
                "TTTC0802U",
                {
                    "CANO":         self._account_prefix(),
                    "ACNT_PRDT_CD": self._account_suffix(),
                    "PDNO":         ticker,
                    "ORD_DVSN":     "00",
                    "ORD_QTY":      str(qty),
                    "ORD_UNPR":     str(order_price),
                    "CTAC_TLNO":    "",
                    "SLL_TYPE":     "01",
                    "ALGO_NO":      ""
                }
            )
            logger.info(f"국내 매수 성공: {ticker} {qty}주 ₩{order_price:,} (현재가 +3%)")
            return True
        except Exception as e:
            logger.error(f"국내 매수 실패 ({ticker}): {e}")
            return False

    def sell_kr(self, ticker: str, qty: int, price: int = 0) -> bool:
        """국내 주식 매도 - 지정가 (현재가 -3%, 호가단위 적용)"""
        try:
            if price <= 0:
                price = int(self.get_kr_price(ticker))
            raw_price   = int(price * 0.97)
            tick        = self._get_kr_tick_size(raw_price)
            order_price = (raw_price // tick) * tick  # 호가단위 내림
            self._post(
                "/uapi/domestic-stock/v1/trading/order-cash",
                "TTTC0801U",
                {
                    "CANO":         self._account_prefix(),
                    "ACNT_PRDT_CD": self._account_suffix(),
                    "PDNO":         ticker,
                    "ORD_DVSN":     "00",
                    "ORD_QTY":      str(qty),
                    "ORD_UNPR":     str(order_price),
                    "CTAC_TLNO":    "",
                    "SLL_TYPE":     "01",
                    "ALGO_NO":      ""
                }
            )
            logger.info(f"국내 매도 성공: {ticker} {qty}주 ₩{order_price:,} (현재가 -3%)")
            return True
        except Exception as e:
            logger.error(f"국내 매도 실패 ({ticker}): {e}")
            return False

    # ─────────────────────────────────────────
    # 거래소 코드 자동 감지
    # ─────────────────────────────────────────

    # 거래소 코드 캐시 (매번 API 호출 방지)
    _excd_cache: dict = {}

    # KIS API 거래소 코드 매핑
    # EXCD(시세조회) → OVRS_EXCG_CD(주문)
    _EXCD_MAP = {
        "NAS": "NASD",  # 나스닥
        "NYS": "NYSE",  # 뉴욕증권거래소
        "AMS": "AMEX",  # 아메리칸증권거래소
        "BAQ": "NASD",  # 나스닥 (대체코드)
    }

    def get_excd(self, ticker: str) -> tuple:
        """
        ticker의 거래소 코드 자동 조회
        반환: (EXCD, OVRS_EXCG_CD)
        예: ("NAS", "NASD") or ("NYS", "NYSE")
        """
        if ticker in self._excd_cache:
            return self._excd_cache[ticker]

        # 나스닥, NYSE, AMEX 순으로 시도
        for excd in ["NAS", "NYS", "AMS"]:
            try:
                data = self._get(
                    "/uapi/overseas-price/v1/quotations/price",
                    "HHDFS00000300",
                    {"AUTH": "", "EXCD": excd, "SYMB": ticker}
                )
                price = float(data["output"]["last"])
                if price > 0:
                    ovrs_cd = self._EXCD_MAP.get(excd, "NASD")
                    self._excd_cache[ticker] = (excd, ovrs_cd)
                    logger.info(f"{ticker} 거래소 자동 감지: {excd} ({ovrs_cd})")
                    return excd, ovrs_cd
            except Exception:
                continue

        # 기본값 나스닥
        logger.warning(f"{ticker} 거래소 감지 실패 → 나스닥으로 기본 설정")
        self._excd_cache[ticker] = ("NAS", "NASD")
        return "NAS", "NASD"

    # ─────────────────────────────────────────
    # 해외 주식
    # ─────────────────────────────────────────

    def get_us_price(self, ticker: str) -> float:
        """미국 주식 현재가 (달러) - 거래소 자동 감지"""
        excd, _ = self.get_excd(ticker)
        data = self._get(
            "/uapi/overseas-price/v1/quotations/price",
            "HHDFS00000300",
            {"AUTH": "", "EXCD": excd, "SYMB": ticker}
        )
        return float(data["output"]["last"])

    def get_us_extended_price(self, ticker: str) -> float:
        """미국 주식 시간외 현재가 (프리/애프터마켓) - 거래소 자동 감지"""
        excd, _ = self.get_excd(ticker)
        try:
            data = self._get(
                "/uapi/overseas-price/v1/quotations/overtime-price",
                "HHDFS76200100",
                {"AUTH": "", "EXCD": excd, "SYMB": ticker}
            )
            price = float(data["output"]["last"])
            if price > 0:
                return price
        except Exception as e:
            logger.warning(f"{ticker} 시간외 가격 조회 실패, 현재가로 대체: {e}")
        return self.get_us_price(ticker)

    def get_us_balance(self) -> tuple:
        """
        해외 잔고 조회 (전체 거래소 통합)
        반환: (holdings_dict, cash_usd)
        """
        holdings = {}

        # 나스닥, NYSE, AMEX 전체 조회
        for ovrs_cd in ["NASD", "NYSE", "AMEX"]:
            try:
                data = self._get(
                    "/uapi/overseas-stock/v1/trading/inquire-balance",
                    "TTTS3012R",
                    {
                        "CANO":           self._account_prefix(),
                        "ACNT_PRDT_CD":   self._account_suffix(),
                        "OVRS_EXCG_CD":   ovrs_cd,
                        "TR_CRCY_CD":     "USD",
                        "CTX_AREA_FK200": "",
                        "CTX_AREA_NK200": ""
                    }
                )
                for item in data.get("output1", []):
                    ticker = item["ovrs_pdno"]
                    qty    = float(item["ovrs_cblc_qty"])
                    avg    = float(item["pchs_avg_pric"])
                    if qty > 0:
                        holdings[ticker] = {"qty": qty, "avg_price": avg}
                        excd = "NAS" if ovrs_cd == "NASD" else \
                               "NYS" if ovrs_cd == "NYSE" else "AMS"
                        self._excd_cache[ticker] = (excd, ovrs_cd)
            except Exception:
                continue

        # ── 주문가능금액은 TTTS3007R (ovrs_ord_psbl_amt) ──
        cash_usd = 0.0
        try:
            data = self._get(
                "/uapi/overseas-stock/v1/trading/inquire-psamount",
                "TTTS3007R",
                {
                    "CANO":           self._account_prefix(),
                    "ACNT_PRDT_CD":   self._account_suffix(),
                    "OVRS_EXCG_CD":   "NASD",
                    "OVRS_ORD_UNPR":  "0",
                    "ITEM_CD":        "AAPL"
                }
            )
            cash_usd = float(data["output"].get("ovrs_ord_psbl_amt", 0))
            logger.info(f"달러 주문가능금액: ${cash_usd:,.2f}")
        except Exception as e:
            logger.error(f"달러 주문가능금액 조회 실패: {e}")

        return holdings, cash_usd

    def get_us_drwg_amt(self) -> float:
        """달러 익일 출금가능금액 조회 (CTRP6504R nxdy_frcr_drwg_psbl_amt)"""
        try:
            data = self._get(
                "/uapi/overseas-stock/v1/trading/inquire-present-balance",
                "CTRP6504R",
                {
                    "CANO":              self._account_prefix(),
                    "ACNT_PRDT_CD":      self._account_suffix(),
                    "WCRC_FRCR_DVSN_CD": "02",
                    "NATN_CD":           "840",
                    "TR_MKET_CD":        "00",
                    "INQR_DVSN_CD":      "00"
                }
            )
            for item in data.get("output2", []):
                if item.get("crcy_cd") == "USD":
                    return float(item.get("nxdy_frcr_drwg_psbl_amt", 0))
        except Exception as e:
            logger.error(f"달러 출금가능금액 조회 실패: {e}")
        return 0.0

    def buy_us(self, ticker: str, qty: int, price: float = 0) -> bool:
        """미국 주식 매수 - 지정가 (현재가 +3%, 거래소 자동 감지)"""
        try:
            _, ovrs_cd = self.get_excd(ticker)
            if price <= 0:
                price = self.get_us_price(ticker)
            order_price = round(price * 1.03, 2)
            self._post(
                "/uapi/overseas-stock/v1/trading/order",
                "TTTT1002U",
                {
                    "CANO":            self._account_prefix(),
                    "ACNT_PRDT_CD":    self._account_suffix(),
                    "OVRS_EXCG_CD":    ovrs_cd,
                    "PDNO":            ticker,
                    "ORD_QTY":         str(qty),
                    "OVRS_ORD_UNPR":   f"{order_price:.2f}",
                    "ORD_SVR_DVSN_CD": "0",
                    "ORD_DVSN":        "00"
                }
            )
            logger.info(f"해외 매수 성공: {ticker} {qty}주 ({ovrs_cd}) ${order_price:.2f} (현재가 +3%)")
            return True
        except Exception as e:
            logger.error(f"해외 매수 실패 ({ticker}): {e}")
            return False

    def sell_us(self, ticker: str, qty: int, price: float = 0) -> bool:
        """미국 주식 매도 - 지정가 (현재가 -3%, 거래소 자동 감지)"""
        try:
            _, ovrs_cd = self.get_excd(ticker)
            if price <= 0:
                price = self.get_us_price(ticker)
            order_price = round(price * 0.97, 2)
            self._post(
                "/uapi/overseas-stock/v1/trading/order",
                "TTTT1006U",
                {
                    "CANO":            self._account_prefix(),
                    "ACNT_PRDT_CD":    self._account_suffix(),
                    "OVRS_EXCG_CD":    ovrs_cd,
                    "PDNO":            ticker,
                    "ORD_QTY":         str(qty),
                    "OVRS_ORD_UNPR":   f"{order_price:.2f}",
                    "ORD_SVR_DVSN_CD": "0",
                    "ORD_DVSN":        "00"
                }
            )
            logger.info(f"해외 매도 성공: {ticker} {qty}주 ({ovrs_cd}) ${order_price:.2f} (현재가 -3%)")
            return True
        except Exception as e:
            logger.error(f"해외 매도 실패 ({ticker}): {e}")
            return False

    # ─────────────────────────────────────────
    # 환율 조회
    # ─────────────────────────────────────────

    def get_usd_krw_rate(self) -> float:
        """달러/원 환율 조회"""
        try:
            data = self._get(
                "/uapi/overseas-stock/v1/trading/inquire-present-balance",
                "CTRP6504R",
                {
                    "CANO":              self._account_prefix(),
                    "ACNT_PRDT_CD":      self._account_suffix(),
                    "WCRC_FRCR_DVSN_CD": "02",
                    "NATN_CD":           "840",
                    "TR_MKET_CD":        "00",
                    "INQR_DVSN_CD":      "00"
                }
            )
            for item in data.get("output2", []):
                if item.get("crcy_cd") == "USD":
                    rate = float(item.get("frst_bltn_exrt", 0))
                    logger.info(f"USD/KRW 환율: {rate:,.2f}")
                    return rate
            return 0.0
        except Exception as e:
            logger.error(f"환율 조회 실패: {e}")
            return 0.0
