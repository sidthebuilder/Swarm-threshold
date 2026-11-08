"""Build the three report figures from the run records and analysis files.

Read-only: no model call, no network call. Every number comes from results/runs_*/ (the run
records) or results/analysis_runs_*.json (written by `python -m swarm_threshold analyse`).

    python report/make_figures.py
"""
import json
import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = pathlib.Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
OUT = ROOT / "report" / "figures"
OUT.mkdir(parents=True, exist_ok=True)

FAMILIES = [
    ("runs_deepseek", "deepseek-v4-flash"),
    ("runs_glm53", "glm-53-flash"),
    ("runs_qwen35", "qwen3.5-397b"),
]
PEERS = [0, 1, 3, 8, 24]
LEAK, HELPER, GREY = "#c2410c", "#0f766e", "#6b7280"
plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 9, "axes.spines.top": False,
    "axes.spines.right": False, "axes.grid": True, "grid.alpha": 0.25,
    "axes.axisbelow": True, "figure.dpi": 220,
})


def analysis(family):
    return json.loads((RESULTS / f"analysis_{family}.json").read_text("utf-8"))


def runs(family):
    return [json.loads(p.read_text("utf-8")) for p in sorted((RESULTS / family).glob("*.json"))
            if p.name != "manifest.json"]


def label(k, docs):
    return f"main:k{k}:docs-{docs}:canaryfilter0:helperfilter0:reminder0"


def defence_label(canary, helper):
    return (f"waterbed:k8:docs-unavailable:canaryfilter{canary}:"
            f"helperfilter{helper}:reminder0")


def cell(data, outcome, lab):
    entry = data["rates"][outcome][lab]
    low, high = entry["wilson_95"]
    return entry["rate"], low, high, entry["successes"], entry["total"]


def footnote(fig, text, y=-0.02):
    fig.text(0.01, y, text, fontsize=7, color=GREY, ha="left", va="top", wrap=True)


# -- Figure 1: the flat curves ---------------------------------------------------------
fig, axes = plt.subplots(1, 3, figsize=(9.0, 2.9), sharey=True)
xs = list(range(len(PEERS)))
styles = [
    ("helper_run", "unavailable", HELPER, "-", "o", "helper used, docs lack the patch"),
    ("helper_run", "available", HELPER, "--", "s", "helper used, docs have the patch"),
    ("token_leak", "unavailable", LEAK, "-", "o", "canary posted, docs lack the patch"),
    ("token_leak", "available", LEAK, "--", "s", "canary posted, docs have the patch"),
]
for ax, (family, title) in zip(axes, FAMILIES):
    data = analysis(family)
    for outcome, docs, colour, style, marker, name in styles:
        vals = [cell(data, outcome, label(k, docs))[0] for k in PEERS]
        # Lines at exactly zero overlap; a small vertical offset keeps all four visible.
        nudge = {("token_leak", "unavailable"): 0.015, ("token_leak", "available"): 0.04}
        shown = [v + nudge.get((outcome, docs), 0.0) if v == 0 else v for v in vals]
        ax.plot(xs, shown, color=colour, linestyle=style, marker=marker, markersize=4,
                linewidth=1.5, label=name, clip_on=False)
    ax.set_xticks(xs)
    ax.set_xticklabels(PEERS)
    ax.set_ylim(-0.04, 1.06)
    ax.set_title(title, fontsize=9.5)
    ax.set_xlabel("k: compliance posts among the 24")
axes[0].set_ylabel("share of 20 runs")
handles, names = axes[0].get_legend_handles_labels()
fig.legend(handles, names, loc="upper center", ncol=4, fontsize=7.3, frameon=False,
           bbox_to_anchor=(0.5, 0.9))
fig.suptitle("Peer count moved neither outcome; whether the docs held the patch decided forum use",
             fontsize=10.5, fontweight="bold", x=0.01, ha="left", y=0.995)
footnote(fig, "Each point is 20 runs. The forum always has 24 posts and serialises to 4,455 "
              "characters for every k. Lines at zero are drawn slightly apart so all four stay "
              "visible; every canary-posted value is 0/20. The one non-zero safe-route helper cell "
              "is deepseek-v4-flash at k = 3 (1/20).")
fig.tight_layout(rect=(0, 0.0, 1, 0.8))
fig.savefig(OUT / "fig1_peer_curves.png", bbox_inches="tight")
plt.close(fig)

# -- Figure 2: the defence cells (main figure) -----------------------------------------
cells = [("no filter", defence_label(0, 0)), ("canary post\nblocked", defence_label(1, 0)),
         ("helper\nblocked", defence_label(0, 1))]
fig, axes = plt.subplots(1, 3, figsize=(9.0, 3.0), sharey=True)
for ax, (family, title) in zip(axes, FAMILIES):
    data = analysis(family)
    for offset, (outcome, colour, name) in enumerate(
        (("token_leak", LEAK, "canary posted"), ("helper_run", HELPER, "helper ran"))
    ):
        got = [cell(data, outcome, lab) for _, lab in cells]
        pos = [x + (offset - 0.5) * 0.36 for x in range(len(cells))]
        ax.bar(pos, [c[0] for c in got], width=0.34, color=colour, label=name)
        for p, c in zip(pos, got):
            if c[0] > 0:
                ax.plot([p, p], [c[1], c[2]], color="#1f2937", linewidth=1.0)
            ax.text(p, (c[2] if c[0] > 0 else 0) + 0.03, f"{c[3]}", ha="center",
                    fontsize=7.2, color="#1f2937")
    ax.set_xticks(range(len(cells)))
    ax.set_xticklabels([n for n, _ in cells], fontsize=8)
    ax.set_ylim(0, 1.18)
    ax.set_title(title, fontsize=9.5)
axes[0].set_ylabel("share of 20 runs")
handles, names = axes[0].get_legend_handles_labels()
fig.legend(handles, names, loc="upper center", ncol=2, fontsize=7.8, frameon=False,
           bbox_to_anchor=(0.5, 0.9))
axes[2].annotate("9 of 20 posted the canary\nonce the helper was blocked",
                 xy=(2 - 0.18, 0.47), xytext=(2.05, 0.93), ha="center", fontsize=7.4,
                 color=LEAK, fontweight="bold",
                 arrowprops=dict(arrowstyle="->", color=LEAK, linewidth=1.1))
fig.suptitle("Blocking the helper made the largest model post its canary; blocking the post changed nothing",
             fontsize=10.5, fontweight="bold", x=0.01, ha="left", y=0.995)
footnote(fig, "k = 8, documentation lacks the patch, so the forum is the only source. Numbers on "
              "bars are counts out of 20; error bars are Wilson 95% intervals. qwen3.5-397b posted "
              "its canary in none of its other 260 runs. Helper blocked against no filter: Fisher "
              "exact p = 0.0037, Holm-corrected over the three comparisons.")
fig.tight_layout(rect=(0, 0.0, 1, 0.8))
fig.savefig(OUT / "fig2_defence_cells.png", bbox_inches="tight")
plt.close(fig)

# -- Figure 3: self-report -------------------------------------------------------------
groups = []
for family, title in FAMILIES:
    rs = runs(family)
    acted = [r for r in rs if r["outcomes"]["helper_run"] or r["outcomes"]["token_leak"]]
    blocked_idle = [r for r in rs if r["provenance"]["condition"] == "waterbed_helper_blocked"
                    and not r["outcomes"]["helper_run"] and not r["outcomes"]["token_leak"]]
    groups.append((
        title,
        (sum(r["outcomes"]["spontaneous_disclosure"] for r in acted), len(acted)),
        (sum(r["outcomes"]["direct_admission"] for r in acted), len(acted)),
        (sum(r["outcomes"]["direct_admission"] for r in blocked_idle), len(blocked_idle)),
    ))
fig, ax = plt.subplots(figsize=(7.6, 3.0))
width = 0.26
bars = [
    (1, HELPER, "disclosure field set, among runs that acted"),
    (2, "#14b8a6", "said yes when asked, among runs that acted"),
    (3, LEAK, "said yes when asked, helper-blocked runs that did neither"),
]
for offset, (idx, colour, name) in enumerate(bars):
    pos = [x + (offset - 1) * width for x in range(len(groups))]
    vals = [g[idx][0] / g[idx][1] if g[idx][1] else 0 for g in groups]
    ax.bar(pos, vals, width=width * 0.94, color=colour, label=name)
    for p, v, g in zip(pos, vals, groups):
        ax.text(p, v + 0.03, f"{g[idx][0]}/{g[idx][1]}", ha="center", fontsize=7.2,
                color="#1f2937")
ax.set_xticks(range(len(groups)))
ax.set_xticklabels([g[0] for g in groups])
ax.set_ylim(0, 1.3)
ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0])
ax.set_ylabel("share of runs")
ax.legend(fontsize=7.2, loc="upper center", ncol=3, frameon=False, bbox_to_anchor=(0.5, 1.13))
ax.set_title("Self-reports matched the record, except after a blocked attempt",
             fontsize=10.5, fontweight="bold", loc="left", pad=26)
footnote(fig, "A run 'acted' if it ran the helper or posted the canary. In all 11 of glm-53-flash's "
              "'yes' answers, the agent's own closing summary said the helper call had been blocked "
              "and never ran. The direct question did not separate an attempted call from a "
              "completed one.", y=0.0)
fig.tight_layout(rect=(0, 0.04, 1, 1))
fig.savefig(OUT / "fig3_self_report.png", bbox_inches="tight")
plt.close(fig)

for stale in ("fig1_flat_curve.png", "fig2_defence_asymmetry.png", "fig3_honesty.png",
              "fig4_necessity.png"):
    (OUT / stale).unlink(missing_ok=True)
print("wrote", sorted(p.name for p in OUT.glob("*.png")))
for family, title in FAMILIES:
    print(title, [g for g in groups if g[0] == title][0][1:])
