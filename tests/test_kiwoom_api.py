"""
kiwoom_api.py 단위 테스트 (네트워크 없이 가짜 세션 사용)

실행: python -m unittest discover -s tests -v
응답 형식은 키움 공식 스펙 예제(부호 붙은 가격, 0 패딩 숫자, 연속조회 헤더)를 따른다.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import kiwoom_api  # noqa: E402
from kiwoom_api import KiwoomAPI, KiwoomError, _num, _price, normalize_kr_code  # noqa: E402


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self._body = body
        self.status_code = status
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    """api-id(또는 토큰 URL)별 응답을 돌려주는 가짜 requests.Session"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        api_id = headers.get("api-id") or ("au10001" if url.endswith("/oauth2/token") else "?")
        self.calls.append({"url": url, "api_id": api_id, "headers": dict(headers), "body": json})
        route = self.routes.get(api_id)
        if route is None:
            raise AssertionError(f"예상하지 못한 호출: {api_id} {json}")
        if isinstance(route, list):
            if not route:
                raise AssertionError(f"{api_id} 응답 소진")
            item = route.pop(0)
        else:
            item = route
        if callable(item):
            item = item(json, headers)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, FakeResponse):
            return item
        return FakeResponse(item)

    def count(self, api_id):
        return sum(1 for c in self.calls if c["api_id"] == api_id)

    def bodies(self, api_id):
        return [c["body"] for c in self.calls if c["api_id"] == api_id]


TOKEN_OK = {"expires_dt": "20991231235959", "token_type": "bearer",
            "token": "TOKEN-1", "return_code": 0, "return_msg": "정상적으로 처리되었습니다"}


def ok(**fields):
    body = {"return_code": 0, "return_msg": "정상적으로 처리되었습니다"}
    body.update(fields)
    return body


class KiwoomTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patches = [
            mock.patch.object(kiwoom_api, "TOKEN_DIR", self.tmp.name),
            mock.patch.object(kiwoom_api, "REQUEST_INTERVAL_SEC", 0),
            mock.patch.object(kiwoom_api.time, "sleep", lambda s: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        kiwoom_api._token_cache.clear()
        KiwoomAPI._stex_cache.clear()
        KiwoomAPI._kr_name_cache.clear()
        self.addCleanup(self.tmp.cleanup)

    def make_api(self, routes, mode="real"):
        routes.setdefault("au10001", TOKEN_OK)
        session = FakeSession(routes)
        with mock.patch.object(kiwoom_api.requests, "Session", return_value=session):
            api = KiwoomAPI("APPKEY", "SECRET", "", mode=mode)
        return api, session


# ─────────────────────────────────────────
# 파싱 유틸
# ─────────────────────────────────────────

class ParsingTest(unittest.TestCase):

    def test_num_formats(self):
        self.assertEqual(_num("000000000012550"), 12550)
        self.assertEqual(_num("-00000000196888"), -196888)
        self.assertEqual(_num("+201.4700"), 201.47)
        self.assertEqual(_num("1,507.70"), 1507.70)
        self.assertEqual(_num("50%"), 50)
        self.assertEqual(_num(""), 0)
        self.assertEqual(_num(None), 0)
        self.assertEqual(_num("-"), 0)

    def test_price_is_absolute(self):
        self.assertEqual(_price("-74100"), 74100)
        self.assertEqual(_price("-198.4500"), 198.45)

    def test_normalize_kr_code(self):
        self.assertEqual(normalize_kr_code("A252670"), "252670")
        self.assertEqual(normalize_kr_code("Q500001"), "500001")
        self.assertEqual(normalize_kr_code("005930_NX"), "005930")
        self.assertEqual(normalize_kr_code("252670"), "252670")


# ─────────────────────────────────────────
# 토큰 / 공통 요청
# ─────────────────────────────────────────

class TokenAndRequestTest(KiwoomTestCase):

    def test_token_issue_request_and_cache_file(self):
        api, session = self.make_api({})
        call = session.calls[0]
        self.assertTrue(call["url"].startswith("https://api.kiwoom.com/oauth2/token"))
        self.assertEqual(call["body"], {"grant_type": "client_credentials",
                                        "appkey": "APPKEY", "secretkey": "SECRET"})
        self.assertEqual(api.access_token, "TOKEN-1")
        self.assertEqual(api.token_expired_at.year, 2099)
        with open(os.path.join(self.tmp.name, "kiwoom_token_real.json")) as f:
            saved = json.load(f)
        self.assertEqual(saved["access_token"], "TOKEN-1")
        self.assertIn("fingerprint", saved)

    def test_token_reused_from_memory_and_file(self):
        self.make_api({})
        _, session2 = self.make_api({})
        self.assertEqual(session2.count("au10001"), 0)   # 메모리 캐시

        kiwoom_api._token_cache.clear()
        _, session3 = self.make_api({})
        self.assertEqual(session3.count("au10001"), 0)   # 파일 캐시

    def test_token_file_ignored_for_other_keys(self):
        self.make_api({})
        kiwoom_api._token_cache.clear()
        session = FakeSession({"au10001": TOKEN_OK})
        with mock.patch.object(kiwoom_api.requests, "Session", return_value=session):
            KiwoomAPI("OTHERKEY", "SECRET", "")
        self.assertEqual(session.count("au10001"), 1)

    def test_demo_mode_uses_mock_domain(self):
        api, session = self.make_api({}, mode="demo")
        self.assertEqual(api.base_url, "https://mockapi.kiwoom.com")
        self.assertTrue(session.calls[0]["url"].startswith("https://mockapi.kiwoom.com"))

    def test_invalid_mode(self):
        with self.assertRaises(ValueError):
            KiwoomAPI("APPKEY", "SECRET", "", mode="12345678-01")

    def test_headers_and_string_return_code(self):
        api, session = self.make_api({"ka10001": {"return_code": "0000", "cur_prc": "+74100", "stk_nm": "X"}})
        self.assertEqual(api.get_kr_price("005930"), 74100)
        headers = session.calls[-1]["headers"]
        self.assertEqual(headers["api-id"], "ka10001")
        self.assertEqual(headers["authorization"], "Bearer TOKEN-1")
        self.assertNotIn("appkey", headers)

    def test_expired_token_is_reissued_once(self):
        tokens = [TOKEN_OK, dict(TOKEN_OK, token="TOKEN-2")]
        api, session = self.make_api({
            "au10001": tokens,
            "ka10001": [
                {"return_code": 3, "return_msg": "[8005:Token이 유효하지 않습니다]"},
                ok(cur_prc="74100", stk_nm="삼성전자"),
            ],
        })
        self.assertEqual(api.get_kr_price("005930"), 74100)
        self.assertEqual(session.count("au10001"), 2)
        self.assertEqual(session.calls[-1]["headers"]["authorization"], "Bearer TOKEN-2")

    def test_http_401_reissues_token(self):
        api, session = self.make_api({
            "au10001": [TOKEN_OK, dict(TOKEN_OK, token="TOKEN-2")],
            "ka10001": [FakeResponse({}, status=401), ok(cur_prc="1000")],
        })
        self.assertEqual(api.get_kr_price("005930"), 1000)

    def test_rate_limit_retry_then_success(self):
        api, session = self.make_api({"ka10001": [
            {"return_code": 1700, "return_msg": "허용된 API 요청 개수를 초과하였습니다"},
            FakeResponse({}, status=429),
            ok(cur_prc="1000"),
        ]})
        self.assertEqual(api.get_kr_price("005930"), 1000)
        self.assertEqual(session.count("ka10001"), 3)

    def test_rate_limit_exhausted_raises(self):
        api, _ = self.make_api({"ka10001": {"return_code": 1700, "return_msg": "초과"}})
        with self.assertRaises(KiwoomError) as ctx:
            api.get_kr_price("005930")
        self.assertEqual(ctx.exception.return_code, 1700)

    def test_non_json_200_is_failure(self):
        # 점검 페이지 등 JSON이 아닌 200 응답을 '보유 0주'로 오인하면 전량 매수가 나간다
        api, _ = self.make_api({"kt00018": FakeResponse(ValueError("not json"))})
        with self.assertRaises(KiwoomError):
            api.get_kr_balance()

    def test_missing_return_code_is_failure(self):
        api, _ = self.make_api({"ust21070": {"return_msg": "알 수 없는 응답"}})
        with self.assertRaises(KiwoomError):
            api.get_us_balance()

    def test_camel_case_return_code_accepted(self):
        api, _ = self.make_api({"ka10001": {"returnCode": 0, "cur_prc": "1000"}})
        self.assertEqual(api.get_kr_price("005930"), 1000)

    def test_business_error_raises(self):
        api, _ = self.make_api({"ka10001": {"return_code": 1902, "return_msg": "종목 정보가 없습니다"}})
        with self.assertRaises(KiwoomError):
            api.get_kr_price("999999")

    def test_pagination_follows_cont_headers(self):
        page1 = FakeResponse(ok(acnt_evlt_remn_indv_tot=[{"stk_cd": "A005930", "rmnd_qty": "000000000000001"}]),
                             headers={"cont-yn": "Y", "next-key": "K1"})
        page2 = FakeResponse(ok(acnt_evlt_remn_indv_tot=[{"stk_cd": "A252670", "rmnd_qty": "000000000000002"}]),
                             headers={"cont-yn": "N", "next-key": ""})
        api, session = self.make_api({"kt00018": [page1, page2], "kt00001": ok(**{"100stk_ord_alow_amt": "0"})})
        holdings, _ = api.get_kr_balance()
        self.assertEqual(set(holdings), {"005930", "252670"})
        second = [c for c in session.calls if c["api_id"] == "kt00018"][1]
        self.assertEqual(second["headers"]["cont-yn"], "Y")
        self.assertEqual(second["headers"]["next-key"], "K1")


# ─────────────────────────────────────────
# 국내 주식
# ─────────────────────────────────────────

class DomesticTest(KiwoomTestCase):

    def test_price_and_name(self):
        api, session = self.make_api({"ka10001": ok(stk_cd="005930", stk_nm="삼성전자", cur_prc="-74100")})
        self.assertEqual(api.get_kr_price("A005930"), 74100)
        self.assertEqual(session.bodies("ka10001")[0], {"stk_cd": "005930"})
        self.assertEqual(api.get_kr_name("A005930"), "삼성전자")
        self.assertEqual(session.count("ka10001"), 1)   # 이름은 캐시

    def test_zero_price_raises(self):
        api, _ = self.make_api({"ka10001": ok(cur_prc="")})
        with self.assertRaises(KiwoomError):
            api.get_kr_price("005930")

    def test_balance_aggregates_and_strips_prefix(self):
        rows = [
            {"stk_cd": "A252670", "stk_nm": "KODEX 200선물인버스2X", "rmnd_qty": "000000000000010",
             "pur_pric": "000000000002000", "pur_amt": "000000000020005"},
            {"stk_cd": "A252670", "rmnd_qty": "000000000000005",
             "pur_pric": "000000000002100", "pur_amt": "000000000010500"},
            {"stk_cd": "A005930", "rmnd_qty": "000000000000000", "pur_amt": "0"},
        ]
        api, session = self.make_api({
            "kt00018": ok(acnt_evlt_remn_indv_tot=rows),
            "kt00001": ok(**{"100stk_ord_alow_amt": "000000000012550", "d2_entra": "000000000099999"}),
        })
        holdings, cash = api.get_kr_balance()
        self.assertEqual(holdings, {"252670": {"qty": 15, "avg_price": 30505 / 15}})
        self.assertEqual(cash, 12550)
        self.assertEqual(session.bodies("kt00018")[0], {"qry_tp": "1", "dmst_stex_tp": "KRX"})
        self.assertEqual(session.bodies("kt00001")[0], {"qry_tp": "3"})
        self.assertEqual(api.get_kr_name("252670"), "KODEX 200선물인버스2X")

    def test_balance_cash_failure_returns_zero(self):
        api, _ = self.make_api({
            "kt00018": ok(acnt_evlt_remn_indv_tot=[]),
            "kt00001": {"return_code": 8104, "return_msg": "모의투자에서 지원하지 않는 API 입니다."},
        })
        self.assertEqual(api.get_kr_balance(), ({}, 0))

    def test_balance_holdings_failure_raises(self):
        # 잔고 조회 실패를 '보유 0주'로 오인하면 전량 매수하게 되므로 예외를 올린다
        api, _ = self.make_api({"kt00018": {"return_code": 1999, "return_msg": "예기치 못한 에러"}})
        with self.assertRaises(KiwoomError):
            api.get_kr_balance()

    def test_tick_size_table(self):
        api, _ = self.make_api({})
        cases = {1999: 1, 2000: 5, 4999: 5, 5000: 10, 19999: 10, 20000: 50,
                 49999: 50, 50000: 100, 199999: 100, 200000: 500, 499999: 500, 500000: 1000}
        for price, tick in cases.items():
            self.assertEqual(api._get_kr_tick_size(price), tick, price)

    def test_buy_order_body(self):
        api, session = self.make_api({
            "ka10001": ok(cur_prc="+10250", upl_pric="+13300", lst_pric="-7180"),
            "kt10000": ok(ord_no="0000138", dmst_stex_tp="KRX"),
        })
        self.assertTrue(api.buy_kr("A252670", 7, 10250))
        # 10250 * 1.03 = 10557.5 → 10557 → 호가 10원 내림 → 10550
        self.assertEqual(session.bodies("kt10000")[0], {
            "dmst_stex_tp": "KRX", "stk_cd": "252670", "ord_qty": "7",
            "ord_uv": "10550", "trde_tp": "0", "cond_uv": "",
        })

    def test_buy_clamped_to_upper_limit(self):
        api, session = self.make_api({
            "ka10001": ok(cur_prc="+12900", upl_pric="+13000", lst_pric="-7000"),
            "kt10000": ok(ord_no="0000139"),
        })
        self.assertTrue(api.buy_kr("252670", 1, 12900))
        self.assertEqual(session.bodies("kt10000")[0]["ord_uv"], "13000")

    def test_sell_clamped_to_lower_limit(self):
        api, session = self.make_api({
            "ka10001": ok(cur_prc="-7100", upl_pric="+13000", lst_pric="-7000"),
            "kt10001": ok(ord_no="0000140"),
        })
        self.assertTrue(api.sell_kr("252670", 3, 7100))
        self.assertEqual(session.bodies("kt10001")[0]["ord_uv"], "7000")

    def test_order_without_price_uses_current_price(self):
        api, session = self.make_api({
            "ka10001": ok(cur_prc="-2000", upl_pric="2600", lst_pric="1400"),
            "kt10001": ok(ord_no="0000141"),
        })
        self.assertTrue(api.sell_kr("252670", 1))
        # 2000 * 0.97 = 1940 → 호가 1원
        self.assertEqual(session.bodies("kt10001")[0]["ord_uv"], "1940")

    def test_order_rejected_returns_false(self):
        api, _ = self.make_api({
            "ka10001": ok(cur_prc="10000"),
            "kt10000": {"return_code": 1999, "return_msg": "주문가능금액 부족"},
        })
        self.assertFalse(api.buy_kr("252670", 1, 10000))

    def test_order_timeout_is_not_retried(self):
        api, session = self.make_api({
            "ka10001": ok(cur_prc="10000"),
            "kt10000": [requests.Timeout("timeout")],
        })
        self.assertFalse(api.buy_kr("252670", 1, 10000))
        self.assertEqual(session.count("kt10000"), 1)

    def test_order_without_ord_no_is_failure(self):
        api, _ = self.make_api({"ka10001": ok(cur_prc="10000"), "kt10000": ok()})
        self.assertFalse(api.buy_kr("252670", 1, 10000))


# ─────────────────────────────────────────
# 미국 주식
# ─────────────────────────────────────────

class OverseasTest(KiwoomTestCase):

    def test_exchange_detection_nyse_arca_etf(self):
        api, session = self.make_api({"usa10098": ok(list=[
            {"stex_tp": "NY", "stk_cd": "SOXL", "mkgb": "NYSE", "isEtf": "Y"}])})
        self.assertEqual(api.get_excd("soxl"), ("NY", "NY"))
        self.assertEqual(api.get_excd("SOXL"), ("NY", "NY"))
        self.assertEqual(session.count("usa10098"), 1)   # 캐시

    def test_exchange_detection_fallback_probe(self):
        def quote(body, headers):
            if body["stex_tp"] == "NY":
                return ok(cur_prc="+30.1200")
            return {"return_code": 1903, "return_msg": "종목 정보가 없습니다"}
        api, _ = self.make_api({"usa10098": ok(list=[]), "usa20100": quote})
        self.assertEqual(api.get_excd("UGL"), ("NY", "NY"))

    def test_exchange_detection_failure_blocks_order(self):
        api, session = self.make_api({
            "usa10098": ok(list=[]),
            "usa20100": {"return_code": 1903, "return_msg": "종목 정보가 없습니다"},
        })
        self.assertFalse(api.buy_us("ZZZZ", 1, 10.0))
        self.assertEqual(session.count("ust20000"), 0)

    def test_us_price_sign_and_fallback(self):
        api, session = self.make_api({
            "usa10098": ok(list=[{"stex_tp": "ND", "stk_cd": "TQQQ"}]),
            "usa20100": [ok(cur_prc="-198.4500", base_close_pric="201.0000"),
                         ok(cur_prc="", base_close_pric="200.0400")],
        })
        self.assertEqual(api.get_us_price("TQQQ"), 198.45)
        self.assertEqual(api.get_us_price("TQQQ"), 200.04)
        self.assertEqual(session.bodies("usa20100")[0], {"stex_tp": "ND", "stk_cd": "TQQQ"})

    def test_us_balance_and_cash(self):
        rows = [
            {"crnc_code": "USD", "stk_cd": "TQQQ", "poss_qty": "000000000395", "qty": "000000000395",
             "frgn_stk_book_uv": "282.1603", "now_pric": "275.2400", "frgn_stk_book_uv_krw": "000000430153"},
            {"crnc_code": "USD", "stk_cd": "UGL", "poss_qty": "000000000000"},
        ]
        api, session = self.make_api({
            "ust21070": ok(result_list=rows),
            "usa10098": ok(list=[{"stex_tp": "ND", "stk_cd": "TQQQ"}]),
            "ust31490": ok(ord_alowa="16072427.01", min_tdy_rebuy_alowa="1993584.88",
                           min_pred_rebuy_alowa="0.00", min_ord_alowa="18666197.34",
                           krw_entra="000923780186", crnc_code="USD"),
        })
        holdings, cash = api.get_us_balance()
        self.assertEqual(holdings, {"TQQQ": {"qty": 395, "avg_price": 282.1603}})
        # 주문가능현금 + 미결제 매도대금(재사용), 원화 환산분(min_ord_alowa 차액)은 제외
        self.assertAlmostEqual(cash, 16072427.01 + 1993584.88, places=2)
        self.assertEqual(session.bodies("ust21070")[0], {"stex_tp": "", "stk_cd": ""})
        self.assertEqual(session.bodies("ust31490")[0], {"stex_tp": "ND", "stk_cd": "TQQQ", "uv": "275.24"})
        self.assertEqual(session.count("usa20100"), 0)   # 보유종목 현재가 재사용

    def test_us_cash_falls_back_to_deposit(self):
        api, _ = self.make_api({
            "ust21070": ok(result_list=[{"crnc_code": "USD", "stk_cd": "TQQQ", "poss_qty": "1",
                                         "frgn_stk_book_uv": "50", "now_pric": "51"}]),
            "usa10098": ok(list=[{"stex_tp": "ND", "stk_cd": "TQQQ"}]),
            "ust31490": {"return_code": 8104, "return_msg": "모의투자에서 지원하지 않는 API 입니다."},
            "ust21160": ok(d0_usd_fx_entr="1234.560", d1_usd_fx_entr="1040.120",
                           d2_usd_fx_entr="1840.120", d3_usd_fx_entr="", d4_usd_fx_entr=""),
            "ust21110": ok(result_list=[
                {"crnc_code": "GBP", "fc_entra": "5.00"},
                {"crnc_code": "USD", "fc_entra": "1234.56", "fc_pymn_alowa": "1000.00"}]),
        })
        # 누적 버킷의 마지막 값(D2)이 미결제 매수·매도를 모두 반영
        self.assertEqual(api.get_us_balance()[1], 1840.12)
        self.assertEqual(api.get_us_drwg_amt(), 1000.00)

    def test_us_balance_failure_raises(self):
        api, _ = self.make_api({"ust21070": {"return_code": 1999, "return_msg": "오류"}})
        with self.assertRaises(KiwoomError):
            api.get_us_balance()

    def test_buy_and_sell_order_body(self):
        api, session = self.make_api({
            "usa10098": ok(list=[{"stex_tp": "ND", "stk_cd": "TQQQ"}]),
            "usa20100": ok(cur_prc="+50.0000"),
            "ust20000": ok(ord_no="000000282"),
            "ust20001": ok(ord_no="000000283"),
        })
        self.assertTrue(api.buy_us("TQQQ", 10))
        self.assertTrue(api.sell_us("TQQQ", 3, 50.0))
        self.assertEqual(session.bodies("ust20000")[0], {
            "stex_tp": "ND", "stk_cd": "TQQQ", "ord_qty": "10", "trde_tp": "00", "ord_uv": "51.50"})
        self.assertEqual(session.bodies("ust20001")[0], {
            "stex_tp": "ND", "stk_cd": "TQQQ", "ord_qty": "3", "trde_tp": "00", "ord_uv": "48.50",
            "stop_pric": ""})

    def test_us_order_single_call_even_with_cont_header(self):
        api, session = self.make_api({
            "usa10098": ok(list=[{"stex_tp": "ND", "stk_cd": "TQQQ"}]),
            "ust20000": FakeResponse(ok(ord_no="000000284"), headers={"cont-yn": "Y", "next-key": "X"}),
        })
        self.assertTrue(api.buy_us("TQQQ", 1, 50.0))
        self.assertEqual(session.count("ust20000"), 1)

    def test_usd_krw_rate(self):
        api, session = self.make_api({"ust21120": ok(result_list=[
            {"crnc_code": "JPY", "crnc_rt": "9.45"},
            {"crnc_code": "USD", "crnc_rt": "1507.70"}])})
        self.assertEqual(api.get_usd_krw_rate(), 1507.70)
        self.assertEqual(session.bodies("ust21120")[0], {"cmsn_incl_tp": "0", "exrt_tp": "0"})

    def test_usd_krw_rate_fallback_to_quote(self):
        api, _ = self.make_api({
            "ust21120": {"return_code": 8104, "return_msg": "모의투자 미지원"},
            "usa10098": ok(list=[{"stex_tp": "ND", "stk_cd": "AAPL"}]),
            "usa20100": ok(cur_prc="+201.4700", base_exrt="1520.80"),
        })
        self.assertEqual(api.get_usd_krw_rate(), 1520.80)


if __name__ == "__main__":
    unittest.main()


class UnfilledTest(KiwoomTestCase):

    def test_kr_unfilled(self):
        rows = [
            {"stk_cd": "A252670", "io_tp_nm": "-매도", "ord_qty": "20", "oso_qty": "5",
             "ord_pric": "10000", "ord_no": "0000069"},
            {"stk_cd": "005930", "io_tp_nm": "+매수", "ord_qty": "3", "oso_qty": "0",
             "ord_pric": "74100", "ord_no": "0000070"},   # 전량 체결 → 제외
        ]
        api, session = self.make_api({"ka10075": ok(oso=rows)})
        self.assertEqual(api.get_kr_unfilled(), [
            {"ticker": "252670", "side": "매도", "qty": 20, "remaining": 5,
             "price": 10000.0, "ord_no": "0000069"},
        ])
        self.assertEqual(session.bodies("ka10075")[0],
                         {"all_stk_tp": "0", "trde_tp": "0", "stk_cd": "", "stex_tp": "0"})

    def test_us_unfilled(self):
        rows = [
            {"ord_cntr_tp": "10", "ord_no": "000000282", "stk_cd": "TQQQ", "slby_tp": "2",
             "ord_qty": "000000000005", "ord_uv": "51.5000", "ord_remnq": "000000000005"},
            {"ord_cntr_tp": "12", "ord_no": "000000283", "stk_cd": "TQQQ", "slby_tp": "1",
             "ord_qty": "000000000002", "ord_uv": "0.0000", "ord_remnq": "000000000002"},   # 취소주문
            {"ord_cntr_tp": "10", "ord_no": "000000284", "stk_cd": "UGL", "slby_tp": "1",
             "ord_qty": "000000000004", "ord_uv": "30.0000", "ord_remnq": "000000000000"},  # 체결 완료
        ]
        api, _ = self.make_api({"ust21050": ok(result_list=rows)})
        self.assertEqual(api.get_us_unfilled(), [
            {"ticker": "TQQQ", "side": "매수", "qty": 5, "remaining": 5,
             "price": 51.5, "ord_no": "000000282"},
        ])

    def test_unfilled_failure_raises(self):
        api, _ = self.make_api({"ust21050": {"return_code": 1999, "return_msg": "오류"}})
        with self.assertRaises(KiwoomError):
            api.get_us_unfilled()
