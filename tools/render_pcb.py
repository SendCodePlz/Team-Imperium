"""Minimal KiCad 9 .kicad_pcb -> PNG renderer (front/back realistic view)."""
import math, sys
import sexpdata
from sexpdata import Symbol
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon, Circle, FancyBboxPatch
from matplotlib.collections import LineCollection, PatchCollection
from matplotlib import transforms as mtrans

PCB, OUT_PREFIX = sys.argv[1], sys.argv[2]
data = sexpdata.loads(open(PCB).read())


def name(n):
    return n[0].value() if isinstance(n, list) and n and isinstance(n[0], Symbol) else None


def kids(n, key):
    return [c for c in n if name(c) == key]


def kid(n, key):
    k = kids(n, key)
    return k[0] if k else None


def val(n, key, i=1, d=None):
    k = kid(n, key)
    return k[i] if k is not None and len(k) > i else d


def xy(n, key):
    k = kid(n, key)
    return (float(k[1]), float(k[2])) if k is not None else None


def s(x):
    return x.value() if isinstance(x, Symbol) else x


def layers_of(n):
    l = kid(n, "layers")
    if l is not None:
        return [s(x) for x in l[1:]]
    l = kid(n, "layer")
    return [s(l[1])] if l is not None else []


def rot(p, a):
    r = math.radians(a)
    return (p[0] * math.cos(r) + p[1] * math.sin(r), -p[0] * math.sin(r) + p[1] * math.cos(r))


def arc_pts(st, md, en, n=24):
    (x1, y1), (x2, y2), (x3, y3) = st, md, en
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [st, en]
    ux = ((x1**2 + y1**2) * (y2 - y3) + (x2**2 + y2**2) * (y3 - y1) + (x3**2 + y3**2) * (y1 - y2)) / d
    uy = ((x1**2 + y1**2) * (x3 - x2) + (x2**2 + y2**2) * (x1 - x3) + (x3**2 + y3**2) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1, a2, a3 = (math.atan2(p[1] - uy, p[0] - ux) for p in (st, md, en))
    def norm(a):
        return a % (2 * math.pi)
    ccw = norm(a2 - a1) < norm(a3 - a1)
    span = norm(a3 - a1) if ccw else -norm(a1 - a3)
    return [(ux + r * math.cos(a1 + span * i / n), uy + r * math.sin(a1 + span * i / n)) for i in range(n + 1)]


# ---------------------------------------------------------------- collect
G = {"edge": [], "lines": [], "polys": [], "circles": [], "texts": [], "pads": [], "holes": []}
# lines: (layer, pts, width) ; polys: (layer, pts) ; circles: (layer, c, r, width, fill)


def add_graphic(n, tf, layer_override=None):
    t = name(n)
    lyr = layers_of(n)
    lyr = lyr[0] if lyr else None
    w = float(val(kid(n, "stroke") or [], "width", d=0.1) or 0.1) if kid(n, "stroke") is not None else float(val(n, "width", d=0.1))
    fill = s(val(n, "fill", d="none")) in ("solid", "yes")
    if t.endswith("_line"):
        G["lines"].append((lyr, [tf(xy(n, "start")), tf(xy(n, "end"))], w))
    elif t.endswith("_arc"):
        G["lines"].append((lyr, [tf(p) for p in arc_pts(xy(n, "start"), xy(n, "mid"), xy(n, "end"))], w))
    elif t.endswith("_rect"):
        (x1, y1), (x2, y2) = xy(n, "start"), xy(n, "end")
        pts = [tf(p) for p in [(x1, y1), (x2, y1), (x2, y2), (x1, y2), (x1, y1)]]
        (G["polys"] if fill else G["lines"]).append((lyr, pts) if fill else (lyr, pts, w))
    elif t.endswith("_circle"):
        c, e = xy(n, "center"), xy(n, "end")
        G["circles"].append((lyr, tf(c), math.hypot(e[0] - c[0], e[1] - c[1]), w, fill))
    elif t.endswith("_poly"):
        pts = [tf((float(p[1]), float(p[2]))) for p in kids(kid(n, "pts"), "xy")]
        if fill:
            G["polys"].append((lyr, pts))
        else:
            G["lines"].append((lyr, pts + pts[:1], w))


def hidden(n):
    if s(val(n, "hide", d="no")) == "yes" or Symbol("hide") in n:
        return True
    e = kid(n, "effects")
    return e is not None and (Symbol("hide") in e or s(val(e, "hide", d="no")) == "yes")


def add_text(n, txt, tf, base_rot):
    at = kid(n, "at")
    p = (float(at[1]), float(at[2]))
    a = float(at[3]) if len(at) > 3 else 0.0
    e = kid(n, "effects")
    size = 1.0
    if e is not None and kid(e, "font") is not None and kid(kid(e, "font"), "size") is not None:
        size = float(kid(kid(e, "font"), "size")[1])
    lyr = layers_of(n)
    G["texts"].append((lyr[0] if lyr else None, tf(p), txt, a, size))


for n in data:
    t = name(n)
    if t in ("gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_poly"):
        add_graphic(n, lambda p: p)
    elif t == "gr_text" and not hidden(n):
        add_text(n, s(n[1]), lambda p: p, 0)
    elif t == "segment":
        G["lines"].append((s(val(n, "layer")), [xy(n, "start"), xy(n, "end")], float(val(n, "width"))))
    elif t == "arc":
        G["lines"].append((s(val(n, "layer")), arc_pts(xy(n, "start"), xy(n, "mid"), xy(n, "end")), float(val(n, "width"))))
    elif t == "via":
        c = xy(n, "at")
        for L in ("F.Cu", "B.Cu"):
            G["circles"].append((L, c, float(val(n, "size")) / 2, 0, True))
        G["holes"].append((c, float(val(n, "drill")) / 2, "via"))
    elif t == "zone":
        for fp in kids(n, "filled_polygon"):
            pts = [(float(p[1]), float(p[2])) for p in kids(kid(fp, "pts"), "xy")]
            G["polys"].append((s(val(fp, "layer")), pts))
    elif t == "footprint":
        at = kid(n, "at")
        fx, fy = float(at[1]), float(at[2])
        fa = float(at[3]) if len(at) > 3 else 0.0
        tf = lambda p, fx=fx, fy=fy, fa=fa: (lambda q: (q[0] + fx, q[1] + fy))(rot(p, fa))
        for c in n:
            ct = name(c)
            if ct in ("fp_line", "fp_arc", "fp_rect", "fp_circle", "fp_poly"):
                add_graphic(c, tf)
            elif ct == "property" and s(c[1]) == "Reference" and not hidden(c):
                add_text(c, s(c[2]), tf, fa)
            elif ct == "fp_text" and not hidden(c) and s(c[1]) == "user":
                add_text(c, s(c[2]), tf, fa)
            elif ct == "pad":
                ptype, shape = s(c[2]), s(c[3])
                pat = kid(c, "at")
                pp = tf((float(pat[1]), float(pat[2])))
                pa = float(pat[3]) if len(pat) > 3 else fa
                sz = kid(c, "size")
                w, h = float(sz[1]), float(sz[2])
                rr = float(val(c, "roundrect_rratio", d=0.25)) if shape == "roundrect" else 0
                if shape == "circle":
                    rr = 0.5
                if shape == "oval":
                    rr = 0.5
                lyrs = layers_of(c)
                G["pads"].append((lyrs, pp, w, h, pa, shape, rr))
                dr = kid(c, "drill")
                if dr is not None and ptype in ("thru_hole", "np_thru_hole"):
                    nums = [x for x in dr[1:] if isinstance(x, (int, float))]
                    if nums:
                        G["holes"].append((pp, float(nums[0]) / 2, ptype))

# ---------------------------------------------------------------- outline
segs = [pts for (l, pts, w) in G["lines"] if l == "Edge.Cuts"]
allpts = [p for sg in segs for p in sg]
minx, maxx = min(p[0] for p in allpts), max(p[0] for p in allpts)
miny, maxy = min(p[1] for p in allpts), max(p[1] for p in allpts)


def chain(segs, tol=0.05):
    segs = [list(sg) for sg in segs]
    loops = []
    while segs:
        cur = segs.pop(0)
        grew = True
        while grew:
            grew = False
            for i, sg in enumerate(segs):
                for cand in (sg, sg[::-1]):
                    if math.dist(cur[-1], cand[0]) < tol:
                        cur += cand[1:]; segs.pop(i); grew = True; break
                if grew:
                    break
        loops.append(cur)
    return loops


loops = chain(segs)
loops.sort(key=lambda L: -(max(p[0] for p in L) - min(p[0] for p in L)))
outline = loops[0]

# ---------------------------------------------------------------- render
SIDES = {
    "front": dict(cu="F.Cu", mask="F.Mask", silk="F.SilkS", mirror=False),
    "back": dict(cu="B.Cu", mask="B.Mask", silk="B.SilkS", mirror=True),
}
C = dict(bg="#0d1117", edge="#c9b37e", mask="#0f5132", cu_under="#2f8a4f",
         pad="#d9b45a", hole="#141414", silk="#f2f2f2", via_ring="#d9b45a")
PAD_MM = 4.0
W_mm, H_mm = maxx - minx + 2 * PAD_MM, maxy - miny + 2 * PAD_MM
FIG_W = 12.0
PT_PER_MM = FIG_W * 72 / W_mm


def pad_patch(pp, w, h, a, rr, color, grow=0.0, z=3):
    w2, h2 = w + 2 * grow, h + 2 * grow
    r = rr * min(w2, h2)
    patch = FancyBboxPatch((-w2 / 2 + r, -h2 / 2 + r), w2 - 2 * r, h2 - 2 * r,
                           boxstyle=f"round,pad={r}" if r > 0 else "square,pad=0",
                           fc=color, ec="none", zorder=z)
    tr = mtrans.Affine2D().rotate_deg(-a).translate(*pp)
    return patch, tr


def on(lyrs, cu):
    return cu in lyrs or "*.Cu" in lyrs


for side, cfg in SIDES.items():
    fig = plt.figure(figsize=(FIG_W, FIG_W * H_mm / W_mm), dpi=200)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(minx - PAD_MM, maxx + PAD_MM)
    ax.set_ylim(maxy + PAD_MM, miny - PAD_MM)
    if cfg["mirror"]:
        ax.invert_xaxis()
    ax.set_aspect("equal")
    ax.axis("off")
    fig.patch.set_facecolor(C["bg"])
    ax.add_patch(Polygon(outline, closed=True, fc=C["mask"], ec=C["edge"], lw=1.6, zorder=1))
    for L in loops[1:]:
        ax.add_patch(Polygon(L, closed=True, fc=C["bg"], ec=C["edge"], lw=1.2, zorder=6))
    cu, silk = cfg["cu"], cfg["silk"]
    # copper under mask
    for l, pts in G["polys"]:
        if l == cu:
            ax.add_patch(Polygon(pts, closed=True, fc=C["cu_under"], ec="none", alpha=0.55, zorder=2))
    lc = [(pts, w) for (l, pts, w) in G["lines"] if l == cu]
    ax.add_collection(LineCollection([p for p, _ in lc], linewidths=[w * PT_PER_MM for _, w in lc],
                                     colors=C["cu_under"], capstyle="round", joinstyle="round", zorder=2.5))
    for l, c, r, w, fill in G["circles"]:
        if l == cu:
            ax.add_patch(Circle(c, r, fc=C["via_ring"], ec="none", zorder=3))
    # exposed pads
    for lyrs, pp, w, h, a, shape, rr in G["pads"]:
        if on(lyrs, cu):
            p, tr = pad_patch(pp, w, h, a, rr, C["pad"])
            p.set_transform(tr + ax.transData)
            ax.add_patch(p)
    for c, r, kind in G["holes"]:
        ax.add_patch(Circle(c, r, fc=C["hole"], ec="none", zorder=4))
    # silkscreen
    sl = [(pts, w) for (l, pts, w) in G["lines"] if l == silk]
    ax.add_collection(LineCollection([p for p, _ in sl], linewidths=[max(w, 0.12) * PT_PER_MM for _, w in sl],
                                     colors=C["silk"], capstyle="round", zorder=5))
    for l, pts in G["polys"]:
        if l == silk:
            ax.add_patch(Polygon(pts, closed=True, fc=C["silk"], ec="none", zorder=5))
    for l, c, r, w, fill in G["circles"]:
        if l == silk:
            ax.add_patch(Circle(c, r, fc=C["silk"] if fill else "none", ec=C["silk"], lw=max(w, .12) * PT_PER_MM, zorder=5))
    for l, p, txt, a, size in G["texts"]:
        if l == silk:
            ang = a if not cfg["mirror"] else -a
            if 90 < (ang % 360) <= 270:
                ang += 180
            ax.text(p[0], p[1], txt, color=C["silk"], fontsize=size * PT_PER_MM * 0.95,
                    rotation=ang, ha="center", va="center", family="DejaVu Sans",
                    fontweight="bold", zorder=5)
    fig.savefig(f"{OUT_PREFIX}_{side}.png", facecolor=C["bg"])
    plt.close(fig)

print("board size: %.1f x %.1f mm" % (maxx - minx, maxy - miny))
print("loops", len(loops), "pads", len(G["pads"]), "vias", sum(1 for h in G["holes"] if h[2] == "via"))
