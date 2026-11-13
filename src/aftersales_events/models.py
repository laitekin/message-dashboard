"""数据模型：原始接口数据、标准化消息、窗口、事件。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- 原始接口数据


class RawRecord(BaseModel):
    id: str
    group_number: str
    group_name: str
    sender_number: str
    display_name: str = ""
    nick_name: str = ""
    content: str = ""
    created_at: str = ""
    send_time: str = ""
    is_inner: int = 0


class RawGroup(BaseModel):
    group_number: str
    group_name: str
    total: int = 0
    records: list[RawRecord] = Field(default_factory=list)


class RawResponse(BaseModel):
    code: int = 0
    msg: str = ""
    uid: str = ""
    start: str = ""
    end: str = ""
    total: int = 0
    groups: list[RawGroup] = Field(default_factory=list)


# ---------------------------------------------------------------- 标准化后


class Message(BaseModel):
    id: str
    group_number: str
    group_name: str
    customer_uid: str | None
    sender_id: str
    sender: str
    role: Literal["内部", "客户"]
    send_time: datetime
    content: str  # 已脱敏
    is_image: bool = False
    entities: dict[str, list[str]] = Field(default_factory=dict)
    hints: list[str] = Field(default_factory=list)  # 规则层命中的候选事件类型


class Window(BaseModel):
    """同一群内一段连续对话，作为一次 LLM 抽取的输入。"""

    window_id: str
    group_number: str
    group_name: str
    customer_uid: str | None
    messages: list[Message]

    @property
    def start(self) -> datetime:
        return self.messages[0].send_time

    @property
    def end(self) -> datetime:
        return self.messages[-1].send_time

    @property
    def hints(self) -> list[str]:
        seen: dict[str, None] = {}
        for m in self.messages:
            for h in m.hints:
                seen.setdefault(h, None)
        return list(seen)


# ---------------------------------------------------------------- LLM 输出（结构化输出 schema）

Status = Literal["新建", "跟进中", "已解决", "已关闭"]
Level = Literal["P0", "P1", "P2", "P3"]


class Attribute(BaseModel):
    key: str = Field(description="属性名，使用该事件类型定义中的 key")
    value: str = Field(description="属性值，尽量引用原文")


class ExtractedEvent(BaseModel):
    type: str = Field(description="事件类型，必须是给定类型之一")
    title: str = Field(description="15 字以内的事件标题")
    summary: str = Field(description="1-3 句话概述：发生了什么、当前进展")
    status: Status
    level: Level
    owner: str | None = Field(description="主要跟进的内部人员姓名，无法判断填 null")
    evidence: list[str] = Field(description="支撑该事件的消息编号，如 m3，按时间顺序")
    attributes: list[Attribute] = Field(description="该类型的专属属性，没有信息的不要填")
    confidence: float = Field(description="0 到 1 之间的置信度")


class WindowExtraction(BaseModel):
    events: list[ExtractedEvent] = Field(description="窗口内识别到的事件，没有则为空数组")


# ---------------------------------------------------------------- 最终事件


class Event(BaseModel):
    event_id: str
    type: str
    title: str
    summary: str
    status: Status
    level: Level
    owner: str | None
    customer_uid: str | None
    group_number: str
    group_name: str
    start_time: datetime
    end_time: datetime
    evidence_msg_ids: list[str]
    attributes: dict[str, str] = Field(default_factory=dict)
    confidence: float
    source_windows: list[str] = Field(default_factory=list)
