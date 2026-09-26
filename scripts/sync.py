#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync.py — 重新从 Garmin Connect CN 拉取最新数据并刷新看板数据包。

用法（必须用已装好 garminconnect 的 venv python 运行）:
  <venv>/Scripts/python.exe scripts/sync.py           # 增量拉取
  <venv>/Scripts/python.exe scripts/sync.py --full    # 强制全量拉取活动

完成后会重新生成 data/processed/analysis.json 与 dashboard_data.js。

安全约定（2026-09-22 加固）:
  - CLI 返回 {"status":"error", ...} 这类"错误桩"是合法 JSON，但**不是数据**。
    本脚本会校验 status 与 data 内容，校验不过就**不落盘**，保留旧文件。
  - 所有写入都是"临时文件 + os.replace"原子替换，进程中途挂掉不会留下半个文件。
  - 覆写前会把上一份成功数据备份到 data/raw/_backup_prev/（只保留一代）。
  - 任一核心任务失败 → 退出码 1（自动化可据此判断"这次没成"）。
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(BASE, "data", "raw")
BACKUP = os.path.join(RAW, "_backup_prev")
RETRY = 1  # 失败额外重试次数
DETAIL_COUNT = 25  # 每次同步抓取最近 N 场跑步的完整详情（分段/心率区间/跑姿）


def log(msg):
    print(msg, flush=True)


def find_garmin_cli():
    # 优先用仓库内置的副本：仓库自包含，CI / 任意机器都能跑，不依赖本机 skills 目录
    in_repo = os.path.join(BASE, "scripts", "garmin_cli.py")
    if os.path.exists(in_repo):
        return in_repo
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".workbuddy", "skills", "garmin-connect-cn-data__skillhub",
                     "scripts", "garmin_cli.py"),
    ]
    sk = os.path.join(home, ".workbuddy", "skills")
    if os.path.isdir(sk):
        for root, _, files in os.walk(sk):
            if "garmin_cli.py" in files:
                candidates.append(os.path.join(root, "garmin_cli.py"))
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


# ───────────────────────── 读写与校验 ─────────────────────────
def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def atomic_write_json(path, doc):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def backup_prev(path):
    """覆写前保留上一份成功数据（一代）。"""
    if not os.path.exists(path):
        return
    os.makedirs(BACKUP, exist_ok=True)
    try:
        shutil.copy2(path, os.path.join(BACKUP, os.path.basename(path)))
    except Exception as e:
        log(f"        [warn] 备份旧数据失败（继续）: {e}")


def payload_ok(cmd, doc):
    """判断 CLI 输出到底是数据还是错误桩。返回 (ok, reason)。"""
    if not isinstance(doc, dict):
        return False, "输出不是 JSON 对象"
    status = doc.get("status")
    if status is not None and status != "success":
        return False, f"CLI status={status}: {str(doc.get('message'))[:120]}"
    if "data" not in doc:
        return False, "缺少 data 字段"
    data = doc["data"]
    if cmd == "sleep":
        recs = data.get("records") if isinstance(data, dict) else None
        if not recs:
            return False, "sleep.data.records 为空"
    elif isinstance(data, (list, dict)):
        if len(data) == 0:
            return False, "data 为空"
    else:
        return False, f"data 类型异常: {type(data).__name__}"
    return True, ""


def run_job(cmd_args, out_path, cmd, extra_check=None, merge=None):
    """执行一次 CLI 调用并通过校验后原子落盘。成功返回 payload data，失败返回 None。"""
    label = os.path.basename(out_path) if out_path else cmd
    for attempt in range(RETRY + 1):
        log(f"[sync] {cmd} -> {label}" + (f" (重试 {attempt})" if attempt else ""))
        try:
            proc = subprocess.run(cmd_args, capture_output=True, text=True,
                                  timeout=600, encoding="utf-8", errors="replace")
        except Exception as e:
            log(f"        [error] 调用失败: {e}")
            continue
        raw = (proc.stdout or "").strip()
        if not raw:
            log(f"        [warn] 无输出；stderr: {(proc.stderr or '').strip()[:300]}")
            continue
        try:
            doc = json.loads(raw)
        except Exception:
            log(f"        [warn] 输出非 JSON；stderr: {(proc.stderr or '').strip()[:300]}")
            continue

        ok, reason = payload_ok(cmd, doc)
        if not ok:
            log(f"        [warn] 校验未通过，丢弃且不落盘：{reason}")
            continue

        if merge is not None:
            merged, why = merge(read_json(out_path), doc.get("data"))
            if why:
                log(f"        [warn] 合并校验未通过，丢弃且不落盘：{why}")
                continue
            doc = dict(doc)
            doc["data"] = merged

        if extra_check is not None:
            ok2, reason2 = extra_check(doc)
            if not ok2:
                log(f"        [warn] 合理性校验未通过，丢弃且不落盘：{reason2}")
                continue

        backup_prev(out_path)
        try:
            atomic_write_json(out_path, doc)
        except Exception as e:
            log(f"        [error] 写盘失败: {e}")
            continue
        log(f"        [ok] 已写入 {label}")
        return doc.get("data")
    log(f"        [fail] {cmd} 未成功，保留原文件不动")
    return None


# ───────────────────────── 增量合并 ─────────────────────────
def merge_activities(old_doc, new_list):
    """按 activity_id 合并历史与增量（新数据覆盖旧的），按日期升序输出。"""
    old = []
    if isinstance(old_doc, dict) and isinstance(old_doc.get("data"), list):
        old = old_doc["data"]
    elif isinstance(old_doc, list):
        old = old_doc
    by_id = {}
    for a in old:
        if isinstance(a, dict) and a.get("activity_id") is not None:
            by_id[str(a["activity_id"])] = a
    for a in (new_list or []):
        if isinstance(a, dict) and a.get("activity_id") is not None:
            by_id[str(a["activity_id"])] = a
    merged = sorted(by_id.values(), key=lambda x: str(x.get("date") or ""))
    if not merged:
        return None, "合并后活动列表为空"
    if old and len(merged) < len(old) * 0.7:
        return None, f"活动条数异常下降（{len(old)} → {len(merged)}）"
    return merged, ""


def activities_start_date():
    """有历史数据就从最后一次跑步前 7 天开始拉；否则全量。"""
    doc = read_json(os.path.join(RAW, "activities_running.json"))
    acts = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(acts, list) or not acts:
        return "2010-01-01", 0
    last = max((str(a.get("date") or "") for a in acts if isinstance(a, dict)), default="")
    try:
        start = (datetime.strptime(last, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
    except Exception:
        start = "2010-01-01"
    return start, len(acts)


# ───────────────────────── 主流程 ─────────────────────────
def main():
    cli = find_garmin_cli()
    if not cli:
        log("[sync] 找不到 garmin_cli.py，请确认技能已安装。")
        sys.exit(1)
    py = sys.executable
    log(f"[sync] python={py}\n[sync] cli={cli}")

    os.makedirs(RAW, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    full = "--full" in sys.argv

    start, have = activities_start_date()
    if full:
        start = "2010-01-01"
    log(f"[sync] 活动范围 {start} ~ {today}（本地已有 {have} 条，"
        f"{'全量' if start == '2010-01-01' else '增量'}）")

    results = {}
    results["activities"] = run_job(
        [py, cli, "activities", "--start", start, "--end", today, "--type", "running"],
        os.path.join(RAW, "activities_running.json"),
        cmd="activities",
        extra_check=lambda doc: (len(doc["data"]) > 0, "活动列表为空"),
        merge=merge_activities,
    )
    results["health"] = run_job([py, cli, "health", "--days", "90"],
                                os.path.join(RAW, "health_90d.json"), cmd="health")
    results["sleep"] = run_job([py, cli, "sleep", "--days", "90"],
                               os.path.join(RAW, "sleep_90d.json"), cmd="sleep")
    results["summary"] = run_job([py, cli, "summary", "--date", today],
                                 os.path.join(RAW, "summary_today.json"), cmd="summary")

    ok = sum(1 for v in results.values() if v is not None)
    log(f"[sync] 核心拉取完成 {ok}/{len(results)}")

    # 最近 N 场跑步的完整详情（分段/心率区间/跑姿动力学），按 activity_id 索引存盘。
    # 这样历史场次的跑后分析也有完整数据，而不只是最新一次。失败不算致命。
    try:
        acts_doc = read_json(os.path.join(RAW, "activities_running.json"))
        acts = acts_doc.get("data", []) if isinstance(acts_doc, dict) else []
        runs = [a for a in acts
                if str(a.get("type") or "") in ("running", "treadmill_running")
                and a.get("activity_id") is not None]
        recent = sorted(runs, key=lambda a: str(a.get("date") or ""))[-DETAIL_COUNT:]
        ids = [str(a["activity_id"]) for a in recent]
        if ids:
            got = run_job([py, cli, "details", "--ids", ",".join(ids)],
                          os.path.join(RAW, "activity_details.json"), cmd="details")
            if got is not None:
                ok_n = sum(1 for v in got.values() if isinstance(v, dict) and "error" not in v)
                log(f"[sync] 已抓取最近 {ok_n}/{len(ids)} 场跑步详情 -> activity_details.json")
    except Exception as e:
        log(f"[sync] 多场详情抓取跳过: {e}")

    # 重新分析并生成看板数据
    analyze_failed = False
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "analyze", os.path.join(BASE, "scripts", "analyze.py"))
        analyze = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(analyze)
        analyze.main()
        log("[sync] 看板数据已刷新 OK")
    except Exception as e:
        log(f"[sync] 分析步骤失败: {e}")
        analyze_failed = True

    if analyze_failed or ok < len(results):
        log("[sync] 本次未完全成功（看板仍用上一份有效数据或部分新数据生成）。")
        sys.exit(1)


if __name__ == "__main__":
    main()
