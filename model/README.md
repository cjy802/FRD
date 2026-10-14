# 气象驱动的低空水患巡查：图像原型

这是一个独立于 `Web/` 的 Python 原型。**一个项目、两个任务模型**：

| 任务 | 标签 | 结果 |
| --- | --- | --- |
| `ponding` 地面积水 | `0` 干地、`1` 湿而无站立水、`2` 可见积水；`255` 忽略 | 疑似积水掩膜与类别概率；重点检查湿路面误报。 |
| `shoreline` 河湖岸线 | `0` 陆地、`1` 可见水面；`255` 忽略 | 单时刻水面掩膜，以及配准后的新增受淹／水面退去像素。 |

两项任务共用图像读取、RGB／热图配准、特征提取、训练评估和证据记录，但**分别训练和评估**。普通照片中的反光、倒影属于 RGB 图像特征；“光折射”不作为独立传感器。二维热图是可选输入，没有热图也能先建立 RGB 基线。

本仓库**不附带训练好的识别模型、实测数据或检测精度声明**。程序输出的是待人工核查线索；河湖模块不从照片推算绝对水位、水深、流量，也不判断堤防内部是否安全。气象与公开水情是巡查背景，不是图像标签，也不是飞行许可。

## 安装

在 `D:\低空经济\model` 执行：

```powershell
python -m pip install -r requirements.txt
python -m pytest -q
```

数据目录、文件格式、人工标注、配准和公开预警如何记录，见 [DATA.md](DATA.md)。`data/`、`artifacts/`、`output/` 已加入本目录 `.gitignore`，不要把原始影像、下载资料或训练权重提交到 Git。

如需演示“气象触发巡查”，先人工核对官方预警适用区域并登记站点，再生成**区域级待巡查清单**：

```powershell
python -m waterwatch plan --event data/context/event.json --sites data/sites.csv --output output/inspection_plan.json
```

该命令只做人工填写的地区名称精确匹配，不推断某个点已受灾，也不自动起飞。

## 最小工作流

先按 [DATA.md](DATA.md) 建立 `data/manifest.csv` 和对应图片、掩膜。**至少为每个任务保留不同拍摄日或地点的测试场景**。下列命令只是用法示例；没有真实标注图时训练会明确报错。

如果在 Labelme 中画了多边形，可先转成训练掩膜或推理 ROI：

```powershell
python -m waterwatch mask-from-labelme --json data/raw/ponding/p001/labels.json --rgb data/raw/ponding/p001/rgb.jpg --task ponding --output data/labels/p001.png
python -m waterwatch mask-from-labelme --json data/raw/ponding/p001/roi.json --rgb data/raw/ponding/p001/rgb.jpg --task roi --output data/raw/ponding/p001/roi.png
```

```powershell
# 积水：分别训练 RGB、热像、融合三种模型，才可以比较热像增益。
python -m waterwatch train --manifest data/manifest.csv --task ponding --mode rgb --paired-support --output artifacts/ponding_rgb.joblib
python -m waterwatch train --manifest data/manifest.csv --task ponding --mode thermal --output artifacts/ponding_thermal.joblib
python -m waterwatch train --manifest data/manifest.csv --task ponding --mode fusion --output artifacts/ponding_fusion.joblib
python -m waterwatch evaluate --manifest data/manifest.csv --model artifacts/ponding_rgb.joblib --split test --output output/ponding_rgb_test.json
python -m waterwatch evaluate --manifest data/manifest.csv --model artifacts/ponding_thermal.joblib --split test --output output/ponding_thermal_test.json
python -m waterwatch evaluate --manifest data/manifest.csv --model artifacts/ponding_fusion.joblib --split test --output output/ponding_test.json

# 河湖水陆分割：独立模型、独立标注。
python -m waterwatch train --manifest data/manifest.csv --task shoreline --mode rgb --output artifacts/shoreline_rgb.joblib
python -m waterwatch evaluate --manifest data/manifest.csv --model artifacts/shoreline_rgb.joblib --split test --output output/shoreline_test.json
```

对新照片推理：

```powershell
python -m waterwatch predict --model artifacts/ponding_rgb.joblib --rgb data/raw/ponding/p001/rgb.jpg --roi data/raw/ponding/p001/roi.png --metadata data/raw/ponding/p001/metadata.json --output output/p001

# 有二维热图时，使用独立训练的 fusion 模型；热图必须已配准，或提供热图到 RGB 的控制点文件。
python -m waterwatch predict --model artifacts/ponding_fusion.joblib --rgb data/raw/ponding/p001/rgb.jpg --thermal data/raw/ponding/p001/thermal.tif --registration data/raw/ponding/p001/thermal_to_rgb.json --roi data/raw/ponding/p001/roi.png --metadata data/raw/ponding/p001/metadata.json --output output/p001_fusion
```

岸线变化需要**同一岸段、同一视角或明确的前后配准点**。可以先使用人工标注水面掩膜验证变化模块，再换成 `shoreline` 模型的预测掩膜：

```powershell
python -m waterwatch shoreline-change --before-rgb data/raw/shoreline/s001_before/rgb.jpg --before-mask data/labels/s001_before.png --before-mask-origin manual --before-metadata data/raw/shoreline/s001_before/metadata.json --after-rgb data/raw/shoreline/s001_after/rgb.jpg --after-mask data/labels/s001_after.png --after-mask-origin manual --after-metadata data/raw/shoreline/s001_after/metadata.json --registration data/raw/shoreline/s001_after/after_to_before.json --output output/s001_change
```

`--event data/context/event.json` 可附加到 `predict` 或 `shoreline-change`，把官方预警或水情的**来源、发布时间和区域**写进结果报告。程序不会把不同日期的官方案例和自拍影像自动认定为同一次事件。

对比三种模式时，RGB 模型必须加 `--paired-support`，这样训练和评估都限于**有热图覆盖的同一批像素**。三份评估报告还需核对相同的 `case_id`、`evaluated_pixels` 与测试地点；同一地点同一天的训练／测试会被拒绝。普通 RGB-only 使用不需这个开关。

`predict` 要求提供一张与 RGB 等宽高的单通道 ROI 图，非零像素是允许检查的地面／水岸区域。天空、车、人、建筑等区域设为 0，避免把整幅图硬分类成水或陆地。输出报告记录输入文件的 SHA-256，便于复核；模型文件只加载自己训练或信任的版本。

## 河湖防汛的后续扩展

第一版先做可验证的**水陆边界变化**。桥涵漂浮物、行洪阻塞需要单独标注对象；堤防背水坡疑似渗漏需要真实或受控模拟的正负样本，不能把一般热异常直接命名为渗漏。二者在有数据后可另建任务模型，与 `ponding`、`shoreline` 共用本项目的读取和评估代码。

## 科学与需求依据

- [江苏省水利厅 2026 年科研需求](https://jswater.jiangsu.gov.cn/art/2026/2/5/art_42984_11726109.html)：第 50 项提出无人机光学＋热像堤防渗流巡检；第 53 项提出城市积水应急监测。属于需求清单，不是本原型已验证的性能。
- [河海大学堤坝双影像巡测论文](https://jour.hhu.edu.cn/hhdxxbzr/article/abstract/20230320?st=aipub)：双影像有工程应用，但受地面与温差条件影响。
- [GAM4water 论文](https://www.sciencedirect.com/science/article/pii/S2215016124004060)：无人机 RGB＋热图可用于河段水面识别；其精度不适用于推断本项目的雨后小水坑性能。
- [卡内基梅隆大学路面积水实验](https://mrsdprojects.ri.cmu.edu/2020teamb/implementation/perception-subsystem/)：积水和路面热平衡时，热图可能没有增益。因此本原型要求 RGB、热像、融合三组实测对照。
