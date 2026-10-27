# refusal — 让角色"真的拒绝"，并且"说了就要算数"

一个独立模块：模型负责判断**想不想拒绝 / 要不要承诺**，本模块负责**拒绝就得算数、承诺到期就得提**。

```bash
python3 refusal.py judge --text "你去群里发个消息"
# ↑ 输出判定提示词，拼进请求交给模型

python3 refusal.py apply --json '{"refuse":true,"kind":"hard","minutes":5,"say":"不要。","mood":"upset","intensity":0.5,"cause":"angry"}'
# ↑ 模型回答后落地：写沉默期 + 情绪 + 承诺

python3 refusal.py gate --scope private
# ↑ 每轮先查：沉默中退出码 10，编排层据此【不调用模型】
```

零依赖（Python 3.10+ 标准库），数据在一个 SQLite 文件里。

## 为什么需要它

模型说"我不理你了"，如果下一轮编排层照常把消息喂给它、照常回复，这句话就是假的——提示词写得再好也救不了。

**"真"必须由系统强制。** 这个模块就干这件事：把一句文本变成一个时间戳，然后在时间戳到达前，真的不调用模型。同理，"等会儿告诉你"也必须变成一条带到期时间的记录，到期了真的主动提。

## 命令分工

| 命令 | 谁调 | 作用 |
|---|---|---|
| `gate` | 编排层，每轮最先 | 沉默中返回退出码 10，让上层直接跳过模型调用 |
| `resume` | 编排层，沉默刚结束时 | 返回残留情绪（"她还没满血复活"），用于注入上下文 |
| `judge` | 编排层，每轮 | 生成判定提示词（性格 + 情绪 + 预算 + 时长档 + 承诺说明） |
| `apply` | 编排层，拿到模型 JSON 后 | 校验并落地：写沉默、写情绪、写承诺 |
| `status` | 调试 / 编排层 | 当前心情、沉默剩余、今日预算、到期承诺数 |
| `commit` | 编排层 / 手动 | 单独记一条承诺（不经过模型） |
| `due` | 编排层，每轮 | 列出到期该主动提起的承诺 |
| `fulfill` / `fail` | 编排层 | 标记承诺完成 / 食言 |
| `break` | 编排层 | 打破沉默（"破功"：群里有人 @ 她，她没忍住回了一句） |
| `expire` | 定时任务 | 清理到期沉默 |
| `audit` | 定期 | 一致性审计：拒绝 / 承诺有没有落地（假话率） |

顺序固定：**gate → resume → judge → 模型 → apply → 发消息**。apply 里先写库再返回，保证不会出现"说了不理、其实还在回"。

## 集成示例

```python
import json, subprocess

def ask(user_text: str, memory: str = ""):
    # 1. 闸门
    g = subprocess.run(["python3", "refusal.py", "gate", "--json"],
                       capture_output=True, text=True)
    if g.returncode == 10:
        return None                      # 沉默中：不调用模型、不回复

    # 2. 残留情绪（沉默刚结束时，别让她满血复活）
    r = subprocess.run(["python3", "refusal.py", "resume", "--json"],
                       capture_output=True, text=True)
    residual = json.loads(r.stdout).get("residual") or ""

    # 3. 到期承诺主动提
    d = subprocess.run(["python3", "refusal.py", "due"],
                       capture_output=True, text=True)
    due = json.loads(d.stdout)["due"]

    # 4. 判定提示词
    j = subprocess.run(["python3", "refusal.py", "judge",
                        "--text", user_text, "--memory", memory],
                       capture_output=True, text=True)
    decision_prompt = j.stdout

    # 5. 调模型（system = persona + residual + due 提醒 + decision_prompt）
    raw = call_llm(system=PERSONA + residual + due_note(due) + decision_prompt,
                   messages=recent_turns)

    # 6. 落地
    decision = extract_first_json(raw)
    subprocess.run(["python3", "refusal.py", "apply",
                    "--json", json.dumps(decision, ensure_ascii=False),
                    "--request", user_text])

    # 7. 发出去
    return decision["say"]
```

## 人格从哪来

判定用的人设**必须**是角色自己的档案，否则会拿别人的脾气替她做决定。

```
--soul <path>      显式指定
$REFUSAL_SOUL      环境变量
./persona.md       当前工作目录
./soul.md          当前工作目录
（都没有）          → 内置中性占位
```

**只看当前工作目录，不往上级找。** 上级目录里可能放着别人的档案（或本仓库的 `example-persona.md`），读它会污染你的角色。

`judge --json` 的 stderr 会带 `soul_source`，用来确认这次到底用了谁：

```json
{"mode":"judge","soul_source":"(内置通用占位)", "mood":"normal", ...}
```

内置占位是**刻意中性**的——只说"她有脾气、会拒绝、被冒犯会沉默"，不描述任何具体角色。忘传档案时得到的是通用人格，不是别人的角色。

## 规则参数（改这里调脾气）

```python
MAX_MINUTES = 30           # 沉默上限
MIN_MINUTES = 2            # 最短沉默
HARD_REFUSAL_BUDGET = 3    # 每天最多几次硬拒绝
SAFETY_WORDS = ("关机","紧急","救命","报警","急事","出事","危险","快帮我","着火","车祸","生病","求救")
```

**时长按情绪强度分档**，模型只能在档内选：

| 情绪强度 | 允许时长 | 语义 |
|---|---|---|
| 0.0–0.6 | 2–5 分钟 | 轻微不满 / 有点累 |
| 0.6–0.85 | 10–20 分钟 | 真的生气 |
| 0.85–1.0 | 20–30 分钟 | 极度生气 |

模型给得超范围就夹紧，并记 `clamped=1`。

> 注意：阈值是按 0–1 的**直觉刻度**对齐的——0.5 是"有点不爽"，不是"真生气"。早期版本把 0.5 落在 10–20 分钟档，会导致轻微闹脾气也沉默十几分钟，显得假。

## 三条硬约束（都由 apply 强制）

1. **安全类不许硬拒绝**。请求里出现"救命""出事了"这类词，`kind` 从 `hard` 降为 `soft`——她可以嘴上嘟囔，但不能拿脾气挡着。
2. **硬拒绝有预算**。一天 3 次，超了自动降级为 `soft`。不设上限，她会用拒绝逃避一切，系统就废了。
3. **时长必须落在档内**，且不超过 30 分钟。没有上限，她可能"三天不理你"，项目直接停摆。

另外，`apply` 对 `refuse` 字段做**严格类型解析**：只有布尔 `true` 才算拒绝（字符串 `"false"` 不会再被 `bool()` 误判成拒绝）。

## 三种拒绝

| kind | 含义 | 系统动作 |
|---|---|---|
| `hard` | 真的不做、真的不说话 | 写 `silences`，gate 拦截 |
| `soft` | 嘴上不情愿，但还是做了 | 只写 `moods`，不拦截 |
| `none` | 没有拒绝 | 只记录 |

## 沉默期怎么让你知道

完全不回，用户会以为机器人挂了。所以沉默期由 `notice` 决定要不要发一句话：

```bash
python3 refusal.py notice --scope private
# ……不想理你。还有十二分钟。      ← 第 1 条
# ……                              ← 第 2、3 条
# （空）                           ← 第 4 条起彻底不回
```

| 第几条消息 | 发什么 |
|---|---|
| 1 | 完整提示（含剩余分钟，中文数字） |
| 2–3 | `……` |
| 4 以后 | 不回了 |
| 群聊 | 默认不提示（别在群里播报"她不理你"） |

**每条都回"沉默中"不是沉默，是另一种骚扰**——所以只完整提示一次。

文案可用 `--template "……哼，{minutes}分钟。"` 覆盖；群聊要提示加 `--allow-group`。

编排层只改一处：

```python
code, _ = cli("refusal.py", "gate", "--json")
if code == 10:
    text = cli("refusal.py", "notice", "--scope", "private").stdout
    if text.strip():
        await send(text)      # 发提示，不调模型
    return
```

## 沉默的粒度与"能量维度"

`--scope` 决定对谁沉默（`private` / `group` / `all`，默认 `private`）。

`cause` 区分**为什么**沉默：`angry`（生气）/ `tired`（累了）/ `cold`（冷淡）/ `hurt`（难过）。"累了不回"和"生气不回"现在能区分了——`resume` 会按 `cause` 给出不同的残留情绪描述。

## 承诺（commitments）

模型说"等会儿告诉你"这类话时，`apply` 会把它落成一条 `commitments` 记录：

```json
{"refuse": false, "kind": "none", "say": "明天告诉你",
 "commitment": {"text": "明天告诉你结果", "due_in": 1440, "kind": "promise"}}
```

`due_in` 是"多少分钟后兑现"。到期后 `due` 命令会把它列出来，编排层主动提醒她兑现；`fulfill` / `fail` 标记完成或食言。这样"等会儿告诉你"才不是空话。

## 破功（打破沉默）

沉默不一定是铁板一块。群里有人 @ 她、她没忍住回了一句——这是最像人的"破功"，由编排层调用：

```bash
python3 refusal.py break --reason "群里被@了，她没忍住"
```

`break` 把当前沉默的 `broken_by` 记上，gate 随即放行。事后 `resume` 会读到这里，让她"别扭一下"而不是若无其事。

## 数据表

```sql
silences(start_ts, end_ts, scope, kind, cause, reason, source_text, broken_by)
moods(ts, mood, intensity, reason)
refusals(ts, request, kind, minutes, cause, reason, mood, clamped)
commitments(ts, text, kind, due_ts, status, done_ts, note)
```

`source_text` 存她当时说的那句话（"我不理你了"），用于事后核对——说了的话有没有对应记录。

## 审计：假话率

```bash
python3 refusal.py audit
# {"refusals": 1, "hard": 1, "hard_without_silence": 0, "fake_rate": 0.0,
#  "commitments": 1, "commitments_done": 0, "commitments_overdue": 1,
#  "verdict": "有说法没落地"}
```

- 一条 `hard` 拒绝，如果 5 秒内没有对应的 `silences` 记录，就算**没落地**（假话）。
- 一条承诺，如果 `due_ts` 已到还挂着 `pending`，就算**食言**（overdue）。

`fake_rate` 应该长期是 0，`commitments_overdue` 应该长期是 0。哪个大于 0，说明哪条路径绕过了 `apply` 或 `fulfill`。

## 恢复后的第一句

gate 放行不代表情绪归零。编排层放行后调 `resume`：

```json
{"mood": "upset", "intensity": 0.7, "residual": "她刚消气不久，语气还带点冷/别扭，不是满血复活"}
```

把 `mood`、`intensity`、`residual` 一起塞进请求，她才会说"……你还在啊。"而不是满血复活。**装作没发生过，就白做了。**

## 测试

所有时间都可以用 `--now <毫秒>` 注入，无需真的等：

```bash
python3 refusal.py reset
python3 refusal.py apply --now 1793090000000 \
  --json '{"refuse":true,"kind":"hard","minutes":5,"mood":"upset","intensity":0.5,"cause":"angry"}'
python3 refusal.py gate --now 1793090060000   # 还在沉默，exit=10
python3 refusal.py break --now 1793090060000 --reason "被@了"   # 破功，gate 放行
python3 refusal.py gate --now 1793090070000   # exit=0
```

## 已知边界

- **判定质量取决于模型**。本模块只做强制，不判断"这次该不该拒绝"。模型若永远输出 `refuse: false`，她也永远不会拒绝。
- **单进程假设**。两个人同时写同一个 db 会锁，单人使用没问题。
- **"破功"要编排层主动调 `break`**。模块不会自己去监听群里有没有人 @ 她。
- **`due` 只返回到期承诺**，至于"怎么提醒她兑现"，由编排层把结果拼进提示词。
