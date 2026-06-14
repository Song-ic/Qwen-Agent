# 架构与设计 · cc → Qwen-Agent

本文讲清楚两件事:**这套协作逻辑怎么跑**,以及**每个设计为什么这么做**。
读完你会明白:Qwen-Agent 的价值不在"模型多聪明",而在"那一圈把笨手框稳的工程"。

---

## 1. 心智模型:大脑 / 手 / 笼子

```
       判断(智能)                    执行(机械)                   保证(确定性)
   ┌──────────────┐            ┌──────────────┐           ┌──────────────┐
   │   Claude     │  规格       │  Qwen-Agent  │  diff      │   护栏 + 验证  │
   │   = 大脑      │ ─────────▶ │   = 手        │ ────────▶ │   = 笼子       │
   └──────────────┘            └──────────────┘           └──────────────┘
     不确定但聪明                 确定但不聪明                  纯确定性
```

三层各司其职,**关键是把"不确定性"挡在每层边界上**:

- **大脑(Claude)**:消化需求里的歧义,做判断,产出一份**无歧义的规格**。不确定性到这里收敛。
- **手(Qwen-Agent + 本地模型)**:拿无歧义规格,机械地探索→改→验。它仍可能犯错(改错文件、改太多、改不中),但不再需要"判断"。
- **笼子(harness)**:用纯确定性的代码,把"手"可能犯的错物理挡住或自动纠回。

> 设计哲学一句话:**把不确定的智能,关进确定的笼子。** 模型越不可靠,笼子越重要。

---

## 2. 主循环:`run_agent` 状态机

整个 agent 就是一个 **request → tool-calls → feedback** 的循环,加上几个止损/纠错的旁路。

```
 messages = [system_prompt, user(task)]
 nudges = 0 ; no_progress = 0
 │
 ▼  for turn in 1..max_turns:
 ┌─────────────────────────────────────────────────────────────┐
 │ chat()  ──HTTP──▶  OpenAI 兼容后端 (non-stream)               │
 │   ├─ HTTPError   → status: http_error  (退出)                 │
 │   └─ Exception   → status: exception   (退出)                 │
 │                                                              │
 │ 解析 message: reasoning_content / content / tool_calls       │
 │ 打印 turn 状态行                                              │
 │                                                              │
 │ ┌── 有 tool_calls? ──────────────── 是 ──────────────────┐  │
 │ │ append assistant(content, tool_calls)                  │  │
 │ │ for tc in tool_calls:                                  │  │
 │ │    args = json.loads(tc.arguments)                     │  │
 │ │    result = exec_tool(name, args)   ← 6 工具 + 护栏      │  │
 │ │    append tool(result)                                 │  │
 │ │                                                        │  │
 │ │ anti-storm:                                            │  │
 │ │   有 edit/write?  → no_progress = 0                     │  │
 │ │   只有 grep/read? → no_progress++                       │  │
 │ │      若 no_progress >= 14 → status: stuck (退出)        │  │
 │ │ continue (下一轮)                                       │  │
 │ └────────────────────────────────────────────────────────┘  │
 │                                                              │
 │ ┌── 没 tool_calls(只有文本)? ───────── 是 ────────────────┐  │
 │ │ is_leak = 文本含 "to=functions." / 以 analysis 开头     │  │
 │ │   文本非空 且 非泄漏  → status: done (退出) ✅           │  │
 │ │   空 或 泄漏:                                           │  │
 │ │     nudges < max_nudge?                                │  │
 │ │        是 → append nudge 提示, nudges++, continue       │  │
 │ │        否 → status: early_stop (退出)                   │  │
 │ └────────────────────────────────────────────────────────┘  │
 └─────────────────────────────────────────────────────────────┘
 │
 ▼  循环结束未退出 → status: max_turns
```

**完成信号的巧思**:agent 没有专门的 `finish()` 工具。**"不再调用任何工具、只回一段纯文本"** 本身就是完成信号(见 system prompt 第 5 条)。这样模型不需要学一个额外动作,符合"少给笨手添负担"的原则。

---

## 3. 工具:`exec_tool`

### 3.1 六个基础工具(始终可用)

| 工具 | 作用 | 关键设计 |
|---|---|---|
| `list_dir(path)` | 列目录 | — |
| `read_file(path)` | 读全文 | system prompt 强约束:**改前必读**;别重复读同一大文件 |
| `grep(pattern, path)` | 正则搜索 | 优先 `rg`,回退 `grep`;`--max-count 40`,结果截断 60 行 → 引导"先定位再读",省 context |
| `edit_file(path, old, new)` | 精确替换 | `old_string` 必须**唯一且逐字匹配**;0 命中 → 给纠错提示;多命中 → 要求加上下文 |
| `write_file(path, content)` | 新建/全覆盖 | 仅用于新文件或整体重写 |
| `run_bash(command)` | 跑命令 | `cwd=项目根`;60s 超时;输出截断 3000 字符 |

### 3.2 codegraph 智能定位工具(自动启用)

`main()` 启动时检测 `{project}/.codegraph/codegraph.db` 是否存在。有 → 自动追加两个工具到 `TOOLS` 列表 + system prompt 追加使用引导;无 → 透明降级,不影响基础工具。

| 工具 | 作用 | 关键设计 |
|---|---|---|
| `search_symbol(query, kind?)` | 按名字查符号(class/method/function/field 等) | 直接查 SQLite FTS 索引,sub-ms;精确匹配优先排序;比 grep 噪声少一个数量级 |
| `find_references(symbol, direction?)` | 查调用关系(callers/callees/both) | 查 edges 表连 nodes,返回调用者/被调用者的 kind + file:line;理解修改影响面 |

实现:**直接 `sqlite3` 查 `.codegraph/codegraph.db`**,零额外依赖(不走 MCP,不起额外进程)。codegraph 的索引由 codegraph MCP server 的 file watcher 维护,Qwen-Agent 只读。

> **为什么内置而非走 MCP?** codegraph MCP 是 Claude Code 的插件(大脑侧),Qwen-Agent 是独立 Python 脚本(手侧)。走 MCP 需要跑 client + server 进程,复杂度上升;直接查 SQLite 零成本、零依赖,且查询结果格式简洁(不像 MCP 返回富文本),更适合本地中小模型消化。

所有路径都过 `_safe()`:`realpath` 归一后必须在 `--project` 根内,否则抛错。这是**始终开启的沙箱**,与白名单无关。

> 为什么基础工具只有 6 个、且故意"小"?因为工具越多、输出越大,本地中小模型越容易迷失。每个工具的输出都有意截断(grep 60 行 / bash 3000 字符),逼模型"精确定位"而非"大水漫灌"。codegraph 工具是例外——它天然返回结构化精简结果(符号名 + file:line),不会淹没 context。

---

## 4. 三层 harness:把手框住

### Layer 0 · 路径沙箱(`_safe`,始终开)
`realpath` 解析后必须 `== ROOT` 或在 `ROOT/` 下。挡 `../` 逃逸。模型再乱也出不了项目目录。

### Layer 1 · 写白名单(`_denied` + `--allow`)
`ALLOW` 激活时,每次 `edit`/`write` 先过 `_denied()`:相对路径不在白名单(支持 `fnmatch` glob)就回 `REFUSED`。
同时白名单也会**追加进 task 文本**告知模型(双保险:既物理拦,也提前说)。
> 挡的是最高频翻车:模型顺手改无关文件 / 自作主张加整个模块。

### Layer 2 · diff 规模 tripwire(`--max-diff-lines`)
跑完用 `git diff --numstat` 累加增删行数,超阈值**告警**(不阻断)。
> Layer 1 挡"改错文件",Layer 2 挡"在对的文件里改太多"——文件内逻辑越界,白名单挡不住的那种。

---

## 5. ACI:工具反馈 > 模型能力

ACI(Agent–Computer Interface)的信条:**与其指望模型更聪明,不如让工具的反馈更友好。** 四个具体设计:

### 5.1 `edit_file` 未命中自动纠错(`_edit_hint`)
`old_string` 没匹配上时,不是干巴巴回"not found",而是:
1. 取 `old_string` 第一行里最长的 token 当探针;
2. 在文件里找含该探针的位置;
3. 回最接近的 2–3 处**带行号的现有内容**,让模型照着逐字重写。

> 本地模型最常见的失败就是 `old_string` 记错缩进/标点。给它看真实内容,比让它"再试一次"有效得多。

### 5.2 anti-storm(14 轮不动手 = stuck)
连续 14 轮只 `grep`/`read`/`list` 却不 `edit`/`write` → 判 `stuck`。
> 本地模型会"定位绕圈"——反复搜来搜去就是不下手。与其空转烧到 `max_turns`,不如早止损,让上游拆更小的片、或直接给精确 `old→new`。阈值 14 是"够它充分探索、又不至于无限绕"的经验值。

### 5.3 nudge(空轮重试)
某轮既没工具调用、也没最终结论(本地模型的 EOS / 通道转换 quirk)→ 推一句"要么调下一个工具,要么给最终总结",最多 `max_nudge`(默认 5)次。
> 默认 5 是因为超复杂任务(大段重写)空转概率高,2 次不够。

### 5.4 tool-call 泄漏检测(`is_leak`)
有些后端会把不完整的工具调用漏进正文(如 `to=functions.run_bash`)。检测到这种文本就**不当成完成**,走 nudge 重试,避免丢工具调用、假完成。

---

## 6. 验证 + 自纠错循环(Aider 式)

`--verify` 启用时,`status=done` 后进入自纠错循环(`main` 内):

```
 for attempt in 0..verify_retries:
   run(verify_cmd)
   ├─ exit 0  → PASS ✓  break
   └─ exit ≠0 → 
        attempt 用尽? → status: verify_failed  break
        否 → 把错误输出(尾 2500 字符)拼成修复任务
              fix_task = "你的改动没过验证 {cmd},错误:{err},定位并只改必要处"
              (若有 --allow,把白名单也带进修复任务)
              run_agent(fix_task)  ← 再跑一轮 agent 自己修
              fix 没 done? → status: verify_failed  break
              否 → 回到循环顶,重新验证
```

> 这把"上游读 diff 找错"换成"agent 自愈到 PASS,上游只看最终结果"。
> 关键前提:**验证命令必须是机器可判的**(编译退出码 / 测试 / `grep -c`)。

---

## 7. 关键默认值与取舍

| 决策 | 取值 | 为什么 |
|---|---|---|
| 流式 | **non-stream** | 本地中小模型流式更易出通道泄漏 / 半截 tool call;非流式 + 标准 tool format 最稳 |
| 温度 | `0.0` | 编码要确定性,贪婪最稳 |
| `max_nudge` | `5` | 超复杂任务空轮概率高,2 不够(实测) |
| `max_turns` | `24` | 健康任务实测峰值 ~20,24=20+余量;空转主要靠 anti-storm 抓,这只兜底"有进展但量大" |
| `timeout` | `300s` | 给 MoE 大模型单次生成留足 |
| grep 截断 | 60 行 / max-count 40 | 逼精确定位,省 context |
| bash 截断 | 3000 字符 | 同上 |
| 基础工具数 | **6**(+ codegraph 项目自动 +2） | 够用即止,工具越多笨手越迷失;codegraph 工具返回结构化精简结果,不增加迷失风险 |

---

## 8. 数据流小结

```
task(规格)
  └─▶ messages[system + user]
        └─▶ chat() ⇄ 本地后端 (tools=6+codegraph, tool_choice=auto, non-stream)
              └─▶ tool_calls ─▶ exec_tool (沙箱+白名单) ─▶ tool result ─▶ 回填 messages
                    └─▶ (循环, 带 anti-storm / nudge)
                          └─▶ done ─▶ [--verify 自纠错] ─▶ [--max-diff-lines 告警] ─▶ exit code
```

整套就这些。没有向量库、没有 planner、没有多 agent 编排——**复杂度被刻意压在最低,把智能留给上游,把稳健交给护栏。**
