# FuXi 下载、配对与训练复现包

这个目录用于在另一台机器上完成三件事：

1. 从 CDS 下载并抽取 ERA5 月度单点观测；
2. 使用本地 FuXi GPU 推理生成新的 `fuxi_era5_pairs_6h.csv` 配对样本；
3. 使用配对 CSV 训练本地点订正模型。

本目录**不包含 FuXi 模型权重**，需要把 `short.onnx` 和外部权重文件 `short` 另外放到 `help\FuXi_EC`。没有这两个文件时仍可使用 `--download-only` 下载观测，但不能运行 FuXi 配对。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `backfill_batches.py` | 下载、校验、抽取观测，并在模型存在时调用 FuXi 生成配对 |
| `download_era5_pressure.py` | 被 `backfill_batches.py` 调用的气压层下载模块 |
| `extract_era5_point.py` | 被 `backfill_batches.py` 调用的目标点抽取模块 |
| `build_fuxi_pairs.py` | 加载 FuXi ONNX，在 GPU 上生成 +6 小时目标点配对 |
| `prepare_fuxi1_input.py` | 构造 FuXi-1.0 的 70 通道输入 |
| `run_fuxi1_short.py` | 准备 CUDA，并提供 FuXi 输入通道定义 |
| `pair_existing_batches.py` | 对已经下载好的月度文件离线配对 |
| `train_corrector.py` | 使用配对 CSV 训练 Ridge 订正模型 |
| `diagnose_season.py` | 可选的月度偏差和滚动检验 |
| `FuXi_EC\short.onnx` | 需要另行复制的 FuXi 模型结构 |
| `FuXi_EC\short` | 需要另行复制的 FuXi 外部权重 |
| `data\fuxi_era5_pairs_6h.csv` | 当前已有的训练输入样本 |
| `data\era5_surface_point_31p5N_119p5E_2025.csv` | 当前已有的 2025 年单点观测，可作为下载续跑的起始文件 |

所有 `.py` 文件必须放在同一个 `help` 目录中，因为脚本之间会直接导入或调用彼此。

## 新机器准备

建议使用 Python 3.10 或更新版本。下载流程需要安装：

```powershell
python -m pip install -r requirements-download.txt
```

训练流程需要安装：

```powershell
python -m pip install -r requirements-train.txt
```

要运行 FuXi GPU 配对，还需要安装：

```powershell
python -m pip install -r requirements-fuxi.txt
```

此外需要 NVIDIA 驱动、可用的 CUDA/cuDNN 和兼容的 GPU。`short.onnx` 与 `short` 必须位于：

```text
help\FuXi_EC\short.onnx
help\FuXi_EC\short
```

下载 ERA5 还需要在新机器配置 CDS API 凭据。凭据不放入此目录，也不要提交到 Git。

## 下载某个月份

先预览，不下载：

```powershell
$py = 'C:\Python311\python.exe'
& $py '.\backfill_batches.py' --year 2025 --months 6 --batch-root '.\backfill'
```

确认后执行下载、校验和单点抽取，但不调用 FuXi：

```powershell
& $py '.\backfill_batches.py' `
  --year 2025 `
  --months 6 `
  --batch-root '.\backfill' `
  --pairs-csv '.\data\fuxi_era5_pairs_6h.csv' `
  --obs-csv '.\data\era5_surface_point_31p5N_119p5E_2025.csv' `
  --execute `
  --download-only
```

这一步会下载或复用：

- 每天 `00/06/12/18 UTC` 的气压层 ZIP；
- 月度地面温度和海平面气压；
- 月度 10 米风；
- 月度逐小时降水及月初所需的上月末降水窗口。

然后把目标点逐小时观测追加到 `--obs-csv` 指定的文件。`--download-only` 不需要 FuXi 模型、不需要 GPU，也不会写入新的 FuXi 配对结果。

如果已有文件有效，脚本会跳过；如果发现 NetCDF 帧数不对或有非有限值，会废弃旧文件并重新下载。下载数据默认放在 `--batch-root` 下。

## 下载并生成新的 FuXi 配对样本

在模型文件已经放入 `help\FuXi_EC` 后，运行：

```powershell
& $py '.\backfill_batches.py' `
  --year 2025 `
  --months 6 `
  --batch-root '.\backfill' `
  --pairs-csv '.\data\fuxi_era5_pairs_6h.csv' `
  --obs-csv '.\data\era5_surface_point_31p5N_119p5E_2025.csv' `
  --model-dir '.\FuXi_EC' `
  --execute
```

这会在完成下载和观测抽取后调用 `build_fuxi_pairs.py`，使用 GPU 运行 FuXi，并将新的配对行追加到 `--pairs-csv`。如果只想先下载和抽点，使用上一节的 `--download-only`；该选项不会调用 GPU。

如果月度文件已经存在，也可以只做离线配对：

```powershell
& $py '.\pair_existing_batches.py' `
  --year 2025 `
  --months 6 `
  --batch-root '.\backfill' `
  --pairs-csv '.\data\fuxi_era5_pairs_6h.csv' `
  --observations '.\data\era5_surface_point_31p5N_119p5E_2025.csv' `
  --model-dir '.\FuXi_EC' `
  --execute
```

## 训练订正模型

如果使用本目录附带的配对数据：

```powershell
& $py '.\train_corrector.py' --train
```

也可以显式指定路径：

```powershell
& $py '.\train_corrector.py' `
  --data '.\data\fuxi_era5_pairs_6h.csv' `
  --output '.\output' `
  --train
```

训练结果写入 `output`，包括 `corrector_ridge.npz`、`metrics.json` 和 `test_predictions.csv`。训练是 CPU 任务，不需要 FuXi 模型或 CUDA。

可选的季节性诊断：

```powershell
& $py '.\diagnose_season.py' `
  --data '.\data\fuxi_era5_pairs_6h.csv' `
  --output '.\output'
```

## 需要修改的路径

优先通过命令行参数修改，不要直接改代码：

- `--batch-root`：ERA5 月度下载目录；
- `--obs-csv`：单点 ERA5 观测 CSV；
- `--pairs-csv`：已有配对 CSV；
- `--latitude`、`--longitude`：目标网格点。
- `--model-dir`：FuXi 模型目录，默认是 `help\FuXi_EC`；
- `--fuxi-python`：运行 FuXi 的 Python 解释器，默认使用当前解释器。

本目录脚本的默认路径都相对于 `help` 目录计算，不再依赖 `D:\低空经济`、`D:\anaconda` 或 `D:\DATASET`。只需要把 `$py` 改成新机器的 Python 路径即可。

## 输出和注意事项

- `era5_surface_point_*.csv` 是单点 ERA5 观测；
- `fuxi_era5_pairs_6h.csv` 是 FuXi 与 ERA5 的监督配对样本；
- `train_corrector.py` 只训练本地点订正模型，不会修改 FuXi 权重；
- `backfill`、`output`、`data` 和 `FuXi_EC\short` 都不应提交到 Git；
- CDS 凭据、下载数据、FuXi 权重和训练输出需要通过文件复制或安全存储单独提供。
