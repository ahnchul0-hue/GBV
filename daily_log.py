"""
daily_log.py
날짜가 바뀌면 그날 파일로 갈아타는 로그 핸들러

logging.FileHandler 는 파일명을 만들 때 한 번 정하고 끝이다. main.py 가
trade_<오늘날짜>.log 로 핸들러를 만들기 때문에, 봇을 며칠씩 켜 두면 모든 로그가
'봇을 켠 날' 파일에 쌓인다. 실제로 09-30 에 띄운 봇이 10-02 까지
trade_20260930.log 에 쓰고 있었다. 날짜로 로그를 찾을 수 없고 파일만 계속 커진다.

TimedRotatingFileHandler 를 쓰지 않은 이유: 그쪽은 trade.log.2026-09-30 처럼
뒤에 날짜를 붙이는데, bot_status 의 /log 는 trade_*.log 를 찾는다. 기존 이름
규칙을 그대로 두는 편이 /log 와 사람 눈 양쪽에 낫다.
"""

import logging
import os
from datetime import datetime


def log_path(dir_path: str, when: datetime = None) -> str:
    """그 날짜의 로그 파일 경로"""
    when = when or datetime.now()
    return os.path.join(dir_path, f"trade_{when.strftime('%Y%m%d')}.log")


class DailyFileHandler(logging.FileHandler):
    """자정을 넘기면 trade_<새날짜>.log 로 옮겨 쓴다"""

    def __init__(self, dir_path: str, encoding: str = "utf-8"):
        self.dir_path = dir_path
        self._day = self._today()
        super().__init__(log_path(dir_path), encoding=encoding)

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y%m%d")

    def _switch_if_needed(self):
        today = self._today()
        if today == self._day:
            return
        # 핸들러를 닫지 않고 스트림만 교체한다 (close() 는 핸들러 등록까지 해제한다)
        if self.stream:
            self.stream.flush()
            self.stream.close()
            self.stream = None
        self._day = today
        self.baseFilename = os.path.abspath(log_path(self.dir_path))
        self.stream = self._open()

    def emit(self, record):
        # emit 은 핸들러 잠금 안에서 불리므로 스트림 교체도 안전하다
        try:
            self._switch_if_needed()
        except Exception:
            self.handleError(record)    # 교체 실패가 로그 유실로 이어지지 않게
        super().emit(record)
