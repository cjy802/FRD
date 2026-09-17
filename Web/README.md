# 低空飞行决策工作台 · 第一版

本项目包含 Vue 3 + Vite + TypeScript 页面、Cesium 地图和 FastAPI 接口。页面将“道路参考路线”和“天气风险”分开：后端优先用江苏 OSM PBF 建成的本地道路索引，按 A/B 周边区域提取道路与建筑，计算演示路线。若未建立索引，才使用原有的小范围 `campus.osm` 快照。天气、空域、道路上方障碍和实际放飞条件仍未评估。

## 启动

后端（Python 3.10+；首次使用 PBF 先建立索引）：

```powershell
cd backend
python -m pip install -r requirements.txt
python scripts/index_pbf.py --source ..\..\jiangsu-260916.osm.pbf
python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

前端（Node 20.19+）：

```powershell
cd frontend
npm install
npm run dev
```

百度地图 JSAPI Three 的 AK 在 `frontend/.env.local` 配置，空白模板见
`frontend/.env.example`。依赖和静态资源复制已配置；配置或修改 AK 后重启前端，
访问 `http://127.0.0.1:5173/baidu-preview.html`。
当前地图接入评估与后续实现步骤见 [BAIDU_JSAPI_THREE.md](BAIDU_JSAPI_THREE.md)。

访问 `http://127.0.0.1:5173`。地图左上方可切换“本地 / 在线”道路参考路线；起点和终点旁的按钮可分别显示或隐藏淡色的 35 米接路搜索范围，开关只改变显示。点击地图选点时，先用本地 OSM 数据检查已收录建筑及范围内道路：建筑点不采纳，离路太远的点立即提示。无建筑记录仅表示可进入下一步核验，**不能据此认定可以降落**。计算期间地图显示毛玻璃加载层，点击叉号会中止浏览器请求并忽略迟到的结果。本地模式使用已建立的 PBF 索引，不在规划时联网；在线模式调用 OSRM 机动车道路路线服务，网络不可用时返回明确错误，不自动改成本地结果。在线路线按机动车行驶时间选择道路，且不检查建筑缓冲，因此两种模式的结果和约束并不相同。在线服务地址可通过 `ONLINE_ROUTER_URL` 改成自己的 OSRM 服务；默认公共演示服务器只适合原型测试，使用时遵守 [OSRM 演示服务政策](https://github.com/Project-OSRM/osrm-backend/wiki/Api-usage-policy)。更换 PBF 后先停止后端，再重新运行索引脚本（Windows 正在使用的数据库文件可能无法替换）。Cesium 脚本和 OpenStreetMap 显示底图仍需网络。`GET /api/health` 的 `road_data` 字段可确认当前本地模式使用 `jiangsu_pbf_index` 还是 `campus_osm`。

## 接口

- `GET /api/health`：服务状态
- `GET /api/weather/status`：气象文件接入状态
- `POST /api/routes/plan`：请求体的 `road_source` 为 `local`（默认）或 `online`。本地模式从索引按 A/B 范围加缓冲提取路网；在线模式向 OSRM 请求完整道路几何。两种模式均输出 GeoJSON 路线、道路名称、道路/直线距离、端点接入距离和未评估标记。端点离所选道路网络超过 35 米时返回 422；在线服务不可用时返回 503。
- `POST /api/landing/check`：用本地索引对候选点做快速初筛，返回 `mapped_building`、`road_nearby` 或 `no_road`，以及 35 米接路半径和最近道路点。`landing_verified` 始终为 `false`。
- `POST /api/routes/preview`：保留原有的两点直线基线接口

可用环境变量 `WEATHER_GRID_PATH` 指向下载好的天气文件。当前只检测文件存在，不读取或推断其数据含义。此前 `app/IMPLEMENTATION_PLAN.md` 描述的是单点 FuXi 预报订正流程；航线风险需要沿线多点、分高度、分时刻数据，不能直接用一个点的结果替代。

默认示例点位位于常州信息职业技术大学：A 约为 `31.67920°N, 119.95150°E`，B 约为 `31.68040°N, 119.94950°E`。本地规划会在 A/B 各自 35 米范围内寻找可连接的道路，并在可达候选中选择总路线；超过半径或接入线穿过已收录建筑缓冲区则报错。当前原型限制 A/B 直线距离不超过 10 公里。道路路线只按二维地图数据计算，不能代表可飞行航线。

江苏 PBF 建成的索引为 `backend/data/jiangsu_roads.sqlite`；原校园快照见 `backend/data/campus.osm`。本地规划不在运行时请求在线道路数据，在线模式则使用 OSRM。PBF 中缺失的弯路仍需实地核对并补入路网；道路/建筑关系、桥梁、通行限制和坐标精度也需核验。`clearance_m` 默认 2 米只作原型可视化参数，不是实际飞行安全距离。道路上方的线缆、树木、建筑高度、动态障碍、禁飞区和天气都需要另行核实。

## 后续接入顺序

1. 明确试点城市、航路高度、飞行时间和禁飞区来源。
2. 将天气数据整理为经纬度 × 高度 × 时间的网格，记录来源和时效；先做可解释的规则评分。
3. 校验道路和建筑快照，补充禁飞区、线缆和树木；在已校验的风险场上比较道路路线与其他候选路线的时间和风险。
4. 有足够仿真和真实任务样本后，再评估 U-Net 降尺度及 PPO 是否带来稳定提升。

此版本使用最短路算法计算道路参考线，没有训练天气或视觉模型，也不提供飞行安全结论。
