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

## 注意

- 依赖 `D:\anaconda\envs\waterseg\python.exe`（推理）和 `model/artifacts/onnx/registry.json`（模型注册），缺失时报明确错误。
- **疑似区域是待人工核查的线索**：占比不是面积，更不是水深、水位或堤防安全结论。当前模型在车载视角道路数据上训练（Puddle-1000 IoU 76.5），对航拍视角的效果尚未实测标定。
- 现场演示样例可用 `data/abcd/{puddle1000,foggy}/val/images/` 中的照片；正式使用换成本机无人机实拍照片。
