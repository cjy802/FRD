"""绘制路面积水识别（YOLO 初筛 + GLM 复检）中文流程图，输出 _figs/fig_recognition_flow_zh.png。

步骤与阈值取自 Web/backend/photo_analysis.py、scripts/run_water_yolo.py、run_water_glm.py 的真实实现。
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Polygon

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

OUT = Path(r"D:\低空经济\_figs\fig_recognition_flow_zh.png")

C_INPUT = "#dce6f2"   # 浅蓝灰：输入/输出
C_YOLO = "#2e6db4"    # 蓝：YOLO 初筛（同漏斗图）
C_GLM = "#1e5c45"     # 深绿：GLM 复检（同漏斗图）
C_HUMAN = "#c65a1e"   # 橙：人工环节（同漏斗图）
C_SKIP = "#f2f2f2"    # 灰：否定分支
C_YOLO_TEXT = "white"
C_GLM_TEXT = "white"
C_HUMAN_TEXT = "white"


def rounded_box(ax, x, y, w, h, facecolor, title, body="", tcolor="black", fs_t=12, fs_b=9.5):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.6,rounding_size=1.2",
                                facecolor=facecolor, edgecolor="#555555", lw=1.0))
    if body:
        ax.text(x + w / 2, y + h * 0.68, title, ha="center", va="center",
                fontsize=fs_t, color=tcolor, weight="bold")
        ax.text(x + w / 2, y + h * 0.32, body, ha="center", va="center",
                fontsize=fs_b, color=tcolor, linespacing=1.45)
    else:
        ax.text(x + w / 2, y + h / 2, title, ha="center", va="center",
                fontsize=fs_t, color=tcolor, weight="bold")


def diamond(ax, cx, cy, w, h, text, fs=10.5):
    ax.add_patch(Polygon([(cx - w / 2, cy), (cx, cy + h / 2), (cx + w / 2, cy), (cx, cy - h / 2)],
                         closed=True, facecolor="#fff3d9", edgecolor="#555555", lw=1.0))
    ax.text(cx, cy, text, ha="center", va="center", fontsize=fs, linespacing=1.4)


def arrow(ax, x1, y1, x2, y2, label="", lx=0, ly=0, color="#333333"):
    ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(arrowstyle="-|>", color=color, lw=1.4, shrinkA=2, shrinkB=2))
    if label:
        ax.text((x1 + x2) / 2 + lx, (y1 + y2) / 2 + ly, label, ha="center", va="center",
                fontsize=10, color="#333333", weight="bold")


def main():
    fig, ax = plt.subplots(figsize=(11, 8.2), dpi=200)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis("off")

    ax.text(50, 97.5, "路面积水智能识别流程：YOLOv8n-seg 初筛 + GLM-5.3-Flash 复检",
            ha="center", va="center", fontsize=15, weight="bold")

    # 主干：x 居中于 36，宽 40
    bx, bw = 16, 40
    rounded_box(ax, bx, 86, bw, 8, C_INPUT,
                "巡查照片 / 巡检视频帧", "视频先自适应抽帧，只送检关键帧")
    arrow(ax, bx + bw / 2, 86, bx + bw / 2, 81.5)

    rounded_box(ax, bx, 68, bw, 13, C_YOLO,
                "① YOLOv8n-seg 初筛（本地 GPU）",
                "四域混训权重：Puddle-1000 + FRED + FloodNet + 本地种子\n输出水面实例掩膜与逐像素置信度图 · 置信度阈值 0.25",
                tcolor=C_YOLO_TEXT, fs_b=9)
    arrow(ax, bx + bw / 2, 68, bx + bw / 2, 63.5)

    diamond(ax, bx + bw / 2, 57.5, 26, 12, "检出疑似\n水面区域？")
    arrow(ax, bx + bw / 2, 51.5, bx + bw / 2, 47, "是", lx=3)

    rounded_box(ax, bx, 34, bw, 13, C_GLM,
                "② GLM-5.3-Flash 复检（仅初筛命中帧）",
                "提示词限定“路面积水”：排除阴影/反光/湿痕/\n屋顶·泳池·河道等非路面水体；返回水坑多边形",
                tcolor=C_GLM_TEXT, fs_b=9)
    arrow(ax, bx + bw / 2, 34, bx + bw / 2, 29.5)

    diamond(ax, bx + bw / 2, 23.5, 26, 12, "水坑置信度\n≥ 0.7 ？")
    arrow(ax, bx + bw / 2, 17.5, bx + bw / 2, 13, "是", lx=3)

    rounded_box(ax, bx, 3, bw, 10, C_INPUT,
                "输出叠加图 + 疑似区域掩膜 + JSON 报告", None)
    arrow(ax, bx + bw, 8, 72, 8, color="#777777")
    rounded_box(ax, 72, 3, 24, 10, C_HUMAN,
                "人工复核确认后下发", "候选线索需人工核对\n像素占比≠面积/水深", tcolor=C_HUMAN_TEXT, fs_b=8.5)

    # 否定分支
    arrow(ax, bx + bw / 2 + 13, 57.5, 72, 57.5, "否", ly=2.5)
    rounded_box(ax, 72, 54, 24, 7, C_SKIP, "判为无积水\n处理下一帧", fs_t=9.5)
    arrow(ax, 84, 54, 84, 44, color="#999999")
    ax.text(84, 42, "（并入下一帧循环）", ha="center", va="center", fontsize=8.5, color="#777777")

    arrow(ax, bx + bw / 2 + 13, 23.5, 72, 23.5, "否", ly=2.5)
    rounded_box(ax, 72, 20, 24, 7, C_SKIP, "过滤为误报\n不产生告警", fs_t=9.5)

    # 右侧成本注解
    ax.text(84, 88, "级联收益：复检只处理初筛命中帧，\n实测任务中占送检帧约 1/4",
            ha="center", va="center", fontsize=9, color="#555555", linespacing=1.5)

    fig.tight_layout()
    fig.savefig(OUT, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print("已生成", OUT)


if __name__ == "__main__":
    main()
