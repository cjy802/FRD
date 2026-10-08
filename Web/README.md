# 低空飞行决策工作台 · 第一版

本项目包含 Vue 3 + Vite + TypeScript 页面、Cesium 地图和 FastAPI 接口。页面将“道路参考路线”和“天气风险”分开：后端优先用江苏 OSM PBF 建成的本地道路索引，按 A/B 周边区域提取道路与建筑，计算演示路线。若未建立索引，才使用原有的小范围 `campus.osm` 快照。区域地面天气已按航线坐标和沿线 ETA 对齐；飞行高度、机型天气限制、空域、道路上方障碍和实际放飞条件仍未评估。逐项实施记录见 [WEATHER_ROADMAP.md](WEATHER_ROADMAP.md)。

## 启动

后端（Python 3.10+；首次使用 PBF 先建立索引）：

```powershell
cd backend
python -m pip install -r requirements.txt
python scripts/index_pbf.py --source ..\..\jiangsu-260916.osm.pbf
python -m uvicorn main:app --reload --host 127.0.0.1 --port 8011
```

前端（Node 20.19+）：

```powershell
cd frontend
npm install
npm run dev
```

前端默认将 `/api` 请求代理到 `http://127.0.0.1:8011`，与上面的后端端口一致。
使用其他端口时，在 `frontend/.env.local` 中设置
`VITE_API_TARGET=http://127.0.0.1:端口` 后重启前端；终端中设置的同名环境变量优先。
开发服务器和 `npm run preview` 使用相同的代理目标。

百度地图 JSAPI Three 的 AK 在 `frontend/.env.local` 配置，空白模板见
`frontend/.env.example`。依赖和静态资源复制已配置；配置或修改 AK 后重启前端，
访问 `http://127.0.0.1:5173/baidu-preview.html`。
当前地图接入评估与后续实现步骤见 [BAIDU_JSAPI_THREE.md](BAIDU_JSAPI_THREE.md)。

访问 `http://127.0.0.1:5173`。地图左上方可切换“本地 / 在线”道路参考路线；起点和终点旁的按钮可分别显示或隐藏淡色的 35 米接路搜索范围，开关只改变显示。点击地图选点时，先用本地 OSM 数据检查已收录建筑及范围内道路：建筑点不采纳，离路太远的点立即提示。无建筑记录仅表示可进入下一步核验，**不能据此认定可以降落**。计算期间地图显示毛玻璃加载层，点击叉号会中止浏览器请求并忽略迟到的结果。本地模式使用已建立的 PBF 索引，不在规划时联网；在线模式调用 OSRM 机动车道路路线服务，网络不可用时返回明确错误，不自动改成本地结果。在线路线按机动车行驶时间选择道路，且不检查建筑缓冲，因此两种模式的结果和约束并不相同。在线服务地址可通过 `ONLINE_ROUTER_URL` 改成自己的 OSRM 服务；默认公共演示服务器只适合原型测试，使用时遵守 [OSRM 演示服务政策](https://github.com/Project-OSRM/osrm-backend/wiki/Api-usage-policy)。更换 PBF 后先停止后端，再重新运行索引脚本（Windows 正在使用的数据库文件可能无法替换）。Cesium 脚本和 OpenStreetMap 显示底图仍需网络。`GET /api/health` 的 `road_data` 字段可确认当前本地模式使用 `jiangsu_pbf_index` 还是 `campus_osm`。

## 接口

- `GET /api/health`：服务状态
- `GET /api/weather/status`：历史单点天气和模型状态、可选预报时间、逐变量使用策略
- `GET /api/weather/forecast`：当前模型订正后的历史预报，含 FuXi 原值、最终使用值、来源和派生风速/风向。可用 `init_time_utc`（带时区）及 `lead_hours` 选择记录，默认最新可用记录；不存在的记录返回 422。
- `GET /api/weather/risk`：同一记录的单点地面风雨提示，不改变航线的未评估状态
- `POST /api/weather/predict`：页面进入时发起区域 FuXi 预测，保留起报场和 +6/12/18/24 小时时次，返回任务 ID 和状态；只共享正在运行的任务，不复用已完成的历史预报。
- `GET /api/weather/predictions/{job_id}`：查询预测进度、输入缺失/过期原因，或最终天气预报。
- `GET /api/weather/predictions/{job_id}/sample?latitude=...&longitude=...`：查询实际坐标的区域地面天气，默认 +6h；可用带时区的 `valid_time_utc` 指定覆盖范围内的时刻。范围外或过期时返回明确错误。
- `POST /api/weather/predictions/{job_id}/route`：请求体包含 `coordinates`（沿路 GeoJSON 坐标 `[经度, 纬度]`）、`departure_time_utc`（带时区）、`speed_kmh`、`altitude_m`。保留路线拐点，并将较长路段细分至不超过 1 km，按累计距离计算各点 ETA。返回沿线天气、覆盖不足位置及原因；高度仍待匹配，`flight_permission` 始终为空。
- `POST /api/routes/plan`：请求体的 `road_source` 为 `local`（默认）或 `online`。本地模式从索引按 A/B 范围加缓冲提取路网；在线模式向 OSRM 请求完整道路几何。两种模式均输出 GeoJSON 路线、道路名称、道路/直线距离、端点接入距离和未评估标记。端点离所选道路网络超过 35 米时返回 422；在线服务不可用时返回 503。
- `POST /api/landing/check`：用本地索引对候选点做快速初筛，返回 `mapped_building`、`road_nearby` 或 `no_road`，以及 35 米接路半径和最近道路点。`landing_verified` 始终为 `false`。
- `POST /api/routes/preview`：保留原有的两点直线基线接口

上线模型默认读取 `Web/mode/output_production/corrector_ridge.npz`，它由两份表去重后的全部可用样本拟合（目前 3893 条，包含 2024、2025、2026 年），使用已验证方案的公共 8 特征。温度、U 风、6 小时降水采用订正结果，V 风和海平面气压严格保留 FuXi 原值；派生风速、风向以及单点风雨提示均使用最终值。全量拟合不再留测试集，也不产生新的独立测试指标；此前 `output_year_transfer_2026` 验证结果保留不变。

页面的“天气数据”进入时自动发起预测并轮询进度，只显示最终温度、U/V 风、气压、降水、风速与风向，不显示原值/订正值对照或历史日期选择。起报、有效时刻和生成时刻都以北京时间显示。模型的 +6h 是从起报场时刻计算，不伪装为从页面访问时刻开始的逐小时预报。表格查询实际起终点中点，不代表整条航线；“气象风险”仅提示该位置的地面风雨，阈值不是机型飞行限值。当前订正模型只在 31.5°N、119.5°E 的地面变量及原生 lead 6/12/24 上训练：该位置的原生预报保留选择性订正，其他坐标、起报场、18h 和时间插值结果不擅自套用单点模型，并显示原因。

航线设置新增“出发时刻 · 北京时间”，默认页面进入后约 30 分钟，可自行调整。计算道路路线且天气预测完成后，“沿线时间匹配”自动查询沿线各点的经过时刻；修改出发时间或速度不会重新加载模型或下载起报场。修改起终点后，需要重新计算对应道路路线，旧路线不会继续用于新点位的天气对齐。瞬时地面变量在相邻 6 小时时次之间线性估计；降水取覆盖任务时刻的完整 6 小时累计区间，单独记录起止时刻，不插值成分钟雨强，也不累计重复的沿线雨量。

当前保留 116–122.5°E、30–35.5°N 的 70 通道区域网格，原生分辨率 0.25°。空间插值不能生成街道尺度信息，时间插值不是逐小时模型预报。每个任务的区域网格保存在后端内存，最多保留 8 个任务；后端重启后需重新预测，已有起报资料仍可使用下载缓存。缺失空间或时间覆盖时标记 `incomplete`，不外推、不替换为其他点位或日期。飞行高度尚未匹配，所以当前仍不能据此决定能否飞行。

实时推理默认由程序自动获取 [NOAA 公开 GFS](https://www.nco.ncep.noaa.gov/pmb/products/gfs/nomads/) 的当前气象场，不要求用户提供起报文件。每次进入页面发起预测：自动获取两帧相隔 6 小时的当前资料 → 转成 FuXi 70 通道输入 → 同一 GPU 会话滚动推理 +6/12/18/24h → 保留区域网格并查询实际坐标 → 仅在订正适用点位/时次使用全量模型 → 显示最终值。FuXi 官方也提供了 [GFS 输入转换示例](https://github.com/tpys/FuXi/blob/main/make_gfs_input.py)。本项目保持真实的 6 小时累计降水，不使用示例中的零值占位；位势高度乘标准重力转为位势，温度/湿度/风/气压保留对应物理单位。

自动获取脚本为 `app/prepare_gfs_live.py`，通过 NOAA 公共对象存储的 GRIB 索引和 HTTP byte range 只下载所需字段（69 个状态字段约 54 MB/帧，另取少量真实降水字段），不下载完整全球 GRIB 文件。优先使用已发布的分析场；分析未发布时使用有效时刻相同的短时背景场，并记录每帧来源周期与提前量。相同起报时次使用缓存，较新的分析场发布后会升级背景场；自动缓存仅保留最近 48 小时。本项目的 ERA5 回测不能直接当作 GFS 起报后的实时精度验证。

首次运行在既有 `fuxi_onnx` 环境补充解码器依赖：

```powershell
& 'D:\anaconda\envs\fuxi_onnx\python.exe' -m pip install -r 'D:\低空经济\app\requirements-live.txt'
```

本机依赖已安装并通过实际 GPU 推理。`FUXI_LIVE_INPUT` 仅用于手动覆盖自动来源；通常不需要设置。可用 `FUXI_PYTHON` 设置 ONNX 环境 Python，`FUXI_MODEL_DIR` 设置权重目录，`WEATHER_CORRECTOR_PATH` 设置订正模型。自动生成的 `app/prepared/latest.nc` 通过本机已有英文目录链接访问，避开 Windows NetCDF 的中文路径限制。输入网络下载失败、已过期或模型不可用时返回明确原因，不使用历史 CSV 伪装未来天气。新页面进入会重新运行 FuXi；只有同时进行中的任务共享一次 GPU 推理。

旧的 `/status`、`/forecast`、`/risk` 接口仅供历史记录诊断，仍可通过 `WEATHER_PAIRS_PATH` 和 `WEATHER_ARCHIVE_YEAR` 配置；页面未来预报流程不调用它们。所有相对路径以项目根目录为基准，模型与数据不纳入 Git。全量重训命令：

```powershell
& 'D:\anaconda\envs\fuxi_onnx\python.exe' `
  'D:\低空经济\Web\mode\fit_production_corrector.py' --train
```

默认示例点位位于常州信息职业技术大学：A 约为 `31.67920°N, 119.95150°E`，B 约为 `31.68040°N, 119.94950°E`。本地规划会在 A/B 各自 35 米范围内寻找可连接的道路，并在可达候选中选择总路线；超过半径或接入线穿过已收录建筑缓冲区则报错。当前原型限制 A/B 直线距离不超过 10 公里。道路路线只按二维地图数据计算，不能代表可飞行航线。

江苏 PBF 建成的索引为 `backend/data/jiangsu_roads.sqlite`；原校园快照见 `backend/data/campus.osm`。本地规划不在运行时请求在线道路数据，在线模式则使用 OSRM。PBF 中缺失的弯路仍需实地核对并补入路网；道路/建筑关系、桥梁、通行限制和坐标精度也需核验。`clearance_m` 默认 2 米只作原型可视化参数，不是实际飞行安全距离。道路上方的线缆、树木、建筑高度、动态障碍、禁飞区和天气都需要另行核实。

## 后续接入顺序

1. 明确试点城市、航路高度、飞行时间和禁飞区来源。
2. 将天气数据整理为经纬度 × 高度 × 时间的网格，记录来源和时效；先做可解释的规则评分。
3. 校验道路和建筑快照，补充禁飞区、线缆和树木；在已校验的风险场上比较道路路线与其他候选路线的时间和风险。
4. 有足够仿真和真实任务样本后，再评估 U-Net 降尺度及 PPO 是否带来稳定提升。

此版本使用最短路算法计算道路参考线，并接入已有的单点天气订正模型；不提供飞行安全结论。
