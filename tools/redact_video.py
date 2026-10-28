# -*- coding: utf-8 -*-
"""演示视频地图文字打码(修复式):地图画布内所有文字默认擦除,仅保留软件界面文字。

思路相对 tools/mask_map_labels.py 反转:
  旧: OCR 认出文字 -> 正则/黑名单判断是不是地名 -> 漏判一个字=泄露
  新: 地图画布里 OCR 读到的一切默认擦掉,只有界面文字保留 -> 误判=多擦一小块无害的

画面区域三分类:
  1) UI 面板: 紧贴屏幕边缘的大块纯白区域(形态学开运算排除地图上的白色道路线)。
     面板内是软件界面文字 -> 只打 地名/坐标/道路编号 子串
  2) 地图画布: 其余区域。is_map 帧的画布文字 -> 白名单外全部擦除;
     字高 >36px 的大字视为解说字幕/标题 -> 只打其中的地名子串
  3) 必应小地图卡: 用"放大地图"按钮定位,整块填白

OCR 引擎: OCR_ENGINE=paddle(官方 PaddleOCR, 需独立 python 环境) / rapid(RapidOCR) / auto。
PaddleOCR 环境示例: D:/anaconda/envs/ocr/python.exe -m redact_video.py ...

用法:
  python redact_video.py ocr              # 全帧 OCR -> boxes.jsonl(可中断续跑)
  python redact_video.py apply            # 依据 boxes.jsonl 分类+inpaint 修复,写回帧
  python redact_video.py sweep            # 对已打码帧再 OCR,补擦残留
  python redact_video.py test <图片路径>   # 单帧全流程测试,出 before/boxes/after 三张图
  python redact_video.py sheet 1 500 1000 # 每4帧拼一张 2x2 抽检图到 qa/
  python redact_video.py tile 1000        # 单帧切 6 块全分辨率送检
  python redact_video.py preview 1 500    # OCR 判定可视化(红=整擦 橙=子串 绿=保留)
  python redact_video.py report           # 输出地图段区间与必应卡片帧

注意: apply/sweep 直接覆盖 frames/ 里的帧;改规则重跑前需重新用 ffmpeg 抽帧
(boxes.jsonl 是 OCR 缓存,换引擎后需清掉重跑)。
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import cv2
import numpy as np

VIDEO = Path(r"D:\低空经济\视频\完整视频_10.25_1.0.mp4")
WORK = Path(r"D:\低空经济\视频\_mask_1025")
FRAMES = WORK / "frames"
BOXES_JSONL = WORK / "boxes.jsonl"
RESID_LOG = WORK / "residual.jsonl"
QA = WORK / "qa"

WORKERS = int(os.environ.get("OCR_WORKERS", "0")) or 14  # paddle 引擎下被 _workers_for() 调低
# 打码判定阈值:paddle 的识别分普遍偏低,全量跑时用 REDACT_MIN_SCORE=0.32
MIN_SCORE = float(os.environ.get("REDACT_MIN_SCORE", "0.45"))
SWEEP_MIN_SCORE = float(os.environ.get("REDACT_SWEEP_MIN_SCORE", "0.62"))
JPEG_Q = 95
SMOOTH_SPAN = 15       # 地图分类沿时间轴前后扩散的帧数
NEIGHBOR_UNION = 2     # 打码区域与前后 N 帧的检测框取并集,防单帧漏检闪烁

# 只处理这块区域(宽高比例,覆盖网页内容区;浏览器黑边/侧栏不碰)
REGION = (0.03, 0.055, 0.985, 1.0)
# 画布上字高超过该值视为解说字幕/章节标题,不整擦(地名子串仍会打)
CAPTION_CH = 36

PANEL_WHITE_MIN = 232   # 近白阈值(三通道最小值)
PANEL_OPEN_K = 21       # 开运算核:掐断细的白色道路线,保住大块面板
PANEL_MIN_AREA = 35000  # 面板最小面积(像素)
PANEL_EDGE_TOL = 8      # 面板须贴近屏幕边缘

# 地图画面特征文字(归一化后包含即判为地图帧;避免"地图""地球"这类必应页也有的词)
MAP_SIGS = {
    "当前成图", "地球模式", "规划模式", "观察角度", "态势角度", "任务高度",
    "2d平面", "3d视角", "路径/主点", "节点/视角", "srt轨迹", "区域道路",
    "地图选点", "退出模拟飞行", "模拟飞行", "飞行安全地图", "飞行区",
    "点击地图设置", "区域监测巡查", "设置区域起点", "设置区域终点",
    "50magl", "近拍点", "航线点", "待评估", "当前底图",
}
# 必应天气页右上角小地图卡的锚点(黑色"放大地图"按钮)
BING_CARD_ANCHOR = "放大地图"

# 界面文字白名单:出现在地图画布但不该擦的软件控件/标题。
# 匹配规则: 归一化后 精确相等,或 长度>=3 的条目作为子串出现(容忍 OCR 误读/粘连),
# 或 命中 WHITELIST_PREFIX 前缀。
WHITELIST = {
    "当前成图", "当前底图", "3d航线", "当前成图·3d航线", "当前底图·3d航线",
    "2d平面", "3d视角", "50magl", "地球模式", "路径/主点", "节点/视角",
    "20千米", "退出模拟飞行", "态势角度", "当前视角", "地图", "规划模式",
    "区域道路重置", "区域道路重画", "区域道路重绘",
    "openstreetmapcontributors", "任务高度", "路名参考路线", "s道路参考路线",
    "层级", "极简阶段", "原型阶段", "管理员", "退出", "创建任务", "模块",
    "点击地图设置区域终点b", "区域监测巡查", "点段点路线", "点到点路线", "52°",
    "1x", "查看", "排序", "此电脑", "详细信息", "倍率", "倍率1x", "地图选点",
    # 道路筛选复选框(以"路"结尾但属于界面)
    "高速道路", "快速/主干道", "一级道路", "二级道路", "三级道路",
    "住宅道路", "普通道路", "服务道路", "连接道路", "连按道路",
    # 章节标题(大字)
    "1.路线线路规划", "2.天气情况预警", "3.飞行路线导入", "4.路况视频分析",
    "5.现场端展示",
    # DJI 屏与 3D 场景控件
    "模拟飞行", "飞行安全地图", "飞行区", "待评估",
    # 向导大按钮/底栏
    "进入框选街道", "规划本次飞行", "逐点设置高度和面向", "继续查看天气",
    "设置区域起点a",
}
WHITELIST_PREFIX = ("航线点", "任务高度", "观察角度", "视觉角度")

# 航点/拍照点等单字符标记(归一化后)
SINGLE_KEEP = {"a", "b", "c", "p", "拍", "低", "✓", "√", "n", "s", "w", "e"}

# HUD/比例尺/高度角标等纯数字+单位串(不含中文)保留
NUMERIC_KEEP_RE = re.compile(
    r"^[\d.,:°'\"x×+/%℃\s-]*(m|s|km|h|hz|ms|m/s|km/h|kmh|agl|米|千米|米/秒|千米/时|千米/小时|时|分|秒|级|倍)?$",
    re.I,
)
# 画布上保留的数字必须带单位(24.6m/s、52°);裸数字串(路号牌 256)要擦
NUMERIC_UNIT_RE = re.compile(
    r"^[\d.,:°'\"x×+/%℃\s-]*(m|s|km|h|hz|ms|m/s|km/h|kmh|agl|米|千米|米/秒|千米/时|千米/小时|时|分|秒|级|倍|°)$",
    re.I,
)
# 字母开头的道路编号(X205/S232)无论在哪种区域都擦
ROAD_NUM_RE = re.compile(r"^[a-z]\d{1,4}[a-z]?$", re.I)
# 经纬度坐标对(31.679,119.949 这类)无论在哪种区域都擦:比地名暴露得更直接
COORD_RE = re.compile(r"\d{1,3}\.\d{3,}\s*[,，]\s*-?\d{1,3}\.\d{3,}")
CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# 面板/必应页等非画布区域里出现的地名子串:命中就只擦那几个字
PLACE_SUBSTRINGS = [
    "常州大学", "常州科教", "常州", "溧阳", "武进", "湖塘镇", "湖塘", "南夏墅",
    "科教城", "高新区", "江苏", "南京", "镇江", "无锡", "苏州", "泰州", "扬州",
    "宣城", "南通", "合肥", "湖州", "金坛", "新北", "天宁", "钟楼",
    "大学新村", "南周新村", "新村", "摩尔上品",
    "鸣新", "常武", "香樟", "育英", "文博", "文津", "文彬", "文枢", "文源",
    "文华", "天祥", "天健", "天润", "天泽", "信息大道", "崇武", "鸥翔",
    "湖滨路", "银杏路", "天津路", "创业大道", "工业中", "信息学院", "网格空间",
    "x205",
]
_PLACE_PATS = [re.compile(r"\s*".join(re.escape(c) for c in p), re.I)
               for p in PLACE_SUBSTRINGS]


def norm(s: str) -> str:
    """归一化:去所有空白,方便匹配 OCR 造成的空格差异。"""
    return re.sub(r"\s+", "", s)


def read_frame(path: Path):
    """cv2.imread 在 Windows 中文路径下失效,改用 fromfile/imdecode。"""
    data = np.fromfile(str(path), dtype=np.uint8)
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def write_frame(path: Path, frame) -> None:
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_Q])
    if ok:
        buf.tofile(str(path))


def frame_files():
    files = sorted(FRAMES.glob("*.jpg"))
    return {int(p.stem): p for p in files}


def box_aabb(box):
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    return min(xs), min(ys), max(xs), max(ys)


def char_height(box):
    x0, y0, x1, y1 = box_aabb(box)
    return min(x1 - x0, y1 - y0)


def pad_rect(x0, y0, x1, y1, pad, w_img, h_img):
    return (max(0, int(x0 - pad)), max(0, int(y0 - pad)),
            min(w_img, int(x1 + pad)), min(h_img, int(y1 + pad)))


def in_region(box, w_img, h_img):
    x0, y0, x1, y1 = box_aabb(box)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    rx0, ry0, rx1, ry1 = REGION
    return (rx0 * w_img <= cx <= rx1 * w_img) and (ry0 * h_img <= cy <= ry1 * h_img)


def find_place_ranges(text: str):
    """返回 text 中命中的地名子串的字符区间列表(原始字符串下标,容忍空格干扰)。"""
    hay = text.lower()
    ranges = []
    for pat in _PLACE_PATS:
        start = 0
        while True:
            m = pat.search(hay, start)
            if not m:
                break
            ranges.append((m.start(), m.end()))
            start = m.end()
    if not ranges:
        return []
    ranges.sort()
    merged = [list(ranges[0])]
    for s, e in ranges[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [tuple(r) for r in merged]


def _wl_hit(tn: str) -> bool:
    if tn in WHITELIST or any(tn.startswith(p) for p in WHITELIST_PREFIX):
        return True
    return any(len(w) >= 3 and w in tn for w in WHITELIST)


def decide(text: str, score: float, ch: float, is_map: bool, in_panel: bool,
           min_score: float = None):
    """返回 None(保留) / ("full", None)(整框擦) / ("sub", ranges)(只擦部分字符)。

    in_panel: 文字位于 UI 面板上 -> 界面文字保留,只打 地名/坐标/路号。
    """
    t = text.strip()
    tn = norm(t).casefold()
    if min_score is None:
        min_score = MIN_SCORE
    if not tn or score < min_score or ch < 7 or ch > 200:
        return None
    if COORD_RE.search(tn):
        return ("full", None)
    if _wl_hit(tn):
        return None
    if in_panel:
        ranges = find_place_ranges(t)
        return ("sub", ranges) if ranges else None
    if len(tn) == 1 and tn in SINGLE_KEEP:
        return None
    if not CJK_RE.search(tn):
        if in_panel and NUMERIC_KEEP_RE.match(tn):
            return None
        if not in_panel and NUMERIC_UNIT_RE.match(tn):
            return None  # 画布上带单位的数字(24.6m/s)是 HUD,保留
        if len(tn) == 1:
            return None
        # 裸数字串(路号牌 256)、路号 X205、杂乱英文:画布上一律擦,其他画面保留
        return ("full", None) if is_map else None
    if is_map:
        if ch > CAPTION_CH:  # 画布大字=解说字幕/标题,只打其中地名
            ranges = find_place_ranges(t)
            return ("sub", ranges) if ranges else None
        return ("full", None)
    ranges = find_place_ranges(t)
    return ("sub", ranges) if ranges else None


# ---------------- OCR 引擎 ----------------
_engine_kind = None
_engine = None


def _init_worker():
    """初始化 OCR 引擎。paddle=官方 PaddleOCR;rapid=RapidOCR(限线程)。"""
    global _engine, _engine_kind
    want = os.environ.get("OCR_ENGINE", "auto")
    if want == "auto":
        try:
            import paddleocr  # noqa: F401
            want = "paddle"
        except Exception:
            want = "rapid"
    if want == "paddle":
        import paddleocr
        v = getattr(paddleocr, "__version__", "2")
        if v.startswith("3"):
            os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
            common = dict(
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                text_det_limit_side_len=1536,
                text_det_limit_type="max",
                text_det_thresh=0.3,
                text_det_box_thresh=0.5,
                text_det_unclip_ratio=1.6,
            )
            try:
                # Paddle 3.x Windows CPU 的 oneDNN 路径有 PIR 属性转换 bug,关掉用纯 CPU 推理
                _engine = paddleocr.PaddleOCR(enable_mkldnn=False, **common)
            except (TypeError, ValueError):
                _engine = paddleocr.PaddleOCR(**common)
        else:
            # 2.x: PP-OCRv4 + MKLDNN。放宽检测/识别阈值,小字地名召回优先
            _engine = paddleocr.PaddleOCR(
                use_angle_cls=False,
                show_log=False,
                enable_mkldnn=True,
                cpu_threads=4,
                det_limit_side_len=1536,
                det_limit_type="max",
                det_db_thresh=0.2,
                det_db_box_thresh=0.3,
                det_db_unclip_ratio=1.8,
                drop_score=0.3,
            )
        _engine_kind = "paddle"
    else:
        from rapidocr_onnxruntime import RapidOCR
        import rapidocr_onnxruntime.utils as rutils

        _orig_options = rutils.SessionOptions

        def _limited_options():
            opt = _orig_options()
            opt.intra_op_num_threads = 3
            opt.inter_op_num_threads = 1
            return opt

        rutils.SessionOptions = _limited_options
        _engine = RapidOCR(det_model_path=None, cls_model_path=None, rec_model_path=None)
        _engine_kind = "rapid"


def _parse_paddle(results):
    out = []
    for r in results or []:
        data = r if isinstance(r, dict) else None
        if data is None:
            v = getattr(r, "json", None)
            if isinstance(v, dict):
                data = v.get("res", v)
        if isinstance(data, dict):
            polys = data.get("rec_polys") or data.get("dt_polys") or data.get("boxes")
            texts = data.get("rec_texts") or data.get("text")
            scores = data.get("rec_scores") or data.get("score")
            if polys is not None and texts is not None:
                scores = scores or [1.0] * len(texts)
                for poly, t, s in zip(polys, texts, scores):
                    out.append(([[float(p[0]), float(p[1])] for p in np.asarray(poly)],
                                str(t), float(s)))
                continue
        # 2.x legacy: [[[box],(text,score)], ...]
        for line in (r if isinstance(r, (list, np.ndarray)) else []):
            try:
                box, ts = line[0], line[1]
                out.append(([[float(p[0]), float(p[1])] for p in box],
                            str(ts[0]), float(ts[1])))
            except Exception:
                continue
    return out


def _paddle_boxes(frame):
    """PaddleOCR 结果 -> [(box4x2, text, score)],兼容 3.x predict / 2.x ocr。"""
    parsed = []
    try:
        parsed = _parse_paddle(_engine.predict(frame))
    except Exception:
        pass
    if parsed:
        return parsed
    return _parse_paddle(_engine.ocr(frame))


def _run_ocr(frame):
    if _engine_kind == "paddle":
        return _paddle_boxes(frame)
    res, _ = _engine(frame)
    return [([[float(p[0]), float(p[1])] for p in box], str(text), float(score))
            for box, text, score in (res or [])]


def _workers_for():
    """paddle 每进程内存大,并发调低防把整机吃死。"""
    env = int(os.environ.get("OCR_WORKERS", "0"))
    if env:
        return env
    return 6 if os.environ.get("OCR_ENGINE", "auto") == "paddle" else 14


def _ocr_one(fidx: int):
    boxes = _run_ocr(read_frame(FRAMES / f"{fidx:06d}.jpg"))
    out = [{"box": b, "text": t, "score": round(s, 3)} for b, t, s in boxes]
    return fidx, out


def cmd_ocr():
    from concurrent.futures import ProcessPoolExecutor

    done = set()
    if BOXES_JSONL.exists():
        with BOXES_JSONL.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        done.add(json.loads(line)["f"])
                    except (json.JSONDecodeError, KeyError):
                        continue
    todo = sorted(set(frame_files()) - done)
    print(f"{len(done)} frames already OCR'd, {len(todo)} to go")
    if not todo:
        return
    t0 = time.time()
    with BOXES_JSONL.open("a", encoding="utf-8") as out, \
         ProcessPoolExecutor(max_workers=_workers_for(), initializer=_init_worker) as ex:
        for i, (fidx, boxes) in enumerate(ex.map(_ocr_one, todo, chunksize=8), 1):
            out.write(json.dumps({"f": fidx, "boxes": boxes}, ensure_ascii=False) + "\n")
            out.flush()
            if i % 100 == 0:
                dt = time.time() - t0
                eta = dt / i * (len(todo) - i)
                print(f"  {i}/{len(todo)} elapsed={dt:.0f}s eta={eta:.0f}s", flush=True)
    print(f"ocr done in {time.time()-t0:.0f}s")


# ---------------- 分类 ----------------
def load_boxes():
    data = {}
    with BOXES_JSONL.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue  # ocr 进程正在追加时可能读到半行
            data[r["f"]] = r["boxes"]
    return data


def classify(all_boxes):
    """返回 is_map / has_bing_card 两个 {帧号: bool}(沿时间轴扩散平滑)。"""
    fidxs = sorted(all_boxes)
    seed_map, seed_card = set(), set()
    for f in fidxs:
        for b in all_boxes[f]:
            tn = norm(b["text"]).casefold()
            if any(s in tn for s in MAP_SIGS):
                seed_map.add(f)
                break
    for f in fidxs:
        for b in all_boxes[f]:
            if BING_CARD_ANCHOR in norm(b["text"]).casefold():
                seed_card.add(f)
                break
    def spread(seeds):
        return {f: any(g in seeds for g in range(f - SMOOTH_SPAN, f + SMOOTH_SPAN + 1))
                for f in fidxs}
    return spread(seed_map), spread(seed_card)


# ---------------- 面板检测 ----------------
def panel_mask(frame):
    """紧贴屏幕边缘的大块近白区域。

    面板里文字/输入框密集,开运算会把白色区切碎成小条,
    所以开运算掐断道路线之后,先膨胀把碎片重新连成整块,再做连通域判定。
    """
    h, w = frame.shape[:2]
    white = (frame.min(axis=2) >= PANEL_WHITE_MIN).astype(np.uint8) * 255
    k_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (PANEL_OPEN_K, PANEL_OPEN_K))
    white = cv2.morphologyEx(white, cv2.MORPH_OPEN, k_open)
    k_big = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (45, 45))
    white = cv2.dilate(white, k_big, 1)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(white, 8)
    out = np.zeros((h, w), np.uint8)
    for i in range(1, n):
        x, y, ww, hh, area = stats[i]
        if area < PANEL_MIN_AREA:
            continue
        if x <= PANEL_EDGE_TOL or y <= PANEL_EDGE_TOL \
                or x + ww >= w - PANEL_EDGE_TOL or y + hh >= h - PANEL_EDGE_TOL:
            out[lab == i] = 255
    k_d = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    return cv2.dilate(out, k_d, 1)


# ---------------- 打码 ----------------
def sub_rects_for_box(box, text, ranges, pad, w_img, h_img):
    """按字符区间把检测框切成若干小矩形(沿框上下边线性插值)。"""
    p0, p1, p2, p3 = box  # tl, tr, br, bl
    n = max(len(text), 1)
    rects = []
    for s, e in ranges:
        ts = (p0[0] + (p1[0] - p0[0]) * s / n, p0[1] + (p1[1] - p0[1]) * s / n)
        te = (p0[0] + (p1[0] - p0[0]) * e / n, p0[1] + (p1[1] - p0[1]) * e / n)
        bs = (p3[0] + (p2[0] - p3[0]) * s / n, p3[1] + (p2[1] - p3[1]) * s / n)
        be = (p3[0] + (p2[0] - p3[0]) * e / n, p3[1] + (p2[1] - p3[1]) * e / n)
        xs = [ts[0], te[0], bs[0], be[0]]
        ys = [ts[1], te[1], bs[1], be[1]]
        rects.append(pad_rect(min(xs), min(ys), max(xs), max(ys), pad, w_img, h_img))
    return rects


def boxes_to_rects(boxes, pm, is_map, w_img, h_img, min_score=None):
    """把 OCR 框按决策转成擦除矩形。"""
    rects = []
    for b in boxes:
        if not in_region(b["box"], w_img, h_img):
            continue
        ch = char_height(b["box"])
        x0, y0, x1, y1 = box_aabb(b["box"])
        cx, cy = int((x0 + x1) / 2), int((y0 + y1) / 2)
        in_panel = bool(pm[cy, cx]) if (0 <= cy < h_img and 0 <= cx < w_img) else False
        d = decide(b["text"], b["score"], ch, is_map, in_panel, min_score)
        if d is None:
            continue
        kind, extra = d
        pad = max(2, int(ch * 0.4))
        if kind == "full":
            rects.append(pad_rect(x0, y0, x1, y1, pad, w_img, h_img))
        else:
            rects.extend(sub_rects_for_box(b["box"], b["text"], extra, pad, w_img, h_img))
    return rects


def bing_card_rect(all_boxes, fidx, w_img, h_img):
    """用"放大地图"黑按钮定位必应小地图卡,返回整体清除矩形。"""
    for nb_f in range(fidx - NEIGHBOR_UNION, fidx + NEIGHBOR_UNION + 1):
        for b in all_boxes.get(nb_f, []):
            if BING_CARD_ANCHOR in norm(b["text"]).casefold():
                x0, y0, x1, y1 = box_aabb(b["box"])
                # 按钮在卡片底部偏右,卡片约 250x270
                return (max(0, int(x0) - 240), max(0, int(y0) - 250),
                        min(w_img, int(x1) + 30), min(h_img, int(y1) + 15))
    return None


def _inpaint(frame, rects):
    mask = np.zeros(frame.shape[:2], dtype=np.uint8)
    for (x0, y0, x1, y1) in rects:
        mask[y0:y1, x0:x1] = 255
    if not mask.any():
        return frame
    mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)), 1)
    return cv2.inpaint(frame, mask, 4, cv2.INPAINT_TELEA)


def _apply_one(task):
    """worker:读帧 -> 面板检测 -> 决策(自框+邻帧并集) -> 必应卡填白 -> inpaint 写盘。

    无改动的帧不写盘,保住原画质。
    """
    fidx, own_boxes, nb_boxes, is_map, card = task
    frame = read_frame(FRAMES / f"{fidx:06d}.jpg")
    if frame is None:
        return fidx, 0
    h_img, w_img = frame.shape[:2]
    pm = panel_mask(frame)
    rects = boxes_to_rects(own_boxes, pm, is_map, w_img, h_img)
    if is_map:
        rects += boxes_to_rects(nb_boxes, pm, True, w_img, h_img)
    changed = 0
    if card:
        x0, y0, x1, y1 = card
        ring = 6
        samples = [frame[max(0, y0 - ring):y0, max(0, x0 - ring):min(w_img, x1 + ring)],
                   frame[y1:min(h_img, y1 + ring), max(0, x0 - ring):min(w_img, x1 + ring)],
                   frame[max(0, y0):y1, max(0, x0 - ring):x0],
                   frame[max(0, y0):y1, x1:min(w_img, x1 + ring)]]
        pix = np.concatenate([s.reshape(-1, 3) for s in samples if s.size], axis=0)
        fill = np.median(pix, axis=0).astype(np.uint8) if pix.size else np.array([255, 255, 255], np.uint8)
        frame[y0:y1, x0:x1] = fill
        changed += 1
    if rects:
        frame = _inpaint(frame, rects)
        changed += len(rects)
    if changed:
        write_frame(FRAMES / f"{fidx:06d}.jpg", frame)
    return fidx, changed


def cmd_apply():
    from concurrent.futures import ProcessPoolExecutor

    all_boxes = load_boxes()
    is_map, has_card = classify(all_boxes)
    n_map = sum(1 for v in is_map.values() if v)
    print(f"classified: {n_map} map-ish frames / {len(is_map)}")

    any_frame = read_frame(next(iter(frame_files().values())))
    h_img, w_img = any_frame.shape[:2]

    tasks = []
    for fidx in sorted(frame_files()):
        m = is_map.get(fidx, False)
        nb = []
        if m:
            for nb_f in range(fidx - NEIGHBOR_UNION, fidx + NEIGHBOR_UNION + 1):
                if nb_f != fidx:
                    nb.extend(all_boxes.get(nb_f, []))
        card = None
        if not m and has_card.get(fidx, False):
            card = bing_card_rect(all_boxes, fidx, w_img, h_img)
        own = all_boxes.get(fidx, [])
        if own or nb or card:
            tasks.append((fidx, own, nb, m, card))
    print(f"{len(tasks)} frames need processing")

    t0 = time.time()
    total = 0
    with ProcessPoolExecutor(max_workers=_workers_for()) as ex:
        for i, (fidx, n) in enumerate(ex.map(_apply_one, tasks, chunksize=4), 1):
            total += n
            if i % 300 == 0:
                print(f"  {i}/{len(tasks)} regions={total} elapsed={time.time()-t0:.0f}s", flush=True)
    print(f"apply done: {total} regions on {len(tasks)} frames, {time.time()-t0:.0f}s")


def _sweep_one(task):
    """worker:对已打码帧重新 OCR,残留则补擦(同样过面板检测)。"""
    fidx, is_map = task
    frame = read_frame(FRAMES / f"{fidx:06d}.jpg")
    if frame is None:
        return fidx, 0, None
    h_img, w_img = frame.shape[:2]
    res = _run_ocr(frame)
    pm = panel_mask(frame)
    rects, left = [], []
    for box, text, score in res:
        if not in_region(box, w_img, h_img):
            continue
        text = str(text)
        ch = char_height(box)
        x0, y0, x1, y1 = box_aabb(box)
        cx, cy = int((x0 + x1) / 2), int((y0 + y1) / 2)
        in_panel = bool(pm[cy, cx]) if (0 <= cy < h_img and 0 <= cx < w_img) else False
        d = decide(text, float(score), ch, is_map, in_panel, min_score=SWEEP_MIN_SCORE)
        if d is None:
            continue
        kind, extra = d
        pad = max(2, int(ch * 0.4))
        if kind == "full":
            rects.append(pad_rect(x0, y0, x1, y1, pad, w_img, h_img))
        else:
            rects.extend(sub_rects_for_box(box, text, extra, pad, w_img, h_img))
        left.append({"t": text, "s": round(float(score), 2), "panel": in_panel})
    if not rects:
        return fidx, 0, None
    frame = _inpaint(frame, rects)
    write_frame(FRAMES / f"{fidx:06d}.jpg", frame)
    return fidx, len(rects), left


def cmd_sweep():
    from concurrent.futures import ProcessPoolExecutor

    all_boxes = load_boxes()
    is_map, _ = classify(all_boxes)
    files = sorted(frame_files())
    t0 = time.time()
    total = frames_with_leak = 0
    with RESID_LOG.open("w", encoding="utf-8") as rlog, \
         ProcessPoolExecutor(max_workers=_workers_for(), initializer=_init_worker) as ex:
        for i, (fidx, n, left) in enumerate(
            ex.map(_sweep_one, [(f, is_map.get(f, False)) for f in files], chunksize=4), 1
        ):
            if n:
                total += n
                frames_with_leak += 1
                rlog.write(json.dumps({"f": fidx, "left": left}, ensure_ascii=False) + "\n")
            if i % 200 == 0:
                dt = time.time() - t0
                eta = dt / i * (len(files) - i)
                print(f"  {i}/{len(files)} leaks={total} frames={frames_with_leak} "
                      f"elapsed={dt:.0f}s eta={eta:.0f}s", flush=True)
    print(f"sweep done: {total} residual regions on {frames_with_leak} frames, {time.time()-t0:.0f}s")
    print(f"residual detail -> {RESID_LOG}")


# ---------------- 单帧测试 ----------------
def cmd_test(args):
    """对任意单帧跑完整流程,输出 before/boxes/after 三张图到 qa/。"""
    QA.mkdir(exist_ok=True)
    src = Path(args[0])
    frame = read_frame(src)
    if frame is None:
        print(f"cannot read {src}")
        return
    h_img, w_img = frame.shape[:2]
    _init_worker()
    t0 = time.time()
    raw = _run_ocr(frame)
    print(f"ocr: {len(raw)} boxes in {time.time()-t0:.1f}s ({_engine_kind})")
    boxes = [{"box": b, "text": t, "score": round(s, 3)} for b, t, s in raw]
    is_map = any(any(s in norm(t).casefold() for s in MAP_SIGS) for _, t, _ in raw)
    pm = panel_mask(frame)
    rects = boxes_to_rects(boxes, pm, is_map, w_img, h_img)

    before = frame.copy()
    vis = frame.copy()
    tinted = vis.copy()
    tinted[pm > 0] = (255, 200, 120)
    vis = cv2.addWeighted(tinted, 0.28, vis, 0.72, 0)
    rx0, ry0, rx1, ry1 = [int(v * (w_img if i % 2 == 0 else h_img)) for i, v in enumerate(REGION)]
    cv2.rectangle(vis, (rx0, ry0), (rx1, ry1), (255, 0, 255), 2)  # 品红=处理区域边界
    n_full = n_sub = n_keep = 0
    for b in boxes:
        x0, y0, x1, y1 = box_aabb(b["box"])
        if not in_region(b["box"], w_img, h_img):
            color = (160, 160, 160)
        else:
            ch = char_height(b["box"])
            cx, cy = int((x0 + x1) / 2), int((y0 + y1) / 2)
            in_panel = bool(pm[cy, cx])
            d = decide(b["text"], b["score"], ch, is_map, in_panel)
            if d is None:
                color, n_keep = (0, 170, 0), n_keep + 1
            elif d[0] == "full":
                color, n_full = (0, 0, 255), n_full + 1
            else:
                color, n_sub = (0, 140, 255), n_sub + 1
        cv2.rectangle(vis, (int(x0), int(y0)), (int(x1), int(y1)), color, 2)
    after = _inpaint(frame, rects)
    tag = src.stem
    for name, img in (("before", before), ("boxes", vis), ("after", after)):
        out = QA / f"test_{tag}_{name}.jpg"
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if ok:
            buf.tofile(str(out))
            print(f"  -> {out}")
    print(f"map={is_map} full={n_full} sub={n_sub} keep={n_keep} rects={len(rects)}")


# ---------------- 质检工具 ----------------
def cmd_sheet(args):
    QA.mkdir(exist_ok=True)
    idxs = [int(a) for a in args]
    scale = 0.5
    for k in range(0, len(idxs), 4):
        group = idxs[k:k + 4]
        tiles = []
        for f in group:
            frame = read_frame(FRAMES / f"{f:06d}.jpg")
            if frame is None:
                continue
            small = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            cv2.putText(small, str(f), (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 3)
            tiles.append(small)
        if not tiles:
            continue
        while len(tiles) < 4:
            tiles.append(np.full_like(tiles[0], 30))
        sheet = np.vstack([np.hstack(tiles[:2]), np.hstack(tiles[2:])])
        out = QA / f"sheet_{group[0]:06d}.jpg"
        ok, buf = cv2.imencode(".jpg", sheet, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if ok:
            buf.tofile(str(out))
            print(f"sheet -> {out}")


def cmd_tile(args):
    QA.mkdir(exist_ok=True)
    f = int(args[0])
    frame = read_frame(FRAMES / f"{f:06d}.jpg")
    if frame is None:
        print("no such frame")
        return
    h, w = frame.shape[:2]
    cols, rows = 3, 2
    for r in range(rows):
        for c in range(cols):
            y0, y1 = h * r // rows, h * (r + 1) // rows
            x0, x1 = w * c // cols, w * (c + 1) // cols
            out = QA / f"tile_{f:06d}_{r * cols + c}.jpg"
            ok, buf = cv2.imencode(".jpg", frame[y0:y1, x0:x1], [cv2.IMWRITE_JPEG_QUALITY, 90])
            if ok:
                buf.tofile(str(out))
    print(f"6 tiles -> {QA}/tile_{f:06d}_*.jpg")


def cmd_preview(args):
    """把 OCR 框+判定画到帧上:红=整擦 橙=擦子串 绿=保留 蓝=面板 品红框=处理区域。"""
    QA.mkdir(exist_ok=True)
    all_boxes = load_boxes()
    is_map, _ = classify(all_boxes)
    for a in args:
        f = int(a)
        if not all_boxes.get(f):
            print(f"frame {f}: 还没 OCR 到,跳过")
            continue
        frame = read_frame(FRAMES / f"{f:06d}.jpg")
        if frame is None:
            continue
        h_img, w_img = frame.shape[:2]
        pm = panel_mask(frame)
        vis = frame.copy()
        tinted = vis.copy()
        tinted[pm > 0] = (255, 200, 120)
        vis = cv2.addWeighted(tinted, 0.28, vis, 0.72, 0)
        rx0, ry0, rx1, ry1 = [int(v * (w_img if i % 2 == 0 else h_img)) for i, v in enumerate(REGION)]
        cv2.rectangle(vis, (rx0, ry0), (rx1, ry1), (255, 0, 255), 2)
        n_full = n_sub = n_keep = 0
        for b in all_boxes[f]:
            x0, y0, x1, y1 = box_aabb(b["box"])
            if not in_region(b["box"], w_img, h_img):
                color = (160, 160, 160)
            else:
                ch = char_height(b["box"])
                cx, cy = int((x0 + x1) / 2), int((y0 + y1) / 2)
                in_panel = bool(pm[cy, cx])
                d = decide(b["text"], b["score"], ch, is_map.get(f, False), in_panel)
                if d is None:
                    color, n_keep = (0, 170, 0), n_keep + 1
                elif d[0] == "full":
                    color, n_full = (0, 0, 255), n_full + 1
                else:
                    color, n_sub = (0, 140, 255), n_sub + 1
            cv2.rectangle(vis, (int(x0), int(y0)), (int(x1), int(y1)), color, 2)
        cv2.putText(vis, f"f{f} map={is_map.get(f, False)} full={n_full} sub={n_sub} keep={n_keep}",
                    (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 0, 255), 2)
        out = QA / f"preview_{f:06d}.jpg"
        ok, buf = cv2.imencode(".jpg", vis, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if ok:
            buf.tofile(str(out))
            print(f"preview -> {out}  (full={n_full} sub={n_sub} keep={n_keep})")


def cmd_report():
    all_boxes = load_boxes()
    is_map, has_card = classify(all_boxes)
    segs = []
    for f in sorted(is_map):
        if not is_map[f]:
            continue
        if segs and f - segs[-1][1] <= 30:
            segs[-1][1] = f
        else:
            segs.append([f, f])
    fps = 30.0
    print("map segments (frame / seconds):")
    for s, e in segs:
        print(f"  {s:5d}-{e:5d}  {s/fps:7.1f}s - {e/fps:7.1f}s")
    cards = sorted(k for k, v in has_card.items() if v)
    print(f"bing card frames: {cards[:30]}{' ...' if len(cards) > 30 else ''}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    os.environ.setdefault("OMP_NUM_THREADS", "3")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "3")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "ocr":
        cmd_ocr()
    elif cmd == "apply":
        cmd_apply()
    elif cmd == "sweep":
        cmd_sweep()
    elif cmd == "test":
        cmd_test(sys.argv[2:])
    elif cmd == "sheet":
        cmd_sheet(sys.argv[2:])
    elif cmd == "tile":
        cmd_tile(sys.argv[2:])
    elif cmd == "preview":
        cmd_preview(sys.argv[2:])
    elif cmd == "report":
        cmd_report()
    else:
        print(__doc__)
