"""
single_instance.py
봇이 한 번에 하나만 돌도록 막는 파일 잠금

두 인스턴스가 동시에 돌면
  - trade_state.py 의 잠금은 threading.Lock 이라 프로세스 사이에서는 소용이 없어
    둘 다 '오늘 미매매'로 읽고 주문을 중복해서 낼 수 있고,
  - 같은 로그 파일에 동시에 append 해서 줄이 유실된다.

PID 를 적어 두고 살아 있는지 확인하는 방식은 쓰지 않는다.
윈도우에서 os.kill(pid, 0) 은 생사 확인이 아니라 TerminateProcess 라 프로세스를 죽인다.
대신 OS 파일 잠금을 쓴다. 프로세스가 어떻게 끝나든 OS 가 잠금을 풀어 주므로
비정상 종료 뒤에 잠금이 남아 봇이 영영 안 뜨는 일이 없다.

잠금 파일과 PID 파일을 나눠 둔 이유: 윈도우에서 잠긴 파일은 다른 프로세스가
열기만 해도 PermissionError 라, 누가 쥐고 있는지 알려 주려면 별도 파일이 필요하다.
"""

import logging
import os

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
LOCK_FILE = os.path.join(_HERE, "bot.lock")
PID_FILE  = os.path.join(_HERE, "bot.pid")

_handle = None


class AlreadyRunning(Exception):
    """다른 인스턴스가 이미 잠금을 쥐고 있음"""

    def __init__(self, pid_text=""):
        self.pid_text = pid_text
        who = f" (PID {pid_text})" if pid_text else ""
        super().__init__(f"봇이 이미 실행 중입니다{who}")


def _lock(f):
    """파일 첫 바이트를 비차단으로 잠근다. 이미 잠겨 있으면 OSError."""
    f.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(f):
    f.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _read_pid(pid_file):
    """잠금을 쥔 프로세스의 PID (알 수 없으면 빈 문자열)"""
    try:
        with open(pid_file, "r", encoding="utf-8") as f:
            return f.read(32).strip()
    except OSError:
        return ""


def acquire(lock_file=None, pid_file=None):
    """잠금을 잡는다. 이미 다른 인스턴스가 있으면 AlreadyRunning 을 던진다."""
    global _handle
    if _handle is not None:
        return                      # 같은 프로세스에서 두 번 불러도 무해하게
    lock_file = lock_file or LOCK_FILE
    pid_file  = pid_file  or PID_FILE

    f = open(lock_file, "a+", encoding="utf-8")
    try:
        _lock(f)
    except OSError:
        f.close()
        raise AlreadyRunning(_read_pid(pid_file))

    _handle = (f, pid_file)
    try:
        with open(pid_file, "w", encoding="utf-8") as pf:
            pf.write(str(os.getpid()))
    except OSError as e:
        logger.warning(f"PID 파일 기록 실패: {e}")   # 잠금 자체는 잡았으므로 계속 진행
    logger.info(f"단일 실행 잠금 획득 (PID {os.getpid()})")


def release():
    """잠금을 푼다. 잡은 적이 없으면 아무 일도 하지 않는다."""
    global _handle
    if _handle is None:
        return
    (f, pid_file), _handle = _handle, None
    try:
        _unlock(f)
    except OSError as e:
        logger.warning(f"단일 실행 잠금 해제 실패: {e}")
    finally:
        f.close()
    try:
        os.remove(pid_file)
    except OSError:
        pass
