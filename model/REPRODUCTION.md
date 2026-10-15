# 公开数据基线复现记录（2026-10-14 至 16）

本项目有两个独立问题：**河湖／洪水可见水面分割**和**地面积水与湿表面区分**。已在本机真实数据上完成四轮实验：三套公开数据随机森林基线（Floodwater、Mendeley、Caltech），以及两篇论文的**深度网络复现**——CMNeXt（ECCV 2024 Caltech 数据集论文的最优 RGB-T 模型）与 ABCDWaveNet（雾天道路积水分割）。随机森林实验不是原论文网络的复现；深度网络实验用的也是官方代码库（固定 commit + 兼容补丁），但受单卡 8GB 显存限制，超参低于论文配置，数字不能与论文表格直接等同。

## 数据放置位置与运行命令

在 `D:\低空经济\model` 执行：

```powershell
python -m pip install -r requirements.txt
python -m waterwatch.download_floodwater
python -m waterwatch.reproduce_floodwater
python -m waterwatch.download_wet_water
python -m waterwatch.reproduce_wet_water_boxes
python -m waterwatch.download_caltech --output data/external/labeled_rgbt_pairs.zip --workers 16
python -m waterwatch.reproduce_caltech_rgbt --split-strategy official --train-count 0 --val-count 0 --test-count 0 --pixels-per-class 60 --trees 32 --jobs 2 --max-edge 256 --output output/reproduction_caltech_rgbt
python -m waterwatch.reproduce_caltech_rgbt --split-strategy trajectory --train-count 0 --val-count 0 --test-count 0 --pixels-per-class 60 --trees 32 --jobs 2 --max-edge 256 --output output/reproduction_caltech_trajectory
python -m pytest -q
```

| 数据 | 本地压缩包（直接从 ZIP 读取，无需解压） | 输出 |
| --- | --- | --- |
| [Floodwater 作者数据](https://github.com/decide-ugent/floodwater-dataset) | `data/external/floodwater_manual.zip` | `artifacts/floodwater_shoreline_rgb.joblib`、`output/reproduction_floodwater/report.json`、`output/reproduction_floodwater/examples/` |
| [Mendeley 水面／湿表面数据](https://data.mendeley.com/datasets/y6zyrnxbfm/4) | `data/external/stagnant_water_wet_surface_34mb.zip` | `output/reproduction_wet_water_boxes/model.joblib`、`output/reproduction_wet_water_boxes/report.json` |
| [Caltech RGB＋热像配对数据](https://data.caltech.edu/records/cks6g-ps927) | `data/external/labeled_rgbt_pairs.zip`；官方划分在 `data/external/caltech_splits/` | `output/reproduction_caltech_rgbt/`、`output/reproduction_caltech_trajectory/` 中的报告、三份模型及样例 |

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

## 3. RGB／热像／融合水面分割：Caltech

[CaltechDATA 官方记录](https://data.caltech.edu/records/cks6g-ps927)提供成对 RGB、热红外及语义标注；`labeled_rgbt_pairs.zip` 已下载到 D 盘，大小 **4,287,170,398 字节**，MD5 `22f42923694e724d4b7e354bace12389` 与官方值一致，ZIP 中央目录有 11,416 项。下载脚本会断点续传、逐段核验，并获取固定在[作者仓库提交 `dc1adec`](https://github.com/aerorobotics/caltech-aerial-rgbt-dataset/tree/dc1adecf72f58f0191e337321c20ede2ace0e49f/splits/rgbt_splits)的 9 份划分文件；`data/external/caltech_splits/source.json` 保存文件 SHA-256。数据及输出均不提交到 Git。作者仓库 [LICENSE](https://github.com/aerorobotics/caltech-aerial-rgbt-dataset/blob/main/LICENSE) 为 CC BY-NC-SA 4.0；商业用途须按许可处理。

官方 RGB／热图配对划分为训练 **1,731**、验证 **272**、测试 **279** 对。每对原始图、热图和掩膜均已按标注键核对；将源标签 `9=水` 映射为项目 `1=水`，`0/1=未知或背景` 映射为项目 `255=忽略`，其余有效类别映射为 `0=非水`。读取真正的单通道 `thermal16` TIFF；作者论文指出热像仪**不具辐射测温能力**，所以这些 16 位值只是相对热信号，不是摄氏温度。热图零值填充与忽略标签不参与评估，三组模型只在同一批有效像素上训练和计分。

两轮实验均将最长边缩至 256 像素、每张训练图每类最多采样 60 像素，使用 32 棵树、2 个并行线程；这是本项目的手工特征＋随机森林二分类基线，不是[原论文](https://www.ecva.net/papers/eccv_2024/papers_ECCV/papers/08026.pdf)的 DeepLabV3+／CMNeXt，也不能把本表与论文的十类指标直接比较。

| 留出测试方式 | 测试图 | RGB 水面 IoU | 热像水面 IoU | 融合水面 IoU | 融合相对 RGB |
| --- | ---: | ---: | ---: | ---: | ---: |
| 作者官方随机帧划分 | 279 | 0.7164 | 0.5101 | **0.7971** | +0.0807 |
| 按完整采集轨迹留出 | 279 | 0.7136 | 0.4785 | **0.7894** | +0.0758 |

官方测试在 11,426,854 个共同有效像素上计算，融合模型的水面精确率 **0.8832**、召回率 **0.8910**；两类混淆矩阵（行真值、列预测，`[非水, 水]`）为 `[[6875693, 479744], [443684, 3627733]]`。在 206 张含水测试图中，融合对 177 张的逐图水面 IoU 优于 RGB，对 28 张较差。完整报告与三个可由 `waterwatch.learning.load_model` 加载的模型见 `output/reproduction_caltech_rgbt/`。

作者官方划分中，测试的 24 条轨迹有 **23 条也出现在训练集**。因此另将全部 2,282 对图按轨迹重新分组；训练 23 条、验证 4 条、测试 4 条，组间无交叉。这一轮测试在 11,427,385 个共同有效像素上计算，融合水面精确率 **0.8783**、召回率 **0.8863**；在 218 张含水测试图中，融合逐图 IoU 优于 RGB 的有 157 张，较差的有 61 张。该轮的测试影像与官方轮不同，**不能把两行分数直接视为同一测试集上的变化**；其报告和独立模型见 `output/reproduction_caltech_trajectory/`。

已检查两轮的 RGB 输入、热图、真值与叠图。官方样例 `output/reproduction_caltech_rgbt/examples/2022-05-15_ColoradoRiver_flight1_mask-06660/` 中，热像单独预测散点误报明显，融合叠图更连贯；轨迹留出样例 `output/reproduction_caltech_trajectory/examples/big_bear_ONR_2022-05-08-11-34-00_mask-00180/` 中，融合减少了 RGB 对天空的误报，但岸边仍有错分。也抽查了**融合变差**的反例：`output/reproduction_caltech_rgbt/failure_examples/2022-12-20_Castaic_Lake_flight3_mask-38612/` 的水面 IoU 从 RGB 0.801 降到融合 0.594；`output/reproduction_caltech_trajectory/failure_examples/caltech_duck_ONR_2023-03-22-14-41-46_mask-27061/` 从 0.643 降到 0.274，叠图可见陆地误报增多。融合并非每张图都受益。`input_rgb.png`、`input_thermal.png` 是输入预览；`rgb.png`、`thermal.png`、`fusion.png` 是预测掩膜。

### 能支持的结论与数据缺口

- 在 **Caltech 的自然水面数据与本随机森林基线**上，融合优于单独 RGB；单独热像低于 RGB。数据同时含无人机与地面视角，且轨迹留出仍有相同地理区域，因此这些分数不代表项目自有无人机或热传感器的现场精度。
- Caltech 没有雨后地面 `干地／湿而无明水／可见积水` 三类标注。前述 Mendeley 数据也只有 RGB 框。**本次仍无法训练或验证项目最终的三类逐像素 RGB＋热像积水模型**。
- [GAM4water](https://github.com/monviso/GAM4water)公开 R 方法，但本次未取得其论文原始 RGB／热像影像及水陆标注，因此没有声称复现其论文表格数值。

## 4. 深度网络复现一：CMNeXt-B2（Caltech RGB-T，ECCV 2024 论文的基准模型）

[ECCV 2024 论文](https://www.ecva.net/papers/eccv_2024/papers_ECCV/papers/08026.pdf)（arXiv 2403.08997）在 Caltech 航拍 RGB+热像数据上把 **CMNeXt**（[官方实现](https://github.com/jamycheung/DELIVER)，CVPR 2023）列为最优 RGB-T 分割模型。本节用官方代码（固定 commit `b3a76c3`）真实训练，替换第 3 节的手工特征随机森林玩具基线。

**环境与流程**（Windows、RTX 4060 Laptop 8GB、waterseg conda 环境、torch 2.7.1+cu118）：

```powershell
python -m waterwatch.download_paper_code      # 克隆两个官方仓库到固定 commit + 应用补丁 + 下载 mit_b2.pth（SHA-256 校验）
python -m waterwatch.export_caltech_deliver   # 本地 ZIP -> DELIVER 训练格式（标签 2..11 -> 0..9，水=7；热图逐图 1..255 归一化，0 保持为填充标记；热图填充区标签置 255，与随机森林基线同支撑集）
python -m waterwatch.train_deliver_caltech --epochs 120 --eval-start 60 --eval-interval 5 --image-size 384 512 --workers 4 --log output/cmnext_caltech_full.log
python -m waterwatch.evaluate_deliver_caltech --output output/reproduction_caltech_cmnext
```

`model/patches/deliver_caltech.patch` 新增 CaltechRGBT 数据集类与配置，并用纯 torch shim 替换仅有的两处 mmcv import（mmcv 在 Windows/torch 2.7 无预编译包；这两处只被 CMNeXt 不使用的模型头引用）。训练中段机器重启一次，从 epoch-80 检查点恢复后又在 epoch 85 遇 Windows DataLoader 死锁（补丁因此把 worker 数做成配置项），最终**停在第 114/120 轮**，采用验证最优的 epoch-105 检查点（验证 10 类 mIoU 75.06）。

**与论文/基线的差异**：论文用更大骨干与分辨率、多卡长训；本机用 **CMNeXt-B2、384×512、batch 4、AdamW 6e-5、AMP**。测试评估按本仓库协议在原生 600×960 整图单次前向（pad 到 32 倍数）。

| 测试 279 张（官方随机划分） | 数值 |
| --- | ---: |
| 10 类 mIoU | 0.7017 |
| **水类 IoU / 精确率 / 召回率** | **0.9748 / 0.9895 / 0.9849** |
| 二值水-非水 IoU（与第 3 节随机森林同支撑集） | 0.9748（RF 融合为 0.7971） |
| 逐图水 IoU 中位数（207 张含水图） | 0.979 |

在 Caltech 的自然水面上，官方 CMNeXt 网络比本项目随机森林基线高约 18 个 IoU 点；该数据不含雨后路面积水类别，此分数仍不能外推为项目自有无人机任务精度。完整报告与样例叠图（含最差反例）在 `output/reproduction_caltech_cmnext/`。

## 5. 深度网络复现二：ABCDWaveNet（道路积水/湿面分割，arXiv 2504.05112）

[ABCDWaveNet](https://arxiv.org/html/2504.05112v1)（[官方代码](https://github.com/Lyu-Dakang/ABCDWaveNet)，固定 commit `8b16021`）做雾天道路积水（puddle）二值分割。论文数据集 Puddle-1000 / Foggy-Puddle 无公开直链，本节使用**同一作者 AGSENet 仓库提供的夸克网盘归档**（用户手动下载到 `data/`；掩膜为 0/1 灰度）。**Night-Puddle 归档只含图片、没有任何掩膜**，论文的第三个数据集 Foggy Low-Light Puddle 亦未公开，按预定降级策略跳过并如实记录。

```powershell
python -m waterwatch.export_puddle_abcd --variant puddle1000   # 786 训练 / 198 验证（丢弃 4 个浏览器重名残留）
python -m waterwatch.export_puddle_abcd --variant foggy
python -m waterwatch.train_abcdwavenet --train-dir data/abcd/puddle1000/train --val-dir data/abcd/puddle1000/val --epochs 80 --batch-size 2 --checkpoint-dir output/abcdwavenet_puddle1000 --log output/abcdwavenet_puddle1000.log
python -m waterwatch.evaluate_abcdwavenet --checkpoint output/abcdwavenet_puddle1000/UNet_train_best_0.7751.pth --dataset data/abcd/puddle1000/val --output output/reproduction_abcdwavenet_puddle1000
```

`model/patches/abcdwavenet_cli.patch` 给官方 `train.py` 增加数据路径/检查点目录 CLI，并删除每批触发 GPU 同步的调试 print。**关闭 AMP**（torch 2.7 上 fp16 小波变换产生 NaN，作者脚本默认也是 fp32）；batch≥6 触发 8GB 显存 WDDM 抖动（15 秒/批），降为 batch 2 + `expandable_segments` 后 3.5 批/秒。

| 验证 198 张 | 论文 IoU | 本机复现 IoU | Dice |
| --- | ---: | ---: | ---: |
| Puddle-1000 | 85.84 | **76.53** | 86.70 |
| Foggy-Puddle | 84.59 | **75.09** | 85.77 |

差距约 9 个 IoU 点，主要归因于 batch 2（论文级 GPU 批量更大、梯度更稳）与 80 vs 论文 300 epochs；脚本是参数化的，换更大显存可用相同命令提高配置复跑。

## 6. ONNX 导出与工作台部署（飞行后照片分析）

```powershell
python -m waterwatch.export_onnx --model abcdwavenet --checkpoint output/abcdwavenet_puddle1000/UNet_train_best_0.7751.pth --output artifacts/onnx/abcdwavenet_ponding.onnx
python -m waterwatch.export_onnx --model cmnext --checkpoint <best.pth> --output artifacts/onnx/cmnext_b2_rgbt.onnx
```

- `abcdwavenet_ponding.onnx`：timm CondConv2d 动态卷积权重使传统追踪导出失败，改用 torch 2.7 dynamo 导出器（onnxscript）；与 PyTorch 最大 logit 偏差 9.5e-06。
- `cmnext_b2_rgbt.onnx`：双输入（RGB ImageNet 归一化 + 三通道热图）；偏差 8.4e-05。
- `artifacts/onnx/registry.json` 指向默认模型（ABCD 积水）；`Web/backend` 的 `/api/photo-analysis/*` 端点通过 `scripts/run_water_onnx.py` 子进程调用该目录，前端"飞行后照片分析"面板上传照片即可得到叠图/疑似掩膜/置信度。环境变量 `WATER_ANALYSIS_PYTHON`、`WATER_ANALYSIS_MODEL_DIR` 可覆盖默认路径。

要验证雨后地面积水方案，仍需自己拍摄**同场景配准的 RGB＋二维热图**，标注干地、湿而无明水、可见积水三类，并按地点／雨次留出测试集。河湖防汛还需要同一岸段前后两期配准影像，才能验证水面变化，而不能仅靠单张图推断真实水位或堤防风险。
