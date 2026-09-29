"""Per-thread pseudonyms.

    seed  = HMAC-SHA256(pepper, "alias:v1:" + thread_id + ":" + device_id + ":" + counter)
    name  = ADJECTIVES[seed[0:2]] + "的" + ANIMALS[seed[2:4]]
    hue   = seed[4:6] mod 360            (avatar colour)

Properties
* Deterministic: same device in the same thread always derives the same seed,
  so the name is stable for the life of the thread.
* Unlinkable for readers: thread_id is part of the HMAC input, so the same
  device gets an unrelated name in every thread; without the pepper nobody
  can test whether two names belong to one device.
* Not unlinkable for the operator: the server must know device_id to enforce
  rate limits and bans and to answer lawful requests, so the (thread, device,
  name) row is stored. That store is the crown jewel; see docs/design.md for
  how it is compartmentalised.
* The counter only changes when two devices would collide on the same name in
  one thread; the chosen name is then persisted, so it never changes later.
"""
from __future__ import annotations

import hashlib
import hmac

ADJECTIVES = (
    "沉默", "好奇", "温柔", "倔强", "慵懒", "机灵", "冷静", "热情", "谨慎", "勇敢",
    "孤独", "迷糊", "认真", "淡定", "傲娇", "害羞", "憨厚", "潇洒", "犀利", "佛系",
    "硬核", "咸鱼", "上进", "摸鱼", "深夜", "清晨", "路过", "隐身", "微醺", "安静",
    "躁动", "焦虑", "松弛", "严肃", "幽默", "神秘", "透明", "复古", "未来", "野生",
    "精致", "朴素", "叛逆", "温暖", "寒冷", "明亮", "暗淡", "忙碌", "悠闲", "疲惫",
    "元气", "佛心", "挑剔", "随和", "固执", "多疑", "乐观", "悲观", "务实", "浪漫",
    "理性", "感性", "内向", "外向", "敏锐", "迟钝", "匆忙", "从容", "沉稳", "跳脱",
    "低调", "高调", "老练", "青涩", "靠谱", "离谱", "谦虚", "骄傲", "耐心", "急躁",
)

ANIMALS = (
    "企鹅", "海獭", "狐狸", "熊猫", "树懒", "刺猬", "水豚", "考拉", "羊驼", "柯基",
    "布偶猫", "仓鼠", "鲸鱼", "海豚", "章鱼", "乌龟", "蜗牛", "蜜蜂", "蝴蝶", "猫头鹰",
    "乌鸦", "麻雀", "鸽子", "鹦鹉", "火烈鸟", "天鹅", "鸭子", "大鹅", "松鼠", "兔子",
    "浣熊", "小鹿", "骆驼", "斑马", "长颈鹿", "河马", "犀牛", "大象", "狮子", "老虎",
    "豹子", "野狼", "棕熊", "北极熊", "海豹", "海象", "水母", "海马", "螃蟹", "龙虾",
    "鲨鱼", "金鱼", "锦鲤", "青蛙", "蜥蜴", "变色龙", "壁虎", "小蛇", "鳄鱼", "恐龙",
    "独角兽", "凤凰", "麒麟", "貔貅", "貂", "鼹鼠", "土拨鼠", "袋鼠", "鸵鸟", "孔雀",
    "老鹰", "海鸥", "信天翁", "蝙蝠", "萤火虫", "蚂蚁", "瓢虫", "蜻蜓", "螳螂", "山羊",
)

MAX_DERIVATION_TRIES = 32


def alias_seed(pepper: bytes, thread_id: str, device_id: str, counter: int = 0) -> bytes:
    if not pepper:
        raise ValueError("alias pepper must not be empty")
    msg = f"alias:v1:{thread_id}:{device_id}:{counter}".encode("utf-8")
    return hmac.new(pepper, msg, hashlib.sha256).digest()


def derive_alias(pepper: bytes, thread_id: str, device_id: str, counter: int = 0) -> tuple[str, int]:
    seed = alias_seed(pepper, thread_id, device_id, counter)
    adjective = ADJECTIVES[int.from_bytes(seed[0:2], "big") % len(ADJECTIVES)]
    animal = ANIMALS[int.from_bytes(seed[2:4], "big") % len(ANIMALS)]
    hue = int.from_bytes(seed[4:6], "big") % 360
    name = f"{adjective}的{animal}"
    if counter >= MAX_DERIVATION_TRIES:
        # 6400 combinations exhausted in one thread: fall back to a numbered
        # name rather than looping forever.
        name = f"{name}#{counter - MAX_DERIVATION_TRIES + 2}"
    return name, hue
