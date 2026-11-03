#!/usr/bin/env python3
"""
dashboard.py  —  Team Imperium  |  Caterpillar Tech Challenge
=============================================================
The rich terminal dashboard: sensor/fault panels and the overall layout.
Pure rendering — it only reads a `State` and returns rich renderables.
(The live plots live in the separate GUI window, see gui_plotter.py.)
"""

from __future__ import annotations

from rich import box
from rich.console import Group
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bms_state import State, FAULT_NAMES, FAULT_LEVELS, NUM_FAULTS


# ─────────────────────────────────────────────────────────────────────────────
#  Display helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clr(value: float, low: float, high: float) -> str:
    """Return 'green', 'yellow', or 'red' based on where value sits."""
    if value <= low:
        return "green"
    if value <= high:
        return "yellow"
    return "red"


def _val_text(label: str, value: str, color: str, unit: str = "",
              flag: str = "") -> tuple[str, Text]:
    """Return a (label, value+unit Text) pair for a sensor table row."""
    t = Text()
    t.append(value, style=f"bold {color}")
    if unit:
        t.append(f" {unit}", style="dim")
    if flag:
        t.append(f"  {flag}", style="yellow")
    return label, t


def _sev_bar(sev: float, width: int = 12) -> Text:
    """Severity bar using block characters. Red/yellow/green by tier."""
    filled = int(round(sev * width))
    empty  = width - filled
    bar = Text()
    if sev >= 0.60:
        bar.append("█" * filled, style="red")
    elif sev >= 0.30:
        bar.append("█" * filled, style="yellow")
    elif sev > 0.00:
        bar.append("█" * filled, style="dim green")
    bar.append("░" * empty, style="dim")
    return bar


def _fault_dot(active: bool, level: str) -> Text:
    t = Text()
    if not active:
        t.append("●", style="dim")
    elif level == "CRITICAL":
        t.append("●", style="bold red")
    elif level == "HIGH":
        t.append("●", style="bold yellow")
    else:
        t.append("●", style="bold cyan")
    return t


def _mosfet_text(label: str, is_open: bool) -> Text:
    t = Text()
    t.append(f"● {label}: ", style="dim")
    if is_open:
        t.append("OPEN", style="bold red")
    else:
        t.append("CLOSED", style="bold green")
    return t


# ─────────────────────────────────────────────────────────────────────────────
#  Panel renderers
# ─────────────────────────────────────────────────────────────────────────────

def render_header(s: State) -> Panel:
    t = Text()
    t.append("TEAM IMPERIUM", style="bold white")
    t.append(" — ", style="dim white")
    t.append("BMS LIVE MONITOR", style="bold white")
    t.append(f"        seq={s.seq}  |  t={s.time_s:.1f}s  |  "
             f"{s.sample_hz:.0f} Hz  |  ",
             style="dim white")
    if s.connected:
        t.append("● CONNECTED", style="bold green")
    else:
        t.append("● DISCONNECTED", style="bold red")
    return Panel(t, box=box.SIMPLE_HEAVY, style="white", padding=(0, 1))


def render_voltages(s: State) -> Panel:
    tbl = Table(box=None, padding=(0, 1), show_header=False,
                show_edge=False, expand=True)
    tbl.add_column("label", style="dim", width=14)
    tbl.add_column("value", justify="right")

    for i, vv in enumerate(s.v):
        color = _clr(abs(vv - s.v[0]) if i > 0 else 0,
                     0.03, 0.05) if i > 0 else (
                     "green" if 2.6 < vv < 4.2 else "red")
        lbl, val = _val_text(f"V{i+1}", f"{vv:.4f}", color, "V")
        tbl.add_row(lbl, val)

    spread = max(s.v) - min(s.v)
    flag   = "⚑" if spread > 0.050 else ""
    color  = _clr(spread, 0.030, 0.050)
    lbl, val = _val_text("ΔV spread", f"{spread*1000:.1f}", color,
                         "mV", flag)
    tbl.add_row(lbl, val)

    return Panel(tbl, title="[dim]CELL VOLTAGES[/dim]",
                 box=box.ROUNDED, border_style="dim", padding=(0, 1))


def render_pack(s: State) -> Panel:
    tbl = Table(box=None, padding=(0, 1), show_header=False,
                show_edge=False, expand=True)
    tbl.add_column("label", style="dim", width=14)
    tbl.add_column("value", justify="right")

    i_color = "cyan" if s.current < 0 else "yellow"
    tbl.add_row(*_val_text("Current", f"{s.current:.3f}", i_color, "A"))
    tbl.add_row(*_val_text("SOC (data)", f"{s.SOC_pct:.1f}",
                           _clr(100 - s.SOC_pct, 0, 85), "%"))
    soc_err = abs(s.soc_est - s.SOC_pct)
    err_flag = "Δ%.1f" % soc_err
    tbl.add_row(*_val_text("SOC (UKF)", f"{s.soc_est:.1f}",
                           _clr(soc_err, 3.0, 8.0), "%", err_flag))
    tbl.add_row(*_val_text("SOH (UKF)", f"{s.soh_est:.1f}",
                           _clr(100 - s.soh_est, 20, 40), "%"))
    tbl.add_row(*_val_text("T_surf", f"{s.T_surf:.2f}",
                           _clr(s.T_surf, 35, 38), "°C"))
    t_flag = "⚑" if s.T_rise > 5.0 else ""
    tbl.add_row(*_val_text("T_rise", f"{s.T_rise:.2f}",
                           _clr(s.T_rise, 4.0, 5.0), "°C", t_flag))
    tbl.add_row(*_val_text("Acoustic", f"{s.acoustic:.4f}",
                           _clr(s.acoustic, 0.040, 0.060), "g"))

    return Panel(tbl, title="[dim]PACK[/dim]",
                 box=box.ROUNDED, border_style="dim", padding=(0, 1))


def render_faults_hardware(s: State) -> Panel:
    """Combined fault status + hardware state panel (right side)."""
    tbl = Table(box=None, padding=(0, 1), show_header=True,
                show_edge=False, expand=True,
                header_style="dim")
    tbl.add_column(" ", width=2)
    tbl.add_column("FAULT STATUS  id · name · sev · state",
                   width=16, no_wrap=True)
    tbl.add_column("", width=14)   # severity bar
    tbl.add_column("", width=9, justify="right")  # number + badge

    for fid in range(NUM_FAULTS):
        active = bool(s.fault_bits & (1 << fid))
        sev    = s.fault_severities[fid] if fid < len(s.fault_severities) else 0.0
        level  = FAULT_LEVELS[fid]
        name   = FAULT_NAMES[fid]

        dot = _fault_dot(active, level)

        if active:
            name_style = "bold red" if level == "CRITICAL" else \
                         "bold yellow" if level in ("HIGH", "WARNING") else \
                         "bold cyan"
            name_text = Text(name, style=name_style)
        else:
            name_text = Text(name, style="dim")

        bar = _sev_bar(sev)

        sev_text = Text()
        if active:
            sev_str = f"{sev:.2f}"
            color   = "red" if sev >= 0.6 else "yellow" if sev >= 0.3 else "white"
            sev_text.append(sev_str, style=f"bold {color}")
            sev_text.append(" !", style="bold red" if sev >= 0.6 else "dim")
        else:
            sev_text.append("clear", style="dim")

        tbl.add_row(dot, name_text, bar, sev_text)

    hw = Text()
    hw.append("\n  HARDWARE STATE\n  ", style="dim")
    hw.append(_mosfet_text("CHG", s.chg_open))
    hw.append("   ")
    hw.append(_mosfet_text("DSC", s.dsc_open))

    content = Group(tbl, hw)
    return Panel(content, title="[dim]FAULT STATUS[/dim]",
                 box=box.ROUNDED, border_style="dim", padding=(0, 1))


def render_log(s: State) -> Panel:
    lines = s.events[:8] if s.events else [Text("  Waiting for data...", style="dim")]
    return Panel(
        Group(*lines),
        title="[dim]EVENT LOG (last 8 events)[/dim]",
        box=box.ROUNDED, border_style="dim",
        padding=(0, 1),
    )


def render_footer(s: State) -> Panel:
    any_fault = s.fault_bits != 0

    prec_style = "green" if s.precision() >= 90 else "yellow"
    rec_style  = "green" if s.recall()    >= 90 else "yellow"
    f1_style   = "green" if s.f1()        >= 90 else "yellow"

    t = Text()
    t.append("ACCURACY  ", style="dim")
    t.append(f"{s.TP}", style="bold green");   t.append("  ")
    t.append(f"{s.TN}", style="bold green");   t.append("  ")
    t.append(f"{s.FP}", style="bold red");     t.append("  ")
    t.append(f"{s.FN}", style="bold yellow");  t.append("    ")
    t.append("TP  TN  FP  FN", style="dim");  t.append("     ")
    t.append(f"Prec: {s.precision():.0f}%",  style=prec_style)
    t.append("   ")
    t.append(f"Recall: {s.recall():.0f}%",   style=rec_style)
    t.append("   ")
    t.append(f"F1: {s.f1():.0f}%",           style=f1_style)

    if any_fault:
        t.append("          ● CRITICAL ACTIVE" if s.fault_bits & 0b0000110 else
                 "          ● FAULT ACTIVE",
                 style="bold red on default")

    return Panel(t, box=box.SIMPLE_HEAVY, style="white", padding=(0, 1))


def render_status_bar(s: State) -> Panel:
    t = Text()
    t.append(f"Streaming {s.sample_hz:.0f} Hz ({s.speed:.1f}×)  |  "
             f"plots → GUI window  |  0-8 inject fault  |  q quit  "
             f"(type in the plot window or terminal)",
             style="dim")
    return Panel(t, box=box.SIMPLE, style="dim", padding=(0, 1))


# ─────────────────────────────────────────────────────────────────────────────
#  Layout builder  (terminal dashboard — plots live in the GUI window)
# ─────────────────────────────────────────────────────────────────────────────

def build_layout(s: State) -> Layout:
    root = Layout()
    root.split(
        Layout(render_header(s),       name="header",  size=3),
        Layout(                        name="body"),
        Layout(render_log(s),          name="log",     size=10),
        Layout(render_footer(s),       name="footer",  size=3),
        Layout(render_status_bar(s),   name="bar",     size=3),
    )
    root["body"].split_row(
        Layout(name="left",  ratio=4),
        Layout(name="right", ratio=5),
    )
    root["left"].split(
        Layout(render_voltages(s)),
        Layout(render_pack(s)),
    )
    root["right"].update(render_faults_hardware(s))
    return root
