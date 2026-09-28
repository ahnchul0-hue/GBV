"당신과 나를 위한 TQQQ 투자법" 독자를 위한 공개 코드입니다.

## 증권사 선택 (키움 / KIS)

`config.txt`의 `broker` 값으로 증권사를 고릅니다. 기본값은 키움입니다.

```
broker      = kiwoom     # kiwoom 또는 kis
kiwoom_mode = real       # real(실전) 또는 demo(모의투자)
app_key    = <App Key>
app_secret = <Secret Key>
account_no =             # 키움은 선택 (앱키에 계좌가 연결됨), KIS는 필수
```

### 키움 REST API 준비

1. [키움 OpenAPI 포털](https://openapi.kiwoom.com)에서 사용 신청 후 앱을 등록하고 App Key / Secret Key를 발급합니다.
2. 실전과 모의투자 키는 서로 다릅니다. `kiwoom_mode`와 맞는 키를 넣으세요.
3. 해외주식(미국) 매매를 하려면 해당 계좌에서 해외주식 거래가 가능해야 합니다.
4. 토큰은 발급받은 IP에서만 쓸 수 있습니다. 봇을 실행하는 PC에서 발급되며 `kiwoom_token_<mode>.json`에 저장됩니다(커밋 금지, `.gitignore` 포함).

### 설치 및 실행

```
pip install -r requirements.txt
python main.py
```

### 테스트

```
python -m unittest discover -s tests -v
```

### 실계좌 적용 전 확인할 것

- `kiwoom_mode = demo`로 먼저 돌려 보세요. 모의투자에서는 일부 조회(주문가능금액, 환율)가 거절될 수 있으며, 이 경우 예수금·시세 환율로 대체됩니다.
- 실전 전환 후 첫 매매는 기준금을 작게 잡아 주문 접수·체결·잔고 반영을 텔레그램 `/balance`로 확인하세요.
- 미국장 매매 시각(`us_market_time`, 기본 19:30)은 미국 프리마켓 시간입니다. 키움에서 이 시각의 지정가 주문이 프리마켓에서 체결되는지 소액으로 먼저 확인하세요.
