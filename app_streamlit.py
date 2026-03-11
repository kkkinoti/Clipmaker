import sys
import os
import re
import threading
import queue
import time
import platform
import pandas as pd
import streamlit as st

# =============================================================================
# PAGE CONFIG
# =============================================================================
st.set_page_config(page_title="ClipMaker 1.1 by B4L1 Kinoti Version", page_icon="⚽", layout="wide")

st.markdown("""
<style>
    .block-container { padding-top: 2rem; padding-bottom: 1rem; }
    .stTextInput > label, .stNumberInput > label, .stCheckbox > label { font-weight: 500; }
    .log-box {
        background: #0e1117; color: #00ff88; font-family: 'Courier New', monospace;
        font-size: 13px; padding: 16px; border-radius: 8px;
        height: 280px; overflow-y: auto; white-space: pre-wrap;
        border: 1px solid #2a2a2a;
    }
    h1 { font-size: 2rem !important; }
    .footer {
        text-align: center; color: #555; font-size: 11px;
        padding-top: 8px; padding-bottom: 4px;
    }
    .progress-label {
        font-size: 13px; color: #aaa; margin-bottom: 4px;
    }
    .match-banner {
        background: #1a1f2e; border: 1px solid #2a3a5c; border-radius: 8px;
        padding: 12px 18px; margin-bottom: 12px;
    }
</style>
""", unsafe_allow_html=True)

# =============================================================================
# FILE / FOLDER DIALOG HELPERS (tkinter)
# =============================================================================

def _pick_file_thread(result_queue, filetypes):
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    try:
        if platform.system() == "Windows":
            root.wm_attributes("-topmost", True)
        elif platform.system() == "Darwin":
            os.system("osascript -e 'tell application \"Python\" to activate'")
    except Exception:
        pass
    path = filedialog.askopenfilename(filetypes=filetypes)
    root.destroy()
    result_queue.put(path)

def _pick_folder_thread(result_queue):
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    try:
        if platform.system() == "Windows":
            root.wm_attributes("-topmost", True)
        elif platform.system() == "Darwin":
            os.system("osascript -e 'tell application \"Python\" to activate'")
    except Exception:
        pass
    path = filedialog.askdirectory()
    root.destroy()
    result_queue.put(path)

def browse_file(filetypes):
    q = queue.Queue()
    t = threading.Thread(target=_pick_file_thread, args=(q, filetypes), daemon=True)
    t.start()
    t.join(timeout=60)
    try:
        return q.get_nowait()
    except queue.Empty:
        return ""

def browse_folder():
    q = queue.Queue()
    t = threading.Thread(target=_pick_folder_thread, args=(q,), daemon=True)
    t.start()
    t.join(timeout=60)
    try:
        return q.get_nowait()
    except queue.Empty:
        return ""

# =============================================================================
# CORE TIMING / PERIOD LOGIC
# =============================================================================

PERIOD_MAP = {
    "FirstHalf": 1, "SecondHalf": 2,
    "ExtraTimeFirstHalf": 3, "ExtraTimeSecondHalf": 4,
    1: 1, 2: 2, 3: 3, 4: 4,
}

def to_seconds(timestamp):
    parts = list(map(int, timestamp.strip().split(":")))
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError(f"Invalid timestamp: '{timestamp}' — use MM:SS or HH:MM:SS")

def assign_periods(df, period_column, fallback_row):
    if period_column:
        if period_column not in df.columns:
            raise ValueError(f"Column '{period_column}' not found. Available: {list(df.columns)}")
        df = df.copy()
        df["resolved_period"] = df[period_column].map(PERIOD_MAP)
        if df["resolved_period"].isna().any():
            bad = df[df["resolved_period"].isna()][period_column].unique()
            raise ValueError(f"Unrecognised period values: {bad}")
        df["resolved_period"] = df["resolved_period"].astype(int)
        return df
    if fallback_row is not None:
        df = df.reset_index(drop=True)
        df["resolved_period"] = (df.index >= fallback_row).astype(int) + 1
        return df
    raise ValueError("No period column or fallback row set.")

def match_clock_to_video_time(minute, second, period, period_start, period_offset):
    if period not in period_start:
        raise ValueError(f"Period {period} not configured in kick-off timestamps.")
    offset_min, offset_sec = period_offset[period]
    elapsed = (minute * 60 + second) - (offset_min * 60 + offset_sec)
    if elapsed < 0:
        raise ValueError(f"Negative elapsed at {minute}:{second:02d} P{period}.")
    return period_start[period] + elapsed

def merge_overlapping_windows(windows, min_gap):
    """Only merge windows from the same period."""
    if not windows:
        return []
    merged = [list(windows[0])]
    for start, end, label, period in windows[1:]:
        prev = merged[-1]
        if start <= prev[1] + min_gap and period == prev[3]:
            prev[1] = max(prev[1], end)
            prev[2] = prev[2] + " + " + label
        else:
            merged.append([start, end, label, period])
    return [tuple(w) for w in merged]

def apply_filters(df, config):
    original = len(df)
    if config.get("filter_types"):
        selected = config["filter_types"]
        if selected:
            df = df[df["type"].isin(selected)]
    if config.get("progressive_only"):
        prog_cols = [c for c in ["prog_pass", "prog_carry"] if c in df.columns]
        if prog_cols:
            mask = df[prog_cols].apply(pd.to_numeric, errors="coerce").fillna(0)
            df = df[(mask > 0).any(axis=1)]
    if config.get("xt_min") is not None and "xT" in df.columns:
        xt_min = config["xt_min"]
        if xt_min > 0:
            df = df[pd.to_numeric(df["xT"], errors="coerce").fillna(0) >= xt_min]
    if config.get("top_n") and "xT" in df.columns:
        n = config["top_n"]
        df = df.copy()
        df["_xt_num"] = pd.to_numeric(df["xT"], errors="coerce").fillna(0)
        df = df.nlargest(n, "_xt_num").drop(columns=["_xt_num"])
    return df, original - len(df)

# =============================================================================
# MODULE-LEVEL FFMPEG HELPERS
# =============================================================================

def _get_ffmpeg_binary():
    import shutil
    cmd = shutil.which("ffmpeg")
    if cmd:
        return cmd
    try:
        from moviepy.config import FFMPEG_BINARY
        if os.path.exists(FFMPEG_BINARY):
            return FFMPEG_BINARY
    except Exception:
        pass
    raise ValueError("FFmpeg not found. Please install FFmpeg and ensure it is on your PATH.")

def _get_video_duration(path, ffmpeg_bin):
    import subprocess
    r = subprocess.run([ffmpeg_bin, "-i", path], capture_output=True, text=True)
    output = r.stdout + r.stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", output)
    if not m:
        raise ValueError(f"Could not determine duration of {path}")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

def _cut_clip_ffmpeg(ffmpeg_bin, src_path, start, end, out_path):
    import subprocess
    duration = end - start
    cmd = [
        ffmpeg_bin, "-y",
        "-ss", str(start), "-i", src_path,
        "-t", str(duration),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", "-avoid_negative_ts", "make_zero",
        out_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError(f"FFmpeg error cutting clip: {result.stderr[-500:]}")

def _cut_and_concat_ffmpeg(ffmpeg_bin, clip_specs, out_path, prog_fn):
    """Cut all clips to temp dir, then concatenate. prog_fn(cur, tot, elapsed)."""
    import subprocess, tempfile
    tmp_dir = tempfile.mkdtemp()
    tmp_files = []
    total = len(clip_specs)
    start_time = time.time()

    for i, (src, start, end) in enumerate(clip_specs, 1):
        tmp_path = os.path.join(tmp_dir, f"part_{i:04d}.mp4")
        _cut_clip_ffmpeg(ffmpeg_bin, src, start, end, tmp_path)
        tmp_files.append(tmp_path)
        prog_fn(i, total, time.time() - start_time)

    list_path = os.path.join(tmp_dir, "concat.txt")
    with open(list_path, "w") as f:
        for p in tmp_files:
            f.write(f"file '{p}'\n")

    cmd = [
        ffmpeg_bin, "-y",
        "-f", "concat", "-safe", "0",
        "-i", list_path, "-c", "copy",
        out_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError(f"FFmpeg concat error: {result.stderr[-500:]}")

    for p in tmp_files:
        try: os.remove(p)
        except: pass
    try: os.remove(list_path)
    except: pass
    try: os.rmdir(tmp_dir)
    except: pass

# =============================================================================
# CLIP WINDOW BUILDER  (shared by single-player and batch modes)
# =============================================================================

def _build_clip_windows(df, config, log_fn):
    """
    Map an events DataFrame to a list of clip windows.
    Returns list of (start_sec, end_sec, label, period) tuples.
    """
    period_start = {
        1: to_seconds(config["half1_time"]),
        2: to_seconds(config["half2_time"]),
    }
    if config.get("half3_time", "").strip():
        period_start[3] = to_seconds(config["half3_time"])
    if config.get("half4_time", "").strip():
        period_start[4] = to_seconds(config["half4_time"])

    # WhoScored minute counter continues past 45 into 2nd half — offsets are match-clock minutes
    period_offset = {1: (0, 0), 2: (45, 0), 3: (90, 0), 4: (105, 0)}

    period_col = config.get("period_column") or None
    fallback = config.get("fallback_row")
    df = assign_periods(df.copy(), period_col, fallback)

    # Ensure second column exists (some sources omit it)
    if "second" not in df.columns:
        df["second"] = 0

    half_filter = config.get("half_filter", "Both halves")
    if half_filter == "1st half only":
        df = df[df["resolved_period"] == 1]
        log_fn("  Filtering to 1st half only.")
    elif half_filter == "2nd half only":
        df = df[df["resolved_period"] == 2]
        log_fn("  Filtering to 2nd half only.")

    df, filtered_count = apply_filters(df, config)
    if filtered_count > 0:
        log_fn(f"  Filters removed {filtered_count} events.")

    timestamps = []
    for _, row in df.iterrows():
        try:
            ts = match_clock_to_video_time(
                int(row["minute"]), int(row["second"]),
                int(row["resolved_period"]), period_start, period_offset
            )
            timestamps.append(ts)
        except ValueError as e:
            log_fn(f"  WARNING: {e}")
            timestamps.append(None)

    df = df.copy()
    df["video_timestamp"] = timestamps
    df = df.dropna(subset=["video_timestamp"]).sort_values("video_timestamp")

    raw_windows = []
    for _, row in df.iterrows():
        ts = row["video_timestamp"]
        period = int(row["resolved_period"])
        label = f"{row['type']} @ {int(row['minute'])}:{int(row['second']):02d} (P{period})"
        raw_windows.append((
            ts - config["before_buffer"],
            ts + config["after_buffer"],
            label, period
        ))

    windows = merge_overlapping_windows(raw_windows, config["min_gap"])
    log_fn(f"  {len(df)} events → {len(windows)} clips after merging.")
    return windows

# =============================================================================
# SINGLE-PLAYER RENDERER  (original thread-based mode for manual CSV use)
# =============================================================================

def monitor_file_progress(out_path, total_frames, fps, progress_queue, stop_event):
    for _ in range(20):
        if os.path.exists(out_path):
            break
        time.sleep(0.5)
    estimated_bytes = (total_frames / max(fps, 1)) * 250_000
    start_time = time.time()
    while not stop_event.is_set():
        try:
            current_bytes = os.path.getsize(out_path)
            frac = min(current_bytes / estimated_bytes, 0.99)
            current_frame = int(frac * total_frames)
            elapsed = time.time() - start_time
            progress_queue.put({
                "current": current_frame, "total": total_frames,
                "elapsed": elapsed, "phase": "assembly"
            })
        except Exception:
            pass
        time.sleep(0.5)

def run_clip_maker(config, log_queue, progress_queue):
    """Single-player reel renderer. Accepts config['dataframe'] or config['data_file']."""
    def log(msg):
        log_queue.put({"type": "log", "msg": msg})
    def prog(current, total, elapsed):
        progress_queue.put({"current": current, "total": total, "elapsed": elapsed, "phase": "clips"})

    try:
        if config.get("dataframe") is not None:
            df = config["dataframe"].copy()
        else:
            df = pd.read_csv(config["data_file"])

        for col in ["minute", "type"]:
            if col not in df.columns:
                raise ValueError(f"Data missing column: '{col}'")

        split_video = config.get("split_video", False)
        windows = _build_clip_windows(df, config, log)
        log(f"Found {len(windows)} total clips.\n")

        if config.get("dry_run"):
            for i, (s, e, lbl, p) in enumerate(windows, 1):
                log(f"  Clip {i:02d}: {s:.1f}s – {e:.1f}s  ({e-s:.0f}s)  |  {lbl}")
            log("\n✓ DRY RUN complete.")
            log_queue.put({"type": "done"})
            return

        ffmpeg_bin = _get_ffmpeg_binary()
        video1_path = config["video_file"].strip().strip("\"'")
        video1_duration = _get_video_duration(video1_path, ffmpeg_bin)
        log(f"Video 1 duration: {video1_duration:.2f}s")

        video2_path_str = None
        video2_duration = None
        if split_video and config.get("video2_file"):
            video2_path_str = config["video2_file"].strip().strip("\"'")
            video2_duration = _get_video_duration(video2_path_str, ffmpeg_bin)
            log(f"Video 2 duration: {video2_duration:.2f}s  [two-file mode]")

        def get_src(period):
            if split_video and video2_path_str and period >= 2:
                return video2_path_str, video2_duration
            return video1_path, video1_duration

        out_dir = config["output_dir"]
        os.makedirs(out_dir, exist_ok=True)
        total_clips = len(windows)
        start_time = time.time()

        if config.get("individual_clips"):
            saved = []
            for i, (start, end, label, period) in enumerate(windows, 1):
                src, src_dur = get_src(period)
                s, e = max(0, start), min(src_dur, end)
                if e <= s:
                    log(f"  SKIPPED clip {i:02d}: out of video bounds")
                    continue
                actions = [pt.split(" @")[0].strip() for pt in label.split(" + ")]
                dominant = max(set(actions), key=actions.count).replace(" ", "_")
                filename = f"{i:02d}_{dominant}.mp4"
                filepath = os.path.join(out_dir, filename)
                log(f"  Rendering {i:02d}/{total_clips}: {filename}")
                _cut_clip_ffmpeg(ffmpeg_bin, src, s, e, filepath)
                saved.append(filepath)
                prog(i, total_clips, time.time() - start_time)
            log(f"\n✓ {len(saved)} clips saved to: {os.path.abspath(out_dir)}/")
        else:
            clip_specs = []
            for start, end, label, period in windows:
                src, src_dur = get_src(period)
                s, e = max(0, start), min(src_dur, end)
                if e > s:
                    clip_specs.append((src, s, e))

            total_dur = sum(e - s for _, s, e in clip_specs)
            log(f"Assembling {len(clip_specs)} clips ({total_dur:.1f}s)...")
            out_path = os.path.join(out_dir, config["output_filename"])

            assembly_start = time.time()
            stop_event = threading.Event()
            fps_est = 25
            total_frames = int(total_dur * fps_est)
            monitor_thread = threading.Thread(
                target=monitor_file_progress,
                args=(out_path, total_frames, fps_est, progress_queue, stop_event),
                daemon=True
            )
            monitor_thread.start()

            def _assembly_prog(cur, tot, elapsed):
                progress_queue.put({"current": cur, "total": tot, "elapsed": elapsed, "phase": "clips"})

            _cut_and_concat_ffmpeg(ffmpeg_bin, clip_specs, out_path, _assembly_prog)
            stop_event.set()
            monitor_thread.join()
            log(f"\n✓ Saved to: {out_path}")

        log_queue.put({"type": "done"})

    except Exception as exc:
        log(f"\n✗ ERROR: {exc}")
        log_queue.put({"type": "error"})

# =============================================================================
# BATCH RENDERER  (multi-player, one reel per player)
# =============================================================================

def _safe_name(s):
    """Sanitise a string for use in a filename."""
    return re.sub(r"[^\w]", "_", str(s)).strip("_")

def run_batch_reels(batch_config, log_queue, progress_queue):
    """
    Render one concatenated highlight reel per selected player.
    Posts progress messages with batch-level metadata so the UI can
    display 'Rendering reel N of M: <Player>'.
    """
    def log(msg):
        log_queue.put({"type": "log", "msg": msg})

    def batch_prog(batch_cur, batch_tot, player_name, clip_cur=0, clip_tot=1, elapsed=0.0):
        progress_queue.put({
            "phase": "batch",
            "batch_current": batch_cur,
            "batch_total": batch_tot,
            "player": player_name,
            "current": clip_cur,
            "total": clip_tot,
            "elapsed": elapsed,
        })

    try:
        players     = batch_config["players"]
        full_df     = batch_config["full_df"]
        home_team   = batch_config["home_team"]
        away_team   = batch_config["away_team"]
        match_date  = batch_config.get("match_date", "")
        exports_dir = batch_config["exports_dir"]
        base_config = batch_config["base_config"]
        n_players   = len(players)

        os.makedirs(exports_dir, exist_ok=True)

        # Load video file(s) once — shared across all reels
        ffmpeg_bin = _get_ffmpeg_binary()
        video1_path = base_config["video_file"].strip().strip("\"'")
        video1_duration = _get_video_duration(video1_path, ffmpeg_bin)
        log(f"Video loaded: {os.path.basename(video1_path)} ({video1_duration:.1f}s)")

        split_video     = base_config.get("split_video", False)
        video2_path_str = None
        video2_duration = None
        if split_video and base_config.get("video2_file"):
            video2_path_str = base_config["video2_file"].strip().strip("\"'")
            video2_duration = _get_video_duration(video2_path_str, ffmpeg_bin)
            log(f"Video 2 loaded: {os.path.basename(video2_path_str)} ({video2_duration:.1f}s)")

        def get_src(period):
            if split_video and video2_path_str and period >= 2:
                return video2_path_str, video2_duration
            return video1_path, video1_duration

        # Build date string for filenames
        date_str = re.sub(r"[^\d]", "", str(match_date))[:8] or "unknown"

        results = []

        for idx, player_name in enumerate(players):
            batch_prog(idx + 1, n_players, player_name)
            log(f"\n{'─'*52}")
            log(f"[{idx + 1}/{n_players}]  {player_name}")
            log(f"{'─'*52}")

            player_df = full_df[full_df["player"] == player_name].copy()
            if player_df.empty:
                log(f"⚠  No events found for {player_name}. Skipping.")
                results.append({"Player": player_name, "Status": "⚠ No events", "File": "—"})
                continue

            # Dynamic opponent logic
            player_team = player_df["team"].iloc[0] if "team" in player_df.columns else None
            if player_team and player_team == home_team:
                opponent = away_team
            elif player_team:
                opponent = home_team
            else:
                opponent = "Unknown"

            log(f"  Team: {player_team or '?'}  →  Opponent: {opponent}")

            # Build output filename
            out_filename = f"{_safe_name(player_name)}_vs_{_safe_name(opponent)}_{date_str}.mp4"
            out_path = os.path.join(exports_dir, out_filename)

            # Build clip windows
            try:
                windows = _build_clip_windows(player_df, base_config, log)
            except Exception as win_err:
                log(f"✗  Could not build clip windows: {win_err}")
                results.append({"Player": player_name, "Status": f"✗ {win_err}", "File": "—"})
                continue

            if not windows:
                log("⚠  No clips generated (check kick-off timestamps and period column).")
                results.append({"Player": player_name, "Status": "⚠ No clips", "File": "—"})
                continue

            if base_config.get("dry_run"):
                for i, (s, e, lbl, _p) in enumerate(windows, 1):
                    log(f"  Clip {i:02d}: {s:.1f}s – {e:.1f}s  ({e-s:.0f}s)  |  {lbl}")
                results.append({
                    "Player": player_name,
                    "Status": f"✓ Dry-run ({len(windows)} clips)",
                    "File": out_filename,
                })
                continue

            # Gather clip specs, clamped to video bounds
            clip_specs = []
            for start, end, label, period in windows:
                src, src_dur = get_src(period)
                s, e = max(0.0, start), min(src_dur, end)
                if e > s:
                    clip_specs.append((src, s, e))
                else:
                    log(f"  SKIPPED: {label} (out of video bounds)")

            if not clip_specs:
                log("⚠  All clips were out of video bounds. Skipping.")
                results.append({"Player": player_name, "Status": "⚠ All out-of-bounds", "File": "—"})
                continue

            total_dur = sum(e - s for _, s, e in clip_specs)
            log(f"  Assembling {len(clip_specs)} clips ({total_dur:.1f}s) → {out_filename}")

            clip_start_time = time.time()

            def make_prog_fn(b_cur, b_tot, pname):
                def _prog(cur, tot, elapsed):
                    progress_queue.put({
                        "phase": "batch",
                        "batch_current": b_cur,
                        "batch_total": b_tot,
                        "player": pname,
                        "current": cur,
                        "total": tot,
                        "elapsed": elapsed,
                    })
                return _prog

            prog_fn = make_prog_fn(idx + 1, n_players, player_name)

            try:
                _cut_and_concat_ffmpeg(ffmpeg_bin, clip_specs, out_path, prog_fn)
                log(f"✓  Saved → {out_path}")
                results.append({
                    "Player": player_name,
                    "Status": f"✓ {len(clip_specs)} clips",
                    "File": out_filename,
                    "Path": os.path.abspath(out_path),
                })
            except Exception as render_err:
                log(f"✗  Render failed: {render_err}")
                results.append({
                    "Player": player_name,
                    "Status": f"✗ {render_err}",
                    "File": "—",
                })

        # Final summary
        ok = sum(1 for r in results if r["Status"].startswith("✓"))
        log(f"\n{'═'*52}")
        log(f"BATCH COMPLETE  {ok}/{n_players} reels rendered successfully.")
        log(f"Output folder:  {os.path.abspath(exports_dir)}")
        log(f"{'═'*52}")

        progress_queue.put({
            "phase": "batch_done",
            "results": results,
            "current": n_players, "total": n_players, "elapsed": 0,
        })
        log_queue.put({"type": "done"})

    except Exception as exc:
        log(f"\n✗ BATCH ERROR: {exc}")
        log_queue.put({"type": "error"})

# =============================================================================
# SESSION STATE
# =============================================================================
_state_defaults = {
    "video_path":        "",
    "video2_path":       "",
    "csv_path":          "",
    "output_dir":        "",
    "scraped_csv_path":  "",
    # Batch / multi-player state
    "full_df":           None,   # complete match events DataFrame
    "home_team":         "",
    "away_team":         "",
    "match_date":        "",
    "match_id_stored":   "",
    "selected_players":  [],
    "batch_results":     [],     # results from last batch render
}
for key, default in _state_defaults.items():
    if key not in st.session_state:
        st.session_state[key] = default

# =============================================================================
# TITLE & TABS
# =============================================================================
st.title("⚽ ClipMaker 1.1 by B4L1")
st.caption("Multi-Player Batch Highlight Processor · Powered by WhoScored & FFmpeg")

tab_scrape, tab_render = st.tabs(["🔍 Scrape Data", "🎬 Render Video"])

# =============================================================================
# TAB 1 — SCRAPE DATA  (fetch ALL events, select players)
# =============================================================================
with tab_scrape:
    st.subheader("WhoScored Match Scraper")
    st.markdown("Fetch **all** events for a match, then pick which players to highlight.")

    sc_col1, sc_col2 = st.columns([2, 1])
    with sc_col1:
        season = st.selectbox("Season", ["2526", "2425"], index=0)
        league = st.text_input("League", "ENG-Premier League")
    with sc_col2:
        headless = st.checkbox("Run in background (headless browser)", value=False,
                               help="Suppress the browser window while scraping.")

    m_col1, m_col2 = st.columns(2)
    with m_col1:
        match_id_input = st.text_input("Match ID", value=st.session_state.match_id_stored,
                                       placeholder="e.g. 1903304")

    if st.button("🚀 Fetch All Events", type="primary"):
        if not match_id_input:
            st.error("Please provide a Match ID.")
        else:
            try:
                import soccerdata as sd
            except ImportError:
                st.error(
                    "`soccerdata` is not installed. "
                    "Activate the venv and run: `pip install soccerdata`"
                )
                st.stop()

            with st.status("Connecting to WhoScored...", expanded=True) as status:
                try:
                    ws = sd.WhoScored(leagues=league, seasons=season,
                                      no_cache=True, headless=headless)

                    status.write("📥 Downloading all match events...")
                    events = ws.read_events(match_id=int(match_id_input))

                    # Flatten MultiIndex so all index levels become regular columns
                    if hasattr(events.index, "names") and events.index.names[0] is not None:
                        events = events.reset_index()

                    # Ensure required columns
                    if "second" not in events.columns:
                        events["second"] = 0
                    if "event_type" not in events.columns and "type" in events.columns:
                        events["event_type"] = events["type"]

                    # ── Match metadata ─────────────────────────────────────────
                    status.write("📋 Extracting match metadata...")
                    team_col = next(
                        (c for c in ["team", "team_name", "club"] if c in events.columns),
                        None
                    )
                    teams_found = (
                        events[team_col].dropna().unique().tolist() if team_col else []
                    )
                    home_team  = teams_found[0] if len(teams_found) > 0 else "Home"
                    away_team  = teams_found[1] if len(teams_found) > 1 else "Away"
                    match_date = ""

                    # Try schedule for authoritative home/away + date
                    try:
                        status.write("📅 Fetching schedule for home/away designation...")
                        schedule = ws.read_schedule()
                        sched = schedule.reset_index() if hasattr(schedule.index, "names") else schedule.copy()
                        id_col = next(
                            (c for c in sched.columns
                             if c.lower() in {"game", "game_id", "match_id", "id"}),
                            None
                        )
                        if id_col:
                            hit = sched[sched[id_col].astype(str) == str(match_id_input)]
                            if not hit.empty:
                                row = hit.iloc[0]
                                h_col = next((c for c in row.index if "home" in c.lower() and "team" in c.lower()), None)
                                a_col = next((c for c in row.index if "away" in c.lower() and "team" in c.lower()), None)
                                d_col = next((c for c in row.index if "date" in c.lower()), None)
                                if h_col: home_team  = str(row[h_col])
                                if a_col: away_team  = str(row[a_col])
                                if d_col: match_date = str(row[d_col])
                    except Exception as sched_err:
                        status.write(f"ℹ️ Schedule unavailable ({sched_err}), using event data.")

                    # Store everything in session state
                    st.session_state.full_df        = events
                    st.session_state.home_team       = home_team
                    st.session_state.away_team       = away_team
                    st.session_state.match_date      = match_date
                    st.session_state.match_id_stored = match_id_input
                    st.session_state.selected_players = []  # reset on new fetch
                    st.session_state.batch_results    = []

                    status.update(label="✅ Events loaded!", state="complete")

                except Exception as exc:
                    st.error(f"Error: {exc}")
                    st.stop()

    # ── Show match data + player selector (persists across reruns) ────────────
    if st.session_state.full_df is not None:
        ht = st.session_state.home_team
        at = st.session_state.away_team
        dt = st.session_state.match_date
        full_df = st.session_state.full_df

        st.markdown(
            f'<div class="match-banner">'
            f'<b>🏟  {ht}  vs  {at}</b>'
            + (f'  ·  📅 {dt}' if dt else '')
            + f'  ·  Match ID: <code>{st.session_state.match_id_stored}</code>'
            f'</div>',
            unsafe_allow_html=True,
        )

        # All unique players in the match
        player_col = "player" if "player" in full_df.columns else None
        if player_col:
            all_players = sorted(full_df[player_col].dropna().unique().tolist())

            st.markdown(f"**{len(all_players)} players found in this match.**  "
                        "Select the ones you want to render reels for:")

            selected = st.multiselect(
                "Players to highlight",
                options=all_players,
                default=st.session_state.selected_players,
                placeholder="Search and select players...",
            )
            st.session_state.selected_players = selected

            if selected:
                st.success(
                    f"{len(selected)} player(s) selected. "
                    "Switch to the **Render Video** tab and click **Process All Reels**."
                )
                # Preview event counts per player
                counts = (
                    full_df[full_df[player_col].isin(selected)]
                    .groupby(player_col)
                    .size()
                    .reset_index(name="Events")
                    .rename(columns={player_col: "Player"})
                    .sort_values("Player")
                )
                team_col = next(
                    (c for c in full_df.columns if c in ["team", "team_name", "club"]), None
                )
                if team_col:
                    team_map = (
                        full_df[full_df[player_col].isin(selected)]
                        .drop_duplicates(player_col)
                        .set_index(player_col)[team_col]
                    )
                    counts["Team"] = counts["Player"].map(team_map)
                st.dataframe(counts, use_container_width=True, hide_index=True)
        else:
            st.warning("Could not find a 'player' column in the events data.")

# =============================================================================
# TAB 2 — RENDER VIDEO
# =============================================================================
with tab_render:
    # ── Shared video & timing settings ───────────────────────────────────────
    st.subheader("Video & Timing")

    left, right = st.columns([1, 1], gap="large")

    with left:
        split_video = st.checkbox("Match is split into two separate video files (1st/2nd half)")

        vc1, vc2 = st.columns([4, 1])
        with vc1:
            lbl1 = "1st Half Video File" if split_video else "Video File"
            video_path = st.text_input(lbl1, value=st.session_state.video_path,
                                       placeholder="Click Browse or paste full path")
        with vc2:
            st.write(""); st.write("")
            if st.button("Browse", key="browse_video"):
                picked = browse_file([("Video files", "*.mp4 *.mkv *.avi *.mov"), ("All files", "*.*")])
                if picked:
                    st.session_state.video_path = picked
                    st.rerun()

        if split_video:
            v2c1, v2c2 = st.columns([4, 1])
            with v2c1:
                video2_path = st.text_input("2nd Half Video File", value=st.session_state.video2_path,
                                            placeholder="Click Browse or paste full path")
            with v2c2:
                st.write(""); st.write("")
                if st.button("Browse", key="browse_video2"):
                    picked = browse_file([("Video files", "*.mp4 *.mkv *.avi *.mov"), ("All files", "*.*")])
                    if picked:
                        st.session_state.video2_path = picked
                        st.rerun()
        else:
            video2_path = ""

        st.subheader("Kick-off Timestamps")
        if split_video:
            st.caption("Timestamps relative to the START of each video file")
        else:
            st.caption("Exactly what your video player shows — MM:SS or HH:MM:SS")

        tc1, tc2 = st.columns(2)
        with tc1:
            half1 = st.text_input("1st Half kick-off", placeholder="e.g. 4:16")
            half3 = st.text_input("ET 1st Half (optional)", placeholder="leave blank")
        with tc2:
            half2 = st.text_input("2nd Half kick-off",
                                   placeholder="e.g. 0:45" if split_video else "e.g. 1:00:32")
            half4 = st.text_input("ET 2nd Half (optional)", placeholder="leave blank")

    with right:
        st.subheader("Half Detection")
        period_col = st.text_input(
            "Period Column Name", value="period",
            help="The CSV/DataFrame column: FirstHalf/SecondHalf or 1/2. WhoScored data uses 'period'."
        )
        fallback_row = st.number_input("Fallback Row Index", min_value=0, value=0, step=1,
                                       help="Row where 2nd half begins (only when period column is blank).")
        use_fallback = st.checkbox("Use fallback row index instead of period column")

        st.subheader("Clip Settings")
        sc1, sc2, sc3 = st.columns(3)
        with sc1:
            before_buf = st.number_input("Before (s)", value=3, min_value=0)
        with sc2:
            after_buf  = st.number_input("After (s)",  value=8, min_value=0)
        with sc3:
            min_gap    = st.number_input("Merge Gap (s)", value=6, min_value=0,
                                         help="Events within this gap are merged into one clip.")

        half_filter = st.selectbox(
            "Halves to include",
            options=["Both halves", "1st half only", "2nd half only"],
        )
        dry_run = st.checkbox("Dry Run (preview clips without rendering)")

    # Shared config dict used by both batch and single-player modes
    _shared_config = {
        "video_file":    st.session_state.video_path or video_path,
        "video2_file":   (st.session_state.video2_path or video2_path).strip().strip("\"'"),
        "split_video":   split_video,
        "half1_time":    half1,
        "half2_time":    half2,
        "half3_time":    half3 or "",
        "half4_time":    half4 or "",
        "period_column": "" if use_fallback else period_col,
        "fallback_row":  int(fallback_row) if use_fallback else None,
        "before_buffer": before_buf,
        "after_buffer":  after_buf,
        "min_gap":       min_gap,
        "half_filter":   half_filter,
        "dry_run":       dry_run,
        # Filters (no-ops by default; overridable in single-player section)
        "filter_types":  [],
        "progressive_only": False,
        "xt_min":        0.0,
        "top_n":         None,
        "individual_clips": False,
    }

    st.divider()

    # ═══════════════════════════════════════════════════════════════════════
    # SECTION A — BATCH PROCESSOR
    # ═══════════════════════════════════════════════════════════════════════
    has_data    = st.session_state.full_df is not None
    has_players = bool(st.session_state.selected_players)

    st.subheader("🎞 Batch Processor")

    if not has_data:
        st.info("No match data loaded. Go to the **Scrape Data** tab and click **Fetch All Events** first.")
    elif not has_players:
        st.info("No players selected. Go to the **Scrape Data** tab and choose players from the multiselect.")
    else:
        ht = st.session_state.home_team
        at = st.session_state.away_team
        dt = st.session_state.match_date
        n  = len(st.session_state.selected_players)

        st.markdown(
            f'<div class="match-banner">'
            f'<b>🏟  {ht}  vs  {at}</b>'
            + (f'  ·  📅 {dt}' if dt else '')
            + f'  ·  <b>{n} player(s)</b> selected'
            f'</div>',
            unsafe_allow_html=True,
        )

        exports_dir_input = st.text_input(
            "Exports Folder", value="exports",
            help="All reels will be saved here. Relative to the app's working directory."
        )

        batch_btn = st.button("▶▶  Process All Reels", type="primary", use_container_width=False)

        batch_progress_ph = st.empty()
        batch_log_ph      = st.empty()
        batch_results_ph  = st.empty()

        if batch_btn:
            _vid = st.session_state.video_path or video_path
            _errors = []
            if not _vid and not dry_run:
                _errors.append("Video file is required.")
            if not half1:
                _errors.append("1st Half kick-off timestamp is required.")
            if not half2:
                _errors.append("2nd Half kick-off timestamp is required.")

            if _errors:
                for err in _errors:
                    st.error(err)
            else:
                batch_config = {
                    "players":     st.session_state.selected_players,
                    "full_df":     st.session_state.full_df,
                    "home_team":   st.session_state.home_team,
                    "away_team":   st.session_state.away_team,
                    "match_date":  st.session_state.match_date,
                    "exports_dir": exports_dir_input or "exports",
                    "base_config": _shared_config,
                }

                log_queue      = queue.Queue()
                progress_queue = queue.Queue()
                log_lines      = []
                last_prog      = {"phase": "batch", "batch_current": 0, "batch_total": n,
                                  "player": "", "current": 0, "total": 1, "elapsed": 0}

                batch_thread = threading.Thread(
                    target=run_batch_reels,
                    args=(batch_config, log_queue, progress_queue),
                    daemon=True,
                )
                batch_thread.start()

                batch_done    = False
                final_results = []

                while batch_thread.is_alive() or not log_queue.empty() or not progress_queue.empty():
                    # Drain progress
                    while not progress_queue.empty():
                        msg = progress_queue.get_nowait()
                        if msg.get("phase") == "batch_done":
                            batch_done    = True
                            final_results = msg.get("results", [])
                        else:
                            last_prog = msg

                    # Drain log
                    updated = False
                    while not log_queue.empty():
                        msg = log_queue.get_nowait()
                        if msg["type"] == "log":
                            log_lines.append(msg["msg"])
                            updated = True

                    # Render progress bar
                    b_cur = last_prog.get("batch_current", 0)
                    b_tot = last_prog.get("batch_total", n)
                    pname = last_prog.get("player", "")
                    c_cur = last_prog.get("current", 0)
                    c_tot = last_prog.get("total", 1)
                    c_ela = last_prog.get("elapsed", 0)

                    # Smooth combined fraction: completed reels + clip progress within current reel
                    clip_frac  = (c_cur / c_tot) if c_tot > 0 else 0
                    outer_frac = ((max(b_cur, 1) - 1 + clip_frac) / b_tot) if b_tot > 0 else 0
                    outer_frac = max(0.0, min(outer_frac, 0.999))

                    if pname:
                        eta_str = ""
                        if c_cur > 0 and c_ela > 0:
                            rate = c_cur / c_ela
                            rem  = (c_tot - c_cur) / rate
                            eta_str = f"  —  ~{int(rem // 60)}m {int(rem % 60):02d}s left on this reel"
                        label_str = (
                            f"Rendering reel {b_cur} of {b_tot}: **{pname}**  "
                            f"(clip {c_cur}/{c_tot}{eta_str})"
                        )
                    else:
                        label_str = "Preparing batch..."

                    with batch_progress_ph.container():
                        st.markdown(
                            f'<div class="progress-label">{label_str}</div>',
                            unsafe_allow_html=True,
                        )
                        st.progress(outer_frac)

                    if updated:
                        batch_log_ph.markdown(
                            f'<div class="log-box">{"<br>".join(log_lines)}</div>',
                            unsafe_allow_html=True,
                        )

                    time.sleep(0.3)

                batch_thread.join()

                # Final log flush
                while not log_queue.empty():
                    msg = log_queue.get_nowait()
                    if msg["type"] == "log":
                        log_lines.append(msg["msg"])
                while not progress_queue.empty():
                    msg = progress_queue.get_nowait()
                    if msg.get("phase") == "batch_done":
                        final_results = msg.get("results", final_results)

                batch_log_ph.markdown(
                    f'<div class="log-box">{"<br>".join(log_lines)}</div>',
                    unsafe_allow_html=True,
                )
                batch_progress_ph.empty()

                # Store results and show summary
                st.session_state.batch_results = final_results
                if final_results:
                    ok_count = sum(1 for r in final_results if r.get("Status", "").startswith("✓"))
                    if ok_count == len(final_results):
                        batch_results_ph.success(
                            f"All {ok_count} reel(s) rendered successfully — "
                            f"saved to `{os.path.abspath(exports_dir_input or 'exports')}`"
                        )
                    else:
                        batch_results_ph.warning(f"{ok_count} of {len(final_results)} reel(s) rendered.")

        # Always show last batch results table if available
        if st.session_state.batch_results:
            df_results = pd.DataFrame(st.session_state.batch_results)
            display_cols = [c for c in ["Player", "Status", "File"] if c in df_results.columns]
            st.dataframe(df_results[display_cols], use_container_width=True, hide_index=True)

    st.divider()

    # ═══════════════════════════════════════════════════════════════════════
    # SECTION B — SINGLE PLAYER (manual CSV mode, inside expander)
    # ═══════════════════════════════════════════════════════════════════════
    with st.expander("🎛  Single-Player / Manual Mode", expanded=not has_data):
        st.caption(
            "Use this section if you have a pre-made CSV and want to render one reel manually, "
            "or to test settings before running a batch."
        )

        sp_left, sp_right = st.columns([1, 1], gap="large")

        with sp_left:
            cc1, cc2 = st.columns([4, 1])
            with cc1:
                csv_path = st.text_input(
                    "CSV File", value=st.session_state.csv_path,
                    placeholder="Click Browse or paste full path",
                    help="CSV with columns: period, minute, second, type"
                )
            with cc2:
                st.write(""); st.write("")
                if st.button("Browse", key="browse_csv"):
                    picked = browse_file([("CSV files", "*.csv"), ("All files", "*.*")])
                    if picked:
                        st.session_state.csv_path = picked
                        st.rerun()

            st.subheader("Action Filters")
            st.caption("Leave all blank to include every action")

            final_csv_for_filter = st.session_state.csv_path or csv_path
            action_types, has_xt, has_prog = [], False, False
            if final_csv_for_filter and os.path.exists(final_csv_for_filter):
                try:
                    _df = pd.read_csv(final_csv_for_filter)
                    action_types = sorted(_df["type"].dropna().unique().tolist()) if "type" in _df.columns else []
                    has_xt   = "xT" in _df.columns
                    has_prog = any(c in _df.columns for c in ["prog_pass", "prog_carry"])
                except Exception:
                    pass

            filter_types = st.multiselect(
                "Action Types to Include", options=action_types,
                placeholder="All types if blank" if action_types else "Load a CSV first",
            )
            fc1, fc2 = st.columns(2)
            with fc1:
                progressive_only = st.checkbox(
                    "Progressive only", disabled=not has_prog,
                    help="Only prog_pass or prog_carry > 0"
                )
            with fc2:
                xt_min = st.number_input(
                    "Min xT", min_value=0.0, value=0.0, step=0.001, format="%.3f",
                    disabled=not has_xt,
                )
            top_n = st.number_input(
                "Top N by xT (0 = all)", min_value=0, value=0, step=1, disabled=not has_xt,
            )

        with sp_right:
            sp_oc1, sp_oc2 = st.columns([4, 1])
            with sp_oc1:
                out_dir_input = st.text_input("Output Folder", value=st.session_state.output_dir,
                                              placeholder="Click Browse to choose folder")
            with sp_oc2:
                st.write(""); st.write("")
                if st.button("Browse", key="browse_out"):
                    picked = browse_folder()
                    if picked:
                        st.session_state.output_dir = picked
                        st.rerun()

            individual = st.checkbox("Save individual clips instead of one combined reel")
            out_filename = st.text_input("Output Filename", value="Highlights.mp4") if not individual else "Highlights.mp4"

        sp_run_col, _ = st.columns([1, 3])
        with sp_run_col:
            run_btn = st.button("▶  Run ClipMaker", type="secondary", use_container_width=True)

        sp_progress_ph = st.empty()
        sp_log_ph      = st.empty()

        final_video   = st.session_state.video_path or video_path
        final_video2  = st.session_state.video2_path or video2_path
        final_csv     = st.session_state.csv_path or csv_path
        final_out_dir = st.session_state.output_dir or out_dir_input or "output"

        if run_btn:
            sp_errors = []
            if not final_video and not dry_run:
                sp_errors.append("Video file is required.")
            if not final_csv:
                sp_errors.append("CSV file is required.")
            if not half1:
                sp_errors.append("1st Half kick-off timestamp is required.")
            if not half2:
                sp_errors.append("2nd Half kick-off timestamp is required.")

            if sp_errors:
                for err in sp_errors:
                    st.error(err)
            else:
                sp_config = {
                    **_shared_config,
                    "video_file":    final_video,
                    "video2_file":   final_video2.strip().strip("\"'"),
                    "data_file":     final_csv,
                    "output_dir":    final_out_dir,
                    "output_filename": out_filename,
                    "individual_clips": individual,
                    "filter_types":  filter_types,
                    "progressive_only": progressive_only,
                    "xt_min":        xt_min,
                    "top_n":         int(top_n) if top_n > 0 else None,
                }

                log_queue      = queue.Queue()
                progress_queue = queue.Queue()
                log_lines      = []
                last_progress  = {"current": 0, "total": 1, "elapsed": 0}

                thread = threading.Thread(
                    target=run_clip_maker,
                    args=(sp_config, log_queue, progress_queue),
                    daemon=True,
                )
                thread.start()

                while thread.is_alive() or not log_queue.empty():
                    while not progress_queue.empty():
                        last_progress = progress_queue.get_nowait()

                    updated = False
                    while not log_queue.empty():
                        msg = log_queue.get_nowait()
                        if msg["type"] == "log":
                            log_lines.append(msg["msg"])
                            updated = True

                    cur  = last_progress["current"]
                    tot  = last_progress["total"]
                    ela  = last_progress["elapsed"]
                    frac = cur / tot if tot > 0 else 0
                    phase = last_progress.get("phase", "clips")

                    eta_str = "Calculating..."
                    if cur > 0 and ela > 0:
                        rate = cur / ela
                        rem  = (tot - cur) / rate
                        eta_str = f"{int(rem // 60)}m {int(rem % 60):02d}s remaining"

                    if phase == "assembly":
                        label_str = ("Finalising..." if frac >= 0.99
                                     else f"Assembling — frame {cur:,}/{tot:,} — {eta_str}")
                    else:
                        label_str = f"Clip {cur} of {tot} — {eta_str}"

                    with sp_progress_ph.container():
                        st.markdown(
                            f'<div class="progress-label">{label_str}</div>',
                            unsafe_allow_html=True,
                        )
                        st.progress(frac)

                    if updated:
                        sp_log_ph.markdown(
                            f'<div class="log-box">{"<br>".join(log_lines)}</div>',
                            unsafe_allow_html=True,
                        )

                    time.sleep(0.3)

                thread.join()

                while not log_queue.empty():
                    msg = log_queue.get_nowait()
                    if msg["type"] == "log":
                        log_lines.append(msg["msg"])

                sp_log_ph.markdown(
                    f'<div class="log-box">{"<br>".join(log_lines)}</div>',
                    unsafe_allow_html=True,
                )
                sp_progress_ph.empty()

# =============================================================================
# FOOTER
# =============================================================================
st.markdown('<div class="footer">@B03GHB4L1</div>', unsafe_allow_html=True)
