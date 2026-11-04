"""只构造显式消息段，模板中的 CQ 始终作为普通文字。"""

import re

from nonebot.adapters.onebot.v11 import Message, MessageSegment

_AT_PLACEHOLDER = re.compile(r"(\{at_user\}|%at_user%)")


def to_message(text: str, *, at_user: int | None = None, image: bytes | None = None) -> Message:
    if image is not None and not isinstance(image, bytes):
        raise TypeError("图片必须是本地字节数据")
    message = Message()
    if at_user is None:
        if text:
            message.append(MessageSegment.text(text))
    else:
        for part in _AT_PLACEHOLDER.split(text):
            if part in ("{at_user}", "%at_user%"):
                message.append(MessageSegment.at(at_user))
            elif part:
                message.append(MessageSegment.text(part))
    if image is not None:
        message.append(MessageSegment.image(image))
    return message
