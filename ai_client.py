"""
ai_client.py
그림자 트레이더·AI 리서처가 함께 쓰는 Claude 호출 조각. 주문 경로와는 무관하다.
"""

import logging

logger = logging.getLogger("ai_client")


def call_structured(client, kwargs: dict, plain_hint: str):
    """client.beta.messages.stream(**kwargs) 로 구조화 출력(output_config)을 받는다.

    SDK 가 인자를 모르거나(TypeError) API 가 스키마를 거부하면(400, 메시지에 output_config)
    output_config 를 빼고 '본문에 JSON 하나만' 지시를 덧붙여 한 번 더 시도한다.
    그 밖의 오류(인증·네트워크·과부하)는 그대로 올린다.
    """
    try:
        with client.beta.messages.stream(**kwargs) as stream:
            return stream.get_final_message()
    except TypeError as e:
        reason = str(e)
    except Exception as e:
        if "output_config" not in str(e):
            raise
        reason = str(e)
    logger.warning(f"구조화 출력 미지원, 텍스트 파싱으로 대체: {reason[:160]}")
    retry = dict(kwargs)
    retry.pop("output_config", None)
    first = dict(kwargs["messages"][0])
    first["content"] = first["content"] + plain_hint
    retry["messages"] = [first] + list(kwargs["messages"][1:])
    with client.beta.messages.stream(**retry) as stream:
        return stream.get_final_message()


def text_of(msg) -> str:
    """응답의 text 블록만 이어 붙인다 (구조화 출력이면 첫 블록이 JSON 그 자체)"""
    return "\n".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
