# Qwen-Agent

[English](README.en.md) · **中文**

> 把强模型当**大脑**、本地模型当**手**的极简编码 agent。
> Claude 出规格 → 本地模型落地 → Claude 验收。
> 一个 **~420 行单文件 Python,零三方依赖**。

<p align="center">
  <em>cc → Qwen-Agent:大脑出意图,手负责机械、安全、可验证地把代码改掉。</em>
</p>

---

## 目录

- [这是什么](#这是什么)
- [为什么这么设计](#为什么这么设计核心赌注)
- [cc → Qwen-Agent 协作闭环](#cc--qwen-agent-协作闭环)
- [特性](#特性)
- [安装](#安装)
- [快速上手](#快速上手)
- [CLI 参数](#cli-参数)
- [护栏详解(核心)](#护栏详解核心)
- [怎么写规格(大脑侧最佳实践)](#怎么写规格大脑侧最佳实践)
- [运行状态与退出码](#运行状态与退出码)
- [设计哲学](#设计哲学harness--模型)
- [局限](#局限)
- [License](#license)

---

## 这是什么

Qwen-Agent 是一个**本地运行的编码 agent**:它接一个 OpenAI 兼容的本地推理后端(作者用 [oMLX](https://github.com/jundot/omlx) 在 Apple Silicon 上跑 Qwen3.6-35B-A3B),用 6 个文件工具端到端完成编码任务——自己探索代码、定位、修改、验证。

它的定位很特别:**它不打算自己很聪明。** 它假设背后是一个"手脚麻利但判断力一般"的本地中小模型,于是把**判断**留给上游(你,或你的 Claude),自己只负责**机械、安全、可验证地落地**。

这套协作我们叫 **cc → Qwen-Agent**:`cc` 是大脑(出规格、做判断、验收),`Qwen-Agent` 是手(在本地把代码改掉)。

**一句话**:它不是"又一个 autonomous agent",而是"一双装了护栏的手"——聪明的部分外包给大脑,它专注把活干稳。

---

## 为什么这么设计

### 第一动机:省 cc token(本质)

强模型(Claude)的 token 贵。让它**亲自改代码** = 读一堆文件(输入 token)+ 输出整段代码(输出 token)+ 每次试错再烧一遍。
**换个思路:Claude 只输出"一行高层意图",把烧 token 的体力活全甩给免费的本地模型。**

| 环节 | Claude 亲自改 | cc → Qwen-Agent |
|---|---|---|
| 读代码 / 定位 | Claude 读,烧输入 token | 本地模型读,不花 cc token |
| 写代码 | Claude 输出整段,烧输出 token | 本地模型写,不花 cc token |
| 反复试错 | 每次试都烧 cc token | 本地试,不花 cc token |
| **Claude 实际只做** | 全程亲力亲为 | **一行意图 + 看 diff 验收** |

> "本地 / 免费" 是**手段**(免费,才敢让它随便读、随便试而不增加 cc 成本),目的始终是**省 cc token、让 Claude 只用在思考上**。
> ⚠️ 这**不是**"数据不出机"——大脑是云端 Claude,代码上下文本来就过了它,隐私不是卖点。

#### 另一翼:codegraph 省"理解端"的 token

Qwen-Agent 省的是**执行**的 token。但 Claude 出规格 / 验收前要**读懂代码**(定位符号、查调用关系),用 grep + read 一堆文件本身也烧 token。**codegraph**(Claude Code 的代码索引 MCP)把理解端也省了:预建索引 sub-ms 查询,返回精确 `file:line` + 源码,不用读整文件就定位。

| 翼 | 省什么 |
|---|---|
| **codegraph(输入侧)** | 省 Claude"读代码 / 理解"的 token —— 索引查询代替 read 整文件 |
| **Qwen-Agent(输出侧)** | 省 Claude"写代码 / 执行"的 token —— 本地模型代替亲敲 |

中间 Claude 只剩高杠杆判断。外加两个质量收益:**规格锚点更准**(基于真实 `file:line` 而非凭记忆 grep)、**不被 shell 压缩吃行**(避免吃掉关键一行 → 误判根因)。

> codegraph 是**大脑侧**工具(Claude Code MCP),方法论的推荐搭配,不是 Qwen-Agent 脚本的一部分。

### 第二支柱:质量靠 harness,不靠模型

但本地中小模型不够聪明,直接放养会乱改。所以:

| 它会犯的错 | 怎么治 |
|---|---|
| **判断力不够**,放养会乱改 | **判断不外包**——大脑把判断做完写进规格,它只做无歧义的机械执行 |
| **越界改文件 / 空转 / 改错** | 三层**护栏 + 验证 + ACI 自纠**兜底,质量靠 harness 不靠模型 |

> **核心赌注:质量靠 harness(规格 + 护栏 + 机器验证),不靠模型聪明。** 模型可以换、可以不够强,但护栏不能少。实测中小本地模型在"判断类任务"上普遍会翻(过度套规则、被变量名误导),所以干脆不让它判断,只让它在大脑画好的框里干活。

---

## cc → Qwen-Agent 协作闭环

```
   ┌──────────────┐    ① 规格(做什么 + 约束 + 定位锚点)   ┌────────────────┐
   │              │ ──────────────────────────────────▶ │                │
   │  Claude(cc)  │                                      │   Qwen-Agent   │
   │   = 大脑      │    ④ 不对? 重写规格再 dispatch         │   = 手 (本地)   │
   │              │ ◀────────────────────────────────── │                │
   │ 判断 / 出规格 │                                      │ 探索→改→自验    │
   │ / 独立验收    │ ◀──── ③ git diff / 编译 / 测试 ────── │ (护栏+自纠错)   │
   └──────────────┘         (不信 self-report)            └────────────────┘
                                                                  │ ②
                                                                  ▼
                                                        ┌────────────────┐
                                                        │ oMLX / 任意      │
                                                        │ OpenAI 兼容后端  │
                                                        │ (本地 LLM)       │
                                                        └────────────────┘
```

1. **大脑出规格**:Claude 把"做什么 + 关键约束 + 定位锚点(类名/方法/文件)"写成一份规格。判断类的消歧(多义词、挑哪个改)在这一步由大脑做完。
2. **手执行**:Qwen-Agent 把规格喂给本地模型,自主 `grep`→`read`→`edit`→`run`,带护栏。
3. **大脑验收**:Claude **独立**用 `git diff` / 编译 / 测试核验,**不信任 agent 的自我报告**。
4. **失败回路**:不对就改规格重 dispatch,而不是在原地反复试。

---

## 特性

- 💰 **省 cc token(核心)**:Claude 只花 token 思考(出规格 + 验收 diff),读码/写码/试错全甩给免费本地模型,不计入 cc 的账。
- 🧠 **大脑/手分离**:判断外包给上游,本地模型只做机械落地。
- 📦 **零依赖单文件**:~420 行纯 Python 标准库(`urllib`/`argparse`/`json`/`subprocess`),没有 LangChain、没有 pip 安装,`chmod +x` 就能跑。
- 🔌 **接任意 OpenAI 兼容后端**:oMLX / LM Studio / `mlx_lm.server` / llama.cpp server / vLLM 均可,`--model` 任意切。
- 🔒 **写白名单越界拦截**(`--allow`):范围外的 `edit`/`write` 物理拒绝。
- 📏 **diff 规模 tripwire**(`--max-diff-lines`):抓"在允许文件内过度改"的逻辑越界。
- ✅ **自动验证 + 自纠错循环**(`--verify`):验证失败把错误喂回模型自己修,Aider 式,最多 N 次。
- 🩹 **ACI 自纠**:`edit` 未命中自动回最接近的现有内容帮逐字重试;空轮 nudge;14 轮不动手判 stuck;tool-call 泄漏检测。
- 🚪 **可脚本化**:`preflight` 探活 + 明确退出码(`0`/`1`/`3`),好嵌进流水线。
- 🛡️ **路径沙箱**:所有文件操作限定在 `--project` 根内,防 `../` 逃逸。

---

## 安装

**前置**:

1. Python 3.8+
2. 一个 **OpenAI 兼容的本地推理后端**,已加载一个支持 **function calling** 的模型。
   作者用 [oMLX](https://github.com/jundot/omlx) 在 Apple Silicon 跑 `Qwen3.6-35B-A3B`(MoE,3B 激活,快且省内存)。
   也可用 LM Studio / `mlx_lm.server` / llama.cpp server / vLLM 等,只要暴露 `/v1/chat/completions` 且支持 `tools`。

**装它本身**(没有 pip 包,就是一个脚本):

```bash
curl -o /usr/local/bin/Qwen-Agent https://raw.githubusercontent.com/Song-ic/Qwen-Agent/main/Qwen-Agent
chmod +x /usr/local/bin/Qwen-Agent
```

**确认后端在线**:

```bash
curl -s http://127.0.0.1:18888/v1/models | jq '.data[].id'
```

---

## 快速上手

```bash
# 最简:一行意图
Qwen-Agent --project ./my-svc --task "把 config.py 里的 PORT 从 8000 改成 9000"

# 从文件读规格(复杂任务推荐)
Qwen-Agent --project . --task-file spec.md --verbose

# 管道喂规格
echo "给 UserService.register 开头加邮箱格式校验,不含 @ 就抛 IllegalArgumentException" \
  | Qwen-Agent --project ./backend

# 带全套护栏(判断类 / 高风险任务推荐)
Qwen-Agent --project . --task-file spec.md \
  --allow "src/user/UserService.java,src/user/UserController.java" \
  --max-diff-lines 30 \
  --verify "mvn -q compile"
```

指定模型 / 后端:

```bash
Qwen-Agent --project . --task "..." \
  --base http://127.0.0.1:18888/v1 \
  --model Qwen3.6-35B-A3B-oQ6-fp16-mtp
```

---

## CLI 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--project` | `.` | agent 操作的项目根目录(所有文件操作被锁在此目录内) |
| `--task` | — | 任务/规格文本 |
| `--task-file` | — | 从文件读规格 |
| *(stdin)* | — | 也可管道喂规格 |
| `--base` | `http://127.0.0.1:18888/v1` | OpenAI 兼容后端地址 |
| `--model` | `Qwen3.6-35B-A3B-…-oQ4-MTP` | 模型 ID(按你后端加载的填) |
| `--allow` | 不限制 | **写白名单**:逗号分隔的相对路径/glob,只允许 `edit`/`write` 这些,其余拒绝 |
| `--max-diff-lines` | 不检查 | 跑完检查 `git diff` 总行数,超阈值**告警**(文件内逻辑越界 tripwire) |
| `--verify` | 无 | `status=done` 后自动跑的验证命令(compile/test/grep 断言) |
| `--verify-retries` | `2` | `--verify` 失败时,把错误喂回模型自纠错的最大次数(`0`=只验不修) |
| `--temp` | `0.0` | 采样温度(编码任务默认贪婪,稳定) |
| `--reasoning-effort` | 无 | `low`/`medium`/`high`,透传给支持的后端 |
| `--max-nudge` | `5` | 空轮(没调工具也没给结论)时 nudge 重试的上限 |
| `--max-turns` | `24` | 单任务最大轮数(兜底;空转主要靠 anti-storm 抓) |
| `--timeout` | `300` | 单次模型请求超时(秒) |
| `--max-tokens` | `8192` | 单次生成上限 |
| `--verbose` | off | 打印每轮 reasoning 摘要 + 每个工具调用与结果 |

---

## 护栏详解(核心)

护栏是这个项目的灵魂——本地模型不够聪明,靠这几道闸把它框住。

### 1. 路径沙箱(始终开启)

所有 `read/edit/write/grep/list` 的路径都经 `realpath` 归一化,**必须落在 `--project` 根内**,`../` 逃逸直接报错。本地模型再怎么乱来也出不了项目目录。

### 2. 写白名单 `--allow`(物理拦越界)

```bash
--allow "src/user/UserService.java,src/user/*.java"
```

范围外的任何 `edit_file`/`write_file` 被工具层**直接拒绝**并回一条 `REFUSED` 提示。这挡的是最常见的翻车:模型"顺手"把无关文件也改了、或自作主张加了一整个模块。**判断类 / 多文件任务强烈建议带上。**

### 3. diff 规模 tripwire `--max-diff-lines`

`--allow` 只能挡"改了不该改的文件",挡不住"在允许的文件里改太多"。这道闸跑完用 `git diff --numstat` 数总改动行数,超阈值就**告警**(不阻断),提示上游 review。专治"过度改 / 文件内逻辑越界"。

### 4. 自动验证 + 自纠错循环 `--verify`(Aider 式)

```bash
--verify "mvn -q compile" --verify-retries 2
```

任务跑到 `done` 后自动执行验证命令:

- 退出码 `0` → `PASS ✓`,收工。
- 非 `0` → 把**错误输出截取喂回模型**,让它定位修复 → 重新验证,最多 `--verify-retries` 次。
- 仍失败 → 整体判 `verify_failed`(进程退出码 `1`)。

把"上游读 diff 找错"换成"agent 自愈到验证通过,上游只看最终 PASS/FAIL"。**完成判据请写成机器可验**(编译退出码 / 测试 / `grep -c xxx`),别写"看起来对"。

### 5. ACI:工具反馈 > 模型能力

几个让"笨手"也能干稳的小设计(ACI = Agent–Computer Interface):

- **edit 未命中自动给提示**:`edit_file` 的 `old_string` 没匹配上时,工具会扫出文件里最接近的几处内容(带行号)回给模型,让它照着逐字重写,而不是瞎猜。
- **anti-storm**:连续 **14 轮**只探索(`grep`/`read`/`list`)却不做任何 `edit`/`write` → 判定 `stuck` 早止损,避免空转烧到 `max_turns`。
- **nudge 重试**:模型某轮既没调工具、也没给最终结论(本地模型常见的 EOS/通道 quirk)→ 推一把让它继续,最多 `--max-nudge` 次。
- **tool-call 泄漏检测**:识别 `to=functions.xxx` 这类被漏进正文的不完整工具调用,避免误判成"任务完成"。

---

## 怎么写规格(大脑侧最佳实践)

Qwen-Agent 的成败 **80% 取决于规格质量**。几条经验:

- **默认给高层意图,别填空**。给"意图 + 定位锚点(方法/类/文件名)+ 关键约束(精确值/异常/规则)",让本地模型自己 `grep` 定位。它找**真实位置**往往比上游凭记忆写 `old_string` 更不容易错。
  - ✅ `给 UserService.register 开头加邮箱校验,不含 @ 就抛 IllegalArgumentException`
- **判断类任务:大脑先把判断做完,写进规格**。"这几个里改哪个""statusTabs 算不算采集状态"这类消歧,**不要留给本地模型**——它会翻。把枚举集、别名警告、概念对应写成事实喂给它,让它只做无歧义的模式匹配。
- **精确值必给**(金额 / 断言 / API 签名)——模型猜不准。
- **大改拆小片**:一次改 >2 文件或大段重写,拆成多个小规格串行跑(本地模型对超大单次改动容易早停)。
- **完成判据写成机器可验**,喂给 `--verify`。

---

## 运行状态与退出码

每轮会打印:`turn / finish_reason / reasoning 字数 / 调用的工具 / content 字数`。

最终状态:

| 状态 | 含义 |
|---|---|
| `done` | 正常完成(给出了纯文本总结,且不再调工具) |
| `early_stop` | nudge 用尽仍空转 |
| `stuck` | anti-storm 触发(14 轮只探索不动手) |
| `max_turns` | 撞轮数上限(通常是任务太大,该拆) |
| `verify_failed` | `--verify` 自纠错 N 次后仍未通过 |
| `http_error` / `exception` | 后端报错 / 运行异常 |

**进程退出码**(便于脚本编排):

| 退出码 | 含义 |
|---|---|
| `0` | `done`(且 `--verify` 通过,若启用) |
| `1` | 其他任何未完成状态(含 `verify_failed`) |
| `3` | 后端不可达(preflight 失败) |

---

## 设计哲学:harness > 模型

这个项目最反直觉、也最值钱的一条:**别赌模型,赌 harness。**

- **模型可换,框架不变**。换更强/更弱的本地模型,代码一行不改——因为聪明的部分(判断)本来就在上游,agent 这层只负责框住"手"。
- **本地中小模型在判断类任务上普遍会翻**(过度套规则、被变量名误导、漏判),这不是换个模型能解决的,所以干脆**不让它判断**。
- **质量来自三件事**:① 上游规格(把判断做完)② 护栏(物理拦越界)③ 机器验证(自纠错到 PASS)。模型只是中间那双手。
- **non-stream + 标准 tool format**:不追求花哨,追求稳——流式在本地中小模型上更容易出通道泄漏/半截 tool call,非流式 + 标准格式最省心。

> 一句话:**把不确定的智能,关进确定的笼子里。**

---

## 局限

- 它**不替你做判断**——规格糊,它就翻。这是设计取舍,不是 bug。
- 依赖后端模型支持 **function calling**;纯 completion 模型不适用。
- 单次大改易早停,需要上游**拆小片**。
- `run_bash` 有 60s 超时、输出截断 3000 字符;`grep` 结果截断 60 行——为省 context 故意限的。
- 没有多轮记忆持久化:一次 dispatch 一个任务,状态不跨进程保留(可恢复性交给上游编排)。

---

## License

MIT — 见 [LICENSE](LICENSE)。

---

<p align="center">
  <sub>Claude 是大脑,Qwen-Agent 是手。聪明留给大脑,稳健交给护栏。</sub>
</p>
