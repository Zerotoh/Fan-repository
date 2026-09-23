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

## 发布到手机看

`dist/` 是发布包（只有 `index.html` + 看板数据，已剔除 `data/raw` 里的原始健康与睡眠数据）。
更新在线版本需要：把最新 `data/processed/dashboard_data.js` 拷进 `dist/data/processed/`，再重新部署。

## 目录

```
index.html                 看板本体（含全部样式与渲染逻辑）
scripts/analyze.py         分析 + 生成看板数据包
scripts/sync.py            拉取 Garmin 数据（含数据保护）
scripts/refresh.bat        一键刷新
data/plan_config.json      训练配置（可编辑）
data/raw/                  Garmin 原始 JSON（含隐私数据，未发布）
data/processed/            分析产物 + goal_history.jsonl（目标进度时间序列）
dist/                      发布包（仅看板文件）
优化建议.md                 2026-09-22 的代码评审与优化清单
```
