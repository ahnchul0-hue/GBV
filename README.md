"당신과 나를 위한 TQQQ 투자법" 독자를 위한 공개 코드입니다.

## 키움증권 REST API 설정

`config.txt.example`을 `config.txt`로 복사한 뒤 키움 앱키를 입력합니다.
(`config.txt`는 실제 키가 들어가므로 git에 올라가지 않습니다.)

```
kiwoom_mode = real       # real(실전) 또는 demo(모의투자)
app_key    = <App Key>
app_secret = <Secret Key>
account_no =             # 선택 (계좌는 앱키에 연결됨)
```

### 키움 REST API 준비

1. [키움 OpenAPI 포털](https://openapi.kiwoom.com)에서 사용 신청 후 앱을 등록하고 App Key / Secret Key를 발급합니다.
2. 실전과 모의투자 키는 서로 다릅니다. `kiwoom_mode`와 맞는 키를 넣으세요.
3. 해외주식(미국) 매매를 하려면 해당 계좌에서 해외주식 거래가 가능해야 합니다.
4. 토큰은 발급받은 IP에서만 쓸 수 있습니다. 봇을 실행하는 PC에서 발급되며 `kiwoom_token_<mode>.json`에 저장됩니다(커밋 금지, `.gitignore` 포함).

### 텔레그램 설정

`config.txt`에 봇 토큰과 본인 채팅 ID를 입력합니다 (`notifier.py`는 고치지 않아도 됩니다).

```
telegram_bot_token = <BotFather에서 받은 토큰>
telegram_chat_id   = <본인 채팅 ID>
```

- 텔레그램 명령(`/set`, `/add_gbv` 등)은 `telegram_chat_id` 채팅에서 온 것만 처리합니다. 다른 사람이 봇에 명령을 보내면 무시하고 로그에 남깁니다.
- `telegram_chat_id`가 비어 있으면 명령을 받지 않습니다(알림 전송도 되지 않음).

### 원격 상태 확인 (텔레그램)

| 명령 | 내용 |
|---|---|
| `/health` | 가동 시간, 모드(실전/모의·dry_run), 시장별 오늘 매매 여부와 다음 매매 시각(휴장일 반영), 마지막 오류 |
| `/log` | 최근 로그 20줄 (`/log 50`: 50줄, 최대 100 / `/log err`: 경고·오류만) |

- 매일 `heartbeat_time`(기본 08:30, 주말 포함)에 `☀️ [GBV] 봇 정상 작동 중` 상태 알림을 보냅니다. 이 알림이 오지 않으면 봇이나 PC가 꺼진 것입니다.
- 시각 변경: `/set heartbeat_time 07:00`, 끄기: `/set heartbeat_time off`

### 설치 및 실행

```
pip install -r requirements.txt
python main.py
```

### 자동 시작 (윈도우 작업 스케줄러)

봇을 수동으로 띄우면 원격 세션이 끊기거나 PC 가 재부팅될 때 조용히 멈춥니다.
`run_bot.bat` 를 작업 스케줄러에 등록해 두면 로그온할 때 자동으로 뜹니다.

PowerShell 에서 (관리자 권한 필요 없음):

```powershell
$action = New-ScheduledTaskAction -Execute 'C:\...\GBV\run_bot.bat' -WorkingDirectory 'C:\...\GBV'
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName 'GBV Trading Bot' -Action $action -Trigger $trigger -Settings $settings
```

- `-ExecutionTimeLimit ([TimeSpan]::Zero)` 가 없으면 작업이 3일 뒤 강제 종료됩니다.
- 비정상 종료 시 1분 간격으로 3회 재시작합니다. Ctrl+C 로 끝낸 경우는 종료코드가 0 이라
  재시작하지 않습니다.
- 이미 봇이 돌고 있으면 단일 실행 잠금이 막아 주므로 중복 실행 걱정은 없습니다.
- 로그온하지 않아도 뜨게 하려면 작업을 `SYSTEM` 계정으로 바꾸고 트리거를 시작 시로 두면
  되는데, 콘솔이 보이지 않아 Ctrl+C 로 끄지 못합니다. 손으로 껐다 켜는 일이 많으면
  로그온 트리거가 편합니다.

### 주간 감사 (선택, Claude API)

`weekly_audit.py`는 지난 7일의 로그·리포트·설정 요약을 Claude 에 보내 **세션 누락, 반복 오류, 체결 품질 변화, 설정 변경 흔적** 네 가지만 짚어 텔레그램으로 보냅니다. 주문 권한이 없고 예측·추천을 하지 않습니다. `main.py`는 이 파일을 쓰지 않으므로 주문 경로에는 LLM 이 없습니다.

```
copy .env.example .env      ← ANTHROPIC_API_KEY 를 채웁니다 (.env 는 git 에 올라가지 않음)
pip install anthropic
python weekly_audit.py --stdout     ← 화면에서 먼저 확인
python weekly_audit.py              ← 텔레그램 발송
```

- 모델은 기본 `claude-opus-5`, 1회 약 $0.1~0.2 입니다. `.env`에 `GBV_AUDIT_MODEL=claude-sonnet-5` 로 바꿀 수 있습니다.
- 보내는 자료에서 봇 토큰·API 키는 가려집니다(`log_redact`). 계좌번호·앱키는 아예 넣지 않습니다.
- 매주 돌리려면 위 자동 시작과 같은 방식으로 작업 스케줄러에 등록하세요 (작업명 `GBV Weekly Audit`, 매주 일요일 09:00 권장 — 08:30 생존 신호 뒤, 휴장일).
- 같은 내용을 Claude 대화창에 붙여 넣고 물어도 됩니다. API 는 자동화만 삽니다.

### AI 실험실 (선택, Claude API)

"AI 에게 매매를 맡기면 더 낫나"를 **돈을 걸지 않고** 확인하는 두 도구입니다. 둘 다 주문 API 를 import 하지 않고(`tests/test_ai_lab.py` 의 SafetyBeltTest 가 강제), 봇 설정도 바꾸지 않습니다. `.env` 의 `ANTHROPIC_API_KEY` 를 씁니다.

```
python shadow_trader.py --dry-run     ← Claude 호출 없이 판단 자료만 확인
python shadow_trader.py --stdout      ← 오늘 판단 → 가상 체결 → 화면 (--stdout 없이면 텔레그램)
python shadow_trader.py --report      ← 지금까지 봇 vs 그림자 비교표
python ai_researcher.py --dry-run     ← 현재 규칙의 세 구간 성적표
python ai_researcher.py --stdout      ← 변형 제안 → 백테스트 검증 → research/날짜.md
```

- **그림자 트레이더** (`shadow_trader.py`): 매 거래일, 봇이 매매 시각에 로그에 적은 **같은 시세**로 Claude 가 매수·매도·관망을 정하고 `shadow/` 안에서만 가상 체결합니다(슬리피지 0.3%, 봇 실측치). 출발선은 첫 실행 때 봇 계좌를 복사합니다. 봇이 아직 시세를 못 적었으면 최대 10분 기다리고, 그래도 없으면 yfinance 1분봉을 쓰되 출처를 기록합니다. 졸업 기준(60거래일 이상·수익률이 봇 이상·MDD 가 봇보다 나쁘지 않음)은 `--report` 가 판정만 하고 **자동으로 하는 일은 없습니다.** 주간 감사 자료에도 비교표가 들어갑니다.
- **AI 리서처** (`ai_researcher.py`): 매주, Claude 가 `rules_lab.RuleParams` 의 숫자만 최대 5개 제안하고, 코로나 2020·2022 하락·2022-23 회복·최근 12개월 네 구간을 백테스트해 **위기 구간 셋을 모두 이기고(+0.5%p 이상, MDD 5%p 넘게 악화 없음) 자금부족이 없는** 것만 통과로 적습니다. 순수 하락 해(2022)를 따로 두는 이유는 회복까지 포함한 구간만 보면 노출만 키운 제안이 전부 이기기 때문입니다. 통과해도 설정은 바꾸지 않고 `research/` 메모와 텔레그램 요약만 남깁니다. 적용은 `/set` 으로 사람이 합니다.
- `rules_lab.py` 는 훅을 모두 끄면 `backtest_dual.run_bucket` 과 센트 단위로 같습니다(테스트로 고정). 허용 범위(`BOUNDS`) 밖 제안은 자동 탈락합니다.
- 작업 스케줄러 등록명: `GBV Shadow Trader` (월~금 22:35 — 스크립트가 봇의 매매 시각(ET→KST, 서머타임 반영) 5분 뒤까지 스스로 기다립니다), `GBV AI Researcher` (일 09:30). 1회 비용은 각각 약 $0.05~0.2.
- `.env` 에 `GBV_SHADOW_MODEL` / `GBV_RESEARCH_MODEL` 로 모델을 바꿀 수 있습니다.

### 설치 점검

```
python check_setup.py
```

- 파이썬 패키지, `config.txt` 값, 텔레그램(봇 토큰 → 채팅 ID → 테스트 메시지), 키움(토큰 발급 → 국내·미국 잔고 조회)을 차례로 점검합니다. **주문은 보내지 않습니다.**
- `telegram_chat_id`를 모르면 봇에게 아무 메시지나 보낸 뒤 실행하세요. 봇이 받은 채팅 ID를 보여 줍니다.
- 키움 오류가 나면 코드별 원인을 알려 줍니다(예: 8001 → 키가 틀렸거나 실전/모의 설정과 키 종류가 다름).
- 봇(`main.py`)이 켜져 있으면 끄고 실행하세요.

### 테스트

```
python -m unittest discover -s tests -v
```

### 실계좌 적용 전 확인할 것

- `dry_run = true`로 먼저 돌려 보세요. 시세·잔고는 실제로 조회하지만 주문은 보내지 않고, 보냈을 주문을 `🧪 [GBV DRY-RUN]` 텔레그램으로 알려 줍니다. 설정 변경은 봇을 재시작해야 적용되며, 연습 매매도 그날 매매한 것으로 기록됩니다.
- `kiwoom_mode = demo`로 먼저 돌려 보세요. 모의투자에서는 일부 조회(주문가능금액, 환율)가 거절될 수 있으며, 이 경우 예수금·시세 환율로 대체됩니다.
- 실전 전환 후 첫 매매는 기준금을 작게 잡아 주문 접수·체결·잔고 반영을 텔레그램 `/balance`로 확인하세요.
- 매매 시각을 적지 않은 시장은 매매하지 않습니다. 국내장만 쓰려면 `us_market_time` 줄을 지우거나 주석 처리하세요(`off`도 됩니다). 기본값은 없습니다.
- 미국장 매매 시각은 `us_market_time = 09:30 ET`처럼 **미국 동부시간으로 적는 것을 권합니다.** `22:30`처럼 한국 시각으로 적으면 미국 서머타임이 끝나는 11월 첫 일요일부터 같은 시각이 정규장 개장(09:30 ET)이 아니라 프리마켓(08:30 ET)이 되어 체결 환경이 바뀝니다. 동부시간으로 적으면 봇이 매일 한국 시각으로 환산합니다(여름 22:30, 겨울 23:30). 한국 자정을 넘기는 시각(16:00 ET 등)은 받지 않습니다.
- 프리마켓에 걸리는 시각을 쓰실 경우, 지정가 주문(현재가 ±3%)이 그 시간대에 체결되는지 소액으로 먼저 확인하세요. 리포트의 `실효체결가`·`슬리피지` 열로 확인할 수 있습니다.

### 하루 한 번 매매 보장 (`trade_state.json`)

- 봇은 시장별로 "오늘 매매함"을 `trade_state.json`에 기록합니다. 주문을 보내기 직전과 매매 사이클이 끝났을 때 기록합니다.
- 매매 시간 중에 봇을 껐다 켜도 이 파일을 보고 같은 날 같은 시장은 다시 매매하지 않습니다.
- 같은 날 다시 매매하려면 텔레그램 `/set us_market_time HH:MM`처럼 매매 시각을 바꾸거나, 봇을 끈 상태에서 파일을 지우면 됩니다.
- 현재가 조회에 실패하면 그날 해당 시장 매매를 건너뛰고 텔레그램으로 알립니다.
- 잔고 조회 등이 일시 장애(키움 RC9002 "서비스 지연" 등)로 실패하면 목표 시각부터 15분 동안 30초마다 다시 시도합니다. 첫 실패와 15분 뒤 포기만 텔레그램으로 알립니다. 주문이 한 번이라도 나간 뒤에는 재시도하지 않습니다(중복 주문 방지).
- 월 증액 기록은 `last_increased_month_us`, `last_increased_month_kr`에 시장별로 저장됩니다(봇이 자동 관리).
- 월 증액은 그달 첫 매매일에 적용됩니다. 첫 거래일에 봇이 꺼져 있었다면 그다음 매매일에 적용됩니다.

### 미체결 확인

- 매매 시각 30분 뒤에 아직 체결되지 않은 주문이 있는지 조회합니다(국내 `ka10075`, 미국 `ust21050`).
- 남은 주문이 있으면 `⏳ [GBV] 미체결 주문` 텔레그램으로 종목·잔량·주문가를 알려 줍니다. 없으면 로그만 남깁니다.
- 봇은 주문을 취소·정정하지 않습니다. 필요하면 영웅문에서 직접 처리하세요.

### 휴장일과 매매 순서

- 한국거래소(설·추석, 노동절, 연말 휴장일 등)와 뉴욕증권거래소(추수감사절, 독립기념일 등)의 휴장일에는 해당 시장을 매매하지 않고, 매매 시각에 텔레그램으로 알립니다(`holidays` 패키지 사용).
- 기준금보다 많이 가진 종목(매도)을 먼저 처리하고, 부족한 종목(매수)은 나중에 처리합니다. 매도 대금을 매수에 쓸 수 있습니다.
