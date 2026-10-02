# 无 FuXi 权重的下载与训练复现包

这个目录用于在另一台机器上完成两件事：

1. 从 CDS 下载并抽取 ERA5 月度单点观测；
2. 使用已有的 FuXi-ERA5 配对 CSV 训练本地点订正模型。

本目录**不包含 FuXi 模型权重**，因此不能在这里运行 FuXi GPU 推理，也不能生成新的 `fuxi_era5_pairs_6h.csv` 配对样本。要生成新的配对样本，必须使用项目 `app` 目录中的完整流程，并另外提供 `FuXi_EC\short.onnx` 和 `FuXi_EC\short`。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `backfill_batches.py` | 下载并校验指定月份的 ERA5 输入，抽取目标点观测；本复现包只允许 `--download-only` |
| `download_era5_pressure.py` | 被 `backfill_batches.py` 调用的气压层下载模块 |
| `extract_era5_point.py` | 被 `backfill_batches.py` 调用的目标点抽取模块 |
| `train_corrector.py` | 使用配对 CSV 训练 Ridge 订正模型 |
| `diagnose_season.py` | 可选的月度偏差和滚动检验 |
| `data\fuxi_era5_pairs_6h.csv` | 当前已有的训练输入样本 |
| `data\era5_surface_point_31p5N_119p5E_2025.csv` | 当前已有的 2025 年单点观测，可作为下载续跑的起始文件 |

`backfill_batches.py`、`download_era5_pressure.py`、`extract_era5_point.py` 必须放在同一个目录中，因为前者会直接导入后两个模块。

## 新机器准备

建议使用 Python 3.10 或更新版本。下载流程需要安装：

```powershell
python -m pip install -r requirements-download.txt
```

训练流程需要安装：

```powershell
python -m pip install -r requirements-train.txt
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

本目录脚本的默认路径都相对于 `help` 目录计算，不再依赖 `D:\低空经济`、`D:\anaconda` 或 `D:\DATASET`。只需要把 `$py` 改成新机器的 Python 路径即可。

## 与完整 FuXi 流程的边界

如果以后要在另一台机器上生成新的 FuXi 配对样本，不能只复制本目录。还需要项目中的 `build_fuxi_pairs.py`、`prepare_fuxi1_input.py`、`run_fuxi1_short.py`、CUDA/ONNX Runtime GPU 环境，以及 FuXi 权重文件。那时应使用完整项目的 `app\backfill_batches.py`，而不是本目录的下载-only版本。
