#!/usr/bin/env python3
"""Generate time tracking CSV and HTML report from Claude session data."""

import json
import os
import glob
import csv
import calendar
from collections import defaultdict
from datetime import datetime, timedelta, timezone

SESSION_META_DIR = os.path.expanduser("~/.claude/usage-data/session-meta/")
PROJECTS_DIR = os.path.expanduser("~/.claude/projects/")
CSV_OUTPUT = os.path.expanduser("~/Downloads/time-tracking.csv")
HTML_OUTPUT = os.path.expanduser("~/Downloads/time-tracking.html")

COLORS = [
    "#4285f4", "#ea4335", "#fbbc04", "#34a853", "#ff6d01",
    "#46bdc6", "#7baaf7", "#f07b72", "#fcd04f", "#71c287",
    "#ff9e40", "#78d9e0", "#a78bfa", "#f472b6", "#38bdf8",
]

LIGHT_COLORS = {"#fbbc04", "#fcd04f", "#ff9e40"}

IDLE_THRESHOLD_MIN = 60
RESPONSE_BUFFER_MIN = 10


def pill_text_color(bg):
    """Return appropriate text color for a pill background."""
    return "#553600" if bg in LIGHT_COLORS else "#fff"


def load_sessions():
    """Load sessions from session-meta + fallback to JSONL files for missing ones."""
    sessions = []
    meta_ids = set()

    # 1. Primary source: session-meta JSON files.
    for f in glob.glob(os.path.join(SESSION_META_DIR, "*.json")):
        with open(f) as fh:
            s = json.load(fh)
        sessions.append(s)
        meta_ids.add(s.get("session_id", ""))

    print(f"  Session-meta: {len(sessions)} sessions")

    # 2. Fallback: scan sessions-index for sessions missing from session-meta,
    #    then parse their JSONL files for user message timestamps.
    fallback_count = 0
    for d in os.listdir(PROJECTS_DIR):
        idx_path = os.path.join(PROJECTS_DIR, d, "sessions-index.json")
        if not os.path.exists(idx_path):
            continue
        with open(idx_path) as f:
            data = json.load(f)
        for entry in data.get("entries", []):
            sid = entry.get("sessionId", "")
            if sid in meta_ids:
                continue

            jsonl_path = entry.get("fullPath", "")
            if not jsonl_path or not os.path.exists(jsonl_path):
                # Try constructing the path from project dir.
                jsonl_path = os.path.join(PROJECTS_DIR, d, f"{sid}.jsonl")
            if not os.path.exists(jsonl_path):
                continue

            # Parse JSONL to extract user message timestamps.
            user_timestamps = []
            project_path = entry.get("projectPath", "")
            try:
                with open(jsonl_path) as jf:
                    for line in jf:
                        obj = json.loads(line)
                        if obj.get("type") == "user" and "timestamp" in obj:
                            user_timestamps.append(obj["timestamp"])
            except (json.JSONDecodeError, IOError):
                continue

            if user_timestamps:
                sessions.append({
                    "session_id": sid,
                    "project_path": project_path,
                    "user_message_timestamps": user_timestamps,
                })
                meta_ids.add(sid)
                fallback_count += 1

    print(f"  JSONL fallback (indexed): {fallback_count} additional sessions")

    # 3. Direct scan: find JSONL session files not yet covered.
    #    Skip agent files and observer sessions.
    direct_count = 0
    for d in os.listdir(PROJECTS_DIR):
        if "observer-sessions" in d:
            continue
        pdir = os.path.join(PROJECTS_DIR, d)
        if not os.path.isdir(pdir):
            continue

        # Resolve project path from directory name (reverse the dash-encoding).
        # Try to get it from sessions-index first.
        project_path = ""
        idx_path = os.path.join(pdir, "sessions-index.json")
        if os.path.exists(idx_path):
            with open(idx_path) as f:
                idx_data = json.load(f)
            for e in idx_data.get("entries", []):
                pp = e.get("projectPath", "")
                if pp:
                    project_path = pp
                    break

        for fname in os.listdir(pdir):
            if not fname.endswith(".jsonl"):
                continue
            if fname.startswith("agent-"):
                continue
            sid = fname.replace(".jsonl", "")
            if sid in meta_ids:
                continue

            jsonl_path = os.path.join(pdir, fname)
            user_timestamps = []
            found_project = project_path
            try:
                with open(jsonl_path) as jf:
                    for line in jf:
                        obj = json.loads(line)
                        if obj.get("type") == "user" and "timestamp" in obj:
                            user_timestamps.append(obj["timestamp"])
                            # Try to get project path from the message itself.
                            if not found_project:
                                cwd = obj.get("cwd", "")
                                if cwd:
                                    found_project = cwd
            except (json.JSONDecodeError, IOError):
                continue

            if user_timestamps:
                sessions.append({
                    "session_id": sid,
                    "project_path": found_project,
                    "user_message_timestamps": user_timestamps,
                })
                meta_ids.add(sid)
                direct_count += 1

    print(f"  JSONL direct scan: {direct_count} additional sessions")
    return sessions


def _compute_active_intervals(timestamps):
    """Compute active (start, end) intervals from sorted message timestamps."""
    intervals = []
    if len(timestamps) == 1:
        t = timestamps[0]
        intervals.append((t, t + timedelta(minutes=RESPONSE_BUFFER_MIN)))
    else:
        burst_start = timestamps[0]
        burst_end = timestamps[0]
        for i in range(1, len(timestamps)):
            gap = (timestamps[i] - burst_end).total_seconds() / 60
            if gap <= IDLE_THRESHOLD_MIN:
                burst_end = timestamps[i]
            else:
                intervals.append((
                    burst_start,
                    burst_end + timedelta(minutes=RESPONSE_BUFFER_MIN),
                ))
                burst_start = timestamps[i]
                burst_end = timestamps[i]
        intervals.append((
            burst_start,
            burst_end + timedelta(minutes=RESPONSE_BUFFER_MIN),
        ))
    return intervals


def _split_by_day(intervals):
    """Split intervals across midnight boundaries. Yields (date_str, start, end)."""
    for start, end in intervals:
        current = start
        while current < end:
            day_end = (current.replace(hour=0, minute=0, second=0, microsecond=0)
                       + timedelta(days=1))
            segment_end = min(day_end, end)
            yield current.strftime("%Y-%m-%d"), current, segment_end
            current = segment_end


def _merge_intervals(intervals):
    """Merge overlapping (start, end) intervals. Returns total minutes."""
    if not intervals:
        return 0.0
    sorted_iv = sorted(intervals)
    merged = [sorted_iv[0]]
    for start, end in sorted_iv[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return sum((e - s).total_seconds() / 60 for s, e in merged)


def aggregate(sessions):
    """Group sessions by (project, date) using gap-based active time.

    Also collects raw intervals per date (across all projects) for deduplication.
    Returns (rows, deduped_daily) where deduped_daily is {date: hours}.
    """
    groups = defaultdict(lambda: defaultdict(lambda: {"minutes": 0.0, "sessions": set()}))
    # All raw intervals per date (across projects) for deduplication.
    all_intervals_by_date = defaultdict(list)

    for s in sessions:
        project_path = s.get("project_path", "")
        project = os.path.basename(project_path) if project_path else "unknown"
        timestamps = s.get("user_message_timestamps", [])
        session_id = s.get("session_id", "")

        if not timestamps:
            continue

        parsed = sorted(
            datetime.fromisoformat(ts.replace("Z", "+00:00")) for ts in timestamps
        )

        active_intervals = _compute_active_intervals(parsed)

        for date_str, seg_start, seg_end in _split_by_day(active_intervals):
            mins = (seg_end - seg_start).total_seconds() / 60
            groups[project][date_str]["minutes"] += mins
            groups[project][date_str]["sessions"].add(session_id)
            all_intervals_by_date[date_str].append((seg_start, seg_end))

    rows = []
    for project in sorted(groups):
        for date in sorted(groups[project]):
            data = groups[project][date]
            total_mins = round(data["minutes"], 1)
            rows.append({
                "project": project,
                "date": date,
                "elapsed_minutes": total_mins,
                "elapsed_hours": round(total_mins / 60, 2),
                "sessions": len(data["sessions"]),
            })

    # Compute deduplicated daily totals (merged across all projects).
    deduped_daily = {}
    for date_str, intervals in all_intervals_by_date.items():
        merged_mins = _merge_intervals(intervals)
        deduped_daily[date_str] = round(merged_mins / 60, 2)

    return rows, deduped_daily


def write_csv(rows):
    """Write aggregated data to CSV."""
    with open(CSV_OUTPUT, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "project", "date", "elapsed_minutes", "elapsed_hours", "sessions",
        ])
        writer.writeheader()
        writer.writerows(rows)
    print(f"CSV written to {CSV_OUTPUT}")


def assign_colors(rows):
    """Assign a consistent color to each project."""
    projects = sorted(set(r["project"] for r in rows))
    return {p: COLORS[i % len(COLORS)] for i, p in enumerate(projects)}


def build_summary(rows):
    """Build per-project summary stats."""
    summary = {}
    for r in rows:
        p = r["project"]
        if p not in summary:
            summary[p] = {"hours": 0, "sessions": 0, "min_date": r["date"], "max_date": r["date"]}
        summary[p]["hours"] += r["elapsed_hours"]
        summary[p]["sessions"] += r["sessions"]
        if r["date"] < summary[p]["min_date"]:
            summary[p]["min_date"] = r["date"]
        if r["date"] > summary[p]["max_date"]:
            summary[p]["max_date"] = r["date"]
    return summary


def build_calendar_data(rows):
    """Build date -> [{project, hours}] lookup."""
    cal = defaultdict(list)
    for r in rows:
        cal[r["date"]].append({"project": r["project"], "hours": r["elapsed_hours"]})
    return cal


def generate_html(rows, color_map, summary, cal_data, deduped_daily):
    """Generate self-contained HTML report with month pager."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    all_dates = sorted(set(r["date"] for r in rows))
    if not all_dates:
        print("No data to report.")
        return

    first = datetime.strptime(all_dates[0], "%Y-%m-%d")
    last = datetime.strptime(all_dates[-1], "%Y-%m-%d")

    # Collect months to render.
    months = []
    current = first.replace(day=1)
    while current <= last:
        months.append((current.year, current.month))
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)

    # Build individual month grids as separate divs with data attributes.
    cal_html = ""
    for idx, (year, month) in enumerate(months):
        month_name = calendar.month_name[month]
        cal_obj = calendar.Calendar(firstweekday=0)
        weeks = cal_obj.monthdayscalendar(year, month)

        # Last month visible by default.
        display = "block" if idx == len(months) - 1 else "none"
        cal_html += f'<div class="month-page" data-index="{idx}" style="display:{display}">'
        cal_html += f'<div class="month-block"><h3>{month_name} {year}</h3>'
        cal_html += '<table class="cal-grid"><thead><tr>'
        for day_name in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]:
            cal_html += f"<th>{day_name}</th>"
        cal_html += "</tr></thead><tbody>"

        for week in weeks:
            cal_html += "<tr>"
            for day in week:
                if day == 0:
                    cal_html += '<td class="empty"></td>'
                else:
                    date_str = f"{year}-{month:02d}-{day:02d}"
                    today_cls = " today" if date_str == today_str else ""
                    entries = cal_data.get(date_str, [])
                    if entries:
                        total = sum(e["hours"] for e in entries)
                        pills = ""
                        tooltip_lines = []
                        for e in sorted(entries, key=lambda x: -x["hours"]):
                            color = color_map.get(e["project"], "#999")
                            txt = pill_text_color(color)
                            pills += (
                                f'<span class="pill" style="background:{color};color:{txt}">'
                                f'{e["project"][:4]} {e["hours"]:.1f}h</span>'
                            )
                            tooltip_lines.append(f'{e["project"]}: {e["hours"]:.1f}h')
                        tooltip = "&#10;".join(tooltip_lines)
                        cal_html += (
                            f'<td class="has-data{today_cls}" data-tooltip="{tooltip}">'
                            f'<div class="day-num">{day}</div>'
                            f'<div class="day-total">{total:.1f}h</div>'
                            f'<div class="pills">{pills}</div></td>'
                        )
                    else:
                        cal_html += f'<td class="no-data{today_cls}"><div class="day-num">{day}</div></td>'
            cal_html += "</tr>"
        cal_html += "</tbody></table></div></div>"

    # Build summary table.
    summary_html = (
        '<table class="summary-table"><thead><tr>'
        '<th>Project</th><th>Total Hours</th><th>Sessions</th><th>Date Range</th>'
        '</tr></thead><tbody>'
    )
    grand_hours = 0
    grand_sessions = 0
    for p in sorted(summary):
        s = summary[p]
        color = color_map.get(p, "#999")
        grand_hours += s["hours"]
        grand_sessions += s["sessions"]
        summary_html += (
            f'<tr><td><span class="color-dot" style="background:{color}"></span>{p}</td>'
            f'<td>{s["hours"]:.1f}h</td><td>{s["sessions"]}</td>'
            f'<td>{s["min_date"]} &rarr; {s["max_date"]}</td></tr>'
        )
    summary_html += (
        f'<tr class="total-row"><td><strong>TOTAL</strong></td>'
        f'<td><strong>{grand_hours:.1f}h</strong></td>'
        f'<td><strong>{grand_sessions}</strong></td><td></td></tr>'
        '</tbody></table>'
    )

    # Build week view: projects as rows, days (Mon-Sun) as columns.
    # Determine all ISO weeks that have data.
    from datetime import date as date_cls
    week_lookup = defaultdict(lambda: defaultdict(float))  # (year, week) -> {project -> {date -> hours}}
    week_dates = {}  # (year, week) -> [mon_date, ..., sun_date]

    for r in rows:
        d = datetime.strptime(r["date"], "%Y-%m-%d")
        iso = d.isocalendar()
        wk_key = (iso[0], iso[1])
        week_lookup[wk_key][(r["project"], r["date"])] = r["elapsed_hours"]

    # Pre-compute the Monday of each ISO week.
    all_week_keys = sorted(week_lookup.keys())
    for yw in all_week_keys:
        yr, wk = yw
        # Monday of ISO week.
        mon = datetime.strptime(f"{yr}-W{wk:02d}-1", "%G-W%V-%u")
        week_dates[yw] = [(mon + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]

    week_html = ""
    for widx, wk_key in enumerate(all_week_keys):
        yr, wk = wk_key
        dates_in_week = week_dates[wk_key]
        mon_str = dates_in_week[0]
        sun_str = dates_in_week[6]

        # Collect projects active this week.
        week_projects = set()
        for (proj, dt), hrs in week_lookup[wk_key].items():
            if hrs > 0:
                week_projects.add(proj)
        week_projects = sorted(week_projects)

        display = "block" if widx == len(all_week_keys) - 1 else "none"
        week_html += f'<div class="week-page" data-index="{widx}" style="display:{display}">'
        week_html += f'<div class="week-block"><h3>Week {wk}, {yr} <span class="week-range">{mon_str} &rarr; {sun_str}</span></h3>'
        week_html += '<table class="week-grid"><thead><tr><th>Project</th>'
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        for i, dn in enumerate(day_names):
            d = datetime.strptime(dates_in_week[i], "%Y-%m-%d")
            is_today = dates_in_week[i] == today_str
            cls = ' class="today-col"' if is_today else ""
            week_html += f'<th{cls}>{dn}<br><span class="week-date">{d.day}</span></th>'
        week_html += '<th>Total</th></tr></thead><tbody>'

        day_totals = [0.0] * 7
        grand_week_total = 0.0

        for proj in week_projects:
            color = color_map.get(proj, "#999")
            week_html += f'<tr><td><span class="color-dot" style="background:{color}"></span>{proj}</td>'
            row_total = 0.0
            for i, dt in enumerate(dates_in_week):
                hrs = week_lookup[wk_key].get((proj, dt), 0)
                day_totals[i] += hrs
                row_total += hrs
                if hrs > 0:
                    week_html += f'<td class="has-hours">{hrs:.1f}h</td>'
                else:
                    week_html += '<td class="no-hours">&mdash;</td>'
            grand_week_total += row_total
            week_html += f'<td class="row-total">{row_total:.1f}h</td></tr>'

        # Day totals row.
        week_html += '<tr class="day-totals-row"><td><strong>Total</strong></td>'
        for i in range(7):
            if day_totals[i] > 0:
                week_html += f'<td><strong>{day_totals[i]:.1f}h</strong></td>'
            else:
                week_html += '<td>&mdash;</td>'
        week_html += f'<td class="grand-total"><strong>{grand_week_total:.1f}h</strong></td></tr>'

        week_html += '</tbody></table></div></div>'

    # Build active hours week view (deduplicated across projects).
    active_week_html = ""
    for widx, wk_key in enumerate(all_week_keys):
        yr, wk = wk_key
        dates_in_week = week_dates[wk_key]
        mon_str = dates_in_week[0]
        sun_str = dates_in_week[6]

        display = "block" if widx == len(all_week_keys) - 1 else "none"
        active_week_html += f'<div class="active-week-page" data-index="{widx}" style="display:{display}">'
        active_week_html += f'<div class="week-block active-week-block"><h3>Week {wk}, {yr} <span class="week-range">{mon_str} &rarr; {sun_str}</span></h3>'
        active_week_html += '<table class="week-grid"><thead><tr>'
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        for i, dn in enumerate(day_names):
            d = datetime.strptime(dates_in_week[i], "%Y-%m-%d")
            is_today = dates_in_week[i] == today_str
            cls = ' class="today-col"' if is_today else ""
            active_week_html += f'<th{cls}>{dn}<br><span class="week-date">{d.day}</span></th>'
        active_week_html += '<th>Total</th></tr></thead><tbody>'

        # Single row: deduplicated active hours per day.
        active_week_html += '<tr class="active-hours-row">'
        week_total = 0.0
        for i, dt in enumerate(dates_in_week):
            hrs = deduped_daily.get(dt, 0)
            week_total += hrs
            if hrs > 0:
                active_week_html += f'<td class="has-hours active-cell"><strong>{hrs:.1f}h</strong></td>'
            else:
                active_week_html += '<td class="no-hours">&mdash;</td>'
        active_week_html += f'<td class="grand-total"><strong>{week_total:.1f}h</strong></td></tr>'

        active_week_html += '</tbody></table></div></div>'

    # Build detailed breakdown.
    details_html = ""
    by_project = defaultdict(list)
    for r in rows:
        by_project[r["project"]].append(r)

    for p in sorted(by_project):
        color = color_map.get(p, "#999")
        proj_rows = by_project[p]
        total_h = sum(r["elapsed_hours"] for r in proj_rows)
        total_s = sum(r["sessions"] for r in proj_rows)
        details_html += (
            f'<details class="project-detail">'
            f'<summary><span class="color-dot" style="background:{color}"></span> '
            f'{p} — {total_h:.1f}h across {total_s} sessions</summary>'
            f'<table class="detail-table">'
            f'<thead><tr><th>Date</th><th>Hours</th><th>Minutes</th><th>Sessions</th></tr></thead>'
            f'<tbody>'
        )
        for r in proj_rows:
            details_html += (
                f'<tr><td>{r["date"]}</td><td>{r["elapsed_hours"]:.1f}h</td>'
                f'<td>{r["elapsed_minutes"]:.0f}</td><td>{r["sessions"]}</td></tr>'
            )
        details_html += "</tbody></table></details>"

    total_months = len(months)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude Code Time Tracking</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  html {{ scroll-behavior: smooth; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #f0f2f5; color: #333; padding: 32px 24px;
    max-width: 1200px; margin: 0 auto;
    -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; line-height: 1.5;
  }}
  h1 {{ margin-bottom: 4px; font-size: 26px; font-weight: 700; color: #1a1a2e; letter-spacing: -0.02em; }}
  .subtitle {{ color: #888; margin-bottom: 24px; font-size: 13px; }}
  h2 {{ margin: 40px 0 16px; font-size: 18px; font-weight: 600; border-bottom: 2px solid #e5e7eb; padding-bottom: 10px; color: #1a1a2e; }}

  .note {{ background: #fefce8; border: 1px solid #fde68a; border-radius: 8px; padding: 12px 16px; margin-bottom: 24px; font-size: 13px; color: #92400e; line-height: 1.5; }}

  /* Summary table */
  .summary-table {{ width: 100%; border-collapse: separate; border-spacing: 0; margin-bottom: 16px; background: #fff; border-radius: 10px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.06), 0 1px 2px rgba(0,0,0,0.04); }}
  .summary-table th {{ background: #f8f9fa; padding: 12px 16px; text-align: left; font-size: 11px; font-weight: 600; color: #888; border-bottom: 2px solid #e9ecef; text-transform: uppercase; letter-spacing: 0.05em; }}
  .summary-table td {{ padding: 12px 16px; border-bottom: 1px solid #f0f0f0; font-size: 14px; }}
  .summary-table tbody tr:hover {{ background: #f8fafc; }}
  .summary-table .total-row td {{ background: #f1f3f5; border-top: 2px solid #dee2e6; font-weight: 600; padding: 14px 16px; }}
  .summary-table th:nth-child(2), .summary-table th:nth-child(3),
  .summary-table td:nth-child(2), .summary-table td:nth-child(3) {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .color-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; vertical-align: middle; margin-right: 8px; }}

  /* Month pager */
  .month-pager {{ display: flex; align-items: center; justify-content: center; gap: 16px; margin-bottom: 16px; }}
  .month-pager button {{
    background: #fff; border: 1px solid #d1d5db; border-radius: 8px; padding: 8px 16px;
    font-size: 14px; font-weight: 500; color: #374151; cursor: pointer;
    transition: all 0.15s ease; box-shadow: 0 1px 2px rgba(0,0,0,0.05);
  }}
  .month-pager button:hover:not(:disabled) {{ background: #f0f7ff; border-color: #4285f4; color: #4285f4; }}
  .month-pager button:disabled {{ opacity: 0.4; cursor: not-allowed; }}
  .month-pager .page-info {{ font-size: 14px; color: #6b7280; font-weight: 500; min-width: 80px; text-align: center; }}

  /* Calendar */
  .month-block {{ background: #fff; border-radius: 10px; padding: 0; box-shadow: 0 1px 3px rgba(0,0,0,0.06), 0 2px 8px rgba(0,0,0,0.04); overflow: hidden; }}
  .month-block h3 {{ margin: 0; padding: 14px 20px; font-size: 15px; font-weight: 600; background: #f8f9fa; border-bottom: 1px solid #e9ecef; color: #374151; }}
  .cal-grid {{ border-collapse: collapse; margin: 12px; }}
  .cal-grid th {{ padding: 6px 8px; font-size: 11px; color: #888; text-align: center; width: 100px; }}
  .cal-grid td {{ width: 100px; height: 90px; vertical-align: top; padding: 6px 8px; border: 1px solid #eee; font-size: 12px; transition: background 0.15s ease, box-shadow 0.15s ease; }}
  .cal-grid td.empty {{ background: #f9fafb; border-color: #f3f4f6; }}
  .cal-grid td.no-data {{ color: #d1d5db; }}
  .cal-grid td.has-data {{ background: #fefefe; cursor: default; position: relative; }}
  .cal-grid td.has-data:hover {{ background: #f0f7ff; box-shadow: inset 0 0 0 2px #4285f4; }}
  .cal-grid td.today {{ box-shadow: inset 0 0 0 2px #4285f4; }}
  .day-num {{ font-weight: 700; margin-bottom: 2px; color: #374151; font-size: 13px; }}
  .day-total {{ font-size: 11px; font-weight: 600; color: #111827; margin-bottom: 4px; }}
  .pills {{ display: flex; flex-wrap: wrap; gap: 3px; }}
  .pill {{ display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: 600; white-space: nowrap; line-height: 1.4; letter-spacing: 0.01em; }}

  /* Styled tooltip */
  .cal-grid td.has-data:hover::after {{
    content: attr(data-tooltip);
    position: absolute; bottom: calc(100% + 6px); left: 50%; transform: translateX(-50%);
    background: #1f2937; color: #fff; padding: 8px 12px; border-radius: 6px;
    font-size: 12px; white-space: pre-line; z-index: 10; pointer-events: none;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15); line-height: 1.5; min-width: 160px; text-align: left;
  }}

  /* Week view */
  .week-block {{ background: #fff; border-radius: 10px; padding: 0; box-shadow: 0 1px 3px rgba(0,0,0,0.06), 0 2px 8px rgba(0,0,0,0.04); overflow: hidden; }}
  .week-block h3 {{ margin: 0; padding: 14px 20px; font-size: 15px; font-weight: 600; background: #f8f9fa; border-bottom: 1px solid #e9ecef; color: #374151; }}
  .week-range {{ font-weight: 400; color: #888; font-size: 13px; margin-left: 8px; }}
  .week-grid {{ width: 100%; border-collapse: collapse; }}
  .week-grid th {{ background: #f9fafb; padding: 10px 12px; text-align: center; font-size: 11px; font-weight: 600; color: #6b7280; text-transform: uppercase; letter-spacing: 0.04em; border-bottom: 1px solid #e5e7eb; }}
  .week-grid th:first-child {{ text-align: left; padding-left: 16px; }}
  .week-grid th:last-child {{ background: #f1f3f5; }}
  .week-grid th.today-col {{ background: #e8f0fe; color: #4285f4; }}
  .week-date {{ font-size: 13px; font-weight: 700; color: #374151; text-transform: none; letter-spacing: 0; }}
  .week-grid td {{ padding: 10px 12px; text-align: center; border-top: 1px solid #f3f4f6; font-size: 13px; font-variant-numeric: tabular-nums; }}
  .week-grid td:first-child {{ text-align: left; padding-left: 16px; font-weight: 500; }}
  .week-grid td.has-hours {{ color: #111827; font-weight: 500; }}
  .week-grid td.no-hours {{ color: #d1d5db; }}
  .week-grid td.row-total {{ background: #f9fafb; font-weight: 600; color: #374151; border-left: 2px solid #e5e7eb; }}
  .week-grid .day-totals-row td {{ background: #f1f3f5; border-top: 2px solid #dee2e6; font-weight: 600; }}
  .week-grid .day-totals-row td.grand-total {{ background: #e8f0fe; color: #4285f4; border-left: 2px solid #e5e7eb; }}
  .week-grid tbody tr:hover {{ background: #f8fafc; }}
  .active-week-block {{ border: 2px solid #4285f4; }}
  .active-hours-row td.active-cell {{ font-size: 18px; color: #4285f4; }}
  .active-hours-row td.grand-total {{ font-size: 18px; }}

  /* Detail sections */
  .project-detail {{ background: #fff; border-radius: 10px; margin-bottom: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); border: 1px solid #f0f0f0; overflow: hidden; }}
  .project-detail summary {{ padding: 14px 16px; cursor: pointer; font-weight: 500; font-size: 14px; display: flex; align-items: center; gap: 10px; list-style: none; user-select: none; }}
  .project-detail summary::-webkit-details-marker {{ display: none; }}
  .project-detail summary::before {{ content: ''; display: inline-block; width: 0; height: 0; border-left: 5px solid #999; border-top: 4px solid transparent; border-bottom: 4px solid transparent; transition: transform 0.2s ease; flex-shrink: 0; }}
  .project-detail[open] summary::before {{ transform: rotate(90deg); }}
  .project-detail summary:hover {{ background: #f8fafc; }}
  .project-detail[open] summary {{ border-bottom: 1px solid #f0f0f0; }}
  .detail-table {{ width: 100%; border-collapse: collapse; }}
  .detail-table th {{ background: #f9fafb; padding: 10px 16px; text-align: left; font-size: 11px; font-weight: 600; color: #6b7280; text-transform: uppercase; letter-spacing: 0.04em; border-bottom: 1px solid #e5e7eb; }}
  .detail-table td {{ padding: 10px 16px; border-top: 1px solid #f3f4f6; font-size: 13px; font-variant-numeric: tabular-nums; }}
  .detail-table tbody tr:nth-child(even) {{ background: #fafbfc; }}
  .detail-table tbody tr:hover {{ background: #f0f7ff; }}
  .detail-table th:nth-child(2), .detail-table th:nth-child(3), .detail-table th:nth-child(4),
  .detail-table td:nth-child(2), .detail-table td:nth-child(3), .detail-table td:nth-child(4) {{ text-align: right; }}

  @media (max-width: 900px) {{
    body {{ padding: 16px; }}
    .month-block {{ overflow-x: auto; }}
    .cal-grid th, .cal-grid td {{ width: 70px; min-width: 70px; height: 75px; padding: 4px; font-size: 11px; }}
    .pill {{ font-size: 9px; padding: 1px 3px; }}
    .summary-table {{ display: block; overflow-x: auto; }}
  }}
  @media (max-width: 600px) {{
    h1 {{ font-size: 22px; }}
    h2 {{ font-size: 16px; margin-top: 32px; }}
    .cal-grid th, .cal-grid td {{ width: 55px; min-width: 55px; height: 65px; }}
    .pill {{ font-size: 8px; padding: 1px 2px; }}
    .day-total {{ font-size: 10px; }}
  }}
</style>
</head>
<body>
<h1>Claude Code Time Tracking</h1>
<p class="subtitle">Generated {datetime.now().strftime("%Y-%m-%d %H:%M")} &middot; Active time (gaps &gt; 60 min excluded, +10 min buffer per burst)</p>

<div class="note">
  <strong>How it works:</strong> Only time between consecutive messages &le; 60 min apart is counted as active. Longer gaps (left the session idle) are excluded. Each active burst gets a 10 min buffer for reading/reviewing/testing.
</div>

<h2>Active Hours</h2>
<p style="color:#6b7280;font-size:13px;margin-bottom:12px;">Actual time using Claude Code per day (overlapping project sessions merged).</p>
<div class="month-pager">
  <button id="prev-active" onclick="changeActive(-1)">&larr; Prev</button>
  <span class="page-info" id="active-info"></span>
  <button id="next-active" onclick="changeActive(1)">Next &rarr;</button>
</div>
{active_week_html}

<h2>Week View</h2>
<p style="color:#6b7280;font-size:13px;margin-bottom:12px;">Per-project breakdown (may overlap when projects run simultaneously).</p>
<div class="month-pager">
  <button id="prev-week" onclick="changeWeek(-1)">&larr; Prev</button>
  <span class="page-info" id="week-info"></span>
  <button id="next-week" onclick="changeWeek(1)">Next &rarr;</button>
</div>
{week_html}

<h2>Month Calendar</h2>
<div class="month-pager">
  <button id="prev-month" onclick="changePage(-1)">&larr; Prev</button>
  <span class="page-info" id="page-info"></span>
  <button id="next-month" onclick="changePage(1)">Next &rarr;</button>
</div>
{cal_html}

<h2>Summary</h2>
{summary_html}

<h2>Detailed Breakdown</h2>
{details_html}

<script>
(function() {{
  const pages = document.querySelectorAll('.month-page');
  const totalPages = pages.length;
  let currentPage = totalPages - 1; // start on latest month

  function updatePager() {{
    pages.forEach((p, i) => p.style.display = i === currentPage ? 'block' : 'none');
    document.getElementById('prev-month').disabled = currentPage === 0;
    document.getElementById('next-month').disabled = currentPage === totalPages - 1;
    document.getElementById('page-info').textContent = (currentPage + 1) + ' / ' + totalPages;
  }}

  window.changePage = function(delta) {{
    currentPage = Math.max(0, Math.min(totalPages - 1, currentPage + delta));
    updatePager();
  }};

  // Keyboard navigation.
  document.addEventListener('keydown', function(e) {{
    if (e.key === 'ArrowLeft') changePage(-1);
    if (e.key === 'ArrowRight') changePage(1);
  }});

  updatePager();

  // Week pager.
  const weekPages = document.querySelectorAll('.week-page');
  const totalWeeks = weekPages.length;
  let currentWeek = totalWeeks - 1;

  function updateWeekPager() {{
    weekPages.forEach((p, i) => p.style.display = i === currentWeek ? 'block' : 'none');
    document.getElementById('prev-week').disabled = currentWeek === 0;
    document.getElementById('next-week').disabled = currentWeek === totalWeeks - 1;
    document.getElementById('week-info').textContent = (currentWeek + 1) + ' / ' + totalWeeks;
  }}

  window.changeWeek = function(delta) {{
    currentWeek = Math.max(0, Math.min(totalWeeks - 1, currentWeek + delta));
    updateWeekPager();
  }};

  updateWeekPager();

  // Active hours pager.
  const activePages = document.querySelectorAll('.active-week-page');
  const totalActive = activePages.length;
  let currentActive = totalActive - 1;

  function updateActivePager() {{
    activePages.forEach((p, i) => p.style.display = i === currentActive ? 'block' : 'none');
    document.getElementById('prev-active').disabled = currentActive === 0;
    document.getElementById('next-active').disabled = currentActive === totalActive - 1;
    document.getElementById('active-info').textContent = (currentActive + 1) + ' / ' + totalActive;
  }}

  window.changeActive = function(delta) {{
    currentActive = Math.max(0, Math.min(totalActive - 1, currentActive + delta));
    updateActivePager();
  }};

  updateActivePager();
}})();
</script>

</body>
</html>"""

    with open(HTML_OUTPUT, "w") as f:
        f.write(html)
    print(f"HTML written to {HTML_OUTPUT}")


def main():
    sessions = load_sessions()
    print(f"Total: {len(sessions)} sessions")

    rows, deduped_daily = aggregate(sessions)
    print(f"Aggregated into {len(rows)} project-day entries")

    color_map = assign_colors(rows)
    summary = build_summary(rows)
    cal_data = build_calendar_data(rows)

    write_csv(rows)
    generate_html(rows, color_map, summary, cal_data, deduped_daily)


if __name__ == "__main__":
    main()
