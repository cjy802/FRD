# 积水风险智能识别与预警系统（AIC）

气象驱动的低空飞行任务工作台：道路参考航线规划 → FuXi/GFS 沿线天气初筛与动态决策 → DJI KMZ 导出 → 飞行后视频关键帧积水分析（结合拍摄高度换算真实面积、按面积分级警报）。

本仓库包含除大模型文件外的全部代码、文档、小体积权重与配置模板。在一台已有大模型的机器上克隆本仓库、放置模型、填好密钥后即可运行。**系统输出均为待人工核查线索，不提供放飞许可。**

## 仓库结构

| 目录 | 内容 | 详细文档 |
| --- | --- | --- |
| `Web/` | 主应用：Vue 3 + Vite + TypeScript 前端、FastAPI 后端（路线、天气、任务、视频/照片分析、DJI 导出） | [Web/README.md](Web/README.md) |
| `model/` | 水患图像分割原型（ponding / shoreline）：训练、评估、复现代码 | [model/README.md](model/README.md) |
| `app/` | FuXi/GFS 气象数据准备、订正与预测脚本（被 Web 后端以子进程调用） | — |
| `help/` | 历史数据回填辅助脚本 | — |
| `tools/` | 演示视频打码/脱敏工具（mask、redact 系列一次性脚本） | — |
| `docs/` | 竞赛材料：2026 AIC 智慧气象竞赛规则、作品方案模板、痛点分析 | — |

## 快速开始

后端（Python 3.10+，端口 8011）：

```powershell
cd Web/backend
python -m pip install -r requirements.txt
copy .env.example .env      # 填入下方"密钥配置"
python -m uvicorn main:app --reload --host 127.0.0.1 --port 8011
```

前端（Node 20.19+，端口 5173）：

```powershell
cd Web/frontend
npm install
copy .env.example .env.local   # 填 VITE_BAIDU_MAP_AK（百度地图 JSAPI Three 浏览器端 AK）
npm run dev
```

访问 `http://127.0.0.1:5173`。`/api` 默认代理到 `http://127.0.0.1:8011`，换端口改 `VITE_API_TARGET`。

## 模型文件清单

**已随仓库分发**（克隆即有）：

| 文件 | 大小 | 用途 |
| --- | --- | --- |
| `model/output/rain_ft/rain3_mix/weights/best.pt` | 6.8 MB | YOLOv8n-seg 雨天场景混训微调权重（照片分析默认模型） |
| `model/output/4090_champions/yoloj_mix4_best.pt` | 6.8 MB | YOLOv8n-seg 通用混训微调权重（备选） |
| `model/artifacts/onnx/registry.json` | <1 KB | ONNX 分割模型清单（模型名、water_class、输入尺寸） |
| `Web/mode/output_production/corrector_ridge.npz` | 4 KB | 天气上线订正模型（含 `training_metadata.json`） |

**需从原机器手动放置**（体积大，不入库；下表为完整目标路径）：

| 文件 / 目录 | 大小 | 放置路径 | 用途 |
| --- | --- | --- | --- |
| FuXi 气象模型资产 | ~60 GB | 仓库根 `FuXi/`、`FuXi_EC/` | 天气预测（`app/predict_fuxi_point.py` 等读取） |
| ABCDWaveNet 本地微调 | ~196 MB | `model/artifacts/onnx/abcdwavenet_local.onnx` **及其同名 `.onnx.data`** | ONNX 积水分割（registry 默认模型） |
| ABCDWaveNet 基础模型 | ~196 MB | `model/artifacts/onnx/abcdwavenet_ponding.onnx` 及 `.onnx.data` | Puddle-1000 基线 |
| CMNeXt RGBT（可选） | 235 MB | `model/artifacts/onnx/cmnext_b2_rgbt.onnx` | Caltech RGBT 复现，Web 端不需要 |
| shoreline 随机森林（可选） | 51 MB | `model/artifacts/floodwater_shoreline_rgb.joblib` | 河湖岸线任务，Web 端不需要 |

没有 ABCD ONNX 时照片分析仍可用（自动改用 YOLO 或远程模型）；没有 FuXi 资产时天气预测不可用，但路线规划、任务管理、照片分析可正常运行。

## 推理环境（conda）

三个独立解释器通过 `Web/backend/.env` 配置，换机器只需改路径：

- `WATER_ANALYSIS_PYTHON` → `waterseg` 环境：ONNX 分割（`onnxruntime`、`opencv`）
- `WATER_ANALYSIS_YOLO_PYTHON` → `yolov8-env` 环境：YOLO 与远程 GLM runner（`ultralytics`、`opencv`）
- `FUXI_PYTHON` → `fuxi_onnx` 环境：FuXi 预测（`app/` 内脚本）

各环境依赖以 `model/requirements.txt` 与 `app/requirements-live.txt` 为基础安装。

## 密钥配置（不要提交 `.env`）

`Web/backend/.env`（模板 `Web/backend/.env.example`）：

- `BAIDU_WEB_SERVICE_AK` — 百度服务端 AK（地点检索/地理编码）
- `WATER_ANALYSIS_REMOTE_URL` / `WATER_ANALYSIS_REMOTE_API_KEY` / `WATER_ANALYSIS_REMOTE_MODEL` — 远程视觉测试模型（BigModel）；Key 仅后端读取
- `WATER_ANALYSIS_*` / `FUXI_PYTHON` — 上面三个解释器与模型路径

`Web/frontend/.env.local`（模板 `Web/frontend/.env.example`）：`VITE_BAIDU_MAP_AK`（浏览器端地图 AK）、`VITE_API_TARGET`。

所有 `.env` 已被 `.gitignore` 排除；仓库中只保留 `.env.example` 空模板。

## 本地数据

- 道路索引：`Web/backend/data/jiangsu_roads.sqlite`（约 520 MB，不入库）。没有索引时自动退回自带校园快照 `campus.osm`；需要全省路网时从江苏 OSM PBF 自建：`python scripts/index_pbf.py --source ..\..\jiangsu-260916.osm.pbf`。
- 任务与媒体数据保存在 `Web/backend/user_data/`（不入库）。
- 天气订正训练说明见 `Web/mode/output_production/training_metadata.json` 与 [Web/WEATHER_ROADMAP.md](Web/WEATHER_ROADMAP.md)。

## 声明

系统提供的道路参考线、天气初筛、积水候选与面积估算均为辅助信息：面积基于“平坦地面 + 近垂直俯拍”假设由像素换算，不代表水深或淹没范围；不具备放飞许可、空域审批或飞行安全判断能力。使用前须现场核验。
