# 数据放置与标注说明

数据放在 **`D:\低空经济\model\data\`**。下列目录由使用者自行创建；它们和所有模型权重、推理结果都不会进入 Git。

```text
model/
  data/
    manifest.csv
    sites.csv                                # 可选：人工登记巡查点
    raw/
      ponding/p001/rgb.jpg
      ponding/p001/thermal.tif              # 可选：单通道二维热图
      ponding/p001/thermal_to_rgb.json      # 热图未预先对齐时填写
      ponding/p001/roi.png                  # 推理区域，非零=可检查
      ponding/p001/metadata.json
      shoreline/s001_before/rgb.jpg
      shoreline/s001_before/metadata.json
      shoreline/s001_after/rgb.jpg
      shoreline/s001_after/after_to_before.json
      shoreline/s001_after/metadata.json
    labels/
      p001.png
      s001_before.png
      s001_after.png
    context/
      event.json                             # 可选：官方预警／水情来源
  artifacts/                                # 训练生成；勿提交
  output/                                   # 报告和掩膜；勿提交
```

## 1. 图片、热图和人工真值

RGB 使用 JPG／PNG。热图使用**单通道二维**的 TIFF／PNG 或 NumPy `.npy` 数组。推荐保留原始 16 位辐射数据及设备参数；普通伪彩色截图不能当成可靠的绝对温度。程序只从二维热图提取**场景内相对热差**，不声称温度已标定。单点红外测温器只输出一个数值，不能作为热图输入。

独立热像和 RGB 必须拍到同一场景、记录各自时间并做像素配准。只有当两张图已经在相同像素坐标系下时，才可在 manifest 的 `thermal_registration` 列写 `aligned`。即使尺寸相同，**不同设备也不自动视为已对齐**。

推理另需同尺寸单通道 `roi.png`：非零表示允许检查的地面／水岸，0 表示天空、车辆、建筑等不应硬分类的区域。训练标注中的 `255` 只对训练／评估生效，不会自动生成推理 ROI。

人工标注图是与 RGB **等宽高的单通道 PNG**，像素类别值如下；用 CVAT、Labelme 或任意图像工具绘制后导出这种索引掩膜即可。不要用伪彩色类别图代替数值标签。

若使用 Labelme，可用 `python -m waterwatch mask-from-labelme` 转换其 JSON 多边形标注。`ponding` 支持标签名 `dry`、`wet`、`ponding`、`ignore`；`shoreline` 支持 `land`、`water`、`ignore`；推理 ROI 使用独立的 `roi` 任务，其多边形标签为 `include`、`exclude`。未标注的训练区域默认为 `255` 忽略，ROI 未标注区域默认为 0 不检查。转换命令会核对标注与 RGB 宽高。

| 任务 | `0` | `1` | `2` | `255` |
| --- | --- | --- | --- | --- |
| `ponding` | 干燥／无水地面 | 潮湿但无站立水 | 可见积水 | 忽略区域 |
| `shoreline` | 陆地／非水域 | 可见水面 | 不使用 | 忽略区域 |

积水样本必须包含**干地、湿而无积水、水坑**三类；额外保留阴影、强反光、不同路面材质和刚停雨的负样本。河湖前后图片应记录相同 `site_id`，分别标注水陆边界。`255` 可用于天空、车辆、严重遮挡等不可靠区域。

## 2. manifest.csv

复制 [examples/manifest.template.csv](examples/manifest.template.csv) 为 `data/manifest.csv`，逐行填写。所有相对路径都以 **`data/manifest.csv` 所在目录**为基准：

| 列 | 含义 |
| --- | --- |
| `case_id` | 唯一影像案例编号。 |
| `site_id` | 同一实地点的编号；河湖前后图相同。 |
| `task` | `ponding` 或 `shoreline`。 |
| `split` | `train`、`val` 或 `test`；按地点或拍摄日期划分，不把同一视频相邻帧分散到训练和测试。 |
| `captured_at` | RGB 拍摄 ISO 8601 时间，必须带时区，如 `2026-10-14T15:30:00+08:00`。 |
| `rgb` | RGB 文件路径。 |
| `thermal` | 可留空；单通道热图路径。 |
| `thermal_registration` | 热图到 RGB 的控制点 JSON 路径；已预对齐时填 `aligned`；无热图时留空。 |
| `mask` | 人工标签 PNG 路径。 |

训练仅读取 `train` 行，评估必须指定 `val` 或 `test`。比较 RGB／热像／融合模式时，RGB 训练加 `--paired-support`，三模式使用**同一批已配准热图案例及其共同覆盖像素**，再核对评估报告的 `case_id` 和 `evaluated_pixels`。同一地点同一天不得同时进入训练与测试；同地点不同日期也会在报告中提示潜在相关性。

## 3. 配准控制点

热图到 RGB、雨后岸线图到雨前岸线图都使用同一种 JSON 格式，见 [examples/registration.template.json](examples/registration.template.json)。`source_points` 写**待变换图**中的像素坐标，`target_points` 写**目标图**中对应的像素坐标；至少 4 对、分布在图像不同位置。程序用单应矩阵变换二维近似平面，不适合把不同视角的立体建筑或高差很大的堤坡强行逐像素重叠。

河湖岸线变化时，如果没有固定参照点或可接受的配准，先并排展示两次影像，不输出“新增淹没像素”。像素数量也不是平方米；计算实际面积还需要地面尺度或正射测绘标定。

## 4. 每次采集的 metadata.json

复制 [examples/metadata.template.json](examples/metadata.template.json)。至少填写 `case_id`、`site_id`、带时区的 `captured_at`、`scene_type`、`rgb_device`。有热像时填写 `thermal_device`、`thermal_captured_at` 和 `thermal_format`（`radiometric` 或 `grayscale_intensity`）；经纬度、视角、飞行高度可在知道时补充。预测报告会保留这些信息，方便人工追溯。

## 5. 气象预警与水情资料

复制 [examples/event.template.json](examples/event.template.json) 到 `data/context/event.json`，**人工核对并填写具体那一次公告的原始网页** `source_url`、`issued_at`、`region` 和摘要。可从[中央气象台预警页面](https://www.nmc.cn/f/alarm.html)或[江苏省水利厅每日水情](https://jswater.jiangsu.gov.cn/col/col55237/index.html)查找。列表页不是具体事件的出处；网页也不是本项目已接入的稳定实时 API，代表站水位不能当成某一小河岸段的实测水位。

历史官方案例与后来自拍的影像必须保留各自日期。`event.json` 只是来源明确的**巡查背景**，程序不会从“下雨”自动推断某个点已经积水或发生堤防渗漏。

若使用 `plan` 命令，复制 [examples/sites.template.csv](examples/sites.template.csv) 为 `data/sites.csv`，填写待巡查点。`region` 必须与 `event.json` 中人工核对后的 `region` **完全一致**；这种字符串匹配仅用于生成候选清单，不代表预警精确覆盖到点位。站点经纬度可留空，知道时再填入。
