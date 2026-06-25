#!/usr/bin/env python3
"""
gui_plotter.py  —  Team Imperium  |  Caterpillar Tech Challenge
===============================================================
The live plot window (matplotlib). It is intentionally styled like the rich
terminal dashboard: dark background, monospaced labels, cyan/green/orange fault
palette, rounded control blocks, live hover readout, mouse zoom/pan, a
scrolling auto-ranging time window, and selectable voltage traces.

Decoupled from the rest of the app: run_gui() takes any object exposing the
State plot fields (hist_t, hist_soc_data, ...) plus a .lock, so it never imports
monitor.py. Runs on the main thread (matplotlib requirement).
"""

from __future__ import annotations

import contextlib
import io
import threading

import numpy as np


# Palette matched to the rich terminal dashboard (xterm-ish colours)
BG        = "#101112"
PANEL     = "#151719"
PANEL_2   = "#191c20"
GRID      = "#34383d"
GRID_SOFT = "#25292e"
FG        = "#8b8d91"
FG_BRIGHT = "#d7d7d7"
ACCENT    = "#22d49a"
CYAN      = "#30c7f2"
ORANGE    = "#f7d94c"
RED       = "#ff5f5f"
MAGENTA   = "#df7aff"
TAB_OFF   = "#20242a"
TAB_HOVER = "#2b3138"


GUI_VIEWS = {
    "soc": {
        "tab": "SOC",
        "series": [("SOC data", "hist_soc_data", CYAN),
                   ("SOC UKF",  "hist_soc_est",  ORANGE)],
        "ylabel": "State of Charge [%]",
        "title":  "SOC — UKF estimate vs dataset",
        "min_span": 2.0,
        "fmt": "{:.1f}",
        "unit": "%",
        "floor": None,
    },
    "soh": {
        "tab": "SOH",
        "series": [("SOH UKF", "hist_soh", ACCENT)],
        "ylabel": "State of Health [%]",
        "title":  "SOH — UKF estimate",
        "min_span": 2.0,
        "fmt": "{:.1f}",
        "unit": "%",
        "floor": None,
    },
    "sev": {
        "tab": "Severity",
        "series": [("severity", "hist_sev", RED)],
        "ylabel": "Fault severity [0-1]",
        "title":  "Fault severity — overall",
        "min_span": 0.2,
        "fmt": "{:.2f}",
        "unit": "",
        "floor": 0.0,
    },
    "volt": {
        "tab": "Voltages",
        "series": [("V1", "hist_v1", CYAN),
                   ("V2", "hist_v2", ORANGE),
                   ("V3", "hist_v3", MAGENTA)],
        "ylabel": "Cell voltage [V]",
        "title":  "Cell voltages",
        "min_span": 0.05,
        "fmt": "{:.3f}",
        "unit": " V",
        "floor": None,
    },
}


def gui_available() -> bool:
    """True if a usable (non-headless) matplotlib backend is available."""
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            import matplotlib
    except Exception:
        return False
    if matplotlib.get_backend().lower() != "agg":
        return True
    for bk in ("MacOSX", "QtAgg", "TkAgg"):
        try:
            matplotlib.use(bk, force=True)
            return True
        except Exception:
            continue
    return False


def _style_axes(ax) -> None:
    ax.set_facecolor(PANEL)
    for spine in ax.spines.values():
        spine.set_color(GRID)
        spine.set_linewidth(1.0)
    ax.tick_params(colors=FG, labelsize=9, length=4, width=0.8)
    ax.xaxis.label.set_color(FG)
    ax.yaxis.label.set_color(FG)
    ax.grid(color=GRID_SOFT, alpha=0.95, linewidth=0.65)
    ax.set_axisbelow(True)


def _rounded(fig, xy, wh, *, fc=PANEL_2, ec=GRID, lw=1.0, radius=0.025,
             alpha=1.0, zorder=-10):
    """Figure-coordinate rounded rectangle."""
    from matplotlib.patches import FancyBboxPatch

    patch = FancyBboxPatch(
        xy, wh[0], wh[1],
        boxstyle=f"round,pad=0.006,rounding_size={radius}",
        transform=fig.transFigure,
        facecolor=fc,
        edgecolor=ec,
        linewidth=lw,
        alpha=alpha,
        zorder=zorder,
    )
    fig.patches.append(patch)
    return patch


def _set_patch_style(patch, *, active: bool, enabled: bool = True) -> None:
    if active:
        patch.set_facecolor(ACCENT)
        patch.set_edgecolor(ACCENT)
        patch.set_alpha(1.0)
    elif enabled:
        patch.set_facecolor(TAB_OFF)
        patch.set_edgecolor(GRID)
        patch.set_alpha(1.0)
    else:
        patch.set_facecolor("#151719")
        patch.set_edgecolor("#262a2f")
        patch.set_alpha(0.55)


def _event_hits(event, patch) -> bool:
    return event.x is not None and event.y is not None and patch.contains(event)[0]


def run_gui(state, stop_ev: threading.Event, cmd_q=None) -> None:
    """Open the live plot window. Reads `state` (shared with the streaming
    thread) so the curves are real-time in sync with what the monitor sends to
    the ESP32. Blocks until the window is closed or stop_ev is set.

    If `cmd_q` is given, keystrokes in the plot window are forwarded to it:
    digits 0-8 inject a fault, 'q' quits. This is what makes the keyboard work
    when the plot window (not the terminal) has focus."""
    import matplotlib as mpl
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation
    from matplotlib.widgets import Slider

    mpl.rcParams.update({
        "font.family": "monospace",
        "font.size": 10,
        "toolbar": "toolbar2",
    })

    fig = plt.figure(figsize=(13.4, 7.8), facecolor=BG)
    try:
        fig.canvas.manager.set_window_title("Team Imperium — BMS Live Plotter")
    except Exception:
        pass

    # Rounded background panels.
    _rounded(fig, (0.040, 0.045), (0.920, 0.875), fc="#111315", ec="#393d42",
             lw=1.2, radius=0.024, zorder=-20)
    _rounded(fig, (0.060, 0.215), (0.880, 0.585), fc=PANEL, ec="#42474d",
             lw=1.0, radius=0.018, zorder=-15)
    _rounded(fig, (0.060, 0.052), (0.880, 0.140), fc="#121416", ec="#30343a",
             lw=1.0, radius=0.018, zorder=-15)

    fig.text(0.060, 0.945, "TEAM IMPERIUM — BMS LIVE PLOTTER",
             color=FG_BRIGHT, fontsize=12.5, fontweight="bold", ha="left")
    fig.text(0.060, 0.910,
             "real-time curves from the same stream sent to the ESP32",
             color=FG, fontsize=8.7, ha="left")

    ax = fig.add_axes([0.090, 0.255, 0.815, 0.485])
    _style_axes(ax)
    ax.set_xlabel("Time [s]", labelpad=10)
    lines = [ax.plot([], [], lw=2.25, solid_capstyle="round")[0]
             for _ in range(3)]
    cursor = ax.axvline(0, color=ACCENT, lw=1.0, alpha=0.0)

    now_txt = ax.text(
        0.015, 0.965, "", transform=ax.transAxes, va="top", ha="left",
        fontsize=9, color=FG_BRIGHT,
        bbox=dict(boxstyle="round,pad=0.35,rounding_size=0.18",
                  fc="#101214", ec="#2c3137", alpha=0.92),
    )
    hover_txt = fig.text(
        0.078, 0.072, "hover over the plot to read values",
        fontsize=8.8, color=ACCENT, ha="left", va="center",
        bbox=dict(boxstyle="round,pad=0.42,rounding_size=0.18",
                  fc="#0e1011", ec="#2a2f34", alpha=0.95),
    )

    keys = list(GUI_VIEWS.keys())
    st = {
        "view": "soc",
        "follow": True,
        "window": 30.0,
        "volt_visible": {"hist_v1": True, "hist_v2": True, "hist_v3": True},
    }

    # Top tab blocks.
    tab_artists = {}
    x0, gap, y, h = 0.060, 0.012, 0.835, 0.047
    w = (0.880 - (len(keys) - 1) * gap) / len(keys)
    for i, key in enumerate(keys):
        x = x0 + i * (w + gap)
        patch = _rounded(fig, (x, y), (w, h), fc=TAB_OFF, ec=GRID,
                         lw=1.0, radius=0.018, zorder=4)
        label = fig.text(x + w / 2, y + h / 2, GUI_VIEWS[key]["tab"],
                         color=FG, fontsize=9.8, ha="center", va="center",
                         fontweight="bold", zorder=5)
        tab_artists[key] = (patch, label)

    # Follow pill.
    fig.text(0.078, 0.181, "CONTROL", color=FG, fontsize=7.8, ha="left",
             va="top", fontweight="bold")
    follow_patch = _rounded(fig, (0.078, 0.112), (0.145, 0.036),
                            fc=ACCENT, ec=ACCENT, radius=0.018, zorder=4)
    follow_label = fig.text(0.1505, 0.130, "FOLLOW LATEST",
                            color="#050707", fontsize=8.4, ha="center",
                            va="center", fontweight="bold", zorder=5)

    # Window slider.
    fig.text(0.335, 0.181, "TIME WINDOW", color=FG, fontsize=7.8, ha="left",
             va="top", fontweight="bold")
    sax = fig.add_axes([0.335, 0.119, 0.285, 0.020])
    sax.set_facecolor("#101214")
    swin = Slider(
        sax, "Window [s]", 5, 600, valinit=30, valstep=5,
        color=ACCENT, track_color="#242a30", handle_style={"facecolor": ORANGE},
    )
    swin.label.set_color(FG)
    swin.valtext.set_color(ORANGE)
    swin.on_changed(lambda v: st.update(window=float(v)))

    # Voltage visibility chips.
    volt_traces_label = fig.text(0.690, 0.181, "VOLTAGE TRACES", color=FG,
                                fontsize=7.8, ha="left", va="top",
                                fontweight="bold")
    volt_chips = {}
    chip_x0, chip_y, chip_w, chip_h = 0.690, 0.112, 0.058, 0.036
    for i, (label, attr, color) in enumerate(GUI_VIEWS["volt"]["series"]):
        x = chip_x0 + i * (chip_w + 0.012)
        patch = _rounded(fig, (x, chip_y), (chip_w, chip_h), fc=TAB_OFF,
                         ec=GRID, radius=0.016, zorder=4)
        txt = fig.text(x + chip_w / 2, chip_y + chip_h / 2, label,
                       color=color, fontsize=9.2, ha="center", va="center",
                       fontweight="bold", zorder=5)
        volt_chips[attr] = (patch, txt, color)

    cache = {"t": np.empty(0), "cols": [], "series": [], "view": "soc"}

    def set_hover_text(text: str, *, active: bool) -> None:
        max_chars = 118
        if len(text) > max_chars:
            text = text[:max_chars - 1] + "…"
        hover_txt.set_text(text)
        hover_txt.set_color(ACCENT if active else FG)

    def selected_series():
        cfg = GUI_VIEWS[st["view"]]
        if st["view"] != "volt":
            return cfg["series"]
        return [ser for ser in cfg["series"] if st["volt_visible"][ser[1]]]

    def refresh_controls():
        for key, (patch, label) in tab_artists.items():
            active = key == st["view"]
            _set_patch_style(patch, active=active)
            label.set_color("#03100c" if active else FG)
            label.set_fontweight("bold" if active else "normal")

        _set_patch_style(follow_patch, active=st["follow"])
        follow_label.set_text("FOLLOW LATEST" if st["follow"] else "FOLLOW OFF")
        follow_label.set_color("#03100c" if st["follow"] else FG)

        show_voltage_chips = st["view"] == "volt"
        volt_traces_label.set_visible(show_voltage_chips)
        for attr, (patch, txt, color) in volt_chips.items():
            visible = st["volt_visible"][attr]
            patch.set_visible(show_voltage_chips)
            txt.set_visible(show_voltage_chips)
            _set_patch_style(patch, active=visible, enabled=visible)
            if show_voltage_chips:
                txt.set_color("#03100c" if visible else color)
                txt.set_alpha(1.0 if visible else 0.45)

    def configure():
        cfg = GUI_VIEWS[st["view"]]
        series = selected_series()
        for i, ln in enumerate(lines):
            if i < len(series):
                ln.set_color(series[i][2])
                ln.set_label(series[i][0])
                ln.set_visible(True)
            else:
                ln.set_visible(False)
                ln.set_label("_nolegend_")

        ax.set_ylabel(cfg["ylabel"], labelpad=12)
        ax.set_title(cfg["title"], color=FG_BRIGHT, fontsize=12,
                     fontweight="bold", pad=14)
        leg = ax.legend(loc="upper right", fontsize=8.5, facecolor=PANEL,
                        edgecolor=GRID, framealpha=0.85)
        for txt in leg.get_texts():
            txt.set_color(FG_BRIGHT)
        refresh_controls()
        fig.canvas.draw_idle()

    def update(_frame):
        if stop_ev.is_set():
            plt.close(fig)
            return lines

        cfg = GUI_VIEWS[st["view"]]
        series = selected_series()
        with state.lock:
            t = np.asarray(state.hist_t, dtype=float)
            cols = [np.asarray(getattr(state, attr), dtype=float)
                    for _, attr, _ in series]

        cache["t"], cache["cols"], cache["series"], cache["view"] = (
            t, cols, series, st["view"])
        if t.size == 0 or not cols:
            return lines

        for i, ln in enumerate(lines):
            if i < len(cols):
                ln.set_data(t, cols[i])

        if st["follow"]:
            x1 = t[-1]
            x0_ = max(t[0], x1 - st["window"])
            ax.set_xlim(x0_, x1 if x1 > x0_ else x0_ + 1.0)
            mask = t >= x0_
        else:
            lo, hi = ax.get_xlim()
            mask = (t >= lo) & (t <= hi)

        if mask.any():
            vis = np.concatenate([c[mask] for c in cols])
            lo_v, hi_v = float(vis.min()), float(vis.max())
            if hi_v - lo_v < cfg["min_span"]:
                mid = 0.5 * (lo_v + hi_v)
                lo_v, hi_v = mid - cfg["min_span"] / 2, mid + cfg["min_span"] / 2
            pad = (hi_v - lo_v) * 0.12
            y_lo, y_hi = lo_v - pad, hi_v + pad
            if cfg["floor"] is not None:
                y_lo = max(cfg["floor"], y_lo)
            ax.set_ylim(y_lo, y_hi)

        fmt, unit = cfg["fmt"], cfg["unit"]
        if st["view"] == "soc":
            now_txt.set_text(
                f"SOC data {fmt.format(cols[0][-1])}{unit}   "
                f"UKF {fmt.format(cols[1][-1])}{unit}   "
                f"delta {abs(cols[1][-1] - cols[0][-1]):.1f}%"
            )
        elif st["view"] == "volt":
            parts = [f"{label} {fmt.format(c[-1])}{unit}"
                     for (label, _, _), c in zip(series, cols)]
            now_txt.set_text("   ".join(parts))
        else:
            now_txt.set_text(f"{series[0][0]} {fmt.format(cols[0][-1])}{unit}")
        return lines

    def on_click(event):
        for key, (patch, _label) in tab_artists.items():
            if _event_hits(event, patch):
                st["view"] = key
                configure()
                return

        if _event_hits(event, follow_patch):
            st["follow"] = not st["follow"]
            refresh_controls()
            fig.canvas.draw_idle()
            return

        if st["view"] == "volt":
            for attr, (patch, _txt, _color) in volt_chips.items():
                if _event_hits(event, patch):
                    enabled = [a for a, on in st["volt_visible"].items() if on]
                    if st["volt_visible"][attr] and len(enabled) == 1:
                        return
                    st["volt_visible"][attr] = not st["volt_visible"][attr]
                    configure()
                    return

    def on_move(event):
        t = cache["t"]
        if event.inaxes is ax and event.xdata is not None and t.size:
            i = int(np.argmin(np.abs(t - event.xdata)))
            cfg = GUI_VIEWS[cache["view"]]
            fmt, unit = cfg["fmt"], cfg["unit"]
            cursor.set_xdata([t[i], t[i]])
            cursor.set_alpha(0.75)
            parts = [f"t {t[i]:7.1f} s"]
            for (label, _, _), c in zip(cache["series"], cache["cols"]):
                parts.append(f"{label} {fmt.format(c[i])}{unit}")
            set_hover_text("    |    ".join(parts), active=True)
        else:
            cursor.set_alpha(0.0)
            set_hover_text("hover over the plot to read values", active=False)
        fig.canvas.draw_idle()

    def on_key(event):
        """Forward keystrokes from the plot window to the monitor's command
        queue, so inject (0-8) and quit (q) work while the window has focus."""
        k = (event.key or "").lower()
        if k == "q":
            stop_ev.set()
            if cmd_q is not None:
                cmd_q.put("q")
            plt.close(fig)
        elif len(k) == 1 and k in "012345678" and cmd_q is not None:
            cmd_q.put(k)

    fig.canvas.mpl_connect("button_press_event", on_click)
    fig.canvas.mpl_connect("motion_notify_event", on_move)
    fig.canvas.mpl_connect("close_event", lambda _e: stop_ev.set())
    fig.canvas.mpl_connect("key_press_event", on_key)

    anim = FuncAnimation(fig, update, interval=200, cache_frame_data=False)
    fig._anim = anim
    configure()

    try:
        plt.show(block=True)
    except Exception:
        pass
    finally:
        stop_ev.set()
