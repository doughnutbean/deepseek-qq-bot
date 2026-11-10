# Git 提交规范

> 管的是**怎么提交**。改代码之前先读 AGENTS.md，改完要提交时读这一份。
>
> 一句话：**一次提交只装「我这次改的东西」**。别人的改动、别的会话的改动，一个都不许捎带。

---

## 0. 为什么这条最硬

这个仓库是**多人 / 多会话同时改**的：机器人常驻在跑，另一个窗口可能正在改 `run.py`、`config.py`。
`git add -A` 的语义是「把工作区所有改动都装进去」——包括别人改到一半的、还没跑测试的、甚至写坏的代码。

所以本仓库的提交单位是**文件路径**，不是「工作区」。

---

## 1. 铁律

| 编号 | 规则 | 反例 |
|---|---|---|
| **G1** | 只用 `git add <明确路径>`；禁止 `git add -A`、`git add .`、`git commit -a` | 图省事，把别人的 8 个文件一起提交 |
| **G1a** | 唯一例外：**主人明确要求「全量提交」**。此时先确认 `.gitignore` 已覆盖 `.env`、`data/`、`models/`、`stickers/`、`backups/`，提交说明里写明是全量提交 | 主人没说要全量，自己图省事用 `-A` |
| **G2** | 暂存列表必须**恰好等于**自己这次改的文件 | 混进一个 `run.py` 都不行 |
| **G3** | 同一文件里既有自己的改动又有别人的改动 → `git add -p` 只挑自己的 hunk | 整文件 add，把别人半截逻辑提上去 |
| **G4** | 不 amend、不 reset 别人的提交，不动别人的暂存区 | 用 `git reset --hard` 清场 |
| **G5** | 提交后，别人的改动必须**仍在工作区** | `git status` 里别人的改动消失了 = 你删了别人的活 |
| **G6** | 不擅自 push；要 push 先确认 remote 和分支 | 把半成品推到别人也会用的远端 |
| **G7** | **收工即提交**：会话 / agent 结束前，必须把自己这次的改动提交掉，`git status` 里不能剩自己的文件 | 干完活直接退出，改动全留在工作区（等于没交付） |

---

## 2. 标准流程

```powershell
git status                              # 1. 现状：哪些是自己的，哪些是别人的
git diff -- qqbot/xxx.py                # 2. 只看自己改的文件
git add qqbot/xxx.py 文档.md            # 3. 逐路径暂存（不用 -A）
git diff --cached --name-only           # 4. 核对：列表 == 自己改的文件
git diff --cached                       # 5. 再看一眼具体行
git commit -m "docs: 说明"              # 6. 提交
git status                              # 7. 别人的改动应仍在 Changes not staged
```

中文文件名直接写；PowerShell 报找不到时加引号：`git add "docs/使用说明.md"`。

---

## 3. 提交信息

```
类型: 一句话（说清「为什么」）
```

| 类型 | 用于 |
|---|---|
| `feat` | 新功能 |
| `fix` | 修 bug |
| `docs` | 文档 |
| `test` | 测试 |
| `refactor` | 重构，不改行为 |
| `chore` | 杂务：依赖、配置、脚本 |
| `perf` | 性能 |
| `style` | 格式，不改逻辑 |

首行 50 字以内；需要展开时再加 `-m "正文"`。

---

## 4. 提交前自检（四条都过再提交）

- [ ] `git diff --cached --name-only` 里**没有**别人的文件
- [ ] 没有 `.env`、`*.db`、`*.log`、`stickers/`、`models/`（`.gitignore` 已挡；**新增**状态文件先补规则）
- [ ] 提交信息说清了「为什么」
- [ ] 提交后 `git status` 仍能看到别人的改动

---

## 5. 单个文件被混着改

```powershell
git add -p qqbot/app.py
```

逐 hunk 回答：`y` 收下、`n` 跳过、`s` 拆得更细、`q` 退出。
只收自己的 hunk，别人的一律 `n`。

---

## 6. 手滑了

| 情况 | 命令 |
|---|---|
| 把别人的文件 add 进来了 | `git restore --staged <文件>` |
| 整个暂存区清空（保留改动） | `git reset` |
| 刚提交但混入了别人的改动（未 push） | `git reset --soft HEAD~1`，再按路径重新 add |
| 提交信息写错（未 push） | `git commit --amend` |

> **禁止** `git checkout -- .` 和 `git reset --hard`：它们会连别人未提交的改动一起抹掉，且不可恢复。

---

## 7. 别人改了你的文件怎么办

先看清是不是自己的：

```powershell
git diff -- <文件>
```

- 是自己的 → 按 G2 提交。
- 是别人的（或自己看不懂）→ **不要 add，不要 restore**，留着，并在回复里说明「这个文件有外部改动，未纳入本次提交」。

---

## 8. 收工即提交（G7）

一个会话的工作**以提交结束**，不以「改完了」结束。收工前跑这三条：

```powershell
python tools/check_git_dirty.py   # 0 = CLEAN，可以收工；1 = DIRTY，照下面处理
git status           # 1. 还有自己的文件没提交？→ 回到第 2 节，按路径 add
git log --oneline -3 # 2. 确认自己的提交真的在历史里
git status --short   # 3. 剩下的应只有别人的改动
```

- 还有自己的改动 → 提交，消息写清这次做了什么、为什么。
- 还有别人的改动 → 原样留着，**不要** `-A`；回复里说明「这些是外部改动，未纳入」。
- 确实不该提交的（临时调试、别人的半成品）→ 也要在回复里说清，不许默默留在工作区。

**为什么**：改动留在工作区等于没交付——下一个人（或下一个会话）看不到，
冲突难拆，会话一断就丢了「这批改动是什么意图」的上下文。
本仓库 2026-10-26 凌晨就出现过一次：某个会话跑完检查直接收工，
40 个修改 + 22 个新文件全堆在工作区，主人以为「提交了」，
翻 `git log` 却什么都没有。

---

## 9. 新功能：把能力写进 commit

用户能感知的新功能，除了代码，还要让她知道自己会了。在 commit message 末尾写
`Capability:` trailer，然后跑 `tools/capability_sync.py --apply`：

```text
feat: 合并转发支持引用里的记录

Capability: 别人转发的聊天记录，你能读到里面的话
Capability-Key: forward
Capability-Flag: enable_forward
Capability-Label: 合并转发
Capability-Off: 关闭合并转发
Capability-On: 打开合并转发
```

- 只写用户能感知的功能；内部重构、测试、`persona.md` 修改不写；
- `Capability-Key` 不能重复；`Flag` / `Off` / `On` 会被校验；
- 生成物 `qqbot/capabilities_auto.json`、`docs/能力表.md` 不要手改；
- 完整字段和流程见 [能力表工作流.md](能力表工作流.md)。

> 这个 Windows 沙箱里 Git 的 `sh.exe` 起不来，pre-commit 钩子在这里会让提交失败；
> 在普通终端用 `python tools/install_git_hooks.py` 安装。
