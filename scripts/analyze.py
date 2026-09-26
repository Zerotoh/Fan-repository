#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
analyze.py — 处理 Garmin CN 导入的原始数据，产出：
  1) data/processed/analysis.json        机器可读的分析结果
  2) data/processed/dashboard_data.js    看板可直接 <script> 引入(window.DASHBOARD_DATA)

核心模块：
  - analyze_runs        历史跑量 / 配速 / 心率聚合
  - assess_ability      跑步能力画像（有氧底盘 / 速度阈值 / 训练规律性 / 恢复 / 综合评级）
                        + Riegel 比赛配速估算 + 配速区间 / 心率区间推导
  - analyze_current_state  当前状态信号（对象数组，修 bug #1）
  - compute_plan        今日课表（含 48–72h 长距离疲劳规则 + 详细可执行课表）
  - build_post_runs / _build_post_run_one  跑后全维度分析（支持近 N 次历史场次切换；仅最新一次含完整分段/跑姿明细）
  - build_ef_trend      跑步效率 EF 趋势（速度÷心率，同配速心率下降=变强）
并在 stdout 打印一份人类可读报告。
依赖: 仅标准库
"""
import json
import os
import statistics
import itertools
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(BASE, "data", "raw")
OUT = os.path.join(BASE, "data", "processed")
CFG_PATH = os.path.join(BASE, "data", "plan_config.json")

# 强制 stdout/stderr 为 UTF-8，避免 Windows GBK 控制台打印非 GBK 字符（原报告里的警告符号）时 'gbk' codec 崩溃
import sys as _sys
try:
    if hasattr(_sys.stdout, "reconfigure"):
        _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(_sys.stderr, "reconfigure"):
        _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def load_plan_config():
    """读取目标导向的训练配置（用户可编辑 data/plan_config.json）。"""
    defaults = {
        "goal": "half_marathon_pb",
        "goal_label": "半程马拉松 PB",
        "race_distance_km": 21.0975,
        "race_date": None,
        "runs_per_week": 4,
        "work_pattern": "2on2off",
        "work_anchor": "2026-09-22",
    }
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            user = json.load(f)
        defaults.update({k: v for k, v in user.items() if v not in (None, "")})
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return defaults


# ─────────────────────────────── 基础工具 ───────────────────────────────
def load(name):
    with open(os.path.join(RAW, name), encoding="utf-8") as f:
        return json.load(f)


def pace_to_sec(pace_str):
    """'5:30' -> 330 (秒/公里); 无效返回 None"""
    if not pace_str or ":" not in pace_str:
        return None
    try:
        m, s = pace_str.split(":")
        return int(m) * 60 + int(s)
    except ValueError:
        return None


def sec_to_pace(sec):
    if sec is None:
        return None
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def sec_to_hm(sec):
    if sec is None:
        return "—"
    sec = int(round(sec))
    h, r = divmod(sec, 3600)
    m = r // 60
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m{sec % 60:02d}s"


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _std(xs):
    xs = [x for x in xs if x is not None]
    return statistics.pstdev(xs) if len(xs) > 1 else 0.0


def clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


def load_activities():
    d = load("activities_running.json")
    data = d.get("data", [])
    for a in data:
        a["pace_sec"] = pace_to_sec(a.get("pace_formatted"))
    data.sort(key=lambda x: x["date"])
    return data


def load_sleep():
    s = load("sleep_90d.json").get("data", {})
    return s.get("records", []), s.get("averages", {})


def load_health():
    h = load("health_90d.json")
    return h.get("data", []), h.get("summary", {})


def load_summary_today():
    return load("summary_today.json").get("data", {})


def load_latest_detail():
    p = os.path.join(RAW, "activity_detail_latest.json")
    if not os.path.exists(p):
        return None
    try:
        doc = json.load(open(p, encoding="utf-8"))
        return doc.get("data", {})
    except Exception:
        return None


def load_details():
    """读取多场跑步详情字典 {activity_id: {laps, hr_zones, running_dynamics, ...}}。
    过滤掉抓取失败的条目（含 'error' 字段）。无文件时返回空字典。"""
    p = os.path.join(RAW, "activity_details.json")
    if not os.path.exists(p):
        return {}
    try:
        doc = json.load(open(p, encoding="utf-8"))
        d = doc.get("data", doc) if isinstance(doc, dict) else {}
        return {k: v for k, v in d.items()
                if isinstance(v, dict) and "error" not in v}
    except Exception:
        return {}


# ─────────────────────────────── 历史跑量聚合 ───────────────────────────────
def analyze_runs(acts, today=None):
    if not acts:
        return {}
    total_dist = sum(a["distance_km"] or 0 for a in acts)
    total_dur = sum(a["duration_sec"] or 0 for a in acts)
    by_type = {}
    for a in acts:
        by_type[a["type"]] = by_type.get(a["type"], 0) + 1

    # 月份聚合
    monthly = {}
    for a in acts:
        ym = a["date"][:7]
        m = monthly.setdefault(ym, {"dist": 0.0, "count": 0, "pace_wsum": 0.0, "pace_wcount": 0.0})
        m["dist"] += a["distance_km"] or 0
        m["count"] += 1
        if a["pace_sec"]:
            m["pace_wsum"] += a["pace_sec"] * (a["distance_km"] or 0)
            m["pace_wcount"] += (a["distance_km"] or 0)

    monthly_out = {}
    for ym in sorted(monthly):
        m = monthly[ym]
        avg_pace = (m["pace_wsum"] / m["pace_wcount"]) if m["pace_wcount"] else None
        monthly_out[ym] = {
            "distance_km": round(m["dist"], 1),
            "runs": m["count"],
            "avg_pace": sec_to_pace(avg_pace),
            "avg_pace_sec": avg_pace,
        }

    last_date = datetime.strptime(acts[-1]["date"], "%Y-%m-%d")

    def window(days):
        cutoff = last_date - timedelta(days=days - 1)
        return [a for a in acts if datetime.strptime(a["date"], "%Y-%m-%d") >= cutoff]

    def window_stats(sub):
        if not sub:
            return {"runs": 0, "distance_km": 0, "avg_pace_sec": None, "avg_pace": None}
        dist = sum(a["distance_km"] or 0 for a in sub)
        pw = sum((a["pace_sec"] or 0) * (a["distance_km"] or 0) for a in sub)
        pc = sum(a["distance_km"] or 0 for a in sub)
        avg_pace = pw / pc if pc else None
        return {"runs": len(sub), "distance_km": round(dist, 1),
                "avg_pace_sec": avg_pace, "avg_pace": sec_to_pace(avg_pace)}

    w7, w30, w90 = window_stats(window(7)), window_stats(window(30)), window_stats(window(90))

    # 配速分布：优先只用"户外跑"——跑步机的配速/心率不可比，
    # 混进来会污染综合配速、最快配速，进而污染阈值配速与五档配速区间。
    outdoor = [a for a in acts if a.get("type") == "running"]
    valid = [a for a in outdoor if (a["distance_km"] or 0) >= 3 and a["pace_sec"]]
    pace_source = "outdoor"
    if len(valid) < 5:  # 户外样本太少则退回全量，避免算不出东西
        valid = [a for a in acts if (a["distance_km"] or 0) >= 3 and a["pace_sec"]]
        pace_source = "all"
    all_pw = sum(a["pace_sec"] * a["distance_km"] for a in valid)
    all_pc = sum(a["distance_km"] for a in valid)
    overall_pace = all_pw / all_pc if all_pc else None
    best_pace = min((a["pace_sec"] for a in valid), default=None)
    best_run = next((a for a in valid if a["pace_sec"] == best_pace), None)

    hrs = [a["avg_hr"] for a in acts if a.get("avg_hr")]
    max_hr_records = [a["max_hr"] for a in acts if a.get("max_hr")]
    hr_avg = _mean(hrs)

    long_runs = [a for a in acts if (a["distance_km"] or 0) >= 15]

    # ACWR（跑量视角）：标准 7 天 / 28 天，窗口右端锚定"今天"
    # （旧实现是 4 周/12 周互相嵌套且锚定"最后一次跑步"，
    #   结果天然趋近 1，且不跑的日子负荷也不衰减 → 反映不了当前）
    end_dt = last_date  # 已是 datetime（最后一次跑步）
    if today:
        try:
            t_dt = datetime.strptime(today, "%Y-%m-%d")
            if t_dt > end_dt:
                end_dt = t_dt
        except Exception:
            pass

    def weekly_mean(days):
        start = end_dt - timedelta(days=days)
        sub = [a for a in acts if start < datetime.strptime(a["date"], "%Y-%m-%d") <= end_dt]
        return sum(a["distance_km"] or 0 for a in sub) / (days / 7.0)

    acute, chronic = weekly_mean(7), weekly_mean(28)
    acwr = round(acute / chronic, 2) if chronic else None

    # 训练规律性：近 90 天周均次数 & 近 12 个完整 ISO 周的周里程变异系数
    runs_per_week = round(w90["runs"] / (90.0 / 7.0), 2) if w90["runs"] else 0
    # 旧实现用 a["date"][:7]（月份）当"周"，字段名和 UI 都写着"周" → 已改为真实 ISO 周。
    # 只统计已过完的 12 个完整周；没跑的周记 0，这样"中断"才会体现在 CV 上。
    week_km = {}
    for a in acts:
        wkey = datetime.strptime(a["date"], "%Y-%m-%d").isocalendar()[:2]
        week_km[wkey] = week_km.get(wkey, 0) + (a["distance_km"] or 0)
    monday_this = end_dt - timedelta(days=end_dt.weekday())
    wk_vals = []
    for i in range(12, 0, -1):  # 从 12 周前排到上周（不含本周未完成）
        wkey = (monday_this - timedelta(days=7 * i)).isocalendar()[:2]
        wk_vals.append(round(week_km.get(wkey, 0.0), 1))
    cv = (_std(wk_vals) / _mean(wk_vals) * 100) if _mean(wk_vals) else 0

    return {
        "total_runs": len(acts),
        "total_distance_km": round(total_dist, 1),
        "total_duration_hours": round(total_dur / 3600, 1),
        "first_date": acts[0]["date"],
        "last_date": acts[-1]["date"],
        "by_type": by_type,
        "monthly": monthly_out,
        "window_7d": w7, "window_30d": w30, "window_90d": w90,
        "overall_pace_sec": overall_pace, "overall_pace": sec_to_pace(overall_pace),
        "best_pace_sec": best_pace, "best_pace": sec_to_pace(best_pace),
        "best_pace_run": best_run,
        "avg_hr": round(hr_avg, 1) if hr_avg else None,
        "max_hr_recorded": max(max_hr_records) if max_hr_records else None,
        "long_runs_count": len(long_runs),
        "acwr_volume": acwr, "acute_weekly_km": round(acute, 1), "chronic_weekly_km": round(chronic, 1),
        "acwr_window": "7d/28d", "acwr_anchor": end_dt.strftime("%Y-%m-%d"),
        "runs_per_week": runs_per_week, "weekly_dist_cv": round(cv, 1),
        "weekly_dist_cv_weeks": wk_vals,
        "pace_source": pace_source,
    }


# ─────────────────────────────── 能力画像 + 比赛估算 ───────────────────────────────
def _robust_best(vals):
    """取"能力基线"：有 2 个以上样本就取第 2 快，避开单次异常值；
    只有 1 个样本时只能用它。（保留给需要更保守口径的场景）"""
    v = sorted(x for x in vals if x)
    if not v:
        return None
    return v[min(1, len(v) - 1)]


# 努力表现会随时间轻微"贬值"：每月 +0.5%，最多 +8%。
# 依据是"9 个月前的半马 1h55m"仍是最强证据，但不能等同于当前能力。
STALE_PER_MONTH = 0.005
STALE_CAP = 0.08


def _stale_factor(days_old):
    if days_old is None or days_old <= 0:
        return 1.0
    return 1.0 + min(STALE_CAP, days_old / 30.0 * STALE_PER_MONTH)


def estimate_race_times(acts, max_hr, resting_hr, today=None):
    """Riegel 公式 T2 = T1 × (D2/D1)^1.06。

    做法（相对旧实现的改动）：
      1. 只用户外跑 —— 跑步机配速/心率不可比；
      2. 每个距离区间里取"最佳努力"并按时间贬值（不是无脑取历史最小）；
      3. 长距离的成绩同时用"短距离证据按 Riegel 外推"来交叉验证，取更有力的一侧——
         旧实现只要 16–25km 区间有一条记录就直接用它，结果会把一次轻松长距离
         （如 18km @6:33，心率虽高但配速很慢）当成半马能力，估出 2h19m。
    """
    # 努力阈值：平均心率需达到 (静息 + 0.70*(最大-静息)) 以上，避免把轻松慢跑当比赛
    effort_hr = resting_hr + 0.70 * (max_hr - resting_hr) if (max_hr and resting_hr) else 0
    bands = {5.0: (4.0, 7.0), 10.0: (8.0, 14.0), 21.0975: (16.0, 25.0)}
    try:
        today_dt = datetime.strptime(today, "%Y-%m-%d") if today else None
    except Exception:
        today_dt = None
    cands = {k: [] for k in bands}       # anchor -> [(调整后秒数, 原始秒数, 日期)]
    for a in acts:
        if a.get("type") != "running":
            continue
        d, p, dur, hr = a.get("distance_km"), a.get("pace_sec"), a.get("duration_sec"), a.get("avg_hr")
        if not (d and p and dur):
            continue
        if effort_hr and (hr or 0) < effort_hr:
            continue
        try:
            a_dt = datetime.strptime(a["date"], "%Y-%m-%d")
        except Exception:
            a_dt = None
        age = (today_dt - a_dt).days if (today_dt and a_dt) else None
        for anchor, (lo, hi) in bands.items():
            if lo <= d <= hi:
                raw = dur * (anchor / d) ** 1.06
                cands[anchor].append((raw * _stale_factor(age), raw, a.get("date")))

    direct, evidence = {}, {}
    for anchor, lst in cands.items():
        if not lst:
            continue
        # 取"最有力的一次努力"（已按时间贬值调整），这是对当前能力最有信息量的证据
        best = min(lst, key=lambda x: x[0])
        direct[anchor] = best
        evidence[anchor] = {"date": best[2], "raw_sec": round(best[1])}

    out = {}
    for label, anchor in [("5K", 5.0), ("10K", 10.0), ("半马", 21.0975), ("全马", 42.195)]:
        options = []
        if anchor in direct:
            options.append((direct[anchor][0], "direct", anchor))
        for shorter in sorted(bands):
            if shorter < anchor and shorter in direct:
                options.append((direct[shorter][0] * (anchor / shorter) ** 1.06,
                                "derived", shorter))
        if not options:
            continue
        sec, kind, src = min(options, key=lambda x: x[0])
        item = {"sec": round(sec), "formatted": sec_to_hm(sec), "basis": kind}
        if kind == "direct":
            ev = evidence.get(anchor) or {}
            item["evidence_date"] = ev.get("date")
            item["evidence_sec"] = ev.get("raw_sec")
            if ev.get("date") and today_dt:
                try:
                    item["evidence_age_days"] = (today_dt - datetime.strptime(ev["date"], "%Y-%m-%d")).days
                except Exception:
                    item["evidence_age_days"] = None
        else:
            item["derived_from"] = {5.0: "5K", 10.0: "10K", 21.0975: "半马"}.get(src, "")
            src_ev = evidence.get(src) or {}
            if src_ev.get("date") and today_dt:
                try:
                    item["evidence_age_days"] = (today_dt - datetime.strptime(src_ev["date"], "%Y-%m-%d")).days
                except Exception:
                    item["evidence_age_days"] = None
        out[label] = item
    return out


def derive_pace_zones(threshold_pace_sec):
    """以阈值配速为基准（Daniels 风格），推算 E/M/T/I/R 五档配速区间。"""
    if not threshold_pace_sec:
        return None
    # 倍率（阈值配速 × 倍率 = 该档配速，>1 表示更慢）
    mult = {
        "E": (1.25, 1.40),   # 轻松/恢复
        "M": (1.12, 1.18),   # 马拉松
        "T": (1.00, 1.06),   # 阈值/节奏
        "I": (0.93, 0.97),   # 间歇
        "R": (0.86, 0.92),   # 重复
    }
    zones = {}
    for name, (lo, hi) in mult.items():
        zones[name] = {
            "pace_lo": sec_to_pace(threshold_pace_sec * lo),
            "pace_hi": sec_to_pace(threshold_pace_sec * hi),
            "pace_lo_sec": round(threshold_pace_sec * lo),
            "pace_hi_sec": round(threshold_pace_sec * hi),
        }
    return zones


def derive_hr_zones(max_hr, resting_hr):
    """基于心率储备(HRR)的 Z1–Z5 目标区间。"""
    if not (max_hr and resting_hr):
        return None
    hrr = max_hr - resting_hr
    edges = [0.50, 0.60, 0.70, 0.80, 0.90, 1.00]
    names = ["Z1 恢复", "Z2 有氧", "Z3 马拉松", "Z4 阈值", "Z5 无氧"]
    zones = []
    for i in range(5):
        lo = resting_hr + edges[i] * hrr
        hi = resting_hr + edges[i + 1] * hrr
        zones.append({"name": names[i], "lo": round(lo), "hi": round(hi)})
    return zones


def recovery_score(resting_hr, hrv_recent_avg=None, hrv_baseline=None, sleep_recent_avg=None):
    """恢复能力评分：各来源**分别归一化到 0–100** 后按权重合并。

    旧实现把 HRV 的原始毫秒值和 0–100 的睡眠评分直接取平均（量纲不同），
    且维度描述还写着"静息心率 X bpm"，与实际分数来源不符。这里一并修掉。

    - 静息心率：35bpm→100，45→70，55→40（权重 0.4）
    - HRV：相对个人基线的偏离，+10% → 80，-10% → 40（权重 0.3）
    - 睡眠评分：本身即 0–100（权重 0.3）
    """
    parts, descs = [], []
    if resting_hr is not None:
        parts.append((clamp(100 - (resting_hr - 35) * 3), 0.4))
        descs.append(f"静息心率 {resting_hr:.0f} bpm")
    if hrv_recent_avg and hrv_baseline:
        dev = (hrv_recent_avg - hrv_baseline) / hrv_baseline
        parts.append((clamp(60 + dev * 200), 0.3))
        descs.append(f"HRV {hrv_recent_avg:.0f}（基线 {hrv_baseline:.0f}）")
    if sleep_recent_avg:
        parts.append((clamp(sleep_recent_avg), 0.3))
        descs.append(f"睡眠 {sleep_recent_avg:.0f}")
    if not parts:
        return 50.0, "数据不足"
    tw = sum(w for _, w in parts)
    return clamp(sum(v * w for v, w in parts) / tw), " · ".join(descs)


def assess_ability(acts, runs_a, summary, today=None):
    max_hr = runs_a.get("max_hr_recorded") or (summary.get("heart_rate") or {}).get("max")
    resting_hr = (summary.get("heart_rate") or {}).get("resting")
    vo2 = summary.get("vo2_max")

    race = estimate_race_times(acts, max_hr, resting_hr, today)
    # 阈值配速：优先 10K，退而求 5K
    thr_sec = None
    if "10K" in race:
        thr_sec = race["10K"]["sec"] / 10.0
    elif "5K" in race:
        thr_sec = race["5K"]["sec"] / 5.0
    pace_zones = derive_pace_zones(thr_sec)
    hr_zones = derive_hr_zones(max_hr, resting_hr)

    # ── 四个维度评分(0-100) ──
    # 1) 有氧底盘：周里程 + ACWR 最佳区间
    chronic = runs_a.get("chronic_weekly_km") or 0
    acwr = summary.get("training_load", {}).get("ratio") or runs_a.get("acwr_volume")
    m = chronic
    aerobic_mileage = clamp(40 + (m - 20) * 1.3) if m > 20 else clamp(m * 2.0)
    acwr_bonus = 0
    if acwr:
        if 0.8 <= acwr <= 1.3:
            acwr_bonus = 15
        elif acwr < 0.8:
            acwr_bonus = 5
        else:
            acwr_bonus = -10
    s_aerobic = clamp(aerobic_mileage + acwr_bonus)

    # 2) 速度 / 阈值：VO2 Max 为主，配速为辅
    vo2_score = None
    if vo2 is not None:
        vo2_score = clamp((vo2 - 30) / (60 - 30) * 100)
    pace_score = None
    if thr_sec:
        # 阈值配速越快得分越高：6:00(360s)->80, 5:00(300s)->92, 4:30(270)->100
        pace_score = clamp(100 - (thr_sec - 270) * 0.4)
    s_speed = clamp(_mean([vo2_score, pace_score]) or 50)

    # 3) 训练规律性：周均次数 + 月里程波动惩罚
    rpw = runs_a.get("runs_per_week") or 0
    reg_freq = clamp(rpw * 20)
    cv = runs_a.get("weekly_dist_cv") or 0
    cv_penalty = clamp(cv * 0.3, 0, 20)
    s_regularity = clamp(reg_freq - cv_penalty)

    # 4) 恢复能力：静息心率 (+ 下游用 HRV/睡眠 联合修正)
    s_recovery, recovery_desc = recovery_score(resting_hr)

    composite = round(s_aerobic * 0.30 + s_speed * 0.25 + s_regularity * 0.20 + s_recovery * 0.25)
    grade = ("S" if composite >= 85 else "A" if composite >= 75 else
             "B" if composite >= 65 else "C" if composite >= 55 else "D")

    dims = [
        {"key": "aerobic", "name": "有氧底盘", "score": round(s_aerobic),
         "desc": f"周均里程 {chronic:.0f}km，ACWR {acwr}"},
        {"key": "speed", "name": "速度/阈值", "score": round(s_speed),
         "desc": f"VO2 Max {vo2}，阈值配速 {sec_to_pace(thr_sec)}/km" if thr_sec else "—"},
        {"key": "regularity", "name": "训练规律性", "score": round(s_regularity),
         "desc": f"周均 {rpw} 次，周里程波动 CV {cv:.0f}%"},
        {"key": "recovery", "name": "恢复能力", "score": round(s_recovery),
         "desc": recovery_desc},
    ]
    return {
        "race_estimates": race,
        "threshold_pace_sec": round(thr_sec) if thr_sec else None,
        "threshold_pace": sec_to_pace(thr_sec),
        "pace_zones": pace_zones,
        "hr_zones": hr_zones,
        "dimensions": dims,
        "composite_score": composite,
        "grade": grade,
    }


# ─────────────────────────────── 当前状态信号（修 bug #1） ───────────────────────────────
def analyze_current_state(runs_a, sleep_recs, sleep_avg, health_recs, health_sum, summary, ability):
    ts = summary.get("training_status") or {}
    tl = summary.get("training_load") or {}
    vo2 = summary.get("vo2_max")
    rhr = (summary.get("heart_rate") or {}).get("resting")
    bb = summary.get("body_battery") or {}
    sleep_today = summary.get("sleep") or {}

    recent_sleep = sleep_recs[-7:] if len(sleep_recs) >= 7 else sleep_recs
    sleep_scores = [r.get("sleep_score") for r in recent_sleep if r.get("sleep_score")]
    sleep_recent_avg = round(_mean(sleep_scores), 1) if sleep_scores else None

    hrv_recent = [r.get("hrv", {}).get("last_night_avg") for r in health_recs[-7:]
                  if isinstance(r.get("hrv"), dict)]
    hrv_recent = [x for x in hrv_recent if x]
    hrv_recent_avg = round(_mean(hrv_recent), 1) if hrv_recent else None

    acwr_garmin = tl.get("ratio")
    acwr_status = tl.get("acwr_status")

    signals = []  # 对象数组（修 bug #1）：{name, desc, level}
    def add(name, desc, level):
        signals.append({"name": name, "desc": desc, "level": level})

    if acwr_garmin:
        if acwr_status == "OPTIMAL":
            add("训练负荷", f"ACWR {acwr_garmin:.1f} 处于最佳区间，身体适应良好", "good")
        elif acwr_status in ("HIGH", "VERY_HIGH"):
            add("训练负荷", f"ACWR {acwr_garmin:.1f} 偏高，注意过度训练风险", "warn")
        else:
            add("训练负荷", f"ACWR {acwr_garmin:.1f} 偏低/不足，可酌情增量", "info")
    if vo2:
        if vo2 >= 50:
            add("VO2 Max", f"{vo2:.0f}，处于业余跑者优秀水平", "good")
        elif vo2 >= 45:
            add("VO2 Max", f"{vo2:.0f}，良好", "good")
        else:
            add("VO2 Max", f"{vo2:.0f}，仍有提升空间", "info")
    if rhr:
        if rhr <= 45:
            add("静息心率", f"{rhr:.0f} bpm，心肺基础优秀、恢复底子好", "good")
        else:
            add("静息心率", f"{rhr:.0f} bpm", "info")
    if sleep_recent_avg:
        if sleep_recent_avg >= 75:
            add("睡眠评分", f"近7天均值 {sleep_recent_avg:.0f}，恢复质量不错", "good")
        elif sleep_recent_avg >= 60:
            add("睡眠评分", f"近7天均值 {sleep_recent_avg:.0f}，偏中等，有改善空间", "warn")
        else:
            add("睡眠评分", f"近7天均值 {sleep_recent_avg:.0f}，恢复不足，需重点关注", "warn")
    sleep_today_sec = sleep_today.get("total_seconds")
    if sleep_today_sec and sleep_today_sec < 6 * 3600:
        add("昨夜睡眠", f"仅 {sleep_today_sec/3600:.1f} 小时，明显不足，今日宜低强度", "warn")
    bb_now = bb.get("most_recent")
    if bb_now is not None and bb_now <= 25:
        add("身体电量", f"当前仅 {bb_now}，整体偏疲劳", "warn")

    # ── 关键：能力分（composite_score / grade）在 assess_ability 已基于稳定的历史性能
    # 维度（有氧底盘 / 速度 / 规律性 / 静息心率）算好，**不在此用每日 HRV/睡眠重算**——
    # 否则昨夜睡眠 59、电量 12 这类单日波动会让评级一天掉一级（如 09-22 B → 09-23 C）。
    # 每日恢复信号只作为「状态分」（compute_readiness 的 score）与下方 signals 展示。

    # ── 个人基线（P2d：状态卡偏离提示）──
    hrv_all = [r.get("hrv", {}).get("last_night_avg") for r in health_recs[-60:]
               if isinstance(r.get("hrv"), dict) and r.get("hrv", {}).get("last_night_avg")]
    hrv_baseline = round(_mean(hrv_all), 1) if hrv_all else None
    sleep_all = [r.get("sleep_score") for r in sleep_recs[-60:] if r.get("sleep_score")]
    sleep_baseline = round(_mean(sleep_all), 1) if sleep_all else None
    rhr_all = [r.get("resting_heart_rate") for r in health_recs[-60:]
               if r.get("resting_heart_rate")]
    if not rhr_all:
        rhr_all = [r.get("resting_heart_rate") for r in sleep_recs[-60:]
                   if r.get("resting_heart_rate")]
    rhr_baseline = round(_mean(rhr_all), 1) if rhr_all else None

    # ── TSB / Form（P1c：今天能不能硬）── chronic - acute
    acute_load = tl.get("acute_load"); chronic_load = tl.get("chronic_load")
    form_tsb = (round(chronic_load - acute_load)
                if (acute_load is not None and chronic_load is not None)
                else round((runs_a.get("chronic_weekly_km") or 0) - (runs_a.get("acute_weekly_km") or 0)))
    if form_tsb >= 15:
        form_label = "状态充沛（可冲强度）"
    elif form_tsb >= 0:
        form_label = "恢复良好（训练窗口佳）"
    elif form_tsb >= -15:
        form_label = "中性（保持节奏）"
    elif form_tsb >= -30:
        form_label = "疲劳累积（宜降量）"
    else:
        form_label = "过度训练风险（强制恢复）"

    return {
        "training_status_label": ts.get("label"),
        "training_status_since": ts.get("since_date"),
        "vo2_max": vo2,
        "resting_hr": rhr,
        "acwr_garmin": acwr_garmin,
        "acwr_status": acwr_status,
        "acute_load": tl.get("acute_load"),
        "chronic_load": tl.get("chronic_load"),
        "sleep_recent_avg_score": sleep_recent_avg,
        "hrv_recent_avg": hrv_recent_avg,
        "body_battery_now": bb_now,
        "body_battery_high": bb.get("highest"),
        "sleep_today_seconds": sleep_today_sec,
        "intensity_minutes_week": (summary.get("intensity_minutes") or {}).get("total"),
        "intensity_minutes_goal": (summary.get("intensity_minutes") or {}).get("goal"),
        "today_distance_km": summary.get("distance_km"),
        "today_steps": summary.get("steps"),
        "last_sync": summary.get("last_sync"),
        "hrv_baseline": hrv_baseline,
        "sleep_baseline": sleep_baseline,
        "rhr_baseline": rhr_baseline,
        "form_tsb": form_tsb,
        "form_label": form_label,
        "signals": signals,
    }


# ─────────────────────────────── 就绪度（共享：课表与整周调整共用） ───────────────────────────────
def compute_readiness(runs_a, state, summary, today=None):
    today = today or datetime.now().strftime("%Y-%m-%d")
    last_run = runs_a.get("last_date")
    days_since = None
    long_fatigue = False
    fatigue_run = None
    if last_run:
        try:
            days_since = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(last_run, "%Y-%m-%d")).days
        except Exception:
            days_since = None
        cutoff = datetime.strptime(today, "%Y-%m-%d") - timedelta(days=3)
        for a in sorted(runs_a.get("_acts", []), key=lambda x: x["date"]):
            if datetime.strptime(a["date"], "%Y-%m-%d") >= cutoff and (a.get("distance_km") or 0) >= 15:
                long_fatigue = True
                fatigue_run = a
                break
    score = 100
    factors = []
    h = (state.get("sleep_today_seconds") or 0) / 3600
    if state.get("sleep_today_seconds") is not None:
        if h < 5:
            score -= 30; factors.append({"name": "昨夜睡眠", "value": f"{h:.1f}h 严重不足", "level": "bad"})
        elif h < 6.5:
            score -= 15; factors.append({"name": "昨夜睡眠", "value": f"{h:.1f}h 偏少", "level": "warn"})
        else:
            factors.append({"name": "昨夜睡眠", "value": f"{h:.1f}h 充足", "level": "good"})
    if state.get("sleep_recent_avg_score") is not None:
        s7 = state["sleep_recent_avg_score"]
        if s7 < 65:
            score -= 15; factors.append({"name": "近期睡眠评分", "value": f"{s7:.0f} 偏低", "level": "warn"})
        elif s7 >= 75:
            factors.append({"name": "近期睡眠评分", "value": f"{s7:.0f} 良好", "level": "good"})
        else:
            factors.append({"name": "近期睡眠评分", "value": f"{s7:.0f} 中等", "level": "info"})
    if state.get("body_battery_now") is not None:
        bb = state["body_battery_now"]
        if bb <= 25:
            score -= 25; factors.append({"name": "身体电量", "value": f"当前 {bb} 偏低", "level": "warn"})
        elif bb >= 50:
            factors.append({"name": "身体电量", "value": f"当前 {bb} 良好", "level": "good"})
        else:
            factors.append({"name": "身体电量", "value": f"当前 {bb}", "level": "info"})
    if state.get("acwr_garmin") is not None:
        ac = state["acwr_garmin"]
        if ac > 1.3:
            score -= 10; factors.append({"name": "训练负荷", "value": f"ACWR {ac:.1f} 偏高", "level": "warn"})
        elif ac < 0.8:
            factors.append({"name": "训练负荷", "value": f"ACWR {ac:.1f} 可加量", "level": "good"})
        else:
            factors.append({"name": "训练负荷", "value": f"ACWR {ac:.1f} 最佳", "level": "good"})
    if state.get("training_status_label"):
        lbl = state["training_status_label"]
        if lbl in ("Recovery", "Overreaching"):
            score -= 10
        factors.append({"name": "训练状态", "value": lbl, "level": "info"})
    if state.get("today_distance_km") and state["today_distance_km"] > 15:
        score -= 15; factors.append({"name": "今日已跑", "value": f"{state['today_distance_km']}km 已冲量", "level": "warn"})
    score = round(clamp(score, 0, 100))
    return score, factors, long_fatigue, fatigue_run, days_since


# ─────────────────────────────── 目标导向 / 作息 / 周期化 整周课表 ───────────────────────────────
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def duty_type(date_str, anchor, pattern):
    """上二休二：主班日(不训练) / 备班日(可训练) / 休息日(可训练)。"""
    if pattern != "2on2off":
        return "rest"
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        a = datetime.strptime(anchor, "%Y-%m-%d")
        off = ((d - a).days % 4 + 4) % 4
        return ("main", "standby", "rest", "rest")[off]
    except Exception:
        return "rest"


def is_blocked(date_str, anchor, pattern):
    """只有主班日禁训；备班日与休息日均可安排训练。"""
    return duty_type(date_str, anchor, pattern) == "main"


def phase_for(race_date, today):
    if not race_date:
        return "build_perm", "永续强化期", None
    try:
        rd = datetime.strptime(race_date, "%Y-%m-%d")
        td = datetime.strptime(today, "%Y-%m-%d")
        wk = (rd - td).days / 7.0
        if wk > 12:
            return "base", "基础期(打底盘)", round(wk)
        if wk > 6:
            return "build", "强化期", round(wk)
        if wk > 3:
            return "peak", "巅峰期(锐化)", round(wk)
        if wk > 0:
            return "taper", "减量期", round(wk)
        return "race", "比赛周", 0
    except Exception:
        return "build_perm", "永续强化期", None


def _resolve_ranges(pace_key, hr_name, ability):
    pz = (ability or {}).get("pace_zones") or {}
    hz = (ability or {}).get("hr_zones") or []
    pr = "—"
    if pace_key and pace_key in pz:
        pr = pz[pace_key]["pace_lo"] + "–" + pz[pace_key]["pace_hi"]
    hr = "—"
    for z in hz:
        if z["name"] == hr_name:
            hr = f"{z['lo']}–{z['hi']}"; break
    return pr, hr


def _mk_sess(sid, name, pace_key, hr_name, duration, rationale, structure, priority, ability):
    pr, hr = _resolve_ranges(pace_key, hr_name, ability)
    return {"id": sid, "name": name, "pace_key": pace_key, "hr_name": hr_name,
            "pace_range": pr, "hr_range": hr, "duration": duration,
            "rationale": rationale, "structure": structure, "priority": priority}


def session_templates(goal, phase, ability):
    """按目标 + 周期阶段返回训练模板（已按优先级排序：1=核心）。"""
    if goal == "half_marathon_pb":
        base = [
            _mk_sess("T", "阈值(短)", "T", "Z4 阈值", "约 45 分钟", "拉升乳酸阈值，强度适中",
                     [("热身", "10 分钟慢跑 + 动态拉伸"), ("主项", "15 分钟 @ 阈值配速(T)，心率 Z4，匀速"), ("冷身", "5–10 分钟慢走 + 拉伸")], 1, ability),
            _mk_sess("L", "长距离(保守)", "E", "Z2 有氧", "70–90 分钟 / 14–16km", "积累有氧底盘与耐力，慢而稳",
                     [("热身", "10 分钟慢跑"), ("主项", "60–80 分钟 轻松跑，配速 E 区、心率 Z2"), ("冷身", "5–10 分钟慢走 + 拉伸")], 1, ability),
            _mk_sess("FL", "法特莱克(轻速度)", "I", "Z4 阈值", "约 50 分钟", "轻量速度刺激，提升跑感与经济性",
                     [("热身", "10 分钟慢跑"), ("主项", "8×(1 分钟稍快 + 1 分钟慢跑)，配速接近 I 区"), ("冷身", "5 分钟慢走 + 拉伸")], 2, ability),
            _mk_sess("E", "轻松有氧", "E", "Z2 有氧", "40–50 分钟", "恢复与有氧基础，低强度",
                     [("热身", "5 分钟慢走/慢跑"), ("主项", "30–40 分钟 轻松跑，配速 E 区、心率 Z2"), ("冷身", "5 分钟拉伸")], 3, ability),
        ]
        if phase in ("base", "build_perm"):
            return base
        if phase == "build":
            return [
                _mk_sess("I", "间歇 I", "I", "Z5 无氧", "约 55 分钟", "提升最大摄氧与速度耐力",
                         [("热身", "15 分钟慢跑 + 4×100m 加速"), ("主项", "6×800m @ I 配速，间歇慢跑 400m 恢复"), ("冷身", "10 分钟慢走 + 拉伸")], 1, ability),
                _mk_sess("T", "阈值/节奏 T", "T", "Z4 阈值", "约 50 分钟", "拉升乳酸阈值，适应比赛节奏",
                         [("热身", "15 分钟慢跑 + 动态拉伸"), ("主项", "20–25 分钟 @ 阈值配速(T)，心率 Z3–Z4"), ("冷身", "10 分钟慢走 + 拉伸")], 1, ability),
                _mk_sess("L", "长距离 L", "M", "Z3 马拉松", "90–120 分钟 / 16–20km", "耐力底盘 + 比赛配速适应",
                         [("热身", "10 分钟慢跑"), ("主项", "80–110 分钟，配速 M 区、心率 Z3，后半可含匀速段落"), ("冷身", "10 分钟慢走 + 拉伸")], 1, ability),
                _mk_sess("E", "轻松有氧", "E", "Z2 有氧", "40–50 分钟", "恢复与有氧基础，低强度",
                         [("热身", "5 分钟慢走/慢跑"), ("主项", "30–40 分钟 轻松跑，配速 E 区、心率 Z2"), ("冷身", "5 分钟拉伸")], 3, ability),
            ]
        if phase == "peak":
            return [
                _mk_sess("I", "间歇(短)", "I", "Z5 无氧", "约 45 分钟", "维持速度，缩短量保刺激",
                         [("热身", "15 分钟慢跑"), ("主项", "5×600m @ I 配速，间歇慢跑 300m 恢复"), ("冷身", "10 分钟慢走 + 拉伸")], 1, ability),
                _mk_sess("T", "阈值含比赛配速", "T", "Z4 阈值", "约 50 分钟", "3×2km @ 比赛配速(M) 段落，模拟比赛",
                         [("热身", "15 分钟慢跑"), ("主项", "3×2km @ M 配速(心率 Z3–Z4)，间歇慢跑 1km"), ("冷身", "10 分钟慢走 + 拉伸")], 1, ability),
                _mk_sess("L", "长距离含 M 配速", "M", "Z3 马拉松", "80–100 分钟 / 16–18km", "中段含比赛配速适应，模拟后半程",
                         [("热身", "10 分钟慢跑"), ("主项", "前 8km E 区，中段 6–8km @ M 配速，后段 E 区"), ("冷身", "10 分钟慢走 + 拉伸")], 1, ability),
                _mk_sess("E", "轻松有氧", "E", "Z2 有氧", "35–45 分钟", "恢复",
                         [("热身", "5 分钟慢跑"), ("主项", "30–40 分钟 轻松跑，心率 Z2"), ("冷身", "5 分钟拉伸")], 3, ability),
            ]
        if phase == "taper":
            return [
                _mk_sess("I", "间歇(短促)", "I", "Z5 无氧", "约 30 分钟", "维持刺激，量大幅减少",
                         [("热身", "10 分钟慢跑"), ("主项", "4×400m @ I 配速，慢跑恢复"), ("冷身", "8 分钟慢走 + 拉伸")], 1, ability),
                _mk_sess("T", "阈值(短)", "T", "Z4 阈值", "约 30 分钟", "保持打开，不疲劳",
                         [("热身", "10 分钟慢跑"), ("主项", "10 分钟 @ T 配速"), ("冷身", "8 分钟慢走 + 拉伸")], 1, ability),
                _mk_sess("L", "长距离(缩短)", "E", "Z2 有氧", "50–60 分钟 / 10km", "保持状态，充分恢复",
                         [("热身", "8 分钟慢跑"), ("主项", "40–50 分钟 轻松跑，心率 Z2"), ("冷身", "8 分钟拉伸")], 1, ability),
                _mk_sess("E", "轻松有氧", "E", "Z2 有氧", "30–40 分钟", "恢复",
                         [("热身", "5 分钟慢跑"), ("主项", "25–35 分钟 轻松跑，心率 Z2"), ("冷身", "5 分钟拉伸")], 3, ability),
            ]
        return []  # race week: 不强制
    # 其他目标暂回落到半马 build 模板
    return session_templates("half_marathon_pb", "build_perm", ability)


def _adjust_session(session, readiness_score, long_fatigue, ability):
    e_short = _mk_sess("E", "轻松恢复(下调)", "E", "Z2 有氧", "30–40 分钟", "原定强度课因恢复不足下调为轻松恢复",
                       [("热身", "5 分钟慢走/慢跑"), ("主项", "25–35 分钟 轻松跑，心率 Z1–Z2"), ("冷身", "5 分钟拉伸")], 1, ability)
    if long_fatigue:
        return e_short, "downgraded", "近 3 天有长距离，处于恢复窗口，已下调为轻松恢复"
    sid = session["id"]
    if readiness_score < 40:
        if sid in ("I", "T", "L", "FL"):
            return e_short, "downgraded", "恢复不足（就绪度<40），强度课下调为轻松恢复"
        return session, "kept", ""
    if readiness_score < 65:
        if sid in ("I", "T", "FL"):
            e_easy = _mk_sess("E", "轻松有氧(下调)", "E", "Z2 有氧", "40–50 分钟", "原定质量课因状态中等下调为轻松有氧",
                              [("热身", "5 分钟慢跑"), ("主项", "30–40 分钟 轻松跑，心率 Z2"), ("冷身", "5 分钟拉伸")], 3, ability)
            return e_easy, "downgraded", "状态中等（就绪度<65），质量课下调为轻松有氧"
        if sid == "L":
            short = dict(session)
            short["name"] = session["name"] + "(缩短)"
            short["duration"] = "缩短至 50–60 分钟"
            short["rationale"] = session["rationale"] + "；状态中等，里程缩短"
            short["structure"] = [("热身", "8 分钟慢跑"), ("主项", "40–50 分钟 轻松/匀速跑，心率 Z2–Z3"), ("冷身", "8 分钟拉伸")]
            return short, "downgraded", "状态中等，长距离缩短里程"
        return session, "kept", ""
    if sid == "E" and readiness_score >= 80:
        t = _mk_sess("T", "阈值(小幅加成)", "T", "Z4 阈值", "约 30 分钟", "状态佳，在轻松日加一段阈值刺激",
                     [("热身", "10 分钟慢跑"), ("主项", "15 分钟 @ T 配速"), ("冷身", "5 分钟拉伸")], 1, ability)
        return t, "upgraded", "状态佳（就绪度≥80），轻松日小幅加成阈值刺激"
    return session, "kept", ""


def _spread_key(quality_days, all_days):
    """排课打分：优先让质量课之间的最小间隔最大，其次整体间隔。"""
    qg = [quality_days[i + 1] - quality_days[i] for i in range(len(quality_days) - 1)]
    ag = [all_days[i + 1] - all_days[i] for i in range(len(all_days) - 1)]
    return (min(qg) if qg else 0, min(ag) if ag else 0, sum(ag))


def _pick_training_days(train_days, n, n_quality=0):
    """在可选训练日里挑 n 天，并决定哪几天放质量课。
    穷举所有组合（一周最多 7 天，组合数极小），打分见 _spread_key：
    先保证**质量课之间**的最小间隔尽量大（≥48h 的恢复窗口），再让整体间隔均匀。

    返回 [(日期, "quality"|"easy")]，按日期升序。

    比旧实现（`round(i*(len-1)/(n-1))` 等间隔取点 + 按顺序填模板）好在：
      - n == 1 时不会除零崩溃（旧实现 runs_per_week=1 直接 ZeroDivisionError）；
      - 去重逻辑不再可能"少排一次课"；
      - 轻松跑会主动去填空位，而不是把长距离和法特莱克挤到相邻两天。
    """
    if not train_days or n <= 0:
        return []
    n = min(int(n), len(train_days))
    q = max(0, min(int(n_quality or 0), n))
    if n == len(train_days):
        return [(d, "quality" if i < q else "easy") for i, d in enumerate(train_days)]
    best, best_key = None, None
    for days in itertools.combinations(range(len(train_days)), n):
        for qd in (itertools.combinations(days, q) if q else [()]):
            key = _spread_key(qd, days)
            if best_key is None or key > best_key:
                best_key, best = key, (days, set(qd))
    days, qset = best
    return [(train_days[i], "quality" if i in qset else "easy") for i in days]


def assign_week(week_start, cfg, ability, phase_key):
    """纯模板排课（不含今日就绪度 / 实际完成调整），返回 {date: session}。

    整周视图与实际完成回看共用同一套排课逻辑，保证"昨天计划了什么"可被稳定重建。"""
    anchor = cfg.get("work_anchor")
    pattern = cfg.get("work_pattern")
    n = int(cfg.get("runs_per_week", 4))
    templates = sorted(session_templates(cfg.get("goal"), phase_key, ability),
                       key=lambda s: s["priority"])[:n]
    week_dates = [(week_start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    # 训练可选日 = 非主班日（备班日 / 休息日 均可训练）
    train_days = [d for d in week_dates if not is_blocked(d, anchor, pattern)]
    n_quality = sum(1 for s in templates if s.get("priority", 9) <= 2)
    slots = _pick_training_days(train_days, n, n_quality)
    # 质量课占用被挑中的"质量日"，轻松跑去填空位
    quality_tpl = [s for s in templates if s.get("priority", 9) <= 2]
    easy_tpl = [s for s in templates if s.get("priority", 9) > 2]
    assign = {}
    qi = ei = 0
    for date_str, kind in slots:
        if kind == "quality" and qi < len(quality_tpl):
            assign[date_str] = quality_tpl[qi]; qi += 1
        elif ei < len(easy_tpl):
            assign[date_str] = easy_tpl[ei]; ei += 1
        elif qi < len(quality_tpl):
            assign[date_str] = quality_tpl[qi]; qi += 1
    return assign


# 实际完成 vs 计划：强度分级（rest < easy < long < quality）
_SESS_ORD = {"rest": 0, "E": 1, "L": 2, "FL": 3, "T": 3, "I": 3}


def _infer_intensity(run, ability):
    """从一次实际跑步推断强度档：0 没跑 / 1 轻松 / 2 长距离 / 3 质量(阈值·间歇)。"""
    if not run:
        return 0
    dist = run.get("distance_km") or 0
    if dist >= 15:
        return 2
    tp = ability.get("threshold_pace")
    tp_sec = pace_to_sec(tp) if tp else None
    pace = pace_to_sec(run.get("pace_formatted"))
    if pace is None and dist > 0 and run.get("duration_sec"):
        pace = run["duration_sec"] / dist
    # 配速接近/快于阈值配速（≤1.12×）→ 视为质量课
    if tp_sec and pace and pace <= tp_sec * 1.12:
        return 3
    return 1


def _planned_id_for_date(date_str, cfg, ability, phase_key):
    """重建某天的计划课（纯模板，不含今日调整）。"""
    try:
        dd = datetime.strptime(date_str, "%Y-%m-%d")
        ws = dd - timedelta(days=dd.weekday())
        cell = assign_week(ws, cfg, ability, phase_key).get(date_str)
        return cell["id"] if cell else "rest"
    except Exception:
        return "rest"


def build_actual_ctx(today, acts, cfg, ability, phase_key, window=3):
    """回看近 window 天：每天「计划强度档」vs「实际强度档」，给出两个信号：
       - overreach    : 有某天实际比计划更狠（计划轻松/休息却跑了质量/长距离）——今日应偏保守。
       - missed_quality: 有某天计划质量/长距离却只跑了轻松/没跑——本周质量课偏少，可在可训练日补。
    同时保留逐日明细供看板展示「实际 vs 计划」。"""
    td = datetime.strptime(today, "%Y-%m-%d")
    overreach = False
    missed_quality = False
    days = []
    by_date = {}
    for a in acts:
        d = (a.get("date") or "")[:10]
        by_date.setdefault(d, []).append(a)
    for k in range(1, window + 1):
        d = (td - timedelta(days=k)).strftime("%Y-%m-%d")
        planned_id = _planned_id_for_date(d, cfg, ability, phase_key)
        actual_runs = by_date.get(d, [])
        actual = actual_runs[0] if actual_runs else None
        actual_ord = _infer_intensity(actual, ability)
        planned_ord = _SESS_ORD.get(planned_id, 0)
        if planned_ord <= 1 and actual_ord >= 2:
            overreach = True
        if planned_ord >= 2 and actual_ord <= 1:
            missed_quality = True
        days.append({
            "date": d,
            "planned": planned_id,
            "planned_label": {"rest": "休息", "E": "轻松", "L": "长距离", "FL": "法特莱克",
                              "T": "阈值", "I": "间歇"}.get(planned_id, planned_id),
            "actual": (actual.get("name") if actual else "（无记录）"),
            "actual_distance_km": (actual.get("distance_km") if actual else 0),
            "actual_pace": (actual.get("pace_formatted") if actual else None),
            "actual_ord": actual_ord,
            "planned_ord": planned_ord,
            "delta": actual_ord - planned_ord,
        })
    return {"overreach": overreach, "missed_quality": missed_quality,
            "window": window, "days": days}


def _apply_actual(ctx, planned, adjusted, change, reason, ability, readiness_score):
    """在就绪度调整之后，再根据「近几日实际完成 vs 计划」叠加一层调整。返回 (adjusted, change, reason)。
    主班日(禁训)绝不被改成训练——调用方已保证只在可训练日传入 planned。"""
    base = adjusted or planned
    if base is None:
        return adjusted, change, reason
    sid = base["id"]
    w = ctx.get("window", 3)
    # 安全优先：近几日有超出计划的强度/量，今日质量课下调为轻松恢复
    if ctx.get("overreach") and sid in ("I", "T", "L", "FL"):
        e = _mk_sess("E", "轻松恢复(防过度)", "E", "Z2 有氧", "30–40 分钟",
                     "检测到近几日有超出计划的强度/里程，今日下调为轻松恢复",
                     [("热身", "5 分钟慢走/慢跑"), ("主项", "25–35 分钟 轻松跑，心率 Z1–Z2"), ("冷身", "5 分钟拉伸")], 1, ability)
        return e, "downgraded", f"实际完成超出计划（近{w}天有额外强度/量），今日下调为轻松恢复以防护"
    # 补课：本周有质量课被轻松跑/休息替代，今天本是轻松日且状态允许 → 升级为一次阈值补课
    if (ctx.get("missed_quality") and not ctx.get("overreach")
            and sid == "E" and change == "kept" and (readiness_score or 0) >= 60):
        t = _mk_sess("T", "阈值(补课)", "T", "Z4 阈值", "约 30 分钟",
                     "近几日质量课被轻松跑/休息替代，今天状态允许补一次阈值刺激",
                     [("热身", "10 分钟慢跑"), ("主项", "15 分钟 @ T 配速(心率 Z4)"), ("冷身", "5 分钟拉伸")], 1, ability)
        return t, "upgraded", f"近{w}天有质量课未完成（实际只跑了轻松/没跑），今天状态允许可补一次质量课"
    return adjusted, change, reason


def build_week(week_start, cfg, ability, phase_key, today, readiness_score, long_fatigue, actual_ctx=None, acts=None):
    anchor = cfg.get("work_anchor")
    pattern = cfg.get("work_pattern")
    assign = assign_week(week_start, cfg, ability, phase_key)
    week_dates = [(week_start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    today_dt = datetime.strptime(today, "%Y-%m-%d")
    by_date = {}
    for a in (acts or []):
        by_date.setdefault((a.get("date") or "")[:10], []).append(a)
    days = []
    for i, d in enumerate(week_dates):
        wd = (week_start + timedelta(days=i)).weekday()
        duty = duty_type(d, anchor, pattern)
        blocked = duty == "main"
        planned = assign.get(d)
        adjusted = None; change = "none"; reason = ""
        actual_run = by_date.get(d, [None])[0] if by_date.get(d) else None
        actual = None
        if actual_run:
            actual = {"distance_km": actual_run.get("distance_km"),
                      "pace": actual_run.get("pace_formatted"),
                      "name": actual_run.get("name"),
                      "id": actual_run.get("activity_id")}
        is_past = d < today
        if d == today and planned:
            adjusted, change, reason = _adjust_session(planned, readiness_score, long_fatigue, ability)
            if actual_ctx is not None:
                adjusted, change, reason = _apply_actual(actual_ctx, planned, adjusted, change, reason, ability, readiness_score)
        days.append({"date": d, "weekday": WEEKDAY_CN[wd], "duty": duty, "blocked": blocked,
                     "planned": planned, "adjusted": adjusted, "change": change,
                     "reason": reason, "is_today": d == today, "is_past": is_past,
                     "actual": actual, "actual_ctx": (actual_ctx if d == today else None)})
    # 本周级“补质量课”：过去有计划的 质量/长距离 被实际轻松/没跑替代 → 把最近一个可训练轻松日升级为阈值补课
    week_missed = 0
    for c in days:
        if c["is_past"] and c["planned"]:
            po = _SESS_ORD.get(c["planned"]["id"], 0)
            ao = _infer_intensity(c["actual"], ability) if c["actual"] else 0
            if po >= 2 and ao <= 1:
                week_missed += 1
    if week_missed > 0:
        for c in days:
            if (not c["is_past"] or c["date"] == today) and c["planned"] and c["planned"]["id"] == "E" and c["change"] == "none":
                c["adjusted"], c["change"], c["reason"] = (
                    _mk_sess("T", "阈值(补课)", "T", "Z4 阈值", "约 30 分钟",
                             "本周已漏 %d 次质量/长距离课，把近期轻松日补成一次阈值刺激" % week_missed,
                             [("热身", "10 分钟慢跑"), ("主项", "15 分钟 @ T 配速(心率 Z4)"), ("冷身", "5 分钟拉伸")], 1, ability),
                    "upgraded", "本周有质量课未按计划完成，已把该轻松日改为阈值补课")
                break
    return days


def compute_plan(today_cell, score, factors, long_fatigue, fatigue_run, days_since, state):
    """由整周课表中“今天”的格子，生成今日详细课表卡片。"""
    today = (today_cell or {}).get("date")
    sess = (today_cell or {}).get("adjusted") or (today_cell or {}).get("planned")
    if sess:
        change = (today_cell or {}).get("change", "none")
        note = sess["rationale"]
        if change == "downgraded":
            note += "（已根据今日恢复数据下调）"
        elif change == "upgraded":
            note += "（已根据今日状态上调）"
        return {
            "date": today, "readiness_score": score, "session_type": sess["id"],
            "long_fatigue": long_fatigue, "days_since_last_run": days_since, "fatigue_run": fatigue_run,
            "title": sess["name"], "target_pace": sess["pace_range"], "target_hr": sess["hr_range"],
            "duration": sess["duration"], "structure": sess["structure"], "note": note, "factors": factors,
        }
    rest_note = "今天没有安排训练（主班日或休息日）。以恢复为主，可做轻微活动，不要加量。"
    ctx = (today_cell or {}).get("actual_ctx")
    if ctx and (ctx.get("missed_quality") or ctx.get("overreach")):
        extra = []
        if ctx.get("missed_quality"):
            extra.append("本周有质量课未按计划完成（实际只跑了轻松/没跑），可在备班或休息日补一次阈值课")
        if ctx.get("overreach"):
            extra.append("近几日有超出计划的强度/里程，今天以恢复为主更稳妥")
        rest_note = "今天没有安排训练（主班日或休息日）。" + "；".join(extra) + "。"
    return {
        "date": today, "readiness_score": score, "session_type": "rest",
        "long_fatigue": long_fatigue, "days_since_last_run": days_since, "fatigue_run": fatigue_run,
        "title": "休息日 / 主动恢复", "target_pace": "—", "target_hr": "—",
        "duration": "完全休息 或 20–30 分钟轻松活动",
        "structure": [("可选活动", "瑜伽/散步/泡沫轴放松，促进血液循环")],
        "note": rest_note,
        "factors": factors,
    }


# ─────────────────────────────── 跑后全维度分析 ───────────────────────────────
def _build_post_run_one(detail, acts, aid=None):
    d = detail or {}
    if aid is None and detail:
        aid = str(d.get("activity_id"))
    rec = next((a for a in acts if str(a.get("activity_id")) == aid), None) if aid else None
    if not (detail or rec):
        return None

    def pick(k, default=None):
        v = d.get(k)
        if v is None and rec:
            v = rec.get(k)
        return v if v is not None else default

    distance_km = pick("distance_km")
    pace_sec = pace_to_sec(d.get("pace_formatted")) or pace_to_sec(pick("pace_formatted"))
    avg_hr = pick("avg_hr")
    max_hr = pick("max_hr")
    duration_sec = pick("duration_sec")
    aerobic_te = pick("aerobic_te")
    anaerobic_te = pick("anaerobic_te")
    calories = pick("calories")
    elevation_gain = pick("elevation_gain")
    avg_cadence = pick("avg_cadence")
    training_load = pick("training_load")
    vo2_max = pick("vo2_max")
    date = (d.get("date") or (rec or {}).get("date") or "")[:10]

    # 分段（每公里）
    laps = d.get("laps") or []
    splits = []
    for lap in laps:
        lm = lap.get("distance_m") or 0
        lp = pace_to_sec(lap.get("pace_formatted"))
        splits.append({
            "km": round(lm / 1000.0, 2),
            "pace_sec": lp,
            "pace": lap.get("pace_formatted"),
            "hr": lap.get("avg_hr"),
            "cadence": lap.get("cadence"),
            "full_km": lm >= 900,
        })

    full = [s for s in splits if s["full_km"] and s["pace_sec"]]
    pace_vals = [s["pace_sec"] for s in full]
    pace_mean = _mean(pace_vals) if pace_vals else None
    pace_cv = (_std(pace_vals) / pace_mean * 100) if pace_mean else None
    pace_fastest = min(pace_vals) if pace_vals else None
    pace_slowest = max(pace_vals) if pace_vals else None

    # 心率漂移（前后半程）
    drift = None
    if len(full) >= 2 and full[0]["hr"] and full[-1]["hr"]:
        half = len(full) // 2
        hr1 = _mean([s["hr"] for s in full[:half] if s["hr"]])
        hr2 = _mean([s["hr"] for s in full[half:] if s["hr"]])
        if hr1:
            drift = round((hr2 - hr1) / hr1 * 100, 1)

    # 心率区间分布
    hr_zones_raw = d.get("hr_zones") or []
    total_zone_sec = sum(z.get("secsInZone", 0) for z in hr_zones_raw) or (duration_sec or 0)
    zone_names = {1: "Z1 恢复", 2: "Z2 有氧", 3: "Z3 马拉松", 4: "Z4 阈值", 5: "Z5 无氧"}
    hr_zone_dist = []
    for z in hr_zones_raw:
        secs = z.get("secsInZone", 0)
        hr_zone_dist.append({
            "zone": z.get("zoneNumber"),
            "name": zone_names.get(z.get("zoneNumber"), f"Z{z.get('zoneNumber')}"),
            "low": z.get("zoneLowBoundary"),
            "secs": round(secs),
            "pct": round(secs / total_zone_sec * 100, 1) if total_zone_sec else 0,
        })

    # 跑姿 / 形态
    form = {"cadence_avg": avg_cadence, "cadence_min": None, "cadence_max": None,
            "stride_est_m": None, "running_dynamics": d.get("running_dynamics")}
    cad_vals = [s["cadence"] for s in full if s["cadence"]]
    if cad_vals:
        form["cadence_min"] = min(cad_vals)
        form["cadence_max"] = max(cad_vals)
    if pace_mean and avg_cadence:
        # 步幅(每步) = 速度(m/min) / 步频(spm)；速度 m/min = (1000m / 配速秒) × 60
        speed_m_per_min = 1000.0 / pace_mean * 60.0
        form["stride_est_m"] = round(speed_m_per_min / avg_cadence, 2)

    # 训练效果解读
    def te_text(v, kind):
        if v is None:
            return None
        if v < 1:
            lvl = "无效"
        elif v < 2:
            lvl = "维持"
        elif v < 3:
            lvl = "小幅提升"
        elif v < 4:
            lvl = "有效提升"
        elif v < 5:
            lvl = "显著提升"
        else:
            lvl = "过度"
        return f"{kind}训练效果 {v:.1f}（{lvl}）"

    te_a = te_text(aerobic_te, "有氧")
    te_an = te_text(anaerobic_te, "无氧")

    # ── 数据驱动的训练建议（强化版：分项解读 + 如何跑得更好）──
    suggestions = []
    rd = d.get("running_dynamics") or {}
    # 1) 配速一致性
    if pace_cv is not None:
        if pace_cv <= 2:
            suggestions.append(f"配速一致性极佳（CV {pace_cv:.1f}%）：匀速能力强，比赛可直接执行“负分割”（后程比前程快 5–10 秒/km）而不崩。")
        elif pace_cv <= 4:
            suggestions.append(f"配速较稳（CV {pace_cv:.1f}%），仍有波动。练法：每公里盯表把误差控在 ±5 秒；长距离后半段主动压心率、不抢配速。")
        else:
            suggestions.append(f"分段配速波动大（CV {pace_cv:.1f}%），说明配速靠感觉。练法：跑步机定速跑 / 节拍器，把每公里误差练到 ±5 秒以内。")
    # 2) 心率漂移
    if drift is not None:
        if drift <= 2:
            suggestions.append(f"心率漂移仅 {drift}%：有氧底盘扎实，脂肪供能占比高，马拉松后程不易崩，继续保持堆量。")
        elif drift <= 5:
            suggestions.append(f"心率漂移 {drift}% 属正常区间：继续把 80% 跑量放在能边跑边聊天的 Z2，拉长慢肌耐力。")
        else:
            suggestions.append(f"心率漂移 {drift}%（前半 HR 高、后程更高）：有氧基础偏弱或起跑过快。练法：起跑前 2km 故意慢 10–15 秒/km；Z2 长跑后半段主动把心率压低 3–5 bpm。")
    # 3) 步频
    if avg_cadence:
        if avg_cadence < 165:
            suggestions.append(f"平均步频 {avg_cadence:.0f} spm 偏低：触地时间长、刹车力大、受伤风险高。练法：节拍器 170–180 spm 短促快频跑、上坡小步快频、缩短步幅而不提速。")
        elif avg_cadence <= 180:
            suggestions.append(f"平均步频 {avg_cadence:.0f} spm 在高效区间（170–180），保持。")
        else:
            suggestions.append(f"平均步频 {avg_cadence:.0f} spm 偏高：检查是否步幅过短导致效率下降（结合步幅一起看）。")
    # 4) 跑姿动力学（来自手表逐样本时间序列）
    if rd:
        sl = rd.get("stride_length_m"); vo = rd.get("vertical_oscillation_cm")
        vr = rd.get("vertical_ratio"); gct = rd.get("ground_contact_ms")
        if sl:
            suggestions.append(f"步幅 {sl:.2f} m：半马目标配速下理想步幅约 0.95–1.05 m。提升靠核心力量+髋伸展，不靠跨大步（跨大步会让垂直振幅↑、刹车力↑）。")
        if vo is not None:
            if vo < 8:
                suggestions.append(f"垂直振幅 {vo:.1f} cm 优秀（<8）：上下起伏小、推进效率高。")
            elif vo <= 10:
                suggestions.append(f"垂直振幅 {vo:.1f} cm 正常，可再降：想象“贴地滑行”、缩短触地、提高步频。")
            else:
                suggestions.append(f"垂直振幅 {vo:.1f} cm 偏高：说明“跳”得多、推进效率低。练法：核心收紧、落地在重心正下方、用快步频替代大步幅。")
        if vr is not None and vr > 8:
            suggestions.append(f"垂直步幅比 {vr:.1f}% 偏高（理想 6–8%）：与垂直振幅高同源，落地更“砸”而非“滚”；用快步频+核心收紧改善。")
        if gct is not None:
            if gct < 220:
                suggestions.append(f"触地时间 {gct:.0f} ms 优秀（精英级 <220）。")
            elif gct <= 250:
                suggestions.append(f"触地时间 {gct:.0f} ms 良好。")
            else:
                suggestions.append(f"触地时间 {gct:.0f} ms 偏长：推进/弹性不足。练法：节奏跑、弹力带髋屈、短促赤足感知（每次 1–2 分钟）、增强小腿与跟腱刚度。")
    else:
        suggestions.append("本次未记录完整跑姿动力学（步幅 / 垂直振幅 / 触地时间）。如需精确跑姿分析，请佩戴 HRM-Run/Pro 心率带，或用支持跑步动力学的手表并确保数据已同步。")
    # 5) 心率区间分布
    if hr_zone_dist:
        has_quality = any(z["zone"] >= 4 and z["pct"] >= 10 for z in hr_zone_dist)
        dom = max((z for z in hr_zone_dist if z["pct"]), key=lambda z: z["pct"], default=None)
        if dom and dom["zone"] <= 2 and not has_quality:
            suggestions.append(f"本次 {dom['pct']:.0f}% 在 Z2 有氧，是典型的打底盘跑，符合基础期定位；要破 1:50，每周还需塞 1–2 次质量课（阈值/间歇）。")
        elif has_quality:
            qpct = max((z["pct"] for z in hr_zone_dist if z["zone"] >= 4), default=0)
            suggestions.append(f"本次触达 Z4/Z5 质量区间（占比 {qpct:.0f}%），速度刺激到位；注意安排 48h+ 恢复再做下一次质量课。")
    # 6) 训练效果
    if te_a and aerobic_te and aerobic_te >= 4:
        suggestions.append(f"有氧训练效果 {aerobic_te:.1f} 已达有效提升区间：安排 48h 以上恢复，再做下一次高质量课。")
    if te_an and anaerobic_te and anaerobic_te >= 4:
        suggestions.append(f"无氧训练效果 {anaerobic_te:.1f} 强：速度/乳酸耐受刺激到位，优先保证睡眠与身体电量恢复。")
    # 7) 负荷与恢复
    if distance_km and distance_km >= 15:
        suggestions.append(f"本次 {distance_km:.0f}km 长距离，身体需要 48–72h 修复：接下来两天以 Z2 轻松/休息为主，别连质量课。")
    elif training_load:
        suggestions.append(f"本次训练负荷 {training_load:.0f}：正常训练刺激，质量课与恢复日交替即可。")

    # ── 综合综述（AI 式总结：本次跑怎么样 + 如何跑得更好）──
    summary_bits = []
    if distance_km and pace_sec:
        summary_bits.append(f"{distance_km:.1f}km、配速 {sec_to_pace(pace_sec)}")
    if avg_hr:
        summary_bits.append(f"平均心率 {avg_hr:.0f}")
    high_stim = (te_a and aerobic_te and aerobic_te >= 4) or (te_an and anaerobic_te and anaerobic_te >= 4)
    verdict = "整体是一次高质量刺激" if high_stim else ("整体是有氧基础跑" if (drift is not None and drift <= 5) else "整体是恢复性慢跑")
    strengths = []
    if pace_cv is not None and pace_cv <= 3:
        strengths.append("配速稳")
    if drift is not None and drift <= 3:
        strengths.append("心率漂移低")
    if avg_cadence and avg_cadence >= 170:
        strengths.append("步频高效")
    if rd and rd.get("vertical_oscillation_cm") is not None and rd["vertical_oscillation_cm"] < 8:
        strengths.append("起伏小")
    weak = []
    if avg_cadence and avg_cadence < 168:
        weak.append("步频偏低")
    if rd and rd.get("vertical_oscillation_cm") and rd["vertical_oscillation_cm"] > 9:
        weak.append("垂直振幅偏高")
    if rd and rd.get("ground_contact_ms") and rd["ground_contact_ms"] > 250:
        weak.append("触地偏长")
    if pace_cv is not None and pace_cv > 4:
        weak.append("配速波动大")
    coaching_summary = "本次" + (", ".join(summary_bits) if summary_bits else "跑步") + "，" + verdict + "。"
    if strengths:
        coaching_summary += "优点：" + "、".join(strengths) + "。"
    if weak:
        coaching_summary += "可优化：" + "、".join(weak) + "——用「快步频 + 核心收紧 + 落地在重心下方」可同步改善步频 / 垂直振幅 / 触地时间。"
    coaching_summary += "要达成半马目标，关键是在周中稳定塞入 1–2 次阈值/间歇质量课，并保证质量课之间 ≥48h 恢复；基础期以堆 Z2 有氧底盘为主。"

    return {
        "activity_id": aid,
        "date": date,
        "name": pick("name"),
        "distance_km": distance_km,
        "pace_sec": pace_sec,
        "pace": sec_to_pace(pace_sec),
        "duration_sec": duration_sec,
        "duration": sec_to_hm(duration_sec),
        "avg_hr": avg_hr,
        "max_hr": max_hr,
        "calories": calories,
        "elevation_gain": elevation_gain,
        "avg_cadence": avg_cadence,
        "aerobic_te": aerobic_te,
        "anaerobic_te": anaerobic_te,
        "training_load": training_load,
        "vo2_max": vo2_max,
        "splits": splits,
        "pace_cv": round(pace_cv, 1) if pace_cv is not None else None,
        "pace_fastest": sec_to_pace(pace_fastest),
        "pace_slowest": sec_to_pace(pace_slowest),
        "cardiac_drift_pct": drift,
        "hr_zone_dist": hr_zone_dist,
        "form": form,
        "te_aerobic": te_a,
        "te_anaerobic": te_an,
        "suggestions": suggestions,
        "coaching_summary": coaching_summary,
    }


def build_post_runs(acts, details):
    """近 12 次跑步的跑后分析列表，供前端下拉切换。

    每场都尽量用同步抓取的完整详情（逐公里分段 / 心率区间 / 跑姿动力学）；
    若某场缺详情，则退化为只含活动摘要的精简分析（距离/配速/心率等仍来自摘要）。
    """
    if not acts:
        return []
    details = details or {}
    recent = sorted(acts, key=lambda a: str(a.get("date") or ""))[-12:]
    out = []
    for a in recent:
        aid = str(a.get("activity_id"))
        pr = _build_post_run_one(details.get(aid), acts, aid)
        if pr:
            out.append(pr)
    return out


# ─────────────────────────────── 目标进度历史 / 近 60 天趋势 ───────────────────────────────
GOAL_HIST = os.path.join(OUT, "goal_history.jsonl")


def update_goal_history(today, ability, runs_a):
    """每天追加一行目标进度快照（同一天覆盖），用来画"是否在同 1:50 收敛"的曲线。

    没有这条时间序列时，看板只能告诉你"现在差 5 分"，
    看不出这是在变好还是变差，也就没法决定该加量还是该收。
    """
    hm = (ability.get("race_estimates") or {}).get("半马", {}) or {}
    row = {
        "date": today,
        "hm_sec": hm.get("sec"),
        "threshold_pace_sec": ability.get("threshold_pace_sec"),
        "acute_weekly_km": runs_a.get("acute_weekly_km"),
        "chronic_weekly_km": runs_a.get("chronic_weekly_km"),
        "composite": ability.get("composite_score"),
    }
    rows = []
    if os.path.exists(GOAL_HIST):
        with open(GOAL_HIST, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if isinstance(r, dict) and r.get("date") and r["date"] != today:
                    rows.append(r)
    rows.append(row)
    rows = rows[-400:]
    os.makedirs(OUT, exist_ok=True)
    tmp = GOAL_HIST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, GOAL_HIST)
    return rows


def build_trends(sleep_recs, health_recs, days=60):
    """近 N 天多指标趋势：睡眠评分 / 睡眠时长 / 静息心率 / HRV。"""
    by_date = {}
    for r in sleep_recs:
        d = r.get("date")
        if not d:
            continue
        cur = by_date.setdefault(d, {})
        cur["sleep_score"] = r.get("sleep_score")
        cur["sleep_hours"] = (round((r.get("total_seconds") or 0) / 3600, 1)
                              if r.get("total_seconds") else None)
        cur["rhr"] = r.get("resting_heart_rate")
    for r in health_recs:
        d = r.get("date")
        if not d:
            continue
        cur = by_date.setdefault(d, {})
        hrv = r.get("hrv") or {}
        cur["hrv"] = hrv.get("last_night_avg")
        if not cur.get("rhr"):
            cur["rhr"] = r.get("resting_heart_rate")
    dates = sorted(by_date)[-days:]
    out = {"dates": [d[5:] for d in dates], "sleep_score": [], "sleep_hours": [],
           "rhr": [], "hrv": []}
    for d in dates:
        v = by_date[d]
        out["sleep_score"].append(v.get("sleep_score"))
        out["sleep_hours"].append(v.get("sleep_hours"))
        out["rhr"].append(v.get("rhr"))
        out["hrv"].append(v.get("hrv"))
    return out


def build_ef_trend(acts, n=24):
    """跑步效率 EF = 速度(m/min) ÷ 平均心率。

    跨次跑的趋势：同一配速下心率越低，EF 越高 = 有氧效率在变好。
    只用户外跑、且距离 ≥3km（排除跑步机与超短距离噪声）。
    """
    rows = []
    for a in acts:
        if a.get("type") != "running":
            continue
        p, hr, d = a.get("pace_sec"), a.get("avg_hr"), a.get("distance_km")
        if not (p and hr and d and d >= 3):
            continue
        speed = 1000.0 / p * 60.0  # m/min
        ef = round(speed / hr, 3)
        rows.append({"date": a["date"], "ef": ef, "pace": a.get("pace_formatted"),
                     "avg_hr": hr, "distance_km": round(d, 1)})
    rows.sort(key=lambda x: x["date"])
    recent = rows[-n:]
    allv = [r["ef"] for r in rows]
    baseline = round(_mean(allv), 3) if allv else None
    rec = [r["ef"] for r in recent[-5:]] if recent else []
    recent_avg = round(_mean(rec), 3) if rec else None
    return {
        "trend": recent,
        "baseline": baseline,
        "recent_avg": recent_avg,
        "delta": round(recent_avg - baseline, 3) if (recent_avg is not None and baseline is not None) else None,
    }


# ─────────────────────────────── 主流程 ───────────────────────────────
def main():
    acts = load_activities()
    sleep_recs, sleep_avg = load_sleep()
    health_recs, health_sum = load_health()
    summary = load_summary_today()
    details = load_details()

    cfg = load_plan_config()
    # 今天永远取真实本地日期；summary 的 date 只是"设备最后同步那天的快照"，
    # 同步一旦断更，看板的日历 / 今日课表仍须按真实日期走，不能停留在旧快照。
    today = datetime.now().strftime("%Y-%m-%d")
    state_date = summary.get("date")   # 恢复 / 睡眠 / 电量等信号的实际快照日期（可能早于今天）
    state_stale_days = None
    if state_date:
        try:
            state_stale_days = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(state_date, "%Y-%m-%d")).days
        except Exception:
            state_stale_days = None

    # 把原始活动塞进 runs_a 供 plan 使用（长距离疲劳规则需要逐条扫描）
    runs_a = analyze_runs(acts, today)
    runs_a["_acts"] = acts

    ability = assess_ability(acts, runs_a, summary, today)
    state = analyze_current_state(runs_a, sleep_recs, sleep_avg, health_recs, health_sum, summary, ability)
    latest_aid = None
    if acts:
        latest_aid = str(sorted(acts, key=lambda a: str(a.get("date") or ""))[-1].get("activity_id"))
    post_run = _build_post_run_one(details.get(latest_aid), acts, latest_aid) if latest_aid else None
    post_runs = build_post_runs(acts, details)
    goal_history = update_goal_history(today, ability, runs_a)
    trends = build_trends(sleep_recs, health_recs)
    ef = build_ef_trend(acts)

    # ── 目标导向 / 整周课表 ──
    score, factors, long_fatigue, fatigue_run, days_since = compute_readiness(runs_a, state, summary, today)
    state["status_score"] = score  # 状态分（今日）：每日就绪度，与稳定的能力分区分
    phase_key, phase_label, weeks_left = phase_for(cfg["race_date"], today)
    today_dt = datetime.strptime(today, "%Y-%m-%d")
    week_start = today_dt - timedelta(days=today_dt.weekday())
    actual_ctx = build_actual_ctx(today, acts, cfg, ability, phase_key, window=2)
    plan_week = build_week(week_start, cfg, ability, phase_key, today, score, long_fatigue, actual_ctx, acts)
    today_cell = next((d for d in plan_week if d["is_today"]), None)
    plan = compute_plan(today_cell, score, factors, long_fatigue, fatigue_run, days_since, state)
    plan["actual_vs_plan"] = actual_ctx
    plan["phase"] = phase_key
    plan["phase_label"] = phase_label
    plan["weeks_left"] = weeks_left
    # 目标完赛时间 vs Riegel 预计
    tgt_min = cfg.get("goal_target_minutes")
    hm = (ability.get("race_estimates") or {}).get("半马", {}).get("formatted")
    proj_min = None
    if hm:
        try:
            _h = hm.split("h")
            _hh = int(_h[0])
            _mm = int((_h[1].replace("m", "").replace("s", "").strip() or "0"))
            proj_min = _hh * 60 + _mm
        except Exception:
            proj_min = None
    plan["goal_target_time"] = cfg.get("goal_target_time")
    plan["goal_target_minutes"] = tgt_min
    plan["goal_projected_str"] = hm
    plan["goal_projected_min"] = proj_min
    plan["goal_gap_min"] = (proj_min - tgt_min) if (proj_min and tgt_min) else None
    plan_next = build_week(week_start + timedelta(days=7), cfg, ability, phase_key, today, score, long_fatigue)

    out = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "today": today,
        "state_date": state_date,
        "state_stale_days": state_stale_days,
        "runs": runs_a,
        "ability": ability,
        "current_state": state,
        "plan": plan,
        "plan_week": plan_week,
        "plan_next": plan_next,
        "plan_config": cfg,
        "post_run": post_run,
        "post_runs": post_runs,
        "goal_history": goal_history,
        "trends": trends,
        "ef_trend": ef,
        "sleep_avg_90d": sleep_avg,
        "health_summary_90d": health_sum,
        "raw_activities_count": len(acts),
        "activities": acts,
    }

    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "analysis.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(OUT, "dashboard_data.js"), "w", encoding="utf-8") as f:
        f.write("window.DASHBOARD_DATA = ")
        json.dump(out, f, ensure_ascii=False, default=str)
        f.write(";\n")

    # ── 人类可读报告 ──
    r = runs_a
    print("=" * 56)
    print("  个人跑步历史数据分析报告")
    print("=" * 56)
    print(f"数据范围 : {r['first_date']} ~ {r['last_date']}  ({r['total_runs']} 次)")
    print(f"总里程   : {r['total_distance_km']} km  | 总时长 {r['total_duration_hours']} h")
    print(f"综合配速 : {r['overall_pace']} /km  | 最快 {r['best_pace']} /km")
    print(f"平均心率 : {r['avg_hr']} bpm | 历史最高 {r['max_hr_recorded']} bpm")
    print(f"周均里程 : 近7天 {r['acute_weekly_km']} / 近28天 {r['chronic_weekly_km']} km "
          f"(ACWR {r['acwr_volume']} 自算, 7d/28d @ {r.get('acwr_anchor')})")
    print("-" * 56)
    print("【能力画像】综合评级:", ability["grade"], f"({ability['composite_score']}/100)")
    for dim in ability["dimensions"]:
        print(f"  {dim['name']:<6} {dim['score']:>3}  {dim['desc']}")
    print("【比赛配速估算(Riegel)】")
    for label, v in ability["race_estimates"].items():
        print(f"  {label:<4} {v['formatted']}")
    print(f"  阈值配速: {ability['threshold_pace']} /km")
    print("-" * 56)
    print("【当前状态信号】")
    for s in state["signals"]:
        icon = {"good": "OK ", "warn": "!! ", "info": ".. "}.get(s["level"], "   ")
        print(f"  [{icon}] {s['name']}: {s['desc']}")
    print("-" * 56)
    print(f"【状态分(今日)】{score}  | TSB/Form {state.get('form_tsb')}（{state.get('form_label')}）")
    print(f"【跑步效率 EF】基线 {ef.get('baseline')}  近5次均值 {ef.get('recent_avg')}  变化 {ef.get('delta')}")
    print("-" * 56)
    print(f"【周期阶段】{phase_label}  | 距比赛 {weeks_left} 周  | 目标 {cfg.get('goal_label')}")
    print("【整周课表】")
    for d in plan_week:
        if d["is_today"]:
            tag = "★今天"
        elif d["duty"] == "main":
            tag = "主班 "
        elif d["duty"] == "standby":
            tag = "备班 "
        else:
            tag = "    "
        if d["planned"]:
            eff = d["adjusted"] or d["planned"]
            chg = ""
            if d["change"] == "downgraded":
                chg = " ⬇下调"
            elif d["change"] == "upgraded":
                chg = " ⬆上调"
            line = f"  {d['weekday']} {d['date']} {tag} {eff['id']} {eff['name']} | {eff['pace_range']} | {eff['hr_range']} | {eff['duration']}{chg}"
        else:
            line = f"  {d['weekday']} {d['date']} {tag} (无训练)"
        print(line)
        if d["reason"]:
            print(f"        → 调整原因: {d['reason']}")
    print("-" * 56)
    print("【今日课表】", plan["title"], f"(就绪度 {plan['readiness_score']})")
    print(f"  目标配速: {plan['target_pace']}  目标心率: {plan['target_hr']}  时长: {plan['duration']}")
    if plan["long_fatigue"]:
        print("  [!] 触发长距离疲劳规则：昨日长距离仍在恢复窗口")
    avp = plan.get("actual_vs_plan")
    if avp:
        flags = []
        if avp["overreach"]:
            flags.append("实际超量[!]")
        if avp["missed_quality"]:
            flags.append("漏质量课")
        if flags:
            print("  【实际 vs 计划】" + " / ".join(flags))
            for r in avp["days"]:
                arrow = {1: "↑超量", -1: "↓偏松", 0: "="}.get(r["delta"], str(r["delta"]))
                print(f"    {r['date']} 计划{r['planned_label']} | 实际 {r['actual']} {r['actual_distance_km']}km {r['actual_pace'] or ''}  {arrow}")
    print("-" * 56)
    if post_run:
        print(f"【跑后分析】{post_run['date']} {post_run['distance_km']}km {post_run['pace']}/km HR{post_run['avg_hr']}")
        print(f"  配速一致性 CV {post_run['pace_cv']}% | 心率漂移 {post_run['cardiac_drift_pct']}%")
        print(f"  步频 {post_run['avg_cadence']} spm | 估算步幅 {post_run['form']['stride_est_m']} m")
        for sug in post_run["suggestions"]:
            print(f"   · {sug}")
    print("=" * 56)
    print(f"已写出: {os.path.join(OUT, 'analysis.json')}")
    print(f"已写出: {os.path.join(OUT, 'dashboard_data.js')}")


if __name__ == "__main__":
    main()
