"""竖版积水识别流程图（PPT「核心二」页用）：精简文字 + 贴 PPT 蓝色版式。

版式与阈值取自 Web/backend/photo_analysis.py、scripts/run_water_yolo.py、run_water_glm.py。
输出 _figs/fig_recognition_flow_zh.png。
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Polygon

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

OUT = Path(r"D:\低空经济\_figs\fig_recognition_flow_zh.png")

NAVY = "#16418E"      # YOLO 初筛
BLUE = "#2E6DB4"      # GLM 复检
LIGHT = "#E8F0FB"     # 输入/输出
BORDER = "#9DB8D9"
ORANGE = "#C65A1E"    # 人工环节
SKIP = "#F4F6F9"      # 否定分支
GRAY = "#6b7686"


def rounded_box(ax, x, y, w, h, fill, text, tcolor="black", fs=12):
    ax.add_patch(FancyBboxPatch((x + 0.7, y - 0.8), w, h,
                                boxstyle="round,pad=0.4,rounding_size=1.4",
                                facecolor="#8fa3bd", alpha=0.30, edgecolor="none"))
    ax.add_patch(FancyBboxPatch((x, y), w, h,
                                boxstyle="round,pad=0.4,rounding_size=1.4",
                                facecolor=fill, edgecolor=BORDER if fill == LIGHT else "none",
                                lw=1.0))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            fontsize=fs, color=tcolor, weight="bold", linespacing=1.4)


def stage_box(ax, x, y, w, h, fill, title, sub):
    ax.add_patch(FancyBboxPatch((x + 0.7, y - 0.8), w, h,
                                boxstyle="round,pad=0.4,rounding_size=1.4",
                                facecolor="#8fa3bd", alpha=0.30, edgecolor="none"))
    ax.add_patch(FancyBboxPatch((x, y), w, h,
                                boxstyle="round,pad=0.4,rounding_size=1.4",
                                facecolor=fill, edgecolor="none", lw=1.0))
    ax.text(x + w / 2, y + h * 0.64, title, ha="center", va="center",
            fontsize=13, color="white", weight="bold")
    ax.text(x + w / 2, y + h * 0.28, sub, ha="center", va="center",
            fontsize=10, color="white", alpha=0.95)


def diamond(ax, cx, cy, w, h, text):
    ax.add_patch(Polygon([(cx - w / 2, cy), (cx, cy + h / 2), (cx + w / 2, cy), (cx, cy - h / 2)],
                         closed=True, facecolor="#FDF3DC", edgecolor="#B08F4C", lw=1.1))
    ax.text(cx, cy, text, ha="center", va="center", fontsize=11, weight="bold")


def arrow(ax, x1, y1, x2, y2, color="#16418E"):
    ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(arrowstyle="-|>", color=color, lw=1.8, shrinkA=2, shrinkB=2))


def main():
    fig, ax = plt.subplots(figsize=(11, 8.2), dpi=200)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis("off")

    bx, bw = 16, 40
    cx = bx + bw / 2

    rounded_box(ax, bx, 86, bw, 8, LIGHT, "巡查照片 / 视频帧", tcolor=NAVY)
    arrow(ax, cx, 86, cx, 81.5)

    stage_box(ax, bx, 68, bw, 13, NAVY, "① YOLOv8n-seg 初筛", "水面掩膜 · 置信度阈值 0.25")
    arrow(ax, cx, 68, cx, 63.5)

    diamond(ax, cx, 57.5, 30, 12, "检出疑似水面？")
    arrow(ax, cx, 51.5, cx, 47)
    ax.text(cx + 3.5, 49.2, "是", ha="center", va="center", fontsize=10.5,
            color=NAVY, weight="bold")

    stage_box(ax, bx, 34, bw, 13, BLUE, "② GLM-5.3-Flash 复检", "限定「路面积水」提示词")
    arrow(ax, cx, 34, cx, 29.5)

    diamond(ax, cx, 23.5, 30, 12, "置信度 ≥ 0.7 ？")
    arrow(ax, cx, 17.5, cx, 13)
    ax.text(cx + 3.5, 15.2, "是", ha="center", va="center", fontsize=10.5,
            color=NAVY, weight="bold")

    rounded_box(ax, bx, 3, bw, 10, LIGHT, "输出叠加图 + 告警报告", tcolor=NAVY, fs=12.5)
    arrow(ax, bx + bw, 8, 71, 8, color="#98a2b3")

    rounded_box(ax, 72, 3, 25, 10, ORANGE, "人工复核确认下发", tcolor="white", fs=12.5)

    # 否定分支
    arrow(ax, cx + 15, 57.5, 71, 57.5, color="#98a2b3")
    ax.text(cx + 18.5, 59.7, "否", ha="center", va="center", fontsize=10.5, color=GRAY, weight="bold")
    rounded_box(ax, 72, 54, 25, 7, SKIP, "判为无积水 · 下一帧", tcolor="#3a4353", fs=10.5)

    arrow(ax, cx + 15, 23.5, 71, 23.5, color="#98a2b3")
    ax.text(cx + 18.5, 25.7, "否", ha="center", va="center", fontsize=10.5, color=GRAY, weight="bold")
    rounded_box(ax, 72, 20, 25, 7, SKIP, "过滤为误报", tcolor="#3a4353", fs=10.5)

    fig.savefig(OUT, bbox_inches="tight", facecolor="white", pad_inches=0.12)
    plt.close(fig)
    print("已生成", OUT)


if __name__ == "__main__":
    main()
