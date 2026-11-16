"""Create portable inference workspaces without importing compute dependencies."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import shutil
from importlib.resources import files

from .workloads import EXAMPLES


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def copy_workspace(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", ".autopipeline", ".git", ".venv"))


def initialize(destination: Path, *, source: Path | None, device: str, dtype: str, example: str = "residual-mlp", model_dir: Path | None = None) -> Path:
    destination = destination.resolve()
    if destination.exists():
        raise FileExistsError(f"Destination already exists: {destination}. Choose a new directory.")
    if source:
        source = source.resolve()
        if not (source / "reference.py").is_file():
            raise ValueError("Source needs reference.py defining Model and get_inputs()")
        if source in destination.parents:
            raise ValueError("Destination must be outside the source workspace")
        if any(p.is_symlink() for p in source.rglob("*")):
            raise ValueError("Source symlinks are unsupported; copy required files first")
    if example not in EXAMPLES:
        raise ValueError(f"Unknown example: {example}")
    if source and (example != "residual-mlp" or model_dir):
        raise ValueError("Use either --source or --example/--model-dir")
    if example == "hf-local":
        if model_dir is None:
            raise ValueError("hf-local requires --model-dir pointing to an existing local model directory")
        model_dir = model_dir.resolve()
        validate_model_directory(model_dir)
    elif model_dir is not None:
        raise ValueError("--model-dir is only supported with --example hf-local")
    workspace = destination / "workload"
    if source:
        copy_workspace(source, workspace)
    else:
        workspace.mkdir(parents=True)
        template = files("autopipeline.workloads").joinpath(example.replace("-", "_") + ".py").read_text(encoding="utf-8")
        (workspace / "reference.py").write_text(template, encoding="utf-8")
        if example == "hf-local":
            write_json(workspace / "model.json", {"model_dir": str(model_dir), "batch_size": 2, "sequence_length": 32,
                                                  "asset_hashes": model_asset_hashes(model_dir)})
    if not (workspace / "candidate.py").exists():
        (workspace / "candidate.py").write_text(
            '"""Optimize this implementation; preserve constructor, weights, and outputs."""\n'
            'from reference import Model\n\n\nclass ModelNew(Model):\n    pass\n', encoding="utf-8")
    workload = {"device": device, "dtype": dtype, "warmup": 5, "repeats": 20,
                "check_seeds": 3, "atol": 1e-5, "rtol": 1e-4}
    write_json(workspace / "workload.json", workload)
    command = ["{python}", "-m", "autopipeline.evaluate"]
    spec = {"name": destination.name, "objective": "Reduce measured end-to-end inference latency while preserving all outputs.",
            "workspace_source": "workload", "target_path": "candidate.py", "editable_paths": ["candidate.py"],
            "reference_paths": ["reference.py", "workload.json"],
            "constraints": ["Edit only candidate.py; preserve ModelNew constructor and state_dict compatibility.",
                            "Do not change the reference, inputs, evaluator, metrics, or profiler artifacts.",
                            "No cached outputs, input-specific constants, or skipped computation."],
            "objective_signals": ["end_to_end_ms", "throughput", "peak_vram_mb"],
            "evaluate_command": command, "final_verify_command": command,
            "metrics_path": "metrics.json", "score_metric": "end_to_end_ms", "score_direction": "lower",
            "timeout_seconds": 600, "metadata": {"example": "custom" if source else example, "runtime": "pytorch", "numerical_tolerances": {"atol": 1e-5, "rtol": 1e-4}}}
    write_json(destination / "pipeline.json", spec)
    (destination / "README.md").write_text(
        "# 模型工作区\n\n"
        f"设备：{device}；数值类型：{dtype}。以下命令从本 README 所在目录执行。\n"
        "已安装 AutoPipeline 并准备好该设备的 PyTorch 后，先完成评估和导出。\n"
        "初始化不会下载权重或调用 agent；评估执行本地代码，不调用 agent 服务。\n\n"
        "## 先得到可复验的结果\n\n"
        "```sh\nautopipeline pipeline check --spec pipeline.json\n"
        "autopipeline pipeline evaluate --spec pipeline.json --run-dir runs/baseline\n"
        "autopipeline report runs/baseline\n"
        "autopipeline pipeline export runs/baseline --output-dir exported\n"
        "autopipeline pipeline evaluate --spec exported/pipeline.json --candidate exported/candidate\n```\n\n"
        "评估通过不代表提速：初始候选沿用参考实现。查看终端解释或 runs/baseline/view/dashboard.html。\n"
        "重复评估时省略 --run-dir 会创建新目录；已有项目、运行和导出均不覆盖。\n\n"
        "## 再让 agent 优化\n\n"
        "先在执行机器配置并认证 Codex，或用 --executor-command 接入其他 agent。\n"
        "下面的 run 会使用服务额度；调用次数不是金额上限，优化不保证提速。\n\n"
        "```sh\nautopipeline pipeline run --spec pipeline.json --executor-backend codex-app-server "
        "--max-rounds 1 --children-per-expansion 1 --max-agent-calls 2 --max-probe-calls 2\n```\n\n"
        "运行会打印实际目录、结果含义和下一步命令。先处理失败原因，再重试或增加预算。\n"
        "中断不会自动续跑；可用 autopipeline report RUN_DIR 查看部分记录。\n\n"
        "## 需要自定义时\n\n"
        "workload/reference.py 定义 Model、get_inputs() 和可选 get_init_inputs()。\n"
        "workload/candidate.py 定义同构造参数、同 state_dict 的 ModelNew；默认仅允许修改此文件。\n"
        "输入支持位置参数或关键字参数；输出支持 tensor 及嵌套容器。\n"
        "运行前固定 workload/workload.json；CUDA 不会自动回退 CPU。\n"
        "改变设备、输入、权重或容差意味着新的比较，不能混用加速比。\n"
        "外部权重不会随导出复制，复验时需要相同资源与依赖。代码按本地程序执行，工作区复制不是系统安全沙箱。\n\n"
        "常用参数：autopipeline pipeline run --help；完整参数：autopipeline pipeline run --help-all。\n",
        encoding="utf-8")
    return destination / "pipeline.json"


def validate_model_directory(directory: Path) -> None:
    if not directory.is_dir() or not (directory / "config.json").is_file():
        raise ValueError(f"Local model needs config.json: {directory}. Download or save a Transformers encoder first.")
    if not any(directory.glob("*.safetensors")) and not any(directory.glob("pytorch_model*.bin")):
        raise ValueError(f"No model weights found in {directory}; expected safetensors or pytorch_model*.bin")


def model_asset_hashes(directory: Path) -> dict[str, str]:
    result = {}
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix in {".json", ".safetensors", ".bin"}:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[path.name] = digest.hexdigest()
    return result
