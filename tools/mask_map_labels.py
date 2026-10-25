# -*- coding: utf-8 -*-
"""把演示视频中地图上的路名/地名/校名模糊掉,字幕与界面文字不动。

用法:
  python mask_map_labels.py collect   # 抽样 OCR,汇总文字清单到 texts.json 供人工审核
  python mask_map_labels.py apply     # 逐帧 OCR,命中地名则高斯模糊并写回帧文件夹
"""
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
from rapidocr_onnxruntime import RapidOCR

VIDEO = Path(r"D:\低空经济\视频\完整视频_10.25_1.0 - 副本.mp4")
FRAMES_DIR = Path(r"D:\低空经济\视频\新建文件夹")
WORKDIR = Path(r"D:\低空经济\视频\_inspect")
TEXTS_JSON = WORKDIR / "texts.json"

FPS = 30.0
TOTAL_FRAMES = 7832

# 131s 之后无地图(已人工逐段确认),只处理 0-131s
SEG_END_S = 131.0

# UI 文本白名单:出现在地图区域但属于界面控件,不模糊
UI_WHITELIST = {
    "当前成图", "3D航线", "2D平面", "3D视角", "50m AGL", "50mAGL", "地球模式",
    "路径/主点", "节点/视角", "20千米", "退出模拟飞行", "态势角度", "当前视角",
    "地图", "规划模式", "区域道路重置", "OpenStreetMap contributors", "任务高度",
    "路名参考路线", "层级", "极简阶段", "管理员", "退出", "创建任务", "模块",
    "点击地图设置区域终点B", "区域监测巡查", "点段点路线", "52°", "1x",
    "查看", "排序", "此电脑", "详细信息", "倍率", "倍率1x", "地图选点",
    # 道路筛选复选框(以"路"结尾但属于界面)
    "高速道路", "快速/主干道", "一级道路", "二级道路", "三级道路",
    "住宅道路", "普通道路", "服务道路", "连接道路", "连按道路",
    # 章节标题(大字)
    "1.路线线路规划", "2.天气情况预警", "3.飞行路线导入", "4.路况视频分析",
    "5.现场端展示",
    # DJI 屏幕与 3D 场景控件
    "模拟飞行", "飞行安全地图", "飞行区", "航线点4", "航线点5", "待评估",
}

# 地名黑名单:抽样确认过的地名与 OCR 误读变体,整词或作为子串出现都模糊
PLACE_NAMES = {
    # 路名
    "创业大道", "育英路", "文博路", "文津路", "天祥路", "信息大道", "崇武中路",
    "常武中路", "鸥翔中路", "鸣新中路", "新中路", "文华路", "天津路", "天泽路",
    "湖滨路", "香樟大道", "香禅天道", "银杏路", "文枢路", "文源路", "天健东路",
    "天健西路", "天润大道", "天润大进", "X205",
    # 区域/城镇/城市
    "常州", "武进", "溧阳", "湖塘", "南夏墅", "科教城", "新区", "安山",
    "无锡", "苏州", "南京", "镇江", "泰州", "扬州", "宣城", "南通", "合肥",
    "摩尔上品", "南周新村", "新村", "大学新村",
    # 学校/建筑
    "常州大学", "常州科教", "武进校区", "成校区", "学校", "学院", "校区",
    "软件与大数据学院", "件与大", "信息学院", "网络空", "文博", "校友林",
    "图书馆", "学生活动中心", "业中心", "工业中", "音乐喷泉", "电子阅览室",
    "相城基督教堂",
}


def read_frame(path: Path):
    """cv2.imread 在 Windows 中文路径下失效,改用 fromfile/imdecode。"""
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def write_frame(path: Path, frame) -> None:
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if ok:
        buf.tofile(str(path))


def char_height(box):
    """横排取框高、竖排取框宽,近似单字高度。"""
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    w = max(xs) - min(xs)
    h = max(ys) - min(ys)
    return min(w, h), min(xs), min(ys), w, h


def _looks_like_place_pattern(t: str) -> bool:
    if re.match(r"^[\u4e00-\u9fa5A-Za-z0-9]{1,8}(路|大道|大街|街|巷|弄|桥|镇|村|园|苑)$", t):
        return True
    if re.match(r"^[\u4e00-\u9fa5]{1,8}(楼|馆|堂|林|学院|大学|校区|中心)$", t):
        return True
    if re.match(r"^[A-Za-z\d]{1,2}号?楼$", t):
        return True
    # "N栋教学楼"及其 OCR 误读(C幢教/D撞童教/E格教...)
    if len(t) <= 5 and t.endswith("教"):
        return True
    if re.match(r"^[A-Z]\d{2,3}$", t):
        return True
    return False


def is_place(text: str) -> bool:
    t = text.strip()
    if not t or t in UI_WHITELIST:
        return False
    if any(name in t for name in PLACE_NAMES):
        return True
    return _looks_like_place_pattern(t)


def should_mask(text: str, score: float, char_h: float, y0: float) -> bool:
    """综合规则:小字地名模糊;字幕/章节标题/低置信度噪声不动。"""
    if score < 0.5:
        return False
    if char_h < 8 or char_h > 60:
        return False
    if char_h > 45 and len(text) >= 8 and y0 > 980:
        return False  # 底部解说字幕
    return is_place(text)


def blur_region(frame, x0, y0, x1, y1, char_h: float):
    """对区域做强高斯模糊,大字号补一遍加深。"""
    x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
    roi = frame[y0:y1, x0:x1]
    if roi.size == 0:
        return
    k = int(max(char_h * 1.8, 15))
    if k % 2 == 0:
        k += 1
    blurred = cv2.GaussianBlur(roi, (k, k), 0)
    if char_h > 18:
        blurred = cv2.GaussianBlur(blurred, (k, k), 0)
    frame[y0:y1, x0:x1] = blurred


def collect():
    ocr = RapidOCR()
    agg = defaultdict(lambda: {"n": 0, "hs": [], "xs": [], "ys": [], "score": 0.0})
    t0 = time.time()
    sampled = 0
    for fidx in range(0, int(SEG_END_S * FPS), 60):
        path = FRAMES_DIR / f"f_{fidx:05d}.jpg"
        frame = read_frame(path)
        if frame is None:
            continue
        res, _ = ocr(frame)
        if not res:
            continue
        sampled += 1
        for box, text, score in res:
            ch, x, y, _, _ = char_height(box)
            e = agg[text.strip()]
            e["n"] += 1
            e["hs"].append(round(ch, 1))
            e["xs"].append(int(x))
            e["ys"].append(int(y))
            e["score"] = max(e["score"], float(score))
        print(f"  frame {fidx}  boxes={len(res)}  ({time.time()-t0:.0f}s)", flush=True)
    out = []
    for text, e in agg.items():
        out.append({
            "text": text, "n": e["n"],
            "char_h_min": min(e["hs"]), "char_h_max": max(e["hs"]),
            "x": e["xs"][0], "y": e["ys"][0],
            "score": round(e["score"], 2),
            "mask": is_place(text),
        })
    out.sort(key=lambda r: -r["n"])
    WORKDIR.mkdir(exist_ok=True)
    TEXTS_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{sampled} frames sampled, {len(out)} unique texts -> {TEXTS_JSON}")


_ocr = None


def _init_worker():
    """每个 worker:限制 ONNX 会话线程数,避免多进程超订阅互相争抢。"""
    global _ocr
    import rapidocr_onnxruntime.utils as rutils

    _orig_options = rutils.SessionOptions

    def _limited_options():
        opt = _orig_options()
        opt.intra_op_num_threads = 4
        opt.inter_op_num_threads = 1
        return opt

    rutils.SessionOptions = _limited_options
    _ocr = RapidOCR(det_model_path=None, cls_model_path=None, rec_model_path=None)


def _process_frame(fidx: int):
    """读一帧,OCR,模糊命中的地名,写回。返回 (帧号, 命中数, 是否写回)。"""
    path = FRAMES_DIR / f"f_{fidx:05d}.jpg"
    frame = read_frame(path)
    if frame is None:
        return fidx, 0, False
    res, _ = _ocr(frame)
    hits = 0
    changed = False
    if res:
        h_img, w_img = frame.shape[:2]
        for box, text, score in res:
            ch, x, y, w, h = char_height(box)
            if not should_mask(text.strip(), float(score), ch, y):
                continue
            pad = max(2, int(ch * 0.4))
            x0 = max(0, x - pad)
            y0 = max(0, y - pad)
            x1 = min(w_img, x + w + pad)
            y1 = min(h_img, y + h + pad)
            blur_region(frame, x0, y0, x1, y1, ch)
            hits += 1
            changed = True
    if changed:
        write_frame(path, frame)
    return fidx, hits, changed


def apply(end_s: float = SEG_END_S, workers: int = 7):
    from concurrent.futures import ProcessPoolExecutor

    f_end = min(int(end_s * FPS) + 1, TOTAL_FRAMES)
    t0 = time.time()
    total_hits = total_touched = 0
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker) as ex:
        for i, (fidx, hits, changed) in enumerate(
            ex.map(_process_frame, range(1, f_end + 1)), 1
        ):
            total_hits += hits
            total_touched += int(changed)
            if i % 50 == 0:
                dt = time.time() - t0
                eta = dt / i * (f_end - i)
                print(f"  {i}/{f_end} frames, hits={total_hits}, "
                      f"touched={total_touched}, elapsed={dt:.0f}s, eta={eta:.0f}s",
                      flush=True)
    print(f"done: {f_end} frames processed, {total_hits} regions blurred, "
          f"{total_touched} frames rewritten, {time.time()-t0:.0f}s total")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "collect":
        collect()
    elif cmd == "apply":
        end_s = float(sys.argv[2]) if len(sys.argv) > 2 else SEG_END_S
        apply(end_s)
    else:
        print(__doc__)
