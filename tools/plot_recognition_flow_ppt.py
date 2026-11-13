"""PPT 版积水识别流程图（2×2 蛇形布局、少字、贴 PPT 蓝色版式），输出 _figs/fig_recognition_flow_ppt.png。

画幅比例约 1.32:1，对应 PPT「核心二·模型识别效果」页左侧红框区域。
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyBboxPatch

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

OUT = Path(r"D:\低空经济\_figs\fig_recognition_flow_ppt.png")

NAVY = "#16418E"      # PPT 深蓝
BLUE = "#2E6DB4"      # 中蓝
LIGHT = "#E8F0FB"     # 浅蓝卡底
BORDER = "#9DB8D9"
ORANGE = "#C65A1E"    # 人工环节（同漏斗图）
GRAY = "#6b7686"

# (x, y, 底色, 编号, 标题, 副题, 卡下注, 卡下注水平偏移)
CARDS = [
    (4,  41, LIGHT, "1", "巡查图像",      "照片 / 视频抽帧",   "自适应抽帧", 0),
    (53, 41, NAVY,  "2", "YOLOv8n-seg",   "① 初筛 · 水面掩膜", "本地GPU · 阈值0.25", -9),
    (53, 7,  BLUE,  "3", "GLM-5.3-Flash", "② 复检 · 路面积水", "限定提示词 · 阈值0.7", -9),
    (4,  7,  ORANGE, "4", "人工复核",      "确认后下发告警",   "像素占比 ≠ 面积", 0),
]
CW, CH = 43.0, 26.0


def card(ax, x, y, fill, num, title, sub, note, note_dx=0):
    ax.add_patch(FancyBboxPatch((x + 0.8, y - 1.0), CW, CH,
                                boxstyle="round,pad=0.4,rounding_size=1.8",
                                facecolor="#8fa3bd", alpha=0.30, edgecolor="none"))
    ax.add_patch(FancyBboxPatch((x, y), CW, CH,
                                boxstyle="round,pad=0.4,rounding_size=1.8",
                                facecolor=fill, edgecolor=BORDER if fill == LIGHT else "none",
                                lw=1.0))
    tcolor = "white" if fill != LIGHT else NAVY
    badge_fc = NAVY if fill == LIGHT else "white"
    badge_tc = "white" if fill == LIGHT else fill
    ax.add_patch(Circle((x + 4.5, y + CH - 4.5), 2.6, facecolor=badge_fc, edgecolor="none"))
    ax.text(x + 4.5, y + CH - 4.55, num, ha="center", va="center",
            fontsize=13, weight="bold", color=badge_tc)
    ax.text(x + CW / 2 + 1.5, y + CH * 0.58, title, ha="center", va="center",
            fontsize=17, weight="bold", color=tcolor)
    ax.text(x + CW / 2 + 1.5, y + CH * 0.24, sub, ha="center", va="center",
            fontsize=11.5, color=tcolor, alpha=0.95)
    ax.text(x + CW / 2 + note_dx, y - 3.4, note, ha="center", va="center", fontsize=9.5, color=GRAY)


def arrow(ax, x1, y1, x2, y2):
    ax.annotate("", xy=(x2, y2), xytext=(x1, y1),
                arrowprops=dict(arrowstyle="-|>", color=NAVY, lw=2.6, shrinkA=0, shrinkB=0))


def main():
    fig, ax = plt.subplots(figsize=(11.5, 8.7), dpi=200)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 76)
    ax.axis("off")

    for x, y, fill, num, title, sub, note, note_dx in CARDS:
        card(ax, x, y, fill, num, title, sub, note, note_dx)

    mid_top = 41 + CH / 2       # 上排中线
    mid_bot = 7 + CH / 2        # 下排中线
    arrow(ax, 4 + CW + 1.2, mid_top, 53 - 1.2, mid_top)          # 1 → 2
    arrow(ax, 53 + CW / 2, 41 - 1.2, 53 + CW / 2, 7 + CH + 1.2)  # 2 → 3
    arrow(ax, 53 - 1.2, mid_bot, 4 + CW + 1.2, mid_bot)          # 3 → 4

    fig.savefig(OUT, bbox_inches="tight", pad_inches=0.12, transparent=True)
    plt.close(fig)
    print("已生成", OUT)


if __name__ == "__main__":
    main()
