# 飞行后照片积水分析 · 本机演示流程

一条从"照片进文件夹"到"区域判断报告"的完整本机流水线，复用 `waterwatch` 训练导出的 ONNX 模型（默认 ABCDWaveNet 道路积水分割，见 [../REPRODUCTION.md](../REPRODUCTION.md) 第 5-6 节）。

## 目录

```
model/demo/inbox/    放入无人机照片（jpg/png/bmp/webp，一次一批）
model/demo/outbox/   每批生成 <时间戳>/ 目录：
                     ├─ <图名>__boxes.png      疑似区域红框+编号（演示主图）
                     ├─ <图名>__overlay.png    蓝色叠图
                     ├─ <图名>__suspected.png  二值掩膜
                     ├─ <图名>__confidence.png 置信度热图
                     ├─ inputs/                原图归档
                     ├─ report.html            可视化报告（浏览器直接打开）
                     └─ report.json            机器可读结果
```

## 演示步骤

```powershell
cd D:\低空经济\model

# 一次性模式：处理 inbox 里现有照片，生成报告并自动打开浏览器
D:\anaconda\python.exe -m waterwatch.demo_watch --open

# 监视模式（推荐现场演示）：启动后每 3 秒扫描，往 inbox 拖照片即自动分析出报告
D:\anaconda\python.exe -m waterwatch.demo_watch --watch --open
```

处理完的照片自动从 inbox 移入该批次的 `inputs/`，不会重复分析。

## 判断规则（透明、可调）

| 级别 | 条件（满足其一） | 含义 |
| --- | --- | --- |
| 疑似积水较多 | 占比 ≥ 5%，或 斑块 ≥ 3 处且占比 ≥ 1% | 建议现场核查 |
| 少量疑似 | 占比 ≥ 1%，或 存在斑块 | 建议复核照片 |
| 未见明显疑似 | 其余 | — |

阈值可用 `--high-fraction / --high-patches / --mid-fraction` 改；斑块按连通域统计，小于 40 像素的碎屑忽略。

## 启发式反光兜底（第二意见）

默认开启（`--no-heuristic` 关闭）。管线在模型判断之外，另用经典反光线索（亮、低饱和、局部平滑）生成橙色线索图 `<图名>__heuristic.png`。**当模型判"未见明显疑似"但启发式发现 ≥2% 高反光区域时，报告会明确提示"模型未检出——请人工打开照片复核（域差距风险）"**。启发式对天空反光型水面灵敏，也会误报雨伞、浅色路面；它只是复核提醒，不是模型结论。

### 为什么需要这一层：当前模型的已知局限

基础模型在**国外砂石土路车载视角**数据（Puddle-1000）上训练。对本地城市沥青路的实拍验证（2026-10-16，2 张含明显积水的照片）显示模型**完全未检出**（水类置信度 ≤7/255）——域差距过大，调阈值无效。修复路径见下节。

## 用本地照片微调模型（域适配，永久修复）

```powershell
cd D:\低空经济\model

# 1. 收集 30-100 张本地道路积水照片（手机/无人机均可），放入一个文件夹
# 2. 生成 labelme 预标注（启发式反光勾出候选多边形，降低标注量）
D:\anaconda\python.exe -m waterwatch.prelabel_reflection --photos data\local_raw --out data\local_labelme
# 3. 在 labelme 中打开 data\local_labelme，修正/增删多边形，标签用 "water"，文件存同目录
# 4. 标注转掩膜（每张一次；task=shoreline 的 water->1/land->0 正是训练格式）
D:\anaconda\python.exe -m waterwatch mask-from-labelme --json data\local_labelme\abc.json --rgb data\local_raw\abc.png --task shoreline --output data\local_ft\masks\abc.png
#    同时把原图拷入 data\local_ft\images\abc.png（文件名须与掩膜一致）
# 5. 从 Puddle-1000 检查点微调（自动切分训练/验证，导出 ONNX + registry）
D:\anaconda\envs\waterseg\python.exe -m waterwatch.fine_tune_ponding --data data\local_ft --epochs 40 --export-dir artifacts\onnx_local
# 6. 演示挂载新模型
D:\anaconda\python.exe -m waterwatch.demo_watch --watch --open --model-dir artifacts\onnx_local
```

微调用低学习率（默认 2e-5）保留基础模型的通用积水知识，只适配本地域；fp32 训练（fp16 小波会 NaN）。标注量 ≥30 张才值得训，越多越稳。微调后模型的水 IoU 会记录在 `artifacts/onnx_local/registry.json`。

## 注意

- 依赖 `D:\anaconda\envs\waterseg\python.exe`（推理）和 `model/artifacts/onnx/registry.json`（模型注册），缺失时报明确错误。
- **疑似区域是待人工核查的线索**：占比不是面积，更不是水深、水位或堤防安全结论。当前模型在车载视角道路数据上训练（Puddle-1000 IoU 76.5），对航拍视角的效果尚未实测标定。
- 现场演示样例可用 `data/abcd/{puddle1000,foggy}/val/images/` 中的照片；正式使用换成本机无人机实拍照片。
