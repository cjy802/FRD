# 公开数据基线复现记录（2026-10-14）

本项目有两个独立问题：**河湖／洪水可见水面分割**和**地面积水与湿表面区分**。这次下载两套公开 RGB 数据，运行两个不同级别的基线。结果来自本机的真实数据运行；它们不等于原论文网络结构或原论文数字的精确复现，也不构成热成像融合精度证明。

## 数据放置位置与运行命令

在 `D:\低空经济\model` 执行：

```powershell
python -m pip install -r requirements.txt
python -m waterwatch.download_floodwater
python -m waterwatch.reproduce_floodwater
python -m waterwatch.download_wet_water
python -m waterwatch.reproduce_wet_water_boxes
python -m pytest -q
```

| 数据 | 本地压缩包（直接从 ZIP 读取，无需解压） | 输出 |
| --- | --- | --- |
| [Floodwater 作者数据](https://github.com/decide-ugent/floodwater-dataset) | `data/external/floodwater_manual.zip` | `artifacts/floodwater_shoreline_rgb.joblib`、`output/reproduction_floodwater/report.json`、`output/reproduction_floodwater/examples/` |
| [Mendeley 水面／湿表面数据](https://data.mendeley.com/datasets/y6zyrnxbfm/4) | `data/external/stagnant_water_wet_surface_34mb.zip` | `output/reproduction_wet_water_boxes/model.joblib`、`output/reproduction_wet_water_boxes/report.json` |

`data/`、`artifacts/`、`output/` 均被 `.gitignore` 排除。**原始数据、模型权重和生成的图像／报告不提交到 Git**。若路径改动，两个复现命令都支持 `--archive` 与 `--output`；Floodwater 另支持 `--model`。完整参数可运行 `python -m waterwatch.reproduce_floodwater --help` 或 `python -m waterwatch.reproduce_wet_water_boxes --help` 查看。

## 1. 无人机洪水可见水面分割：Floodwater

[作者仓库](https://github.com/decide-ugent/floodwater-dataset)给出 700 对单独打包的人工标注 RGB 图和二值水面掩膜，作者声明 GPL-3.0。下载包大小 132,896,325 字节；本次取得的 SHA-256 为 `67cd987be5c2172dea4134c93b9c8bc4f851be239baa2bd6391ddaaac28267ff`，ZIP 全部条目通过 CRC 校验。脚本逐对检查 700 张图与掩膜的尺寸和标注值；466 对为 1280×720，234 对为 1920×1080。源标注 `0=非水、255=水`，导入项目时转换为 `0=非水、1=水`，因为项目的 `255` 表示忽略。

使用现有 RGB／HSV／纹理／亮度反射线索特征和 48 棵树的随机森林；最长边缩至 320 像素。按文件名连续分块：前 500 张训练，中间 100 张验证，最后 100 张测试；训练图每类抽取最多 120 个像素。训练与留出集没有字节完全相同的 RGB 文件。未获人工集的来源视频 ID，**不能保证相似帧、相同地点或相同日期不跨集**；原作者把这 700 对图作为单独人工评估集，我们在此将它重新划分用于探索性基线，不能与论文官方实验直接比较。

| 留出集 | 水面 IoU | 水面精确率 | 水面召回率 | 水面 F1 | 平均类别 IoU |
| --- | ---: | ---: | ---: | ---: | ---: |
| 验证 100 张 | 0.7297 | 0.8017 | 0.8904 | 0.8437 | 0.7834 |
| 测试 100 张 | **0.7592** | **0.8444** | **0.8827** | **0.8631** | **0.8066** |

测试混淆矩阵按行是真值、按列是预测，顺序为 `[非水, 水]`：`[[3368299, 334536], [241378, 1815787]]`。共评估 5,760,000 个**缩放后**像素；测试图逐张水面 IoU 中位数 0.7483，最低 0.2851。打开 `output/reproduction_floodwater/examples/sample_000651/` 可看到典型误检：部分树冠、远处地物被误分为水。输出模型符合本项目 `waterwatch.learning.load_model` 的 `shoreline/rgb` 格式，但这些洪水影像上的分数不能外推为本地河湖巡检精度。

原论文 [Efficient On-Board Processing of Oblique UAV Video for Rapid Flood Extent Mapping](https://doi.org/10.1109/JSTARS.2026.3729310) 的重点包括连续视频的 Temporal Token Reuse；本脚本只验证公开人工标注集上的静态 RGB 分割基线，没有实现该视频方法。

## 2. 地面积水／湿表面可区分性：Mendeley

[Mendeley v4 页面](https://data.mendeley.com/datasets/y6zyrnxbfm/4)描述 1,976 张清理后图像；我们下载的 34.9 MiB **小包是较早的 2,000 张版本**，不是页面所述最终 1,976 张归档。它是手机拍摄的 256×256 RGB 图片，标注格式是 YOLO **矩形框**，`0=water`、`1=wet surface`；没有像素掩膜或热图。页面声明 CC BY 4.0。该小包 36,555,626 字节，SHA-256 `84e08c2c2dea792b7b06b431fb898a0392127873b67b5a5d940654c6a84ba117` 与发布元数据相符，ZIP CRC 检验通过。

脚本检查所有图像、框坐标和类别；31,364 个原始框中丢弃 3 个零面积框。还发现 166 组 SHA-256 完全重复图像，**所有这些重复组的框标注都不一致**，其中 23 组连出现的类别也不一致。为避免完全重复图跨训练和测试，脚本每组只保留文件名字典序最前的一张及其标签，得到 1,831 张有有效框的图；按整图分成训练 1,281、验证 275、测试 275 张。这个确定性保留规则仅用于让实验可重复，不能修复标注矛盾。

模型对**人工标注给出的框位置**截取图像块，再使用 RGB／HSV／纹理特征和随机森林区分水面与湿表面。测试集共 4,447 个框，混淆矩阵按 `[water, wet_surface]` 排列为 `[[2536, 228], [287, 1396]]`；macro-F1 为 **0.8760**，水面 F1 为 0.9078，湿表面 F1 为 0.8443；验证 macro-F1 为 0.8651。数据缺少拍摄地点、雨次和场景 ID，近重复图仍可能跨集。因此这个分数仅代表**给定真实框后的两类裁剪分类**，没有验证自动发现积水位置、像素级边界或无人机视角性能。

## 热成像与论文精确复现的边界

- 两套**已下载**的数据都没有成对热红外图。它们不能用于计算 RGB 与热像融合的增益，也不能直接训练项目定义的 `dry / wet_without_standing_water / suspected_ponding` 三类逐像素模型。
- [Caltech Aerial RGB-Thermal Dataset](https://data.caltech.edu/records/cks6g-ps927)公开同步无人机 RGB、热像与水体等语义标注；成对标注包约 **4.3 GB**。本机对该文件的试传速度约 **0.59 MiB/s**，本次未下载该大包，也没有报告任何热像或融合指标。它主要覆盖自然水面，不能代替雨后小积水数据。若后续使用，按[作者类别定义](https://github.com/aerorobotics/caltech-aerial-rgbt-dataset)可将 `9=water` 转为本项目水类，并分别在同一批成对样本上比较 RGB、热像、融合；16 位热图不能直接解释为摄氏温度。
- [GAM4water](https://github.com/monviso/GAM4water)公开 R 方法，但本次未取得其论文原始 RGB／热像影像及水陆标注，因此没有声称复现其论文表格数值。

要验证本项目最终的雨后地面积水方案，仍需自己拍摄**同场景配准的 RGB＋二维热图**，标注干地、湿而无明水、可见积水三类，并按地点／雨次留出测试集。河湖防汛还需要同一岸段前后两期配准影像，才能验证水面变化，而不能仅靠单张图推断真实水位或堤防风险。
