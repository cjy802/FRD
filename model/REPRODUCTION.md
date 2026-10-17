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

## 7. 域适配：FRED 淹水道路微调（本地城市照片验收通过）

基础模型（第 5 节）在本地城市场景完全失效：2026-10-16 用 2 张本地实拍（城市沥青路浑浊泥水坑洼、整片天空反光淹水街道）做零样本测试，水类最高置信度仅 7/255、检出 0%——Puddle-1000 的砂石土路域与本地差距过大，调阈值无效。改用用户提供的 [FRED 数据集](https://huggingface.co/datasets/CMalone-Jupiter/FRED)（Flooded Road Environments Dataset，澳大利亚布里斯班 5 处地点真实淹水道路，车载前视相机逐帧语义标注，CC BY-NC-SA 4.0）做第二级微调。

**流程**（token 需用户在 HF 接受数据集条款后提供）：

```powershell
python -m waterwatch.export_fred_abcd      # flooded/KITTI-style 前视子集 -> 二值掩膜（label==1 为水，SDK 官方口径；标注 NEAREST 缩放到 1920x1200）
python -m waterwatch.fine_tune_ponding --data data/fred_ft --epochs 30 --batch-size 2 --lr 2e-5 --output output/abcdwavenet_fred --export-dir artifacts/onnx_fred
```

数据决策：flooded 前视共 5 序列；**Mount-Cotton_20241217（1,442 对）标注中没有任何水类像素（仅 0/2=unknown），整段排除**以防错误负样本。最终训练 812 对（Cambogan/DairyCreek/Pullenvale）、验证 420 对（Holmview，地点与训练集完全不交叉）。水面占比 16%-30%。

| 指标 | 数值 |
| --- | ---: |
| 跨地点验证集水 IoU（Holmview） | **0.6513** |
| ONNX 导出最大 logit 偏差 | 2.3e-05 |

**零样本验收**（2 张本地城市场景照片，训练数据不含任何本地影像）：

| 照片 | 微调前 | 微调后 |
| --- | --- | --- |
| 淹水街道（天空反光） | 0% 检出，置信度 4/255 | **疑似 11.2%、9 斑块、置信度 0.96**，水面定位正确 |
| 坑洼浑浊泥水路 | 0% 检出，置信度 7/255 | **疑似 1.1%、13 斑块、置信度 0.87**，主水坑命中 |

FRED 模型曾设为默认，但本地实拍暴露了残余缺口：城市沥青路上的**大面积浑浊泥水坑**（低反光、颜色接近湿沥青）仍检不出（区域水类概率均值 0.008，调阈值无效）——两类公开数据都缺少这种形态。

## 8. 第三级微调：本地种子标注混合训练（浑浊水坑补齐）

用 3 张本地实拍（含 1 张浑浊水坑路）做种子标注：`waterwatch/seed_local_labels.py` 内置目检后的多边形（标注叠图人工核对；截图内模型涂鸦区已避开——**训练/测试务必用原始照片，不要带叠图标记的截图**）。本地 3 张 × 水平翻转 × 15 倍过采样（90 样本，占 10%）与 FRED 812 对混合，从 FRED 检查点以更低学习率（1e-5、15 轮）继续微调，防止冲掉已学到的反光水面能力。

| 指标 | FRED-only | 混合微调后 |
| --- | ---: | ---: |
| FRED 跨地点验证水 IoU | 0.6513 | **0.7691**（继续训练仍有余量） |
| 本地浑浊水坑照片 | 漏检主水坑 | **疑似 7.6%（标注真值 7.8%），主水坑群全部覆盖**，置信度 0.96 |
| 本地淹水街道 / 坑洼路 | 11.2% / 1.1% | 49.5% / 7.6%，置信度 0.97-0.99 |

混合模型已设为默认（`artifacts/onnx/registry.json`，注明种子标注来源与替换计划）。局限如实说明：3 张种子图只能证明路径打通，模型可能部分记忆了这些场景；鲁棒的本地域覆盖仍需按 `demo/README.md` 流程积累 30+ 张原始照片（labelme 预标注辅助）再训练。ONNX 导出偏差 9.0e-05。

## 9. 方法对比基线：YOLOv8-seg 实例分割与检测框（同数据同协议）

第 5、7/8 节的 ABCDWaveNet 是语义分割；工程上常见的替代是 YOLO 目标检测或实例分割。本节在同一批数据、同一 IoU 口径下补齐两条 YOLO 对照：**YOLOv8n-seg**（预测实例掩膜并集成一张二值水掩膜）和 **YOLOv8n 纯检测框**（把预测矩形框填实当作伪掩膜）。检测框填实后的像素 IoU 直接量化"矩形框在不规则积水形状上损失多少"，回答是否只能靠检测框做积水识别。

环境：复用机器上现成的 `yolov8-env` conda 环境（ultralytics 8.4.41、torch 2.7.1+cu118），**未安装任何新包**。标签由 `prepare` 从第 5/7 节同一批 0/1 掩膜生成（cv2 外轮廓多边形，丢弃 <30 像素碎斑；多边形不表达掩膜内空洞，属标注简化），写到图片同级 `labels/` 目录——这是 ultralytics 把图片路径中 `/images/` 替换为 `/labels/` 的固定查找规则；seg 与 detect 共用同一份多边形标签，检测框由 segments 自动派生，避免两套标签互相覆盖。训练/验证划分与第 5、7 节完全相同（Puddle-1000 786/198，FRED 812/420，验证地点不交叉）。

```powershell
D:\anaconda\envs\yolov8-env\python.exe -m waterwatch.yolo_seg_baseline prepare --dataset puddle1000
D:\anaconda\envs\yolov8-env\python.exe -m waterwatch.yolo_seg_baseline prepare --dataset fred
D:\anaconda\envs\yolov8-env\python.exe -m waterwatch.yolo_seg_baseline train --dataset puddle1000 --model yolov8n-seg --epochs 100 --batch 8 --run-name puddle1000_seg
D:\anaconda\envs\yolov8-env\python.exe -m waterwatch.yolo_seg_baseline train --dataset fred --model yolov8n-seg --epochs 100 --batch 8 --run-name fred_seg
D:\anaconda\envs\yolov8-env\python.exe -m waterwatch.yolo_seg_baseline evaluate --dataset puddle1000 --weights output/yolo_seg/puddle1000_seg/weights/best.pt --output output/reproduction_yoloseg_puddle1000
D:\anaconda\envs\yolov8-env\python.exe -m waterwatch.yolo_seg_baseline evaluate --dataset fred --weights output/yolo_seg/fred_seg/weights/best.pt --output output/reproduction_yoloseg_fred
# 检测框对照：--model 换成 yolov8n（不含 seg 即检测任务），评估命令同上，输出在 output/reproduction_yolodetect_*
```

训练配方：imgsz 640、batch 8、100 epochs、默认增强、seed 42；seg 模型 3.26M 参数（训练约 91/159 分钟），detect 模型 3.01M 参数（约 45/32 分钟）。评估统一 conf 0.25、原图分辨率逐图前向，IoU/Dice/精确率/召回率与 `evaluate_abcdwavenet` 同口径全局累计。

| 验证集 | 模型与输出形式 | 水 IoU | Dice | 精确率 | 召回率 | 逐图 IoU 中位数 | 原生 mAP50 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Puddle-1000（198） | ABCDWaveNet 语义分割（第 5 节） | **0.7653** | 0.8670 | — | — | — | — |
| Puddle-1000 | YOLOv8n-seg 实例掩膜并集 | 0.7562 | 0.8612 | 0.8451 | 0.8778 | 0.6867 | mask 0.677 / box 0.756 |
| Puddle-1000 | YOLOv8n 检测框填实 | 0.4287 | 0.6001 | 0.4455 | 0.9192 | 0.4589 | box 0.753 |
| FRED Holmview（420） | ABCDWaveNet，FRED-only（第 7 节） | 0.6513 | — | — | — | — | — |
| FRED | ABCDWaveNet，加本地种子（第 8 节最好成绩） | 0.7691 | — | — | — | — | — |
| FRED | YOLOv8n-seg 实例掩膜并集 | **0.8583** | 0.9237 | 0.9017 | 0.9469 | 0.8896 | mask 0.963 / box 0.960 |
| FRED | YOLOv8n 检测框填实 | 0.7610 | 0.8643 | 0.7893 | 0.9550 | 0.7684 | box 0.982 |

能支持的结论与限制：

- **检测框损失随水体形态剧变**：同为 640px 配方，检测框填实在 Puddle-1000 上比实例掩膜低 **32.8 个 IoU 点**（0.7562→0.4287），在 FRED 上低 9.7 点（0.8583→0.7610）。Puddle 是小水洼碎片，矩形框内大部分是背景（精确率 0.446 对掩膜的 0.845）；FRED 是整片连通淹水，矩形近似损失较小。`demo_watch` 的三级判级依赖水面占比与斑块数，矩形框无法提供这两类统计。
- **检测指标会严重高估可用性**：FRED 检测框 mAP50 高达 0.982，填实后像素 IoU 只有 0.761；只报 mAP 的检测方案回答不了"积了多少水、几个坑"。
- **YOLOv8n-seg 与 ABCDWaveNet 各赢一个域**：Puddle-1000 打平（0.756 对 0.765）；FRED 上 YOLOv8n-seg 更高（0.858，对 FRED-only 0.651、加本地种子后的最好成绩 0.769）。但两者配方不同：YOLO 用其推荐配方（640px、batch 8、默认增强、100 epochs），ABCD 是受 8GB 显存限制的降配复现（256px、batch 2、fp32），第 5 节已把其与论文的差距归因于该降配。因此本表是"各自常规配方"的对比，不是严格架构公平对比；FRED 上的差距更可能主要来自输入分辨率（256px 对整片大面积水体的信息损失）而非架构本身——提高 ABCD 推理/微调分辨率是比换模型更值得先试的方向。
- 其余简化：评估置信度阈值固定 0.25；ABCD 行的 Dice/精确率等见第 5、7、8 节各自报告，此处只并列 IoU。

## 10. 无人机航拍域适配：FloodNet 双模型对决（跨域遗忘检查）

第 5-9 节两个模型都只见过地面视角；[FloodNet-Supervised v1.0](https://github.com/BipinMeghwal/FloodNet)（飓风 Harvey 后 DJI Mavic Pro 航拍，4000×3000，10 类逐像素索引标注，官方 Train/Val/Test = 1445/450/448）是与本项目目标无人机视角最接近的公开数据。`waterwatch/export_floodnet` 把 **Water(5)∪Road-flooded(3)** 合并为水（Pool 是泳池、Building-flooded 是被淹建筑屋顶，均不算水），图像缩到长边 1280，水体占比约 13%，48% 帧完全无水。两模型均从各自 FRED 阶段检查点继续微调：ABCD 用 2e-5 低学习率、30 轮（fp32+CondConv 在第 11 轮附近 CUDA OOM 中断，滚动最佳=第 8 轮 0.7093 且验证已平台期，损失可忽略；复跑建议加 `PYTORCH_CUDA_ALLOC_CONF=expandable_segments`）；YOLOv8n-seg 用默认配方 100 轮。

```powershell
python -m waterwatch.export_floodnet --archive D:/DATASET/FloodNet.zip
D:\anaconda\envs\yolov8-env\python.exe -m waterwatch.yolo_seg_baseline prepare --dataset floodnet
D:\anaconda\envs\waterseg\python.exe -m waterwatch.fine_tune_ponding --data data/floodnet --train-dir data/floodnet/train --val-dir data/floodnet/val --epochs 30 --batch-size 2 --lr 2e-5 --base-checkpoint output/abcdwavenet_fred/abcdwavenet_local_best_0.6513.pth --output output/abcdwavenet_floodnet
D:\anaconda\envs\yolov8-env\python.exe -m waterwatch.yolo_seg_baseline train --dataset floodnet --model output/yolo_seg/fred_seg/weights/best.pt --epochs 100 --batch 8 --run-name floodnet_seg
# 评估：Test 用 --dataset floodnettest（YOLO）/ --dataset data/floodnet/test（ABCD）；FRED 遗忘检查各自对 data/fred_ft/val
```

| 模型 | FloodNet Test 水 IoU（448 张） | FRED val 水 IoU（微调前 → 后） | 本地 3 张照片 |
| --- | ---: | ---: | --- |
| ABCDWaveNet | **0.7056** | 0.6513 → 0.3172 | 未测（第 8 节混合模型仍是本地域最优） |
| YOLOv8n-seg | 0.6920 | 0.8583 → **0.2081**（逐图中位数 0.000） | **全部零检出** |

能支持的结论与限制：

- **两个模型都能学会航拍域**（Test 0.69-0.71，对照引用该数据的 DINOv2 论文 10 类 mIoU 仅 54）；ABCD 略高，且训练量只有 YOLO 的约 1/3 就已平台。
- **两模型都灾难性遗忘 FRED**。ABCD（低学习率）遗忘较轻（0.3172 对 0.2081，逐图中位数 0.30 对 0.000），证实遗忘烈度与配方相关；但"顺序微调跨大域"对双方都不可行是共性结论。单一部署模型要么做多域**混合回放训练**（FRED+FloodNet+本地同批混训，如第 8 节 local_mix 的推广），要么**按视角分模型**（航拍模型管无人机图、地面模型管街景）。
- YOLO 微调后对本地照片零检出、FRED 逐图中位数 0.000，说明大学习率下旧域几乎清空；其 100 轮配方对域适配任务本身偏猛，续训场景应显著调低学习率。
- ABCD 行的本地照片未在本轮重测：其第 8 节混合模型（FRED IoU 0.7691、浑浊水坑命中）仍是当前本地域最优与部署默认。

要验证雨后地面积水方案，仍需自己拍摄**同场景配准的 RGB＋二维热图**，标注干地、湿而无明水、可见积水三类，并按地点／雨次留出测试集。河湖防汛还需要同一岸段前后两期配准影像，才能验证水面变化，而不能仅靠单张图推断真实水位或堤防风险。
