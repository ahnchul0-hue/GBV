"""
telegram_handler.py
텔레그램으로 모든 설정 관리

명령어:
  /set [키] [값]                  → 모든 설정 변경
  /add_gbv [종목] [기준금] [월증액률] → GBV 종목 추가
  /remove_gbv [종목]              → GBV 종목 제거
  /balance                        → 계좌 조회
  /status                         → 전체 설정 조회
  /health                         → 봇 상태 (가동 시간, 오늘 매매 여부, 다음 매매, 마지막 오류)
  /log [줄수|err]                 → 최근 로그 (기본 20줄, err: 경고·오류만)
  /help                           → 도움말

※ 명령은 telegram_chat_id(주인) 채팅에서 온 것만 처리하고 나머지는 무시한다.

예시:
  /set trading_enabled true
  /set us_market_time 19:00
  /set TQQQ 80000
  /set tqqq_monthly_rate 0.02
"""

import logging
import threading
import time
import telebot
from config_manager import (
    load_config, _set_value, _delete_keys,
    get_all_us_tickers, get_all_kr_tickers
)
from notifier import get_telegram_settings, _send

logger = logging.getLogger(__name__)

# 전역 bot 객체
_bot = None


def is_owner(message) -> bool:
    """주인(telegram_chat_id) 채팅에서 온 메시지인지"""
    _, chat_id = get_telegram_settings()
    return bool(chat_id) and str(message.chat.id) == chat_id


def setup_handlers(bot):
    """봇 핸들러 등록 (모든 명령은 주인 채팅에서만 동작)"""
    
    # 증권사 API 임포트 (지연 임포트)
    from broker import get_broker
    from config_manager import load_config, get_outside_tqqq
    
    @bot.message_handler(commands=['set'], func=is_owner)
    def set_value(message):
        """모든 설정값 변경 (시스템 파라미터 + 종목)"""
        try:
            parts = message.text.split()
            
            # 사용법 안내
            if len(parts) < 3:
                bot.reply_to(message,
                            "사용법:\n"
                            "/set [key] [value]\n"
                            "/set [ticker] [base] [rate]\n\n"
                            "시스템:\n"
                            "/set trading_enabled true\n"
                            "/set us_market_time 19:00\n"
                            "/set outside_tqqq 1500\n\n"
                            "종목:\n"
                            "/set tqqq 100000 0.02\n"
                            "/set 252670 2000000 0.01")
                return
            
            # 허용된 시스템 키
            SYSTEM_KEYS = {
                "trading_enabled", "us_market_time", "kr_market_time",
                "outside_tqqq"
            }
            
            key = parts[1].strip()
            key_lower = key.lower()
            
            # === 3개 파라미터: 종목 기준금 + 월증액률 ===
            if len(parts) >= 4:
                ticker = key.upper()
                base_amount = parts[2].strip()
                monthly_rate = parts[3].strip()
                
                try:
                    base = float(base_amount)
                    rate = float(monthly_rate)
                    if base < 0:
                        raise ValueError("기준금은 0 이상")
                    if not (0 <= rate <= 1):
                        raise ValueError("월증액률은 0~1 사이")
                except ValueError as e:
                    bot.reply_to(message, f"error: {str(e)}")
                    return
                
                # 기준금 설정
                _set_value(ticker, str(int(base)))
                # current_base 리셋
                _set_value(f"{ticker}_current_base", str(int(base)))
                # 월증액률 설정
                _set_value(f"{ticker}_monthly_rate", monthly_rate)
                
                logger.info(f"종목 설정: {ticker} = {int(base)}, rate = {rate}, current_base 리셋")
                
                is_kr = ticker.isdigit()
                currency = "₩" if is_kr else "$"
                bot.reply_to(message, 
                            f"ok\n{ticker} = {currency}{int(base):,}\n{ticker}_monthly_rate = {rate}")
                return
            
            # === 2개 파라미터: 시스템 또는 개별 설정 ===
            value = parts[2].strip()
            
            # 시스템 파라미터
            if key_lower in SYSTEM_KEYS:
                # 값 유효성 검사
                try:
                    if key_lower == "trading_enabled":
                        if value.lower() not in ("true", "false"):
                            raise ValueError("true 또는 false만 가능")
                    elif "time" in key_lower:
                        h, m = value.split(":")
                        if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
                            raise ValueError("시간 형식 오류")
                    elif key_lower == "outside_tqqq":
                        if int(value) < 0:
                            raise ValueError("0 이상이어야 함")
                except ValueError as e:
                    bot.reply_to(message, f"error: {str(e)}")
                    return
                
                _set_value(key_lower, value)
                logger.info(f"파라미터 변경: {key_lower} = {value}")
                bot.reply_to(message, f"ok\n{key_lower} = {value}")
                
            # 월증액률
            elif key_lower.endswith("_monthly_rate"):
                try:
                    rate = float(value)
                    if not (0 <= rate <= 1):
                        raise ValueError("0~1 사이여야 함")
                except ValueError as e:
                    bot.reply_to(message, f"error: {str(e)}")
                    return
                
                _set_value(key_lower, value)
                logger.info(f"월증액률 변경: {key_lower} = {value}")
                bot.reply_to(message, f"ok\n{key_lower} = {value}")
                
            # 종목 기준금만
            else:
                ticker = key.upper()
                try:
                    base = float(value)
                    if base < 0:
                        raise ValueError("0 이상이어야 함")
                except ValueError as e:
                    bot.reply_to(message, f"error: {str(e)}")
                    return
                
                _set_value(ticker, str(int(base)))
                _set_value(f"{ticker}_current_base", str(int(base)))
                logger.info(f"기준금 변경: {ticker} = {int(base)}, current_base 리셋")
                
                is_kr = ticker.isdigit()
                currency = "₩" if is_kr else "$"
                bot.reply_to(message, f"ok\n{ticker.lower()} = {currency}{int(base):,}\n{ticker.lower()}_current_base = {currency}{int(base):,} (리셋)")
            
        except Exception as e:
            bot.reply_to(message, f"error: {str(e)}")
            logger.error(f"set 오류: {e}", exc_info=True)
    
    
    @bot.message_handler(commands=['balance'], func=is_owner)
    def balance(message):
        """계좌 잔고 조회"""
        try:
            config = load_config()
            api = get_broker(config)  # 메인 루프와 같은 객체 공유 (토큰/요청 간격 공유)
            
            # 미국
            us_holdings, us_cash = api.get_us_balance()
            outside_tqqq = get_outside_tqqq(config)
            
            us_total = us_cash
            us_lines = []
            for ticker, info in us_holdings.items():
                try:
                    price = api.get_us_price(ticker)
                    qty = info["qty"]
                    if ticker == "TQQQ" and outside_tqqq > 0:
                        total_qty = qty + outside_tqqq
                        value = total_qty * price
                        us_lines.append(f"{ticker}: {total_qty}주 (${value:,.2f}, outside={outside_tqqq})")
                    else:
                        value = qty * price
                        us_lines.append(f"{ticker}: {qty}주 (${value:,.2f})")
                    us_total += value  # 전체 평가금 더하기
                except:
                    pass
            
            us_cash_ratio = us_cash / us_total * 100 if us_total > 0 else 0
            
            # 국내
            kr_holdings, kr_cash = api.get_kr_balance()
            kr_total = kr_cash
            kr_lines = []
            for ticker, info in kr_holdings.items():
                try:
                    price = api.get_kr_price(ticker)
                    name = api.get_kr_name(ticker)
                    qty = info["qty"]
                    value = qty * price
                    kr_lines.append(f"{name}({ticker}): {qty}주 (₩{int(value):,})")
                    kr_total += value
                except:
                    pass
            
            kr_cash_ratio = kr_cash / kr_total * 100 if kr_total > 0 else 0
            
            # 응답
            response = "[미국장]\n"
            response += f"cash: ${us_cash:,.2f} ({us_cash_ratio:.1f}%)\n"
            response += f"total: ${us_total:,.2f}\n"
            response += "\n".join(us_lines) if us_lines else "no holdings"
            
            response += "\n\n[국내장]\n"
            response += f"cash: ₩{int(kr_cash):,} ({kr_cash_ratio:.1f}%)\n"
            response += f"total: ₩{int(kr_total):,}\n"
            response += "\n".join(kr_lines) if kr_lines else "no holdings"
            
            bot.reply_to(message, response)
            logger.info("/balance 실행")
            
        except Exception as e:
            bot.reply_to(message, f"error: {str(e)}")
            logger.error(f"balance 오류: {e}", exc_info=True)
    
    
    @bot.message_handler(commands=['add_gbv'], func=is_owner)
    def add_gbv(message):
        """GBV 종목 추가"""
        try:
            parts = message.text.split()
            if len(parts) < 4:
                bot.reply_to(message,
                            "사용법: /add_gbv UGL 30000 0\n"
                            "[종목] [기준금] [월증액률]")
                return
            
            ticker = parts[1].strip().upper()
            base_amount = float(parts[2])
            monthly_rate = float(parts[3])
            
            _set_value(ticker, str(int(base_amount)))
            _set_value(f"{ticker}_current_base", str(int(base_amount)))
            _set_value(f"{ticker}_monthly_rate", str(monthly_rate))
            
            bot.reply_to(message, f"ok\nGBV 추가: {ticker}")
            logger.info(f"GBV 추가: {ticker}")
            
        except Exception as e:
            bot.reply_to(message, f"error: {str(e)}")
            logger.error(f"add_gbv 오류: {e}", exc_info=True)
    
    
    @bot.message_handler(commands=['remove_gbv'], func=is_owner)
    def remove_gbv(message):
        """GBV에서 제거"""
        try:
            parts = message.text.split()
            if len(parts) < 2:
                bot.reply_to(message, "사용법: /remove_gbv UGL")
                return
            
            ticker = parts[1].strip().upper()
            
            config = load_config()
            all_us = get_all_us_tickers(config)
            all_kr = get_all_kr_tickers(config)
            
            if ticker not in all_us and ticker not in all_kr:
                bot.reply_to(message, f"{ticker}는 설정 안 됨")
                return
            
            _delete_keys(ticker)
            
            bot.reply_to(message, f"ok\nGBV 제거: {ticker}")
            logger.info(f"GBV 제거: {ticker}")
            
        except Exception as e:
            bot.reply_to(message, f"error: {str(e)}")
            logger.error(f"remove_gbv 오류: {e}", exc_info=True)
    
    
    @bot.message_handler(commands=['status'], func=is_owner)
    def status(message):
        """전체 설정 현황"""
        try:
            config = load_config()
            all_us = get_all_us_tickers(config)
            all_kr = get_all_kr_tickers(config)
            
            lines = []
            
            lines.append("[시스템]")
            lines.append("trading_enabled: " + config.get('TRADING_ENABLED', 'true'))
            lines.append("us_market_time: " + config.get('US_MARKET_TIME', '19:00'))
            lines.append("kr_market_time: " + config.get('KR_MARKET_TIME', '09:05'))
            lines.append("outside_tqqq: " + config.get('OUTSIDE_TQQQ', '0'))
            lines.append("")
            
            # GBV - 미국
            us_tickers = sorted([t for t in all_us.keys() if all_us[t] > 0])
            if us_tickers:
                lines.append("[GBV - 미국]")
                for ticker in us_tickers:
                    base = all_us[ticker]
                    current_base = config.get(f"{ticker}_CURRENT_BASE", str(int(base)))
                    lines.append(f"{ticker.lower()}: ${int(base):,} (현재 ${current_base})")
                    rate = config.get(f"{ticker}_MONTHLY_RATE", '0')
                    lines.append(f"{ticker.lower()}_monthly_rate: {rate}")
                lines.append("")
            
            # GBV - 국내
            kr_tickers = sorted([t for t in all_kr.keys() if all_kr[t] > 0])
            if kr_tickers:
                lines.append("[GBV - 국내]")
                for ticker in kr_tickers:
                    base = all_kr[ticker]
                    current_base = config.get(f"{ticker}_CURRENT_BASE", str(int(base)))
                    lines.append(f"{ticker.lower()}: ₩{int(base):,} (현재 ₩{current_base})")
                    rate = config.get(f"{ticker}_MONTHLY_RATE", '0')
                    lines.append(f"{ticker.lower()}_monthly_rate: {rate}")
            else:
                lines.append("[GBV - 국내]")
                lines.append("(없음)")
            
            bot.reply_to(message, "\n".join(lines))
            
        except Exception as e:
            bot.reply_to(message, f"error: {str(e)}")
            logger.error(f"status 오류: {e}", exc_info=True)
    
    
    @bot.message_handler(commands=['health'], func=is_owner)
    def health(message):
        """봇 상태 요약"""
        try:
            import bot_status
            bot.reply_to(message, bot_status.health_text())
            logger.info("/health 실행")
        except Exception as e:
            bot.reply_to(message, f"error: {str(e)}")
            logger.error(f"health 오류: {e}", exc_info=True)
    
    
    @bot.message_handler(commands=['log'], func=is_owner)
    def log(message):
        """최근 로그 보기: /log, /log 50, /log err"""
        try:
            import bot_status
            parts = message.text.split()
            arg = parts[1].lower() if len(parts) > 1 else ""
            errors_only = arg in ("err", "error", "오류")
            count = 20
            if arg.isdigit():
                count = max(1, min(int(arg), 100))
            bot.send_message(message.chat.id, bot_status.tail_log(count, errors_only=errors_only))
        except Exception as e:
            bot.reply_to(message, f"error: {str(e)}")
            logger.error(f"log 오류: {e}", exc_info=True)
    
    
    @bot.message_handler(commands=['help'], func=is_owner)
    def help_command(message):
        """도움말"""
        help_text = """commands:

/set [key] [value]
/add_gbv [ticker] [base] [rate]
/remove_gbv [ticker]
/balance
/status
/health
/log [줄수|err]
/help

examples:
/set trading_enabled true
/set us_market_time 19:00
/set outside_tqqq 1500
/set TQQQ 80000
/set tqqq_monthly_rate 0.02
/add_gbv UGL 30000 0
/remove_gbv UGL
/log 50
/log err
/set heartbeat_time 08:30   (off: 끄기)"""
        bot.reply_to(message, help_text)


def _add_stranger_guard(bot):
    """주인이 아닌 채팅의 메시지는 무시하고 기록만 남김 (반드시 마지막에 등록)"""
    @bot.message_handler(func=lambda m: not is_owner(m), content_types=['text'])
    def ignore_stranger(message):
        user = getattr(message.from_user, "username", None) or getattr(message.from_user, "id", "?")
        logger.warning(f"허가되지 않은 채팅의 명령 무시: chat_id={message.chat.id} user={user} text={message.text!r}")


def start_polling(stop_event):
    """텔레그램 봇 폴링 시작"""
    global _bot
    
    try:
        token, chat_id = get_telegram_settings()
        if not chat_id or chat_id.startswith("XXX"):
            logger.error("telegram_chat_id가 설정되지 않아 텔레그램 명령을 받지 않습니다")
            return
        _bot = telebot.TeleBot(token)
        logger.info("텔레그램 봇 초기화 완료")
        
        setup_handlers(_bot)
        _add_stranger_guard(_bot)
        logger.info("텔레그램 핸들러 등록 완료")
        
        logger.info("텔레그램 폴링 시작...")
        while not stop_event.is_set():
            try:
                _bot.polling(none_stop=False, timeout=10)
            except Exception as e:
                logger.error(f"폴링 오류: {e}")
                if stop_event.is_set():
                    break
                time.sleep(5)
        
        logger.info("텔레그램 폴링 종료")
        
    except Exception as e:
        logger.error(f"텔레그램 봇 시작 실패: {e}", exc_info=True)
