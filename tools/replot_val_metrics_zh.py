"""把 YOLO val 输出的英文指标图重绘为中文版，供作品方案/PPT 使用。

曲线图（BoxPR_curve / BoxF1_curve）按像素提取"全部类别"粗蓝线数据；
两张混淆矩阵按原图读数直接重绘。输出到 _figs/。
"""
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

VAL_DIR = Path(r"D:\低空经济\model\runs\detect\val")
OUT_DIR = Path(r"D:\低空经济\_figs")

BLUE = "#0000FF"
THIN = "#1f77b4"


def find_axes_box(arr):
    """定位绘图区黑色边框，返回 left/right/top/bottom 像素坐标。"""
    dark = (arr[..., :3] < 100).all(axis=2)
    h, w = dark.shape
    cols = np.where(dark.sum(axis=0) > 0.5 * h)[0]
    rows = np.where(dark.sum(axis=1) > 0.5 * w)[0]
    assert cols.size >= 2 and rows.size >= 2, "未找到坐标轴边框"
    return cols.min(), cols.max(), rows.min(), rows.max()


def extract_blue_curve(png):
    """提取坐标区内的纯蓝粗线（all classes），坐标轴范围按 0-1 映射。"""
    arr = np.asarray(Image.open(png).convert("RGB"))
    left, right, top, bottom = find_axes_box(arr)
    r, g, b = arr[..., 0].astype(int), arr[..., 1].astype(int), arr[..., 2].astype(int)
    blue = (b > 150) & (r < 100) & (g < 100)
    xs, ys = [], []
    for c in range(left + 1, right):
        rows = np.where(blue[top + 1 : bottom, c])[0]
        if rows.size == 0:
            continue
        xs.append((c - left) / (right - left))
        ys.append((bottom - (rows.mean() + top + 1)) / (bottom - top))
    return np.array(xs), np.array(ys)


def plot_curve(png, out, title, xlabel, ylabel, legend2, vline=None):
    x, y = extract_blue_curve(png)
    fig, ax = plt.subplots(figsize=(8, 5.5), dpi=200)
    ax.plot(x, y, color=THIN, lw=1.2, label="积水（water）")
    ax.plot(x, y, color=BLUE, lw=3, label=legend2)
    if vline is not None:
        ax.axvline(vline, color="gray", ls="--", lw=1, alpha=0.6)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1.02)
    ax.set_xlabel(xlabel, fontsize=13)
    ax.set_ylabel(ylabel, fontsize=13)
    ax.set_title(title, fontsize=15)
    ax.grid(True, ls="--", lw=0.5, alpha=0.4)
    ax.legend(loc="upper right", fontsize=11, framealpha=0.9)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    return x, y


def plot_cm(mat, out, title, fmt, vmax, mask_bg=True):
    """按 YOLO 版式重绘混淆矩阵：行=预测类别，列=真实类别。"""
    fig, ax = plt.subplots(figsize=(8, 6.5), dpi=200)
    data = mat.astype(float).copy()
    if mask_bg:
        data[1, 1] = np.nan  # 背景×背景格 YOLO 原图留空
    im = ax.imshow(np.ma.masked_invalid(data), cmap="Blues", vmin=0, vmax=vmax)
    for i in range(2):
        for j in range(2):
            if mask_bg and i == 1 and j == 1:
                continue
            val = mat[i, j]
            ax.text(j, i, fmt.format(val), ha="center", va="center",
                    fontsize=15, color="white" if val > 0.55 * vmax else "black")
    ax.set_xticks([0, 1], ["积水", "背景"])
    ax.set_yticks([0, 1], ["积水", "背景"])
    ax.set_xlabel("真实类别", fontsize=13)
    ax.set_ylabel("预测类别", fontsize=13)
    ax.set_title(title, fontsize=15)
    fig.colorbar(im, ax=ax, shrink=0.85)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    x, y = plot_curve(
        VAL_DIR / "BoxPR_curve.png", OUT_DIR / "fig_metrics_pr_zh.png",
        "精确率–召回率曲线", "召回率（Recall）", "精确率（Precision）",
        "全部类别 0.753  mAP@0.5")
    print("PR 曲线点数 %d，梯形面积(≈mAP) %.3f" % (x.size, float(np.trapezoid(y, x))))

    x, y = plot_curve(
        VAL_DIR / "BoxF1_curve.png", OUT_DIR / "fig_metrics_f1_zh.png",
        "F1 分数–置信度曲线", "置信度（Confidence）", "F1 分数",
        "全部类别 0.73 @ 0.331", vline=0.331)
    print("F1 曲线点数 %d，峰值 %.3f @ %.3f" % (x.size, y.max(), x[y.argmax()]))

    plot_cm(
        np.array([[0.75, 1.00], [0.25, 0.0]]), OUT_DIR / "fig_cm_norm_zh.png",
        "混淆矩阵（按真实类别归一化）", "{:.2f}", 1.0)
    plot_cm(
        np.array([[540, 209], [177, 0]]), OUT_DIR / "fig_cm_raw_zh.png",
        "混淆矩阵（样本数）", "{:.0f}", 540)
    print("混淆矩阵完成，输出目录:", OUT_DIR)


if __name__ == "__main__":
    main()
