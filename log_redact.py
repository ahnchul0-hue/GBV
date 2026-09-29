"""
log_redact.py
로그로 새어 나가는 비밀값 가리기

requests 예외 메시지에는 요청 URL 이 통째로 들어간다. 텔레그램 API 는 URL 경로에
봇 토큰을 담기 때문에, 폴링이 한 번 실패하면

    .../bot<봇ID>:<토큰>/getUpdates?offset=...

이 그대로 로그 파일에 남고 /log 명령으로 다시 텔레그램에 실려 나간다.
토큰이 있으면 누구나 그 봇으로 메시지를 읽고 보낼 수 있다.

Filter 가 아니라 Formatter 로 거는 이유: logger.error(..., exc_info=True) 의
트레이스백은 record.getMessage() 에 없고 포매터가 따로 붙인다. 포매팅이 끝난
문자열을 가려야 메시지와 트레이스백을 모두 덮는다.
"""

import logging
import re

# .../bot<봇ID>:<토큰>/... 에서 콜론 뒤 토큰만 가린다 (봇 ID 는 디버깅에 쓸모 있고 비밀이 아니다)
_TELEGRAM_URL_TOKEN = re.compile(r"(/bot\d+:)[A-Za-z0-9_-]{20,}")

_secrets = []


def add_secret(value):
    """로그에서 가릴 문자열 추가 (config 에서 읽은 토큰 등, URL 밖에 나오는 경우 대비)"""
    value = str(value or "").strip()
    if len(value) >= 8 and value not in _secrets:
        _secrets.append(value)


def redact(text: str) -> str:
    text = _TELEGRAM_URL_TOKEN.sub(r"\1***", text)
    for s in _secrets:
        text = text.replace(s, "***")
    return text


class RedactingFormatter(logging.Formatter):
    """포매팅 결과에서 비밀값을 지우는 포매터"""

    def format(self, record):
        try:
            return redact(super().format(record))
        except Exception:
            return super().format(record)   # 가리기 실패가 로그 유실로 이어지면 안 된다
