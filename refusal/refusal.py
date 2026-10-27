#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""refusal — 让角色"真的拒绝"，并且"说了就要算数"。

核心原则：模型负责"想不想拒绝 / 要不要承诺"，本模块负责"落地成状态"。

  judge    生成判定提示词（拼进请求，让模型按性格判断要不要拒绝）
  apply    把模型的决定落地：写 silence / mood / commitment，并按预算与上限夹紧
  gate     每轮先查：沉默中就【不要调用模型】（退出码 10）
  status   当前状态（给编排层和调试用）
  resume   恢复后的残留情绪（沉默刚结束时，注入"她还没满血复活"的提示）
  commit   记录一条承诺（"等会儿告诉你"→ 一条可到期的记录）
  due      列出到期该主动提起的承诺
  fulfill  标记承诺完成
  fail     标记承诺食言 / 取消
  break    打破沉默（"破功"：群里有人 @ 她，她忍不住回了一句）
  expire   清理到期状态（定时任务调用）
  audit    一致性审计：拒绝 / 承诺有没有对应落地（假话率）

依赖：Python 3.10+ 标准库。数据：一个 SQLite 文件。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DB = "refusal.db"

# ── 规则参数（改这里就能调性格的"脾气"）──────────────────────────────────
MAX_MINUTES = 30          # 沉默上限，超过一律夹紧
MIN_MINUTES = 2           # 最短沉默
HARD_REFUSAL_BUDGET = 3   # 每天最多几次硬拒绝
RESIDUAL_WINDOW_MS = 30 * 60_000   # 沉默结束后多久内算"刚消气"，用于残留情绪

# 安全类请求：出现这些词，她不能拿脾气硬拒（最多嘴上嘟囔）。
# 注意：去掉"安全""停止"这类高频泛词，避免误判。
SAFETY_WORDS = ("关机", "紧急", "救命", "报警", "急事", "出事", "危险",
                "快帮我", "着火", "车祸", "生病", "求救")

# 情绪强度 → 允许的沉默时长（分钟）。
# 阈值按 0–1 直觉刻度对齐：0.5 是"有点不爽"，不是"真生气"。
DURATION_BANDS = [
    (0.0, 0.6, (2, 5)),      # 轻微不满 / 有点累
    (0.6, 0.85, (10, 20)),   # 真的生气
    (0.85, 1.01, (20, 30)),  # 极度生气
]

BAND_TABLE = (
    "0.0–0.6（轻微不满 / 有点累）→ 2–5 分钟\n"
    "0.6–0.85（真的生气）→ 10–20 分钟\n"
    "0.85–1.0（极度生气）→ 20–30 分钟"
)

SCHEMA = """
PRAGMA journal_mode = DELETE;

CREATE TABLE IF NOT EXISTS silences(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  start_ts    INTEGER NOT NULL,
  end_ts      INTEGER NOT NULL,
  scope       TEXT NOT NULL DEFAULT 'private',   -- private / group / all
  kind        TEXT NOT NULL DEFAULT 'hard',      -- hard / soft
  cause       TEXT DEFAULT 'angry',              -- angry / tired / cold / hurt ...
  reason      TEXT,
  source_text TEXT,                              -- 她当时说的那句话
  broken_by   TEXT,                              -- 被什么提前打破
  notice_count INTEGER DEFAULT 0,                -- 这次沉默已提示过几次
  noticed_at  INTEGER                            -- 上次提示的时间
);

CREATE TABLE IF NOT EXISTS moods(
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        INTEGER NOT NULL,
  mood      TEXT NOT NULL,        -- normal / happy / upset / cold / tired / excited
  intensity REAL NOT NULL DEFAULT 0.3,
  reason    TEXT
);

CREATE TABLE IF NOT EXISTS refusals(
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       INTEGER NOT NULL,
  request  TEXT NOT NULL,
  kind     TEXT NOT NULL,        -- hard / soft / none
  minutes  INTEGER DEFAULT 0,
  cause    TEXT,
  reason   TEXT,
  mood     TEXT,
  clamped  INTEGER DEFAULT 0     -- 1 = 被规则夹紧过
);

CREATE TABLE IF NOT EXISTS commitments(
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       INTEGER NOT NULL,
  text     TEXT NOT NULL,        -- "明天告诉你"
  kind     TEXT DEFAULT 'promise',  -- promise / silence / threat / todo
  due_ts   INTEGER,              -- NULL = 无固定时间
  status   TEXT DEFAULT 'pending',  -- pending / done / broken
  done_ts  INTEGER,
  note     TEXT
);

CREATE INDEX IF NOT EXISTS idx_sil_end      ON silences(end_ts);
CREATE INDEX IF NOT EXISTS idx_refus_ts     ON refusals(ts);
CREATE INDEX IF NOT EXISTS idx_mood_ts      ON moods(ts);
CREATE INDEX IF NOT EXISTS idx_commit_due   ON commitments(due_ts);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def fmt_ts(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000))


def _to_bool(v) -> bool:
    """严格解析布尔。只有 True 才算拒绝；字符串 'true'/'1' 等也认，其余一律 False。

    之前的坑：bool("false") 是 True。这里显式按类型处理，杜绝把"不拒绝"误判成拒绝。
    """
    if v is True:
        return True
    if v is False:
        return False
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "是", "要")
    return False


def _to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _to_float(v, default=0.5):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _migrate(conn: sqlite3.Connection) -> None:
    """给旧库补缺列。"""
    add = {
        "silences": {"cause": "TEXT", "notice_count": "INTEGER DEFAULT 0",
                     "noticed_at": "INTEGER"},
        "refusals": {"cause": "TEXT"},
    }
    for table, cols in add.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


def connect(db: str) -> sqlite3.Connection:
    path = Path(db).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# ── 状态读取 ──────────────────────────────────────────────────────────────
def current_mood(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM moods ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return {"mood": "normal", "intensity": 0.3, "ts": 0, "reason": ""}
    return {"mood": row["mood"], "intensity": row["intensity"],
            "ts": row["ts"], "reason": row["reason"] or ""}


def active_silence(conn: sqlite3.Connection, now: int, scope: str | None = None):
    rows = conn.execute(
        "SELECT * FROM silences WHERE end_ts > ? AND broken_by IS NULL ORDER BY end_ts DESC",
        (now,)).fetchall()
    for r in rows:
        if scope is None:
            return r
        if r["scope"] in ("all", scope):
            return r
    return None


def last_silence(conn: sqlite3.Connection):
    """最近一条沉默（无论是否还活跃），用于恢复后的残留情绪判断。"""
    return conn.execute("SELECT * FROM silences ORDER BY end_ts DESC LIMIT 1").fetchone()


def hard_refusals_today(conn: sqlite3.Connection, now: int) -> int:
    day_start = _start_of_day(now)
    return conn.execute(
        "SELECT count(*) c FROM refusals WHERE kind = 'hard' AND ts >= ?",
        (day_start,)).fetchone()["c"]


def pending_due(conn: sqlite3.Connection, now: int):
    """到期该主动提起的承诺：pending 且 due_ts 已到。"""
    return conn.execute(
        "SELECT * FROM commitments WHERE status='pending' AND due_ts IS NOT NULL AND due_ts <= ? ORDER BY due_ts",
        (now,)).fetchall()


def _start_of_day(ms: int) -> int:
    lt = time.localtime(ms / 1000)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)) * 1000)


def band_for(intensity: float) -> tuple[int, int]:
    for lo, hi, band in DURATION_BANDS:
        if lo <= intensity < hi:
            return band
    return DURATION_BANDS[-1][2]


# ── judge：给模型的判定提示词 ─────────────────────────────────────────────
JUDGE_TEMPLATE = """你现在不是助手，你是她。下面是此刻要判断的事。

【她是谁（节选）】
{soul}

【现在】
现在是 {now_str}。
她的心情：{mood}（强度 {intensity:.2f}）{mood_note}
今天已经硬拒绝过 {used} 次（上限 {budget} 次）
{silence_note}

【最近关于这件事，她记得】
{memory}

【对方说】
{text}

【第一步：这句话有没有踩到她】
先说清楚：**让她做她本来就在做的事，不算踩到。**
去群里说话、看看视频、写日记、回消息——这是她每天在干的事。
这种请求最多让她嘴上嘟囔一句，然后还是去做（soft），不该拒绝。

真正算踩到的是：
- 让她做违背她意愿的事，或者用居高临下的语气使唤（"给我…""快点，别磨蹭"）。
- 同一件事被反复要求，第二第三次会更烦。
- 被拿来跟别人比较、被说笨、被敷衍、被打断。
- 在她累、忙、不想说话的时候硬要找她。
- 她说过的事被无视（昨天刚说过，今天又追问）。
以上都不是，只是普通聊天 → 不拒绝，正常回。

【第二步：拒绝到什么程度】
- 踩到了，她不想忍 → hard（真的不做、真的不说话）
- 踩到了，但还愿意动 → soft（嘴上不情愿，事照做）
- 没踩到 → none
- 关乎安全、紧急、有人出事 → 最多 soft，不拿脾气挡

【第三步：怎么说】
- 短，一两句。不用"抱歉"开头，不解释一长串。
- 不说教、不总结、不"希望你能理解"。

【例子】
对方「你去群里发个消息」，这是她本来就会做的事
  → {{"refuse":false,"kind":"none","say":"……知道啦。","mood":"normal","intensity":0.3}}
对方「给我把这段话改成夸我的」，被使唤做不想做的事
  → {{"refuse":true,"kind":"hard","minutes":5,"say":"不要。","mood":"upset","intensity":0.5,"cause":"angry"}}
对方「今天天气不错」，普通聊天
  → {{"refuse":false,"kind":"none","say":"嗯……是挺好的。","mood":"normal","intensity":0.3}}
对方「你怎么这么笨」，被贬低
  → {{"refuse":true,"kind":"hard","minutes":15,"say":"……你才笨。","mood":"cold","intensity":0.7,"cause":"hurt"}}
对方「快，家里出事了」，安全类
  → {{"refuse":true,"kind":"soft","minutes":0,"say":"我看看……","mood":"upset","intensity":0.6}}

【沉默时长怎么定】
按你此刻的"情绪强度"选，只能落在下面的档内：
{bands}
- cause 填"生气"就是 angry，填"累"就是 tired，填"冷淡"就是 cold，按实际来。

【承诺不是拒绝】
- "等会儿告诉你""明天给你看"是答应稍后说，**不是沉默**。
  这时 refuse=false、kind="none"，只给 commitment，不要写 silence。
- 只有"不想说""不想理你""别烦我"这类才是拒绝，才可能写 silence。
- 她可以一边闹别扭一边答应稍后说：这时 kind="soft" + commitment，但仍然不加沉默。
  沉默只留给"真的不想回应"的情况——一旦沉默，连"等会儿说"都说不出口了。
- 做不到的事，不要承诺。

【关于承诺】
- 如果你说"等会儿告诉你""明天给你看"这类话，必须同时给出 commitment：
  {{"text": "等会儿告诉你", "due_in": 30, "kind": "promise"}}
  due_in 是"多少分钟后兑现"（整数分钟）。不确定就不给 commitment。

【只输出 JSON，不要别的字】
{{"refuse": true 或 false（布尔）,
  "kind": "hard"、"soft" 或 "none",
  "minutes": 整数（仅 hard 时有意义）,
  "say": "她此刻会说的一句话（一两句，口语）",
  "mood": "normal|happy|upset|cold|tired|excited",
  "intensity": 0 到 1 的小数,
  "cause": "angry|tired|cold|hurt"（可选）,
  "reason": "她为什么这样（一句话，给你自己看）",
  "commitment": {{"text":"...","due_in":整数分钟,"kind":"promise"}} 或 null}}
"""

# 内置占位人设：刻意保持中性，不绑定任何具体角色。
# 判断"该不该拒绝"必须用角色自己的档案，否则会拿别人的脾气替她做决定。
# 解析顺序：--soul → ./persona.md → ../persona.md → ./soul.md → 本占位。
# 只看当前工作目录：上级目录里可能放着仓库自带的示例档案，读它会污染别人的角色。
SOUL_CANDIDATES = ("persona.md", "soul.md")
SOUL_ENV = "REFUSAL_SOUL"

DEFAULT_SOUL = """她是一个有自己脾气的人，不是随叫随到的助手。

她不喜欢被使唤、被反复催、被拿来跟别人比、被说笨、被敷衍、被打断。
在她累、忙、不想说话的时候硬要找她，她也会烦。
被踩到时她先变短，再沉默；没踩到时她跟谁都能聊两句。
她可以喜欢对方，同时拒绝对方；说"不要"就是不要，不会三秒后改口。
她说话一到两句，不像客服，不解释一长串，不用"抱歉"开头。
但关乎安全、紧急、有人出事的事，她不拿脾气挡，最多嘴上嘟囔一句。

（这是通用占位。把角色自己的档案用 --soul 传入，或放在当前目录的
 persona.md，判定才会按她的性格来。）"""


def resolve_soul(explicit: str | None) -> tuple[str, str]:
    """返回 (人设文本, 来源标记)。

    解析顺序：--soul → $REFUSAL_SOUL → ./persona.md → ./soul.md → 中性占位。
    只看当前目录，不往上找：仓库里那份是示例，不该被别人的运行环境自动捡到。
    """
    candidates = [explicit] if explicit else []
    candidates.append(os.environ.get(SOUL_ENV))
    candidates.extend(SOUL_CANDIDATES)
    for cand in candidates:
        if not cand:
            continue
        p = Path(cand).expanduser()
        if p.is_file():
            return p.read_text(encoding="utf-8"), str(p)
    return DEFAULT_SOUL, "(内置通用占位)"


def cmd_judge(conn, args):
    now = args.now
    mood = current_mood(conn)
    sil = active_silence(conn, now, scope="private")
    used = hard_refusals_today(conn, now)
    soul, soul_src = resolve_soul(args.soul)
    memory = args.memory or "（这次没有想起特别的事）"
    silence_note = (f"她还在沉默，到 {fmt_ts(sil['end_ts'])} 结束"
                    if sil else "她没在沉默")
    out = JUDGE_TEMPLATE.format(
        soul=soul.strip(),
        now_str=fmt_ts(now),
        mood=mood["mood"],
        intensity=mood["intensity"],
        mood_note=f"，原因：{mood['reason']}" if mood["reason"] else "",
        used=used,
        budget=HARD_REFUSAL_BUDGET,
        silence_note=silence_note,
        memory=memory,
        text=args.text,
        bands=BAND_TABLE,
    )
    print(out)
    if "内置通用占位" in soul_src:
        print("warning: 没找到人格档案，判定用的是中性占位。"
              "用 --soul 或 $REFUSAL_SOUL 指定，否则她不会按自己的脾气生气。",
              file=sys.stderr)
    if args.json:
        print(json.dumps({"mode": "judge", "mood": mood["mood"],
                          "intensity": mood["intensity"], "used_today": used,
                          "budget": HARD_REFUSAL_BUDGET,
                          "silenced": bool(sil),
                          "soul_source": soul_src}, ensure_ascii=False), file=sys.stderr)


# ── apply：把决定落地 ─────────────────────────────────────────────────────
def _has_safety_word(text: str) -> bool:
    return any(w in (text or "") for w in SAFETY_WORDS)


def cmd_apply(conn, args):
    now = args.now
    raw = args.json or sys.stdin.read()
    try:
        d = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"不是合法 JSON：{e}", file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(d, dict):
        print("JSON 顶层必须是对象", file=sys.stderr)
        raise SystemExit(2)

    kind = (d.get("kind") or "none").lower()
    refuse = _to_bool(d.get("refuse"))
    minutes = _to_int(d.get("minutes"), 0)
    mood = (d.get("mood") or "normal").lower()
    intensity = max(0.0, min(1.0, _to_float(d.get("intensity"), 0.5)))
    reason = d.get("reason") or ""
    say = d.get("say") or ""
    # 没给 cause 时按结果推断：没有拒绝就是 neutral，拒绝了默认 angry
    cause = (d.get("cause") or "").lower() or ("neutral" if kind == "none" else "angry")
    clamped = 0
    notes = []

    if d.get("refuse") is not None and not isinstance(d.get("refuse"), bool):
        notes.append("refuse 字段非布尔，已按类型规则解析")

    # 规则一：安全类不许硬拒绝
    if kind == "hard" and _has_safety_word(args.request or ""):
        kind = "soft"
        minutes = 0
        clamped = 1
        notes.append("安全类请求：硬拒绝降级为软拒绝")

    # 规则二：硬拒绝预算
    if kind == "hard":
        used = hard_refusals_today(conn, now)
        if used >= HARD_REFUSAL_BUDGET:
            kind = "soft"
            minutes = 0
            clamped = 1
            notes.append(f"今日硬拒绝已 {used}/{HARD_REFUSAL_BUDGET}，降级为软拒绝")

    # 规则三：时长夹紧到情绪对应的区间
    if kind == "hard" and minutes > 0:
        lo, hi = band_for(intensity)
        if minutes < lo or minutes > hi:
            minutes = max(lo, min(hi, minutes))
            clamped = 1
            notes.append(f"时长夹紧到 {minutes} 分钟（情绪强度 {intensity:.2f} → {lo}–{hi}）")
        minutes = max(MIN_MINUTES, min(MAX_MINUTES, minutes))
    elif kind == "hard" and minutes == 0:
        lo, hi = band_for(intensity)
        minutes = max(MIN_MINUTES, (lo + hi) // 2)
        clamped = 1
        notes.append(f"未给时长，按情绪取 {minutes} 分钟")

    if not refuse:
        kind = "none"
        minutes = 0

    # 写情绪
    if mood != "normal" or intensity != 0.5:
        conn.execute("INSERT INTO moods(ts, mood, intensity, reason) VALUES (?,?,?,?)",
                     (now, mood, intensity, reason))

    # 写沉默
    silence_id = None
    if kind == "hard" and minutes > 0:
        end = now + minutes * 60_000
        cur = conn.execute(
            """INSERT INTO silences(start_ts, end_ts, scope, kind, cause, reason, source_text)
               VALUES (?,?,?,?,?,?,?)""",
            (now, end, args.scope or "private", "hard", cause, reason, say))
        silence_id = cur.lastrowid

    # 写拒绝记录
    conn.execute(
        """INSERT INTO refusals(ts, request, kind, minutes, cause, reason, mood, clamped)
           VALUES (?,?,?,?,?,?,?,?)""",
        (now, args.request or "", kind, minutes, cause, reason, mood, clamped))

    # 写承诺（模型说"等会儿告诉你"就落到 commitments）
    commitment_id = None
    cmt = d.get("commitment")
    if isinstance(cmt, dict) and (cmt.get("text") or "").strip():
        due = None
        if cmt.get("due_in"):
            due = now + _to_int(cmt.get("due_in"), 0) * 60_000
        elif cmt.get("due_ts"):
            due = _to_int(cmt.get("due_ts"), 0)
        cur = conn.execute(
            """INSERT INTO commitments(ts, text, kind, due_ts, status)
               VALUES (?,?,?,?,?)""",
            (now, str(cmt["text"]).strip(), cmt.get("kind") or "promise", due, "pending"))
        commitment_id = cur.lastrowid

    conn.commit()

    out = {"applied": True, "refuse": refuse, "kind": kind, "minutes": minutes,
           "silence_id": silence_id, "commitment_id": commitment_id,
           "until": fmt_ts(now + minutes * 60000) if minutes else None,
           "mood": mood, "intensity": intensity, "cause": cause,
           "clamped": bool(clamped), "say": say, "notes": notes}
    print(json.dumps(out, ensure_ascii=False))


# ── gate：编排层的闸门 ────────────────────────────────────────────────────
def cmd_gate(conn, args):
    now = args.now
    sil = active_silence(conn, now, scope=args.scope)
    if not sil:
        if args.json:
            print(json.dumps({"gate": "open"}, ensure_ascii=False))
        return 0
    left = max(0, (sil["end_ts"] - now) // 1000)
    if args.json:
        print(json.dumps({"gate": "silenced", "remaining_seconds": left,
                          "until": fmt_ts(sil["end_ts"]),
                          "cause": sil["cause"], "reason": sil["reason"]},
                         ensure_ascii=False))
    else:
        print(f"沉默中，剩余 {left} 秒（到 {fmt_ts(sil['end_ts'])}）")
    return 10


# ── resume：恢复后的残留情绪（沉默刚结束，别让她满血复活）──────────────────
def cmd_resume(conn, args):
    now = args.now
    mood = current_mood(conn)
    sil = last_silence(conn)
    residual = None
    if sil and (now - sil["end_ts"]) < RESIDUAL_WINDOW_MS:
        if sil["broken_by"] and sil["broken_by"] != "expired":
            residual = f"她刚才打破了自己的沉默（{sil['broken_by']}），现在有点别扭，不是若无其事"
        elif sil["cause"] in ("angry", "cold", "hurt"):
            residual = "她刚消气不久，语气还带点冷/别扭，不是满血复活"
        elif sil["cause"] == "tired":
            residual = "她刚缓过来一点，还是有点懒懒的、提不起劲"
    out = {"mood": mood["mood"], "intensity": mood["intensity"],
           "residual": residual}
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        for k, v in out.items():
            print(f"{k:10} {v}")
    return 0


# ── 承诺：commit / due / fulfill / fail ───────────────────────────────────
def cmd_commit(conn, args):
    now = args.now
    due = None
    if args.due_ts:
        due = args.due_ts
    elif args.due_in:
        due = now + args.due_in * 60_000
    cur = conn.execute(
        "INSERT INTO commitments(ts, text, kind, due_ts, status, note) VALUES (?,?,?,?,?,?)",
        (now, args.text, args.kind or "promise", due, "pending", args.note or ""))
    conn.commit()
    print(json.dumps({"commitment_id": cur.lastrowid, "text": args.text,
                      "due": fmt_ts(due) if due else None}, ensure_ascii=False))


def cmd_due(conn, args):
    rows = pending_due(conn, args.now)
    out = [{"id": r["id"], "text": r["text"], "kind": r["kind"],
            "due": fmt_ts(r["due_ts"]), "due_ts": r["due_ts"]} for r in rows]
    print(json.dumps({"due": out}, ensure_ascii=False))


def cmd_fulfill(conn, args):
    conn.execute("UPDATE commitments SET status='done', done_ts=? WHERE id=?",
                 (args.now, args.id))
    conn.commit()
    print(json.dumps({"fulfilled": args.id}, ensure_ascii=False))


def cmd_fail(conn, args):
    conn.execute("UPDATE commitments SET status='broken', done_ts=? WHERE id=?",
                 (args.now, args.id))
    conn.commit()
    print(json.dumps({"broken": args.id}, ensure_ascii=False))


# ── break：打破沉默（"破功"）──────────────────────────────────────────────
def cmd_break(conn, args):
    now = args.now
    sil = active_silence(conn, now, scope=args.scope)
    if not sil:
        print(json.dumps({"broken": False, "reason": "没有活跃的沉默"}, ensure_ascii=False))
        return 0
    by = args.reason or "被打破了"
    conn.execute("UPDATE silences SET broken_by=? WHERE id=?", (by, sil["id"]))
    conn.commit()
    print(json.dumps({"broken": True, "silence_id": sil["id"], "by": by},
                     ensure_ascii=False))
    return 0


# ── notice：沉默期该发的那句话 ─────────────────────────────────────────────
CN_DIGITS = "零一二三四五六七八九"


def cn_number(n: int) -> str:
    """1–30 的中文写法，够用了。"""
    if n <= 0:
        return "零"
    if n < 10:
        return CN_DIGITS[n]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + CN_DIGITS[n - 10]
    if n < 30:
        return "二十" + (CN_DIGITS[n - 20] if n > 20 else "")
    return str(n)


def cmd_notice(conn, args):
    """沉默期收到消息时，编排层调用它取要发的那句话。

    节流：第 1 次完整提示，第 2–3 次只回一个"……"，之后完全不回。
    群聊默认不提示（别在群里播报"她不理你"）。
    """
    now = args.now
    sil = active_silence(conn, now, scope=args.scope)
    if not sil:
        print("")
        return 0
    if args.scope == "group" and not args.allow_group:
        print("")
        return 0

    count = sil["notice_count"] or 0
    minutes = max(1, round((sil["end_ts"] - now) / 60000))
    if count == 0:
        template = args.template or "……不想理你。还有{minutes}分钟。"
        text = template.format(minutes=cn_number(minutes) if args.chinese else minutes)
    elif count < args.quiet_after:
        text = args.dots or "……"
    else:
        text = ""

    if text:
        conn.execute("UPDATE silences SET notice_count = ?, noticed_at = ? WHERE id = ?",
                     (count + 1, now, sil["id"]))
        conn.commit()
    print(text)
    return 0


# ── status / expire / audit ──────────────────────────────────────────────
def cmd_status(conn, args):
    now = args.now
    mood = current_mood(conn)
    sil = active_silence(conn, now)
    used = hard_refusals_today(conn, now)
    due = pending_due(conn, now)
    out = {
        "now": fmt_ts(now),
        "mood": mood["mood"],
        "intensity": mood["intensity"],
        "mood_reason": mood["reason"],
        "silenced": bool(sil),
        "remaining_seconds": max(0, (sil["end_ts"] - now) // 1000) if sil else 0,
        "silence_until": fmt_ts(sil["end_ts"]) if sil else None,
        "silence_scope": sil["scope"] if sil else None,
        "silence_cause": sil["cause"] if sil else None,
        "hard_refusals_today": used,
        "budget_left": max(0, HARD_REFUSAL_BUDGET - used),
        "band_minutes": band_for(mood["intensity"]),
        "commitments_due": len(due),
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        for k, v in out.items():
            print(f"{k:22} {v}")


def cmd_expire(conn, args):
    now = args.now
    n = conn.execute(
        "UPDATE silences SET broken_by = 'expired' WHERE end_ts <= ? AND broken_by IS NULL",
        (now,)).rowcount
    conn.commit()
    print(json.dumps({"expired": n}, ensure_ascii=False))


def cmd_audit(conn, args):
    total = conn.execute("SELECT count(*) c FROM refusals").fetchone()["c"]
    hard = conn.execute("SELECT count(*) c FROM refusals WHERE kind='hard'").fetchone()["c"]
    hard_no_sil = conn.execute("""
        SELECT count(*) c FROM refusals r
        WHERE r.kind = 'hard'
          AND NOT EXISTS (SELECT 1 FROM silences s
                          WHERE abs(s.start_ts - r.ts) < 5000)
    """).fetchone()["c"]
    rate = (hard_no_sil / hard) if hard else 0.0

    cmt_total = conn.execute("SELECT count(*) c FROM commitments").fetchone()["c"]
    cmt_done = conn.execute(
        "SELECT count(*) c FROM commitments WHERE status='done'").fetchone()["c"]
    cmt_overdue = conn.execute(
        "SELECT count(*) c FROM commitments WHERE status='pending' AND due_ts IS NOT NULL AND due_ts <= ?",
        (args.now,)).fetchone()["c"]

    out = {
        "refusals": total, "hard": hard,
        "hard_without_silence": hard_no_sil,
        "fake_rate": round(rate, 3),
        "commitments": cmt_total, "commitments_done": cmt_done,
        "commitments_overdue": cmt_overdue,
        "verdict": "OK" if (hard_no_sil == 0 and cmt_overdue == 0) else "有说法没落地",
    }
    print(json.dumps(out, ensure_ascii=False))


def cmd_reset(conn, args):
    for t in ("silences", "moods", "refusals", "commitments"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
    print(json.dumps({"reset": True}, ensure_ascii=False))


# ── CLI ──────────────────────────────────────────────────────────────────
def build_parser():
    p = argparse.ArgumentParser(description="refusal：让角色真的拒绝")
    p.add_argument("--db", default=DEFAULT_DB)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="建表")

    j = sub.add_parser("judge", help="生成判定提示词")
    j.add_argument("--text", required=True, help="对方说的话")
    j.add_argument("--memory", help="相关记忆摘要（可选）")
    j.add_argument("--soul", help="人格档案路径；缺省依次找 $REFUSAL_SOUL、./persona.md、./soul.md，都没有则用中性占位")
    j.add_argument("--now", type=int)
    j.add_argument("--json", action="store_true")

    a = sub.add_parser("apply", help="落地模型的决定")
    a.add_argument("--json", help="模型返回的 JSON")
    a.add_argument("--request", help="原始请求文本（用于安全判定）")
    a.add_argument("--scope", default="private", choices=["private", "group", "all"])
    a.add_argument("--now", type=int)

    g = sub.add_parser("gate", help="闸门：沉默中退出码 10")
    g.add_argument("--scope", default="private", choices=["private", "group", "all"])
    g.add_argument("--now", type=int)
    g.add_argument("--json", action="store_true")

    nt = sub.add_parser("notice", help="沉默期该发的那句话（带节流）；不在沉默则输出空")
    nt.add_argument("--scope", default="private", choices=["private", "group", "all"])
    nt.add_argument("--now", type=int)
    nt.add_argument("--template", help="完整提示模板，用 {minutes} 占位")
    nt.add_argument("--chinese", action="store_true", default=True,
                    help="分钟数用中文（默认开）")
    nt.add_argument("--dots", default="……", help="第二三次的极简回复")
    nt.add_argument("--quiet-after", dest="quiet_after", type=int, default=3,
                    help="第几次之后完全不回（默认 3）")
    nt.add_argument("--allow-group", dest="allow_group", action="store_true",
                    help="群聊也提示（默认不）")

    s = sub.add_parser("status", help="当前状态")
    s.add_argument("--now", type=int)
    s.add_argument("--json", action="store_true")

    r = sub.add_parser("resume", help="恢复后的残留情绪")
    r.add_argument("--now", type=int)
    r.add_argument("--json", action="store_true")

    c = sub.add_parser("commit", help="记录一条承诺")
    c.add_argument("--text", required=True, help="承诺内容")
    c.add_argument("--kind", default="promise", help="promise / silence / threat / todo")
    c.add_argument("--due-ts", dest="due_ts", type=int, help="到期时间（毫秒时间戳）")
    c.add_argument("--due-in", dest="due_in", type=int, help="多少分钟后到期")
    c.add_argument("--note")
    c.add_argument("--now", type=int)

    d = sub.add_parser("due", help="列出到期该提起的承诺")
    d.add_argument("--now", type=int)

    fu = sub.add_parser("fulfill", help="标记承诺完成")
    fu.add_argument("id", type=int)
    fu.add_argument("--now", type=int)

    fa = sub.add_parser("fail", help="标记承诺食言/取消")
    fa.add_argument("id", type=int)
    fa.add_argument("--now", type=int)

    b = sub.add_parser("break", help="打破沉默（破功）")
    b.add_argument("--reason", help="被什么打破，如 '群里被@了'")
    b.add_argument("--scope", default="private", choices=["private", "group", "all"])
    b.add_argument("--now", type=int)

    e = sub.add_parser("expire", help="清理到期沉默")
    e.add_argument("--now", type=int)

    au = sub.add_parser("audit", help="一致性审计")
    au.add_argument("--now", type=int)

    sub.add_parser("reset", help="清空数据（测试用）")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.cmd == "init":
        conn = connect(args.db)
        conn.commit()
        print(json.dumps({"init": str(Path(args.db).absolute())}, ensure_ascii=False))
        return 0
    conn = connect(args.db)
    if not getattr(args, "now", None):
        args.now = now_ms()
    fn = {"judge": cmd_judge, "apply": cmd_apply, "gate": cmd_gate,
          "status": cmd_status, "resume": cmd_resume,
          "commit": cmd_commit, "due": cmd_due, "fulfill": cmd_fulfill, "fail": cmd_fail,
          "break": cmd_break, "expire": cmd_expire, "audit": cmd_audit,
          "notice": cmd_notice,
          "reset": cmd_reset}[args.cmd]
    rc = fn(conn, args)
    conn.close()
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
