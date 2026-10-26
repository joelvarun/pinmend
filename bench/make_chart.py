"""Render docs/before_after.png and docs/pareto.png from docs/results.json.

Reads only what run_benchmark.py measured. Nothing here computes a number.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "docs" / "results.json"

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BASELINE_FILL = "#eb6834"
PINMEND_FILL = "#2a78d6"
ACCENT = "#eb6834"
REPORT_FILL = "#1baf7a"

LABEL_BASE = "blame driven loop"
LABEL_EXACT = "pinmend minimum"


def style(ax, value_axis: str = "x") -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
        ax.spines[side].set_linewidth(0.8)
    ax.tick_params(colors=MUTED, labelsize=8.5, length=0)
    ax.grid(True, axis=value_axis, color=GRID, linewidth=0.8, linestyle="-")
    ax.set_axisbelow(True)


def panel_examples(ax, examples: list[dict]) -> None:
    rows = sorted(examples, key=lambda r: (r["minimum_size"] - r["greedy_size"], -r["minimum_size"]))
    names = [r["name"].replace("-", " ") for r in rows]
    series = [
        ("lines the report names", [r["report_named"] for r in rows], REPORT_FILL, 0.26),
        (LABEL_BASE, [r["greedy_size"] for r in rows], BASELINE_FILL, 0.0),
        (LABEL_EXACT, [r["minimum_size"] for r in rows], PINMEND_FILL, -0.26),
    ]
    spots = list(range(len(rows)))
    for label, values, color, offset in series:
        ax.barh([s + offset for s in spots], values, height=0.24,
                color=color, label=label, zorder=3)
        for spot, value in zip(spots, values):
            ax.text(value + 0.12, spot + offset, str(value), va="center", ha="left",
                    fontsize=7.5, color=INK_2)
    ax.set_yticks(spots)
    ax.set_yticklabels(names, fontsize=8.5, color=INK_2)
    ax.set_xlabel("requirement lines", fontsize=9, color=INK_2)
    ax.set_title("Seven real pin conflicts", fontsize=10.5, color=INK, loc="left", pad=10)
    ax.set_xlim(0, max(max(v) for _, v, _, _ in series) + 1.2)
    style(ax, "x")


def panel_suite(ax, rows: list[dict]) -> None:
    sizes = sorted({r["minimum_size"] for r in rows})
    base, exact, counts = [], [], []
    for size in sizes:
        bucket = [r for r in rows if r["minimum_size"] == size]
        base.append(sum(r["greedy_size"] for r in bucket) / len(bucket))
        exact.append(float(size))
        counts.append(len(bucket))
    spots = range(len(sizes))
    width = 0.38
    ax.bar([s - 0.21 for s in spots], base, width=width,
           color=BASELINE_FILL, label=LABEL_BASE, zorder=3)
    ax.bar([s + 0.21 for s in spots], exact, width=width,
           color=PINMEND_FILL, label=LABEL_EXACT, zorder=3)
    for s, value in zip(spots, base):
        ax.text(s - 0.21, value + 0.12, f"{value:.1f}", ha="center", va="bottom",
                fontsize=7.5, color=INK_2)
    ax.set_xticks(list(spots))
    ax.set_xticklabels([f"{size}\nn={count}" for size, count in zip(sizes, counts)],
                       fontsize=8, color=INK_2)
    ax.set_xlabel("minimum repair size of the file", fontsize=9, color=INK_2)
    ax.set_ylabel("requirement lines unpinned", fontsize=9, color=INK_2)
    ax.set_title("150 randomly pinned stale files, by difficulty",
                 fontsize=10.5, color=INK, loc="left", pad=10)
    ax.set_ylim(0, max(base) * 1.18)
    style(ax, "y")


def before_after(payload: dict, out: Path) -> None:
    summary = payload["summary"]
    fig, axes = plt.subplots(1, 2, figsize=(11.2, 5.6), dpi=170)
    fig.patch.set_facecolor(SURFACE)
    panel_examples(axes[0], payload["examples"])
    panel_suite(axes[1], payload["suite_rows"])

    handles, labels = axes[0].get_legend_handles_labels()
    legend = fig.legend(handles, labels, loc="upper left", bbox_to_anchor=(0.012, 0.875),
                        frameon=False, fontsize=9, ncol=3, handlelength=1.1,
                        handleheight=0.9, columnspacing=1.6)
    for text in legend.get_texts():
        text.set_color(INK_2)

    fig.suptitle("Requirement lines you have to unpin to make a broken file install",
                 fontsize=13, color=INK, x=0.012, ha="left", y=0.985)
    fig.text(0.012, 0.935,
             "Following the resolver conflict report one line at a time unpins "
             f"{summary['mean_greedy_size']} lines per file on average; the proved minimum is "
             f"{summary['mean_minimum_size']}.",
             fontsize=9.5, color=INK_2, ha="left")
    fig.text(0.012, 0.905,
             f"The loop overshoots on {summary['greedy_suboptimal_pct']} percent of files, by up to "
             f"{summary['greedy_max_excess']} extra lines, and the report itself names the wrong "
             f"number of lines on {round(100 - summary['report_exact_pct'], 1)} percent of them.",
             fontsize=9.5, color=INK_2, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.845))
    fig.savefig(out, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    print("wrote", out)


def tiebreak(payload: dict, out: Path) -> None:
    """Among repairs of the same minimum size, how much movement the exact
    tie break saves. One dot per file in the random suite."""
    rows = payload["suite_rows"]
    summary = payload["summary"]
    arbitrary = [r["arbitrary_minimum_steps"] for r in rows]
    best = [r["minimum_steps"] for r in rows]
    top = max(arbitrary + best) * 1.06

    fig, ax = plt.subplots(figsize=(7.6, 5.8), dpi=170)
    fig.patch.set_facecolor(SURFACE)
    ax.plot([0, top], [0, top], color=AXIS, linewidth=1.2, zorder=2)
    ax.scatter(arbitrary, best, s=34, color=PINMEND_FILL, zorder=3,
               edgecolors=SURFACE, linewidths=1.2)
    ax.text(top * 0.97, top * 0.93, "no gain", fontsize=8.5, color=MUTED,
            ha="right", va="bottom", rotation=45, rotation_mode="anchor")
    ax.set_xlim(0, top)
    ax.set_ylim(0, top)
    ax.set_xlabel("release steps moved, an arbitrary minimum repair", fontsize=9, color=INK_2)
    ax.set_ylabel("release steps moved, the best minimum repair", fontsize=9, color=INK_2)
    style(ax, "both")
    fig.suptitle("Equally small repairs are not equally good",
                 fontsize=12.5, color=INK, x=0.015, ha="left", y=0.985)
    fig.text(0.015, 0.935,
             f"Each dot is one file. Same number of lines unpinned either way, but "
             f"{summary['tie_break_helps_files']} of {summary['files']} files",
             fontsize=9.5, color=INK_2, ha="left")
    fig.text(0.015, 0.902,
             "land closer to the versions they pinned. Mean movement "
             f"{summary['mean_steps_arbitrary_minimum']} to "
             f"{summary['mean_steps_best_minimum']} release steps.",
             fontsize=9.5, color=INK_2, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.875))
    fig.savefig(out, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    print("wrote", out)


def main() -> int:
    if not RESULTS.exists():
        print("run bench/run_benchmark.py first", file=sys.stderr)
        return 1
    payload = json.loads(RESULTS.read_text(encoding="utf-8"))
    before_after(payload, RESULTS.parent / "before_after.png")
    tiebreak(payload, RESULTS.parent / "tiebreak.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
