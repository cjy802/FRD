# 百度地图 JSAPI Three 接入准备

当前航线工作台仍使用 Cesium 和 OSM 底图。百度地图 AK 从 `frontend/.env.local`
的 `VITE_BAIDU_MAP_AK=` 读取；修改后需重启 Vite。空白模板见
`frontend/.env.example`。访问
`http://127.0.0.1:5173/baidu-preview.html` 可核对 JSAPI Three 地图。
预览页和航线工作台相互独立，填入 AK 不会自动替换航线工作台中的地图。

依赖已经安装：`@baidumap/mapv-three@1.7.2` 与 `three@0.179.1`。当前项目中使用
`three@0.186.0` 时，浏览器虽然能渲染建筑，但地图文字和 POI 图标不显示；
改用 `three@0.179.1` 后已验证这些标注正常显示，因此这里固定 Three.js 版本。运行
`npm run dev` 或 `npm run build` 时，`scripts/copy-mapv-three-assets.mjs` 会将包的
`dist/assets` 自动复制到 `public/mapvthree/assets`。预览页在引擎加载前设置
`window.MAPV_BASE_URL = '/mapvthree/'`，并在有 AK 时调用 `Engine` 初始化百度矢量地图。
浏览器端 AK 会用于浏览器请求，请在百度控制台将应用类型和使用域名配置正确。

## 官方入门方法

1. 安装 `@baidumap/mapv-three` 和 `three`，从包的 `dist/assets` 复制静态资源，
   并在加载引擎前设置 `window.MAPV_BASE_URL` 指向该资源目录；当前项目已完成这一步。
2. 在创建引擎前设置 `mapvthree.BaiduMapConfig.ak`。使用百度矢量底图时，AK
   应是百度地图开放平台创建的“浏览器端”应用密钥。
3. 使用 `new mapvthree.Engine(container, { map: { provider, center, pitch,
   heading, range } })` 初始化地图；可用 `GeoJSONDataSource` 添加点线面数据。
   预览页已有这段初始化代码，并在页面卸载时调用 `engine.dispose()`。

这些步骤来自[安装与配置](https://lbs.baidu.com/jsapithree/tutorial/usage/Install)和
[Hello World](https://lbs.baidu.com/jsapithree/tutorial/usage/HelloWorld)。

## 接入当前路线页面前需要核对

- 当前路线接口返回 OSM 的 WGS84 GeoJSON。百度底图使用 BD-09 数据，叠加路线、
  点击取点与保存 A/B 坐标时都要使用一致的坐标处理方式，并用实际点位核对偏移。
- JSAPI Three 是一套新的渲染引擎，不能通过替换 Cesium 脚本直接接管现有的
  `Viewer`、实体和地图点击代码。保留后端路线接口，另做地图适配层更合适。
- 百度底图 AK 只负责地图数据访问；本地 PBF 路线与在线 OSRM 路线是独立的数据源。
- 当前页面适配手机，启用 JSAPI Three 前需要验证移动设备性能。

坐标系说明见百度地图的[坐标与投影文档](https://lbs.baidu.com/docs/jsapi?title=jsapi4%2Fguide%2Fconcept%2Fprojection)。