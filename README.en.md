# Qwen-Agent

**English** · [中文](README.md)

> A minimal coding agent that uses a frontier model as the **brain** and a local model as the **hands**.
> Claude writes the spec → the local model does the work → Claude reviews.
> One **~550-line single-file Python script, zero third-party dependencies**.

---

## What is this

Qwen-Agent is a **local coding agent**: it talks to any OpenAI-compatible local inference backend (the author runs Qwen3.6-35B-A3B on Apple Silicon via [oMLX](https://github.com/jundot/omlx)) and completes coding tasks end-to-end with 6 base tools (+ 2 codegraph smart-locating tools, auto-enabled when the project has an index) — it explores, locates, edits, and verifies on its own.

Its positioning is unusual: **it doesn't try to be smart.** It assumes the local model behind it is "nimble-handed but mediocre at judgment," so it leaves the **judgment** to whatever sits upstream (you, or your Claude), and focuses only on **executing mechanically, safely, and verifiably**.

We call this collaboration **cc → Qwen-Agent**: `cc` is the brain (writes specs, makes judgment calls, reviews), `Qwen-Agent` is the hands (changes the code locally).

**In one line**: it's not "yet another autonomous agent" — it's "a pair of hands with guardrails bolted on." The clever part is outsourced to the brain; the agent just gets the work done reliably.

---

## Why it's built this way

### Primary motive — save cc tokens (the essence)

A frontier model's (Claude's) tokens are expensive. Having it **edit code directly** = reading a pile of files (input tokens) + emitting whole blocks of code (output tokens) + burning more on every retry.
**Flip it: Claude emits only "a one-line intent," and offloads the token-burning grunt work to a free local model.**

| Step | Claude does it itself | cc → Qwen-Agent |
|---|---|---|
| Read / locate code | Claude reads — burns input tokens | local model reads — no cc tokens |
| Write code | Claude emits whole blocks — burns output tokens | local model writes — no cc tokens |
| Trial & error | every attempt burns cc tokens | tried locally — no cc tokens |
| **Claude actually only** | does everything itself | one-line intent + reviews the diff |

> "Local / free" is the **means** (free is what lets it read and retry freely without adding cc cost); the **goal is always to save cc tokens and keep Claude on thinking only**.
> ⚠️ This is **not** about "keeping data on-device" — the brain is cloud Claude, so your code context already passes through it. Privacy is not a selling point here.

### The other wing — codegraph saves tokens on the "understanding" side

Qwen-Agent saves tokens on **execution**. But before Claude writes a spec or reviews, it must **understand the code** (locate symbols, trace calls) — and doing that with grep + reading a pile of files also burns tokens. **codegraph** (a code-index MCP for Claude Code) saves the understanding side too: a prebuilt index with sub-millisecond queries that returns precise `file:line` + source, so you locate code without reading whole files.

**As of v2, codegraph is promoted from "brain-side MCP recommendation" to built-in support inside Qwen-Agent.** When a project has a `.codegraph/codegraph.db` index, Qwen-Agent auto-detects it and enables two additional tools:

- **`search_symbol`** — find symbols (class/method/function/field) by name. Much more precise than grep.
- **`find_references`** — find callers/callees of a symbol, understand dependencies.

Both query SQLite directly — zero extra dependencies, sub-ms queries. Projects without a codegraph index transparently fall back to grep with no configuration needed.

| Wing | What it saves | codegraph role |
|---|---|---|
| **Brain side (Claude)** | "read / understand" tokens — index queries instead of reading whole files | Claude Code MCP (unchanged) |
| **Hands side (Qwen-Agent)** | "write / execute" tokens — a local model instead of hand-typing | **v2: built-in `search_symbol` + `find_references`** |

Claude is left with only the high-leverage judgment in the middle. Two quality bonuses: **more accurate spec anchors** (based on real `file:line`, not grep-from-memory) and **no lines silently eaten by shell-output compression** (avoids "a key line dropped → wrong root cause").

### Second pillar — quality from the harness, not the model

A mid-size local model isn't smart enough; left unsupervised it makes a mess. So:

| Mistakes it makes | How they're handled |
|---|---|
| **Weak judgment**, makes a mess if left alone | **judgment is not outsourced** — the brain finishes the judgment and bakes it into the spec; the model only does unambiguous mechanical execution |
| **Edits wrong files / spins / changes the wrong thing** | three layers of **guardrails + verification + ACI self-correction**; quality from the harness, not the model |

> **Core bet: quality comes from the harness (spec + guardrails + machine verification), not from the model being smart.** The model can be swapped or weak — the guardrails cannot be missing.

---

## The cc → Qwen-Agent loop

```
   ┌──────────────┐   ① spec (what to do + constraints + anchors)  ┌────────────────┐
   │              │ ─────────────────────────────────────────────▶ │                │
   │  Claude (cc) │                                                 │   Qwen-Agent   │
   │   = brain    │   ④ wrong? rewrite the spec & re-dispatch       │   = hands      │
   │              │ ◀───────────────────────────────────────────── │   (local)      │
   │ judge / spec │                                                 │ explore→edit→  │
   │ / review     │ ◀──── ③ git diff / compile / test ───────────── │ self-verify    │
   └──────────────┘          (never trust self-report)              └────────────────┘
```

1. **Brain writes the spec** — "what to do + key constraints + locating anchors." Judgment-type disambiguation (which of several to change, overloaded terms) is settled here, not left to the local model.
2. **Hands execute** — the local model autonomously explores → edits → verifies, all under guardrails. One task per dispatch.
3. **Brain reviews** — **independently** via git diff / compile / test. **Never trusts the agent's self-report.**
4. **Failure loop** — if it's wrong, **rewrite the spec and re-dispatch**, rather than retrying in place. Failures are almost always a weak spec.

---

## Features

- 💰 **Saves cc tokens (the core)** — Claude spends tokens only on thinking (spec + diff review); reading/writing/retrying is offloaded to a free local model, off cc's bill.
- 🧠 **Brain/hands split** — judgment stays upstream; the local model only does mechanical execution.
- 📦 **Zero-dependency single file** — ~550 lines of pure Python stdlib (`urllib`/`argparse`/`json`/`subprocess`). No LangChain, no pip install — `chmod +x` and go.
- 🔌 **Any OpenAI-compatible backend** — oMLX / LM Studio / `mlx_lm.server` / llama.cpp server / vLLM; swap with `--model`.
- 🔒 **Write allowlist** (`--allow`) — out-of-scope `edit`/`write` is physically refused.
- 📏 **Diff-size tripwire** (`--max-diff-lines`) — catches "over-editing inside an allowed file."
- ✅ **Auto-verify + self-correction** (`--verify`) — on failure, feed the error back to the model to fix itself, Aider-style, up to N times.
- 🩹 **ACI self-correction** — failed `edit` returns the closest existing content for a byte-accurate retry; nudge on empty turns; `stuck` after 14 turns without action; tool-call leak detection.
- 🔍 **codegraph smart locating** (auto-enabled) — when the project has a `.codegraph/` index, `search_symbol` + `find_references` are injected automatically; more precise than grep. Falls back transparently without an index.
- 🚪 **Scriptable** — preflight health check + clear exit codes (`0`/`1`/`3`).
- 🛡️ **Path sandbox** — all file ops are confined to the `--project` root; `../` escapes are blocked.

---

## Install

**Prerequisites:**

1. Python 3.8+
2. An **OpenAI-compatible local inference backend** with a **function-calling** model loaded.
   The author runs `Qwen3.6-35B-A3B` on Apple Silicon via [oMLX](https://github.com/jundot/omlx) (MoE, 3B active — fast and memory-light). LM Studio / `mlx_lm.server` / llama.cpp server / vLLM also work, as long as they expose `/v1/chat/completions` with `tools` support.

**Install the agent itself** (no pip package — it's a single script):

```bash
curl -o /usr/local/bin/Qwen-Agent https://raw.githubusercontent.com/Song-ic/Qwen-Agent/main/Qwen-Agent
chmod +x /usr/local/bin/Qwen-Agent
```

**Confirm the backend is up:**

```bash
curl -s http://127.0.0.1:18888/v1/models | jq '.data[].id'
```

### Companion tool: qspec-attest (optional)

`qspec-attest` is a SHA-256 lock for spec files, ensuring specs aren't silently tampered with after dispatch:

```bash
curl -o /usr/local/bin/qspec-attest https://raw.githubusercontent.com/Song-ic/Qwen-Agent/main/qspec-attest
chmod +x /usr/local/bin/qspec-attest
```

Usage:
```bash
qspec-attest lock spec.md     # Lock the spec's SHA
qspec-attest verify spec.md   # Verify it hasn't been tampered with
qspec-attest clear spec.md    # Unlock (before editing the spec)
```

---

## Quick Start

```bash
# Simplest: a one-line intent
Qwen-Agent --project ./my-svc --task "Change PORT in config.py from 8000 to 9000"

# Read the spec from a file (recommended for complex tasks)
Qwen-Agent --project . --task-file spec.md --verbose

# Pipe the spec via stdin
echo "Add email-format validation at the top of UserService.register; throw IllegalArgumentException if it lacks '@'" \
  | Qwen-Agent --project ./backend

# Full guardrails (recommended for judgment-heavy / high-risk tasks)
Qwen-Agent --project . --task-file spec.md \
  --allow "src/user/UserService.java,src/user/UserController.java" \
  --max-diff-lines 30 \
  --verify "mvn -q compile"
```

Pick a model / backend:

```bash
Qwen-Agent --project . --task "..." \
  --base http://127.0.0.1:18888/v1 \
  --model Qwen3.6-35B-A3B-oQ6-fp16-mtp
```

---

## CLI arguments

| Flag | Default | Description |
|---|---|---|
| `--project` | `.` | Project root the agent operates in (all file ops are locked inside it) |
| `--task` | — | Task / spec text |
| `--task-file` | — | Read the spec from a file |
| *(stdin)* | — | Spec can also be piped in |
| `--base` | `http://127.0.0.1:18888/v1` | OpenAI-compatible backend URL |
| `--model` | `Qwen3.6-35B-A3B-oQ6-fp16-mtp` | Model ID (match what your backend loaded) |
| `--allow` | unrestricted | **Write allowlist**: comma-separated relative paths/globs; only these may be `edit`/`write`, the rest are refused |
| `--max-diff-lines` | off | After the run, check total `git diff` lines and **warn** above the threshold (logic-overreach tripwire) |
| `--verify` | none | Verification command run after `status=done` (compile/test/grep assertion) |
| `--verify-retries` | `2` | Max self-correction retries when `--verify` fails (`0` = verify only, no fix) |
| `--temp` | `0.0` | Sampling temperature (greedy by default for stable coding) |
| `--reasoning-effort` | none | `low`/`medium`/`high`, passed through to backends that support it |
| `--max-nudge` | `5` | Max nudge-retries on empty turns (no tool call, no final answer) |
| `--max-turns` | `24` | Max turns per task (backstop; spinning is mainly caught by anti-storm) |
| `--timeout` | `300` | Per-request timeout (seconds) |
| `--max-tokens` | `8192` | Per-generation cap |
| `--verbose` | off | Print per-turn reasoning summary + every tool call and result |

---

## Guardrails (the core)

The guardrails are the soul of this project — the local model isn't smart enough, so these gates fence it in.

### 1. Path sandbox (always on)
Every path is normalized via `realpath` and **must land inside the `--project` root**; `../` escapes error out. No matter how the model misbehaves, it can't leave the project directory.

### 2. Write allowlist `--allow` (physically blocks overreach)
```bash
--allow "src/user/UserService.java,src/user/*.java"
```
Any out-of-scope `edit_file`/`write_file` is **refused at the tool layer** with a `REFUSED` hint. This blocks the most common failure: the model "helpfully" editing unrelated files or inventing a whole new module. **Strongly recommended for judgment-heavy / multi-file tasks.**

### 3. Diff-size tripwire `--max-diff-lines`
`--allow` blocks "edited the wrong file" but not "edited too much in an allowed file." This gate counts total changed lines via `git diff --numstat` and **warns** (does not block) above the threshold. It catches "over-editing / logic overreach within a file."

### 4. Auto-verify + self-correction `--verify` (Aider-style)
```bash
--verify "mvn -q compile" --verify-retries 2
```
After a task reaches `done`, the verification command runs automatically:
- exit `0` → `PASS ✓`, done.
- non-`0` → **feed the trimmed error back to the model** to locate and fix → re-verify, up to `--verify-retries` times.
- still failing → overall `verify_failed` (process exit `1`).

This trades "upstream reads the diff to find errors" for "the agent self-heals until verification passes; upstream only checks the final PASS/FAIL." **Write completion criteria as machine-verifiable** (compile exit code / tests / `grep -c xxx`), not "looks right."

### 5. ACI: tool feedback > model capability
A few designs that let "dumb hands" still work (ACI = Agent–Computer Interface):
- **Auto-hint on failed edit** — when `edit_file`'s `old_string` doesn't match, the tool surfaces the closest existing content (with line numbers) so the model rewrites it byte-accurately instead of guessing.
- **anti-storm** — 14 consecutive turns of only exploring (`grep`/`read`/`list`) without any `edit`/`write` → declared `stuck` to cut losses early.
- **nudge-retry** — when a turn produces neither a tool call nor a final answer (a common local-model EOS/channel quirk) → push it to continue, up to `--max-nudge` times.
- **tool-call leak detection** — recognizes incomplete tool calls leaked into prose (e.g. `to=functions.xxx`) and avoids mistaking them for "done."

---

## How to write specs (brain-side best practices)

Qwen-Agent's success is **80% spec quality**. A few rules:

- **Default to high-level intent, don't fill in the blanks.** Give "intent + locating anchors (method/class/file) + key constraints (exact values/exceptions/rules)" and let the local model `grep` for the real location. It finding the **real spot** is often more reliable than upstream writing `old_string` from memory.
  - ✅ `Add email validation at the top of UserService.register; throw IllegalArgumentException if it lacks '@'`
- **Judgment-type tasks: do the judgment upstream and write it into the spec.** Disambiguation like "which of these to change" or "does statusTabs count as a collection state" must **not** be left to the local model — it will get it wrong. Bake the enum set, alias warnings, and concept mapping into the spec as facts so it only pattern-matches.
- **Always give exact values** (amounts / assertions / API signatures) — the model can't guess them.
- **Split big changes** — for >2 files or large rewrites, split into several small specs run serially (local models tend to early-stop on oversized single edits).
- **Write completion criteria as machine-verifiable** and feed them to `--verify`.

---

## Status & exit codes

Each turn prints: `turn / finish_reason / reasoning chars / tools called / content chars`.

Final status:

| Status | Meaning |
|---|---|
| `done` | Completed normally (gave a plain-text summary and stopped calling tools) |
| `early_stop` | Still idle after nudges ran out |
| `stuck` | anti-storm triggered (14 turns exploring without acting) |
| `max_turns` | Hit the turn cap (usually the task is too big — split it) |
| `verify_failed` | `--verify` still failing after N self-correction rounds |
| `http_error` / `exception` | Backend error / runtime exception |

**Process exit codes** (for scripting):

| Exit code | Meaning |
|---|---|
| `0` | `done` (and `--verify` passed, if enabled) |
| `1` | Any other unfinished status (incl. `verify_failed`) |
| `3` | Backend unreachable (preflight failed) |

---

## Design philosophy: harness > model

The most counterintuitive — and most valuable — idea here: **don't bet on the model, bet on the harness.**

- **Swap the model, keep the framework.** Swapping in a stronger/weaker local model needs zero code change — the smart part (judgment) is already upstream; this layer only fences in the "hands."
- **Mid-size local models routinely fail at judgment tasks** (over-applying rules, fooled by variable names, missing cases). No model swap fixes this, so **don't let it judge**.
- **Quality comes from three things**: ① the upstream spec (judgment done) ② guardrails (physically block overreach) ③ machine verification (self-heal to PASS). The model is just the hands in between.
- **non-stream + standard tool format** — not fancy, just stable; streaming on mid-size local models is more prone to channel leaks / half tool calls.

> In one line: **cage the uncertain intelligence inside a deterministic box.**

---

## Limitations

- It **won't judge for you** — a vague spec means a wrong result. That's a design trade-off, not a bug.
- Requires a backend model with **function calling**; pure completion models won't work.
- Large single changes tend to early-stop; upstream must **split into smaller pieces**.
- `run_bash` has a 60s timeout and 3000-char output cap; `grep` results are capped at 60 lines — deliberately limited to save context.
- codegraph tools require the project to have a `.codegraph/codegraph.db` index (maintained by the codegraph MCP server's file watcher); without an index, the agent transparently falls back to grep.
- No persisted multi-turn memory: one task per dispatch; state isn't kept across processes (recoverability is left to the upstream orchestrator).

---

## License

MIT — see [LICENSE](LICENSE).

---

<p align="center">
  <sub>Claude is the brain, Qwen-Agent is the hands. Keep the smarts upstream, leave the safety to the guardrails.</sub>
</p>
