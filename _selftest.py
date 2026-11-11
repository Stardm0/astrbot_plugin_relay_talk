# -*- coding: utf-8 -*-
"""离线自检：不依赖 AstrBot，用替身模块验证核心逻辑。

跑法： python _selftest.py
仅用于开发期自查，不随插件加载（文件名不是 main.py，无 @register）。

1.0.2 覆盖重点（指令解析全量修复）：
  · COMMAND_MATRIX 单一数据源，运行时 /话筒 校验 与离线实跑逐条对齐
  · At 段 [主人(QQ号)] / CQ:at / @前缀 清洗，消息链跳过非 Plain 组件
  · 全角半角冒号紧贴、英文大小写、多来源取原文、关键字兜底
  · 原有覆盖：上下文闸门、来回传话、AI 现写回执、confirm、潜台词
"""
import asyncio
import importlib.util
import json
import logging
import os
import re
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(ROOT, "main.py")


def stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _Star:
    def __init__(self, context=None, *a, **k):
        self.context = context


class _Filter:
    EventMessageType = types.SimpleNamespace(ALL="ALL")

    def __getattr__(self, _):
        def deco(*a, **k):
            def wrap(fn):
                return fn
            return wrap
        return deco


class _Chain:
    def __init__(self):
        self.text = ""

    def message(self, t):
        self.text = t
        return self


class _Plain:
    def __init__(self, t):
        self.text = t


class _At:
    """模拟真实 AstrBot 的 At 组件（有 text 属性，但不是 Plain）。"""
    def __init__(self, qq):
        self.qq = qq
        self.text = f"[主人({qq})]"


class _MsgObj:
    def __init__(self, t):
        self.message = [_Plain(t)]


class _Event:
    def __init__(self, text, umo, full=None):
        self.message_str = text
        self.unified_msg_origin = umo
        self.stopped = False
        # 模拟某些版本：message_str 只剩参数，完整原文在 message_obj 里
        if full is not None:
            self.message_obj = _MsgObj(full)

    def stop_event(self):
        self.stopped = True


class _Ctx:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []
        self.calls = 0
        self.prompts = []

    async def get_current_chat_provider_id(self, umo=None):
        return "stub-provider"

    async def llm_generate(self, **kwargs):
        self.calls += 1
        # 同时记 system 与 user：有些约束拼在 user 侧
        self.prompts.append(
            str(kwargs.get("system_prompt") or "") + "\n" + str(kwargs.get("prompt") or "")
        )
        r = self.replies.pop(0) if self.replies else '{"action":"none"}'
        return types.SimpleNamespace(completion_text=r)

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain.text))
        return True


logging.basicConfig(level=logging.CRITICAL)
stub("astrbot", api=types.ModuleType("astrbot.api"))
stub("astrbot.api", logger=logging.getLogger("t"))
stub("astrbot.api.event", AstrMessageEvent=_Event, MessageChain=_Chain, filter=_Filter())


def _fresh_data_dir(*a, **k):
    # 每个插件实例一份独立 state，避免指令矩阵互相污染
    return tempfile.mkdtemp(prefix="relay_selftest_")


stub(
    "astrbot.api.star",
    Context=_Ctx,
    Star=_Star,
    register=lambda *a, **k: (lambda cls: cls),
    StarTools=types.SimpleNamespace(get_data_dir=_fresh_data_dir),
)
spec = importlib.util.spec_from_file_location("relay_mod", MAIN)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(detail)) if detail and not cond else ""))
    if not cond:
        failures.append(name)


A, B, C = (
    "aiocqhttp:FriendMessage:10001",
    "aiocqhttp:FriendMessage:10002",
    "aiocqhttp:FriendMessage:10003",
)
BOUND = {"session_a": A, "session_b": B, "name_a": "阿泽", "call_a": "泽哥",
         "name_b": "小李", "call_b": "小李", "mode": "context", "history_rounds": 4,
         "context_rounds": 6, "ack_sender": True}


def reply(obj):
    return json.dumps(obj, ensure_ascii=False)


RELAY = {
    "action": "relay",
    "say": "这周末他那边抽不开身，让你先别留档了。",
    "care": "诶你昨天不是说为了这个推了别的安排吗，别白忙一场哈。",
    "care_to": "receiver",
    "ack": "行，我记着了，等会儿替你问一声。",
    "expects_reply": True,
    "replying": False,
    "subtext": "",
    "subtext_to": "none",
    "about_self": "最近容易临时改约，但会提前打招呼",
    "about_peer": "为了这次约会把别的安排推了",
}
RELAY_BACK = {
    "action": "relay",
    "say": "他说改天再约也行，让你别空等。",
    "care": "",
    "care_to": "none",
    "ack": "嗯，这话我给你捎回去了。",
    "expects_reply": False,
    "replying": True,
    "subtext": "",
    "subtext_to": "none",
}
ASK_QUESTION = "你帮我问问他周末有没有空"
DEFER_QUESTION = "后边再说吧，你再问问他就是了"
IDLE_CHAT = "今天这天气也太闷了，你说是不是要下雨"


async def main():
    # ==================================================================
    # 1. 绑定硬门槛
    # ==================================================================
    for cfg, label in (
        ({}, "空配置"), ({"session_a": A}, "只填一个"),
        ({"session_a": A, "session_b": A}, "两个相同"),
        ({"session_a": "abc", "session_b": "def"}, "不像 UMO"),
    ):
        cx = _Ctx([])
        px = mod.RelayTalkPlugin(cx, cfg)
        check("绑定: " + label + " -> 未绑定", px._configured_pair() == [])
    cx = _Ctx([])
    px = mod.RelayTalkPlugin(cx, BOUND)
    check("绑定: 两项齐全且不同 -> 已绑定", px._configured_pair() == [A, B])
    check("绑定: 配对不写进 state", "pair" not in px.state)

    cx = _Ctx([reply(RELAY)])
    px = mod.RelayTalkPlugin(cx, {})
    ev = _Event(ASK_QUESTION, A)
    await px.on_any(ev)
    check("休眠: 不拦截", not ev.stopped)
    check("休眠: 零模型调用", cx.calls == 0, cx.calls)
    check("休眠: 零发送", cx.sent == [], cx.sent)

    cx = _Ctx([reply(RELAY)])
    px = mod.RelayTalkPlugin(cx, BOUND)
    ev = _Event(ASK_QUESTION, C)
    await px.on_any(ev)
    check("第三人: 不拦截/不调用/不发送", not ev.stopped and cx.calls == 0 and cx.sent == [])

    # ==================================================================
    # 2. 上下文闸门（核心：不是每句都判，闲聊能正常陪）
    # ==================================================================
    for t, label in (
        (IDLE_CHAT, "纯闲聊"), ("给我讲个笑话", "问机器人问题"),
        ("[旺柴]", "纯表情"), ("嗯", "单字"), ("？？？", "纯标点"),
        ("12345", "纯数字"), ("/话筒 状态", "斜杠指令"),
    ):
        cx = _Ctx([])
        px = mod.RelayTalkPlugin(cx, BOUND)
        ev = _Event(t, A)
        await px.on_any(ev)
        check("闸门放行: " + label,
              cx.calls == 0 and not ev.stopped and cx.sent == [], (cx.calls, cx.sent))
        check("闸门放行但仍记上下文: " + label,
              len(px.state.get("turns", [])) >= 1 or t.startswith("/"),
              px.state.get("turns"))

    for t, label in (
        (ASK_QUESTION, "你帮我问问他"),
        (DEFER_QUESTION, "后边再说你再问问他"),
        ("你跟他说我周末可能去不了了", "你跟他说"),
        ("帮我带个话，明天老时间", "带个话"),
        ("替我问一句他还要不要那本书", "替我问一句"),
        ("待会告诉他别等了", "待会告诉他"),
        ("转告他一声", "转告他"),
    ):
        cx = _Ctx([reply({"action": "none"})])
        px = mod.RelayTalkPlugin(cx, BOUND)
        await px.on_any(_Event(t, A))
        check("闸门召回: " + label, cx.calls == 1, cx.calls)

    # 判定 none 完全不打扰，且记进上下文
    cx = _Ctx([reply({"action": "none"})])
    px = mod.RelayTalkPlugin(cx, BOUND)
    ev = _Event("他那边最近好像也挺忙的", A)
    await px.on_any(ev)
    check("判 none: 不拦截", not ev.stopped)
    check("判 none: 不发送", cx.sent == [], cx.sent)
    check("判 none: 记进上下文", len(px.state["turns"]) == 1)

    # 判定失败 / 非法 JSON 一律放行，不吞闲聊
    cx = _Ctx(["不是 JSON"])
    px = mod.RelayTalkPlugin(cx, BOUND)
    ev = _Event(ASK_QUESTION, A)
    await px.on_any(ev)
    check("非法 JSON 放行", not ev.stopped and cx.sent == [])

    # ==================================================================
    # 3. 上下文注入到判定器
    # ==================================================================
    cx = _Ctx([reply(RELAY), reply({"action": "none"})])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.on_any(_Event(IDLE_CHAT, A))          # 先闲聊，记进上下文
    await px.on_any(_Event(ASK_QUESTION, A))       # 再判，prompt 应能看到那句闲聊
    check("判定器: 收到对话上下文",
          bool(cx.prompts) and IDLE_CHAT[:10] in cx.prompts[0], cx.prompts[:1])
    check("判定器: 有 last_delivery 段",
          bool(cx.prompts) and "上一次你替他们递过去的话" in cx.prompts[0])

    # ==================================================================
    # 4. relay 基本行为 + AI 生成回执
    # ==================================================================
    cx = _Ctx([reply(RELAY)])
    px = mod.RelayTalkPlugin(cx, BOUND)
    ev = _Event(ASK_QUESTION, A)
    await px.on_any(ev)
    check("relay: 已掐掉默认回复", ev.stopped)
    check("relay: B 收到转述", any(u == B and "抽不开身" in t for u, t in cx.sent), cx.sent)
    check("relay: B 收到 care", any(u == B and "别白忙" in t for u, t in cx.sent), cx.sent)
    check("relay: A 收到 AI 现写的 ack",
          any(u == A and "等会儿替你问一声" in t for u, t in cx.sent), cx.sent)
    check("relay: 不用固定回执文案",
          all("嗯，我帮你带到了～" not in t for _, t in cx.sent), cx.sent)
    check("relay: 记下上一次递话", bool(px._last_delivery())
          and px._last_delivery()["to"] == B, px._last_delivery())
    check("relay: expects_reply 存对", px._last_delivery()["expects_reply"] is True)
    check("relay: 原文不进 history",
          all(ASK_QUESTION not in h["say"] for h in px.state["history"]), px.state["history"])
    check("relay: B 没收到原文照搬",
          all(ASK_QUESTION not in t for u, t in cx.sent if u == B), cx.sent)

    # ack 每次不同（AI 自己写）
    cx = _Ctx([reply({**RELAY, "ack": "好嘞，这就去问。"}), reply({**RELAY, "ack": "收到，晚点我提一嘴。"})])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.on_any(_Event(ASK_QUESTION, A))
    await px.on_any(_Event("再帮我问问他好了没", A))
    acks = [t for u, t in cx.sent if u == A]
    check("ack: 两轮不相同", len(acks) >= 2 and acks[0] != acks[1], acks)
    check("ack: 两轮都用 AI 给的文案",
          "好嘞，这就去问" in acks[0] and "晚点我提一嘴" in acks[1], acks)

    # AI 没给 ack 就别硬凑固定句
    cx = _Ctx([reply({**RELAY, "ack": ""})])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.on_any(_Event(ASK_QUESTION, A))
    check("ack 缺失: 不编固定回执",
          not any(u == A for u, _ in cx.sent), cx.sent)

    # care 指向发话方
    cx = _Ctx([reply({**RELAY, "care_to": "sender", "ack": ""})])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.on_any(_Event(ASK_QUESTION, A))
    check("care→sender: A 收到关心", any(u == A and "别白忙" in t for u, t in cx.sent), cx.sent)
    check("care→sender: B 只有转述",
          any(u == B and "别白忙" not in t for u, t in cx.sent), cx.sent)

    # 复读重试
    cx = _Ctx([reply({**RELAY, "say": ASK_QUESTION}), reply(RELAY)])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.on_any(_Event(ASK_QUESTION, A))
    check("复读: 重试一次", cx.calls == 2, cx.calls)
    check("复读: 最终不照搬",
          all(ASK_QUESTION not in t for u, t in cx.sent if u == B), cx.sent)

    # ==================================================================
    # 5. B 回复 A 的话 → 同样走 relay 带回去（v3.5 核心）
    # ==================================================================
    cx = _Ctx([reply(RELAY), reply(RELAY_BACK)])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.on_any(_Event(ASK_QUESTION, A))       # A → B，建立 last_delivery to=B
    cx.sent.clear()
    ev = _Event("那就周日吧", B)                  # B 的回复不含任何"他/问"，旧版必漏
    await px.on_any(ev)
    check("回复场景: 仍交给 AI 判（上下文闸门命中）", cx.calls == 2, cx.calls)
    check("回复场景: 掐掉默认回复", ev.stopped)
    check("回复场景: 带回到 A", any(u == A and "改天再约" in t for u, t in cx.sent), cx.sent)
    check("回复场景: B 收到 AI 现写的 ack",
          any(u == B and "捎回去了" in t for u, t in cx.sent), cx.sent)
    check("回复场景: 判定器收到上一次递话内容",
          bool(cx.prompts) and "抽不开身" in cx.prompts[-1], cx.prompts[-1:] )
    d = px._last_delivery()
    check("回复场景: last_delivery 反向更新为 to=A", bool(d) and d["to"] == A, d)

    # 来回接力：A 再回一句也应被接住
    cx = _Ctx([reply(RELAY), reply(RELAY_BACK), reply(RELAY)])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.on_any(_Event(ASK_QUESTION, A))
    await px.on_any(_Event("那就周日吧", B))
    cx.sent.clear()
    await px.on_any(_Event("行，那就这么定", A))
    check("来回接力: A 的第三句也能带过去",
          any(u == B and "抽不开身" in t for u, t in cx.sent), cx.sent)

    # B 纯闲聊（跟刚才那件事无关）→ 判 none，放行、不打扰
    cx = _Ctx([reply(RELAY), reply({"action": "none"})])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.on_any(_Event(ASK_QUESTION, A))
    cx.sent.clear()
    ev = _Event("今天中午吃了啥呀，好无聊", B)
    await px.on_any(ev)
    check("回复场景: 无关闲聊判 none 后不拦截", not ev.stopped)
    check("回复场景: 无关闲聊零发送", cx.sent == [], cx.sent)

    # 上一次递话超时后不再作为回复线索（但仍可因对方指代被判）
    cx = _Ctx([reply(RELAY), reply({"action": "none"})])
    px = mod.RelayTalkPlugin(cx, {**BOUND, "context_reply_timeout_minutes": 1})
    await px.on_any(_Event(ASK_QUESTION, A))
    px.state["last_delivery"]["ts"] = "2020-01-01T00:00:00"
    check("上下文: 递话线索超时失效", px._last_delivery() is None)

    # ==================================================================
    # 6. confirm：拿不准就问，点头才带；文案 AI 现写
    # ==================================================================
    CONFIRM = {"action": "confirm", "note": "刚那句，要我替你带过去吗？"}
    AMBIGUOUS = "唉，他昨天那句话我有点介意"

    cx = _Ctx([reply(CONFIRM)])
    px = mod.RelayTalkPlugin(cx, BOUND)
    ev = _Event(AMBIGUOUS, A)
    await px.on_any(ev)
    check("confirm: 问 A 一句（AI 写的）",
          any(u == A and "要我替你带过去吗" in t for u, t in cx.sent), cx.sent)
    check("confirm: 没擅自投给 B", all(u != B for u, _ in cx.sent), cx.sent)
    check("confirm: 存下原话与对端", bool(px._confirm())
          and px._confirm()["raw"] == AMBIGUOUS and px._confirm()["dst"] == B)
    check("confirm: 已掐掉默认回复", ev.stopped)

    cx = _Ctx([reply(CONFIRM), reply({"answer": "yes"}), reply(RELAY)])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.on_any(_Event(AMBIGUOUS, A))
    cx.sent.clear()
    await px.on_any(_Event("带吧，跟他说一声", A))
    check("confirm yes: B 收到转述", any(u == B and "抽不开身" in t for u, t in cx.sent), cx.sent)
    check("confirm yes: 状态已清", px._confirm() is None)
    check("confirm yes: 不重发询问",
          all("要我替你带过去吗" not in t for _, t in cx.sent), cx.sent)

    cx = _Ctx([reply(CONFIRM), reply({"answer": "no", "note": "好，那就咱俩说说算啦～"})])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.on_any(_Event(AMBIGUOUS, A))
    cx.sent.clear()
    await px.on_any(_Event("算了，别让他知道", A))
    check("confirm no: 不投给 B", all(u != B for u, _ in cx.sent), cx.sent)
    check("confirm no: 用 AI 写的收尾", any(u == A and "咱俩说说" in t for u, t in cx.sent), cx.sent)
    check("confirm no: 状态已清", px._confirm() is None)

    cx = _Ctx([reply(CONFIRM),
               reply({"answer": "none", "retry_question": "我刚没说清～这句要不要递过去？"}),
               reply({"answer": "none", "give_up": "那先放着，回头你想说再叫我。"})])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.on_any(_Event(AMBIGUOUS, A))
    cx.sent.clear()
    await px.on_any(_Event("啊？你说啥", A))
    check("confirm none: 用 AI 现写的追问（非固定模板）",
          any(u == A and "我刚没说清" in t for u, t in cx.sent), cx.sent)
    check("confirm none: 状态还在且计一轮",
          bool(px._confirm()) and int(px._confirm()["rounds"]) == 1)
    cx.sent.clear()
    await px.on_any(_Event("哦哦，没看手机", A))
    check("confirm none: 两轮后作罢", px._confirm() is None)
    check("confirm none: 用 AI 现写的收尾",
          any(u == A and "回头你想说" in t for u, t in cx.sent), cx.sent)
    check("confirm none: 始终不投给 B", all(u != B for u, _ in cx.sent), cx.sent)

    cx = _Ctx([reply(CONFIRM)])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.on_any(_Event(AMBIGUOUS, A))
    before = px._confirm()
    ev = _Event("今天好热", B)
    await px.on_any(ev)
    check("confirm: 对方说话不接管", not ev.stopped and bool(px._confirm()))
    check("confirm: 状态未被改坏", px._confirm()["raw"] == before["raw"])

    cx = _Ctx([reply(CONFIRM), "不是 JSON"])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.on_any(_Event(AMBIGUOUS, A))
    ev = _Event("带吧", A)
    await px.on_any(ev)
    check("confirm: 判定失败放行不拦截", not ev.stopped)

    cx = _Ctx([reply(CONFIRM), reply(RELAY)])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.on_any(_Event(AMBIGUOUS, A))
    await px.cmd_relay(_Event("/带话 我晚点到", A))
    check("confirm: 手动发起后旧询问作废", px._confirm() is None)

    cx = _Ctx([reply(CONFIRM)])
    px = mod.RelayTalkPlugin(cx, {**BOUND, "confirm_timeout_minutes": 1})
    await px.on_any(_Event(AMBIGUOUS, A))
    px.state["confirm_pending"]["ts"] = "2020-01-01T00:00:00"
    check("confirm: 超时自动作罢", px._confirm() is None)

    # ==================================================================
    # 7. 判定档位
    # ==================================================================
    check("档位: 默认 context", mod.RelayTalkPlugin(_Ctx([]), BOUND)._judge_mode() == "context")
    check("档位: smart 旧名归到 context",
          mod.RelayTalkPlugin(_Ctx([]), {**BOUND, "mode": "smart"})._judge_mode() == "context")
    cx = _Ctx([])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.cmd_admin(_Event("/话筒 判定 always", A))
    check("运行时: 切 always 生效", px._judge_mode() == "always")
    await px.cmd_admin(_Event("/话筒 判定 清除配置", A))
    check("运行时: 清除覆盖回配置", px._judge_mode() == "context")
    await px.cmd_admin(_Event("/话筒 判定 乱写", A))
    check("运行时: 非法档位不写入", px.state.get("mode_override") is None)

    cx = _Ctx([reply({"action": "none"})])
    px = mod.RelayTalkPlugin(cx, {**BOUND, "mode": "always"})
    await px.on_any(_Event("嗯", A))
    check("always: 连单字也判", cx.calls == 1, cx.calls)

    cx = _Ctx([reply(RELAY)])
    px = mod.RelayTalkPlugin(cx, {**BOUND, "mode": "natural"})
    await px.on_any(_Event(DEFER_QUESTION, A))
    check("natural: 旧关键词粗筛会漏「再问问他」", cx.calls == 0, cx.calls)

    # ==================================================================
    # 8. 提示词边界（静态）
    # ==================================================================
    jp = mod.JUDGE_PROMPT
    for phrase, label in (
        ("你帮我问问他周末有没有空", "收录主动带话例子"),
        ("后边再说，你再问问他就是了", "收录延后说法例子"),
        ("他在回复对方刚递过来的话", "收录回复场景"),
        ("跟对方无关的闲聊一律不许用 confirm", "confirm 极窄"),
        ("没有带话意思 → none，彻底放行", "none 是默认"),
        ("每次现写，别背模板", "ack 必须现写"),
        ("expects_reply", "输出 expects_reply"),
        ("replying", "输出 replying"),
        ("ack", "输出 ack"),
        ("绝不写成断言", "潜台词禁断言"),
        ("绝不把拒绝猜成答应", "潜台词不翻案"),
        ("不把闲聊硬解读成托付", "不硬解读闲聊"),
        ("上下文第一", "上下文优先"),
    ):
        check("提示词: " + label, phrase in jp, label)
    check("提示词: 已无强力模式", "强力" not in jp and "chatter" not in jp)
    check("提示词: 已无旧保守方向", "宁可少带一次话，也不要把闲聊吞掉" not in jp)

    # ==================================================================
    # 9. 插件内分段（不再与任何模式互斥）
    # ==================================================================
    SEG = {**BOUND, "relay_segment": True, "segment_delay_min": 0,
           "segment_delay_max": 0, "segment_min_chars": 8, "segment_max_parts": 4}
    cx = _Ctx([reply(RELAY)])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.on_any(_Event(ASK_QUESTION, A))
    check("分段默认关: B 只收到一条", len([1 for u, _ in cx.sent if u == B]) == 1, cx.sent)

    cx = _Ctx([reply(RELAY)])
    px = mod.RelayTalkPlugin(cx, SEG)
    await px.on_any(_Event(ASK_QUESTION, A))
    seg = [t for u, t in cx.sent if u == B]
    check("分段开: B 收到多条", len(seg) >= 2, seg)
    check("分段开: 拼回去内容完整",
          any("抽不开身" in t for t in seg) and any("别白忙" in t for t in seg), seg)

    long_text = "。".join(f"第{i}句说点什么吧" for i in range(1, 12)) + "。"
    parts = mod.RelayTalkPlugin(_Ctx([]), SEG)._segment_text(long_text)
    check("分段: 不超过 max_parts", len(parts) <= 4, parts)
    check("分段: 没丢内容", "".join(parts).replace("\n", "") == long_text)
    check("分段: 无标点整句不切",
          mod.RelayTalkPlugin(_Ctx([]), SEG)._segment_text("好呀我这就去看看") ==
          ["好呀我这就去看看"])

    # v3.5：即便配置里残留旧 power_mode，分段也不再被忽略
    cx = _Ctx([reply(RELAY)])
    px = mod.RelayTalkPlugin(cx, {**SEG, "power_mode": True})
    check("分段: 不再与强力模式互斥", px._segment_active() is True)
    await px.on_any(_Event(ASK_QUESTION, A))
    check("分段: 旧 power_mode 残留下仍生效",
          len([1 for u, _ in cx.sent if u == B]) >= 2, cx.sent)

    cx = _Ctx([])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.cmd_admin(_Event("/话筒 分段 开", A))
    check("运行时: 分段开生效", px._segment_active() is True)
    await px.cmd_admin(_Event("/话筒 分段 关", A))
    check("运行时: 分段关生效", px._segment_active() is False)
    await px.cmd_admin(_Event("/话筒 分段 清除配置", A))
    check("运行时: 清除覆盖回配置", px.state["segment_override"] is None)

    # ==================================================================
    # 10. 手动指令 /带话
    # ==================================================================
    cx = _Ctx([reply(RELAY)])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.cmd_relay(_Event("/带话 我晚点到", A))
    check("/带话: B 收到转述", any(u == B and "抽不开身" in t for u, t in cx.sent), cx.sent)
    check("/带话: 强制 relay 标记注入",
          any("本轮强制 relay" in s for s in cx.prompts), cx.prompts[:1])
    cx = _Ctx([reply(RELAY)])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.cmd_relay(_Event("/带话", A))
    check("/带话: 空内容给用法提示",
          any(u == A and "用法" in t for u, t in cx.sent), cx.sent)
    cx = _Ctx([reply(RELAY)])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.cmd_relay(_Event("/带话 我晚点到", C))
    check("/带话: 第三人被拒", all(u != B for u, _ in cx.sent), cx.sent)

    # ==================================================================
    # 11. 指令矩阵：清单里每一条都实跑一遍
    # ==================================================================
    MATRIX = [
        ("/话筒", "传话筒状态", [], False),
        ("/话筒 状态", "传话筒状态", [], False),
        ("/话筒 检查", "传话筒状态", [], False),
        ("/话筒 status", "传话筒状态", [], False),
        ("/话筒 check", "传话筒状态", [], False),
        ("/话筒 指令", "指令一览", [], False),
        ("/话筒 commands", "指令一览", [], False),
        ("/话筒 help", "指令一览", [], False),
        ("/话筒 自检", "全部就绪", [], False),
        ("/话筒 selftest", "全部就绪", [], False),
        ("/话筒 verify", "全部就绪", [], False),
        ("/话筒 上下文", "最近对话上下文", [], False),
        ("/话筒 context", "最近对话上下文", [], False),
        ("/话筒 上下文 清", "都清掉了", [], False),
        ("/话筒 带 我晚点到", "抽不开身", [reply(RELAY)], True),
        ("/话筒 传 我晚点到", "抽不开身", [reply(RELAY)], True),
        ("/话筒 传话 我晚点到", "抽不开身", [reply(RELAY)], True),
        ("/话筒 说 我晚点到", "抽不开身", [reply(RELAY)], True),
        ("/话筒 潜台词", "潜台词翻译：关闭", [], False),
        ("/话筒 subtext", "潜台词翻译：关闭", [], False),
        ("/话筒 潜台词 开", "潜台词翻译已开启", [], False),
        ("/话筒 潜台词 关", "潜台词翻译已关闭", [], False),
        ("/话筒 潜台词 清除配置", "已清除运行时覆盖", [], False),
        ("/话筒 潜台词 尺度", "当前潜台词尺度", [], False),
        ("/话筒 潜台词 尺度 猜保守点", "好，之后按这个分寸猜", [], False),
        ("/话筒 判定", "判定档位：", [], False),
        ("/话筒 mode", "判定档位：", [], False),
        ("/话筒 判定 context", "判定档位已切到 context", [], False),
        ("/话筒 判定 always", "判定档位已切到 always", [], False),
        ("/话筒 判定 natural", "判定档位已切到 natural", [], False),
        ("/话筒 判定 清除配置", "已回到配置面板值", [], False),
        ("/话筒 分段", "插件内分段：关", [], False),
        ("/话筒 segment", "插件内分段：关", [], False),
        ("/话筒 分段 开", "插件内分段已开启", [], False),
        ("/话筒 分段 关", "插件内分段已关闭", [], False),
        ("/话筒 分段 清除配置", "已回到配置面板", [], False),
        ("/话筒 待确认", "当前没有在等你点头", [], False),
        ("/话筒 confirm", "当前没有在等你点头", [], False),
        ("/话筒 待确认 清", "那句就先不带了", [], False),
        ("/话筒 清空", "都已清空", [], False),
        ("/话筒 reset", "都已清空", [], False),
        ("/话筒 风格 更松弛一点", "好，之后按这个味道", [], False),
        ("/话筒 style 更松弛一点", "好，之后按这个味道", [], False),
        ("/话筒 称呼 老泽", "好，以后叫你", [], False),
        ("/话筒 call 老泽", "好，以后叫你", [], False),
    ]
    for raw, expect, replies, to_b in MATRIX:
        cx = _Ctx(replies)
        px = mod.RelayTalkPlugin(cx, BOUND)
        ev = _Event(raw, A)
        await px.cmd_admin(ev)
        joined = "\n".join(t for _, t in cx.sent)
        ok = bool(cx.sent) and expect in joined
        if to_b:
            ok = ok and any(u == B for u, _ in cx.sent)
        check("指令生效: " + raw, ok, cx.sent)

    # 清单完整性 + 强力模式确已下线
    for token in ("/带话", "/话筒 上下文", "/话筒 判定", "/话筒 分段",
                  "/话筒 待确认", "/话筒 自检", "/话筒 潜台词",
                  "/话筒 清空", "/话筒 风格", "/话筒 称呼"):
        check("清单完整: " + token, token in mod.COMMAND_LIST_TEXT)
    check("清单: 已无强力模式", "强力" not in mod.COMMAND_LIST_TEXT)
    check("清单: 已无待回音", "待回" not in mod.COMMAND_LIST_TEXT)

    src = open(MAIN, encoding="utf-8").read()
    for token in ("_needs_judge", "_record_turn", "_last_delivery", "_context_text",
                  "_delivery_text", "_handle_confirm", "_segment_text", "_send_out",
                  "CONFIRM_JUDGE_PROMPT", "JUDGE_PROMPT", "expects_reply", "replying"):
        check("代码分支: " + token, token in src)
    for gone in ("_power_on", "_handle_pending", "_power_judge", "POWER_JUDGE_PROMPT",
                 "chatter", "power_override", "ack_text"):
        check("已移除: " + gone, gone not in src, gone)

    # ==================================================================
    # 12. 指令解析鲁棒性（v3.1.1 的回归）
    # ==================================================================
    for raw, label in (
        ("/话筒 检查", "完整原文"), ("检查", "只剩参数"),
        ("话筒 检查", "半剥指令名"), ("／话筒 检查", "全角斜杠"),
        ("/话筒\u200b 检查", "零宽空格"), ("@机器人 /话筒 检查", "@ 前缀"),
        ("/话筒", "不带参数"), ("/话筒 状态", "状态别名"),
        ("/话筒 check", "英文别名"),
    ):
        cx = _Ctx([])
        px = mod.RelayTalkPlugin(cx, BOUND)
        await px.cmd_admin(_Event(raw, A, full=raw if "检查" in raw else None))
        check("解析: " + label,
              bool(cx.sent) and "传话筒状态" in "\n".join(t for _, t in cx.sent), cx.sent)

    # ==================================================================
    # 13. 潜台词翻译
    # ==================================================================
    SUB = {**BOUND, "subtext_mode": True}
    cx = _Ctx([reply({**RELAY, "subtext": "我猜他其实有点不好意思开口。",
                      "subtext_to": "receiver"})])
    px = mod.RelayTalkPlugin(cx, SUB)
    await px.on_any(_Event(ASK_QUESTION, A))
    check("潜台词: B 收到带揣测标记的一句",
          any(u == B and "我猜" in t for u, t in cx.sent), cx.sent)
    cx = _Ctx([reply({**RELAY, "subtext": "他其实很在意你",
                      "subtext_to": "receiver"})])
    px = mod.RelayTalkPlugin(cx, SUB)
    await px.on_any(_Event(ASK_QUESTION, A))
    check("潜台词: 无揣测标记整句丢弃",
          all("在意你" not in t for u, t in cx.sent if u == B), cx.sent)
    cx = _Ctx([reply({**RELAY, "subtext": "我猜他有点犹豫",
                      "subtext_to": "receiver", "about_peer": "我猜他有点犹豫"})])
    px = mod.RelayTalkPlugin(cx, SUB)
    await px.on_any(_Event(ASK_QUESTION, A))
    check("潜台词: 猜测不进画像",
          "我猜" not in str(px.state["profiles"].get(B, {}).get("about", "")),
          px.state["profiles"].get(B))

    # ==================================================================
    # 14. 状态总览 / 自检 / 清空
    # ==================================================================
    cx = _Ctx([reply(RELAY), reply(CONFIRM)])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.on_any(_Event(ASK_QUESTION, A))
    await px.on_any(_Event(AMBIGUOUS, B))   # B 也来一句含糊的 → confirm
    await px.cmd_admin(_Event("/话筒", A))
    s = "\n".join(t for _, t in cx.sent)
    check("状态: 显示判定档", "判定档：context" in s, s)
    check("状态: 显示上下文轮数", "上下文：" in s, s)
    check("状态: 显示上一次递话", "上一次递话：" in s, s)
    check("状态: 不再显示强力", "强力" not in s, s)

    cx = _Ctx([])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.cmd_admin(_Event("/话筒 自检", A))
    s = "\n".join(t for _, t in cx.sent)
    check("自检: 全部就绪", "全部就绪" in s, s)
    check("自检: 显示上下文轮数", "上下文：" in s, s)

    cx = _Ctx([reply(RELAY)])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.on_any(_Event(ASK_QUESTION, A))
    await px.cmd_admin(_Event("/话筒 上下文 清", A))
    check("清空上下文: turns 空", px.state["turns"] == [])
    check("清空上下文: last_delivery 空", px._last_delivery() is None)
    await px.cmd_admin(_Event("/话筒 清空", A))
    check("全清: history 空", px.state["history"] == [])
    check("全清: confirm 空", px._confirm() is None)

    # ==================================================================
    # 15. 提示词 format 不炸
    # ==================================================================
    import ast
    tree = ast.parse(src)
    for nm in ("JUDGE_PROMPT", "CONFIRM_JUDGE_PROMPT", "COMMAND_LIST_TEXT"):
        node = next(n for n in tree.body if isinstance(n, ast.Assign)
                    and any(getattr(x, "id", "") == nm for x in n.targets))
        p = ast.literal_eval(node.value)
        keys = set(re.findall(r"(?<!\{)\{([a-zA-Z_]\w*)\}(?!\})", p))
        p.format(**{k: "X" for k in keys})
        check("format OK: " + nm, True)

    # ==================================================================
    # 16. 全量指令矩阵（单一数据源）：逐条解析 + 逐条实跑生效
    #    复现用户报的「好多指令没被正确解析生效」
    # ==================================================================
    M = mod.COMMAND_MATRIX
    check("矩阵非空", len(M) >= 60, len(M))

    parse_bad = []
    for raw, want_head, want_rest, kind, _expect in M:
        if kind == "relaycmd":
            h, r = "带", mod.RelayTalkPlugin._strip_command(raw, ("带话", "话筒"))
        else:
            h, r = mod.RelayTalkPlugin._parse_text(raw)
        if h != want_head or (want_rest and r != want_rest):
            parse_bad.append((raw, h, r, want_head, want_rest))
    check(f"解析层 {len(M)}/{len(M)} 全通过", not parse_bad, parse_bad[:6])

    run_bad = []
    for raw, _h, _r, kind, expect in M:
        cx = _Ctx([reply(RELAY)] if kind in ("relay", "relaycmd") else [])
        px = mod.RelayTalkPlugin(cx, BOUND)
        ev = _Event(raw, A)
        if kind == "relaycmd":
            await px.cmd_relay(ev)
        else:
            await px.cmd_admin(ev)
        joined = "\n".join(t for _, t in cx.sent)
        ok = bool(cx.sent) and expect in joined
        if kind in ("relay", "relaycmd"):
            ok = ok and any(u == B for u, _ in cx.sent)
        if not ok:
            run_bad.append((raw, expect, joined[:100]))
    check(f"生效层 {len(M)}/{len(M)} 全通过（含真传话）", not run_bad, run_bad[:6])

    # 校验指令本身：运行时逐条核对、零状态改动
    cx = _Ctx([])
    px = mod.RelayTalkPlugin(cx, BOUND)
    await px.cmd_admin(_Event("/话筒 校验", A))
    v = "\n".join(t for _, t in cx.sent)
    check("/话筒 校验 报全部通过", "全部通过 ✅" in v and f"{len(M)}/{len(M)}" in v, v)
    check("/话筒 校验 声明零状态改动", "零状态改动" in v, v)
    check("/话筒 校验 不改绑定与历史",
          tuple(px._configured_pair()) == (A, B)
          and not px.state.get("history") and not px.state.get("turns"),
          (px._configured_pair(), px.state.get("history")))
    for alias in ("/话筒 验指令", "/话筒 检查指令", "/话筒 verify_cmds"):
        cx = _Ctx([])
        px2 = mod.RelayTalkPlugin(cx, BOUND)
        await px2.cmd_admin(_Event(alias, A))
        check(f"校验别名生效: {alias}", "全部通过 ✅" in "\n".join(t for _, t in cx.sent))

    # ==================================================================
    # 17. At 段污染回归（QQ 聊天截图复现）
    #    旧版 head 被解析成 [主人(2581793700)]/话筒，除「检查」被关键字
    #    误救外，自检/判定/分段/潜台词/风格/称呼全掉进「只回清单」兜底
    # ==================================================================
    AT_FORMS = [
        "[主人(2581793700)]/话筒 自检",
        "[主人(2581793700)]/话筒 检查",
        "/话筒 检查[主人(2581793700)]",
        "[主人(2581793700)]/带话 晚安",
        "[CQ:at,qq=2581793700] /话筒 自检",
        "@机器人(2581793700) /话筒 自检",
        "@机器人 /话筒 帮助",
        "／话筒 自检",
        "/话筒\u200b自检",
        "话筒 自检",
        "自检",
    ]
    clean = mod.RelayTalkPlugin._clean_input
    check("清洗确实删掉 [主人(QQ)]",
          clean("[主人(2581793700)]/话筒 自检") == "/话筒 自检",
          clean("[主人(2581793700)]/话筒 自检"))
    check("清洗保留 [旺柴]", clean("晚安[旺柴]") == "晚安[旺柴]", clean("晚安[旺柴]"))
    check("清洗保留 [微笑]", "[微笑]" in clean("带话[微笑]"))
    check("清洗删 CQ at", "[CQ:at" not in clean("[CQ:at,qq=2581793700]/话筒 自检"))
    check("多个 @ 前缀",
          mod.RelayTalkPlugin._parse_text("@机器人 @大肥鱼 /话筒 自检")[0] == "自检")

    for raw in AT_FORMS:
        if "/带话" in raw:
            # /带话 走 cmd_relay，剥掉指令名后剩下的整体是正文
            body = mod.RelayTalkPlugin._strip_command(raw, ("带话", "话筒"))
            check(f"At 形态解析: {raw[:26]}", body == "晚安", (raw, body))
        else:
            h, _ = mod.RelayTalkPlugin._parse_text(raw)
            check(f"At 形态解析: {raw[:26]}", h in mod.ALL_KNOWN_HEADS, (raw, h))

    # 冒号紧贴 / 英文大小写（旧版全部掉兜底）
    for raw, want in (("/话筒 判定：context", "判定"), ("/话筒 分段：开", "分段"),
                      ("/话筒 潜台词：开", "潜台词"), ("/话筒 风格：更松弛", "风格"),
                      ("/话筒 称呼：老泽", "称呼"), ("/话筒 带：我晚点到", "带"),
                      ("/话筒 STATUS", "状态"), ("/话筒 HELP", "指令")):
        h, _ = mod.RelayTalkPlugin._parse_text(raw)
        check(f"冒号/大小写: {raw}", h == want, (raw, h, want))

    # 多来源：message_str 只剩 At 文本，真指令在 Plain 段里
    ev_obj = _Event("[主人(2581793700)]", A)
    ev_obj.message_obj = types.SimpleNamespace(
        message=[_At(2581793700), _Plain("/话筒 自检")])
    check("消息链跳过 At 只取 Plain",
          mod.RelayTalkPlugin._plain_from_event(ev_obj) == "/话筒 自检",
          mod.RelayTalkPlugin._plain_from_event(ev_obj))
    raw2 = mod.RelayTalkPlugin._command_text(ev_obj)
    check("_command_text 从消息链捞回真指令", "自检" in raw2, raw2)
    cx = _Ctx([])
    px3 = mod.RelayTalkPlugin(cx, BOUND)
    await px3.cmd_admin(ev_obj)
    check("端到端：只剩 At 文本也能执行自检",
          "全部就绪" in "\n".join(t for _, t in cx.sent), cx.sent)

    # /带话 正文洁净：At 段不能被当成要带过去的话
    cx = _Ctx([reply(RELAY)])
    px4 = mod.RelayTalkPlugin(cx, BOUND)
    await px4.cmd_relay(_Event("[主人(2581793700)]/带话 晚安", A))
    check("/带话 正文不含 At 文本",
          all("2581793700" not in t and "主人(" not in t for _, t in cx.sent), cx.sent)

    # 二级冒号：尺度：描述 也能落到「尺度」分支
    cx = _Ctx([])
    px5 = mod.RelayTalkPlugin(cx, BOUND)
    await px5.cmd_admin(_Event("/话筒 潜台词 尺度：猜保守点", A))
    check("潜台词 尺度：描述 生效",
          "好，之后按这个分寸猜" in "\n".join(t for _, t in cx.sent), cx.sent)

    # 兜底诊断
    cx = _Ctx([])
    px6 = mod.RelayTalkPlugin(cx, BOUND)
    await px6.cmd_admin(_Event("/话筒 瞎写的东西", A))
    check("未知参数带 [诊断] 清洗后原文",
          "[诊断]" in "\n".join(t for _, t in cx.sent), cx.sent)

    # ==================================================================
    # 18. 版本与文档一致性
    # ==================================================================
    meta = {}
    for line in open(os.path.join(ROOT, "metadata.yaml"), encoding="utf-8"):
        if ":" in line and not line.startswith(" ") and not line.strip().startswith("#"):
            k, val = line.split(":", 1)
            meta[k.strip()] = val.strip().strip('"')
    check("metadata 版本 1.0.2", meta.get("version") == "1.0.2", meta.get("version"))
    check("register 版本 1.0.2", '"1.0.2"' in src)
    check("全仓无 3.5.0 残留", "3.5.0" not in src)
    check("清单含 /话筒 校验", "/话筒 校验" in mod.COMMAND_LIST_TEXT)
    check("清单含 At 段说明", "主人(QQ号)" in mod.COMMAND_LIST_TEXT or "At 显示段" in mod.COMMAND_LIST_TEXT)
    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    for key in ("/话筒 校验", "COMMAND_MATRIX", "没认出参数"):
        check(f"README 含「{key}」", key in readme, key)
    check("README 与矩阵同源说明", "COMMAND_MATRIX" in readme)

    print()
    if failures:
        print(f"结果：{len(failures)} 项失败 -> {failures}")
        sys.exit(1)
    print("结果：全部通过 OK")


asyncio.run(main())
