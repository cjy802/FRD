# 雨天种子 → 4090 混训（mix5）操作包

目标：把 2026-10-24 的 9 张雨天种子（`data/rain_seed`，6 视频帧 + 3 照片，
视觉核验通过）并入冠军配方重训，替换/扩充原 3 张本地种子，得到雨域更强的新冠军。

## 1. 本地准备

```bash
cd model
python -m waterwatch.seed_rain_labels        # 重建 data/rain_seed（images/masks/checks）
# 人工再翻一遍 data/rain_seed/checks/ ——种子标注不是真值，训练前必须核验。
```

## 2. 打包上传（恒源云 4090，仓库/权重已按既有流程 scp 过）

```bash
tar czf rain_seed.tgz -C data rain_seed
scp rain_seed.tag root@<4090>:/root/model/data/
ssh root@<4090> "cd /root/model/data && tar xzf rain_seed.tgz"
```

## 3. 组 mix5 并训练（沿用 mix4 冠军配方：YOLOv8n-seg, 100ep, COCO 基座）

服务器上在 mix4 语料（Puddle 786 + FRED 812 + FloodNet 500）基础上混入
rain_seed ×30 过采样（与原本地种子同系数；如同时保留旧 3 张本地种子，
总量 2188 + 9×30 = 2458 对）：

```bash
# mix4 的构建脚本路径不变，把 rain_seed/images+masks 追加进同一 images/masks 目录，
# 过采样方式与 local_seed 相同（×30 + 水平翻转 ×2）。
python -m waterwatch.yolo_seg_baseline --dataset mix5 --model yolov8n-seg \
    --epochs 100 --imgsz 640 --workers 16 --cache ram
```

要点：`workers=16 + cache=ram`（REPRODUCTION.md §11e：dataloader 保守配置会把
4090 利用率压到 29%）；评测沿用四域 water IoU 同口径，另把 9 张雨种子的
留出副本（若拆 8 训 1 验不现实，则全部参训、以 checks 叠图做定性验收）。

## 4. 验收与回传

- 四域均分不低于现任冠军 0.7324 才换冠，否则雨域增益按挑战者记录归档。
- 新权重与评测 json 回传 `output/4090_champions/`（命名如
  `yoloj_mix5_rain_best.pt` + `report_yoloj_mix5_*.json`），并把
  `Web/backend/photo_analysis.py` 的 `DEFAULT_YOLO_WEIGHTS` 指向新权重。

## 后续扩充

`data/rain_raw/` 还有 22 张去重帧未标（concat_017-028、snow_002-013 等），
标注流程同 `seed_rain_labels.py`：视觉逐张画归一化多边形 → checks 核验 →
重建。30+ 张独立雨天标注可显著降低过采样记忆风险。
