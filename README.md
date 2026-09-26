# 个人跑步教练（跑步看板）

纯前端看板 + 一条数据管道。双击 `index.html` 即用，零后端、零构建。

## 数据从哪来

```
Garmin Connect CN ──sync.py──> data/raw/*.json ──analyze.py──> data/processed/dashboard_data.js ──> index.html
```

- `scripts/sync.py` —— 用 Garmin CN 技能拉取：跑步活动（**增量**，从最后一次跑步前 7 天开始）、近 90 天健康/睡眠、今日综合状态、最新一次跑步的分段明细。
- `scripts/analyze.py` —— 只依赖标准库，从 `data/raw` 生成 `analysis.json`（机器可读）和 `dashboard_data.js`（看板用，`window.DASHBOARD_DATA`）。`sync.py` 会在拉取结束后自动调用它。
- `index.html` —— 六个标签页：当前状态 / 历史分析 / 能力画像 / 今日课表 / 每周课表 / 跑后分析。

## 怎么刷

**必须用装好 garminconnect 的解释器**（系统默认 python 里没有，用错会导致同步失败）：

```bat
C:\Users\Fan\.workbuddy\binaries\python\envs\garmin-cn\Scripts\python.exe D:\WorkSpace\个人跑步教练\scripts\sync.py
```

或直接双击 `scripts/refresh.bat`（Windows 一键）。只想重算不算重拉：

```bat
python scripts\analyze.py
```

## 刷失败了怎么办

脚本已经做了保护，失败**不会**破坏已有数据：

- 会校验 CLI 返回的 `status` / `data` 内容，`{"status":"error",...}` 这类错误桩会被丢弃、不落盘；
- 写盘是"临时文件 + 原子替换"，进程中途挂掉不会留下半个文件；
- 覆写前把上一份成功数据备份到 `data/raw/_backup_prev/`（一代）；
- 任一核心任务失败 → 退出码 `1`。

排错顺序：① 是不是用错了 python（`requirements.txt`）；② 凭证 `~/.config/garmin-cn/credentials.json` 是否过期（重新走一次 Garmin 登录）；③ 看输出里的 `[warn]` 行，那里写了具体校验失败原因。

## 配置

`data/plan_config.json` 可以直接编辑：

| 字段 | 含义 |
|---|---|
| `goal_label` / `goal_target_time` / `goal_target_minutes` | 目标（当前：半马跑进 1:50） |
| `race_date` | 比赛日期，决定周期阶段（基础/强化/巅峰/减量） |
| `runs_per_week` | 每周训练次数 |
| `work_pattern` / `work_anchor` | 上二休二作息；`work_anchor` 是一个**主班日**的日期 |

作息规则：一个 4 天循环 = 主班日 / 备班日 / 休息日 / 休息日，**只有主班日不安排训练**。

## 每天自动更新（本地同步 → 推送）

**数据源只有一个：本机。** 不再在云端（GitHub Actions）登录 Garmin——海外 IP 反复登录易触发验证码/锁号，且云端包曾覆盖本地。

每天 07:00 的本地自动化会跑 `scripts/auto_sync_push.bat`：

```
本机 07:00 自动化
   └─ scripts/auto_sync_push.bat
        ├─ garmin-cn venv 跑 sync.py  → 拉 Garmin CN + 分析生成看板
        └─ 用 pat.txt 的 token 提交并推 index.html / data/processed / data/plan_config.json / .nojekyll
             └─ git push → origin/main
                  └─ GitHub Pages 从 main 分支根目录自动重建（纯备份）
```

- 同步成功才推送，失败不推送（保留上一份有效数据）。
- 手动刷新+推送：`scripts\auto_sync_push.bat`，或只刷新不推送 `scripts\refresh.bat`。
- 凭据：Garmin 走 `~/.config/garmin-cn/credentials.json`（本地）；GitHub 走仓库内 `pat.txt`。

## 把改动推到 GitHub（push.bat）

本地改完双击 `push.bat` 即可推送（已取代旧的 `deploy.bat`）：

```bat
D:\WorkSpace\个人跑步教练\push.bat
```

- 它从本地 `pat.txt` 读 GitHub PAT（**该文件已 gitignore，切勿提交**），全程不用手贴 token。
- 推的是看板产物：`index.html`、`data/processed/`、`data/plan_config.json`、`.nojekyll`（**不再推 `daily.yml`**，云端工作流已删）。
- PAT 只需 `repo` scope（`workflow` 已不需要）。
- `pat.txt` 只放一行 token（纯文本、无 BOM）；用记事本建容易存成 `pat.txt.txt`，注意改名。

## 发布到手机看

**主力视图：WorkBuddy 在线托管链接 `https://run-dashboard.app.workbuddy.host/`**（公网可达，不在家也能随时看最新，国内网络稳定）。

GitHub 仓库只作**备份**：Pages 从 `main` 根目录部署（`https://zerotoh.github.io/Fan-repository/`），但国内访问可能偏慢。两处显示的都是同一份本地生成的 `dashboard_data.js`，数据源统一，不会再有「云端覆盖本地」的情况。

`dist/` 是另一份发布包（只有 `index.html` + 看板数据，已剔除 `data/raw` 里的原始健康与睡眠数据），对应 WorkBuddy 在线发布通道；如需重新发布：把最新 `data/processed/dashboard_data.js` 拷进 `dist/data/processed/`，再重新部署。

## 关于「今天」与状态快照

- 看板的**日历 / 今日课表 / 本周高亮**永远按**真实本地日期**走；即使同步断更，也不会停在旧数据上假装是今天。
- 但**恢复 / 睡眠 / 身体电量 / HRV** 这些信号来自 Garmin 设备最后一次同步的快照（`state_date`）。若 `state_date` 早于今天，页头会标红提示「状态/睡眠/电量来自 X 月 X 日快照（已过期 N 天）」，请先跑一次同步刷新。`generated_at` 超过 24 小时也会触发整体陈旧报警。

## 指标与功能说明（2026-09-25 新增）

**能力分 vs 状态分（重要）**
- **能力分**：在「能力画像」页的评级（S/A/B/C/D）。基于稳定的历史性能维度——有氧底盘（周里程/ACWR）、速度/阈值（VO2 Max/阈值配速）、训练规律性、静息心率——**不再随单日睡眠/电量波动**。以前昨夜睡眠差就会让评级一天掉一级，现已修正。
- **状态分（今日）**：在「当前状态」页顶部卡片，等于每日就绪度（昨夜睡眠、近 7 天睡眠、身体电量、训练负荷、训练状态综合得出）。它才是每天会变的数。
- **Form(TSB)**：`chronic − acute` 训练压力平衡，正=恢复良好/可冲强度，负=疲劳累积。与状态分一起回答「今天能不能硬」。

**半马依据新鲜度**
- 比赛估算表里每项都标注证据来源与**距今天数**（如「由 10K 外推（依据 52 天前的实测）」）。长距离证据陈旧时一眼可见，不会误以为是最新能力。

**跑步效率 EF 趋势**
- EF = 速度(m/min) ÷ 平均心率。同配速下心率越低、或同心率下配速越快，EF 越高 = 有氧效率在提升。趋势向上=变强。只看户外跑、距离≥3km。

**本周完成度**
- 「每周课表」页新增「本周完成度」进度条：计划 vs 实际 的**训练次数**与**质量课（阈值/间歇）**完成比，以及本周实跑里程合计。

**跑后分析（历史场次 + 主观反馈）**
- 顶部分下拉可切换**最近 12 次跑步**（仅最新一次含完整分段/跑姿明细，历史场次为摘要）。
- 每场跑步可填 **RPE / 体重 / 伤痛备注**，存浏览器 `localStorage`，按日期关联，**不上传**（纯本地记录，换设备不保留）。

**HRV / 睡眠 个人基线带**
- 状态卡里 HRV、睡眠评分展示 60 天基线，并标「高于/低于基线」，偏离一眼可见。

**已修的小瑕疵**
- 训练状态 badge 不再硬编码「进行中」，改为按 Garmin 状态动态着色。
- 静息心率 badge 不再留空（≤45 优秀 / ≤55 良好 / 否则偏高）。

## 目录

```
index.html                 看板本体（含全部样式与渲染逻辑）
scripts/analyze.py         分析 + 生成看板数据包（today 取真实日期，状态快照另存 state_date）
scripts/sync.py            拉取 Garmin 数据（含数据保护）
scripts/refresh.bat        一键刷新（本地，不推送）
scripts/auto_sync_push.bat 一键「同步 → 推送 GitHub」（供每日自动化调用，失败不推送）
push.bat                   推到 GitHub（从本地 pat.txt 读 PAT，已取代 deploy.bat）
pat.txt                    本地 GitHub PAT（gitignore，勿提交）
data/plan_config.json      训练配置（可编辑，已入库）
data/raw/                  Garmin 原始 JSON（含隐私数据，未发布）
data/processed/            分析产物 + goal_history.jsonl（目标进度时间序列）
dist/                      发布包（仅看板文件，对应 WorkBuddy 在线发布）
.nojekyll                  GitHub Pages 关闭 Jekyll 处理
优化建议.md                 代码评审与优化清单（2026-09-22 基线 + 2026-09-25 二期）
```
