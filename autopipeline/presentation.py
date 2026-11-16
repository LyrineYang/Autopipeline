"""User-facing workflow messages; never decides whether a candidate is valid."""
from __future__ import annotations

import json
from pathlib import Path
import shlex


def command(*parts: object) -> str:
    """Copyable POSIX-shell command, including paths with spaces."""
    return shlex.join([str(part) for part in parts])


def evaluate_command(spec: Path, candidate: Path | None = None) -> str:
    parts = ["autopipeline", "pipeline", "evaluate", "--spec", str(spec)]
    if candidate is not None:
        parts += ["--candidate", str(candidate)]
    return command(*parts)


def outcome(receipt: dict) -> dict[str, str]:
    status = receipt.get("status")
    labels = {
        "verified": ("评估通过", "所选实现已通过最终验证；这不代表取得优化收益。"),
        "completed_improved": ("找到改进", "搜索选出了有代码改动、评分更好的候选，并通过最终验证。收益需在相同条件下重复测量。"),
        "completed_no_improvement": ("完成，未找到改进", "所选实现已通过最终验证；本次搜索没有选出更好的候选，不必立即增加预算。"),
        "failed": ("运行失败", "本次运行未完成有效交付，请先处理失败原因。"),
        "interrupted": ("运行已中断", "已保留部分记录；当前不支持从中断位置自动续跑。"),
        "running": ("尚未完成", "这是运行记录中的状态；不代表进程仍在运行，也不能作为验证通过的证据。"),
    }
    title, detail = labels.get(status, ("结果未确认", "尚无可识别的完成状态，请检查运行记录。"))
    if status in {"verified", "completed_improved", "completed_no_improvement"} and receipt.get("verified") is not True:
        title, detail = "结果未确认", "完成状态缺少验证通过记录，不能据此使用或导出候选。"
    return {"title": title, "detail": detail,
            "reason": str(receipt.get("failure_reason") or receipt.get("error") or "")}


def print_outcome(receipt: dict) -> None:
    message = outcome(receipt)
    print(f"结果：{message['title']}。{message['detail']}")
    if message["reason"]:
        print(f"原因：{message['reason']}")


def load_receipt(run_dir: Path) -> dict | None:
    path = run_dir / "result.json"
    if not path.exists():
        return None  # Legacy tree runs do not have a product receipt.
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(receipt, dict):
        raise ValueError(f"Invalid run receipt: {path}. Expected an object.")
    return receipt


def next_steps(run_dir: Path, receipt: dict) -> list[str]:
    if receipt.get("status") == "running":
        return ["可重新读取报告查看已有记录；若原进程已停止，请用新运行目录重试。",
                command("autopipeline", "report", run_dir)]
    if receipt.get("verified") is True and receipt.get("status") in {
        "verified", "completed_improved", "completed_no_improvement",
    }:
        destination = run_dir.parent / f"{run_dir.name}-export"
        suffix = 2
        while destination.exists():
            destination = run_dir.parent / f"{run_dir.name}-export-{suffix}"
            suffix += 1
        return ["导出并复验所选实现：", command("autopipeline", "pipeline", "export", run_dir,
                                              "--output-dir", destination)]
    lines = [f"诊断记录：{run_dir}", "先查看原因和评测日志；不要放宽正确性要求来消除失败。"]
    if receipt.get("status") == "failed":
        for path in [run_dir / "final_verification/verification_report.json",
                     *sorted(run_dir.glob("nodes/*/verification/verification_report.json"))]:
            try:
                report = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(report, dict) or report.get("accepted") is not False:
                continue
            explanations = {
                "candidate_failed": "候选执行或数值检查失败；修正候选实现，或补齐其运行依赖。",
                "reference_failed": "参考实现未能完成评估；先检查输入、设备和依赖。",
                "performance_regressed": "候选未通过性能要求；检查实现并在相同条件下复验。",
                "protected_files_changed": "固定文件被改动；恢复参考与评测配置，只修改允许的候选文件。",
            }
            lines.append(explanations.get(report.get("failure_reason"), "评测未通过；请查看下面的具体证据。"))
            lines.append(f"评测详情：{path}")
            for label in ("reference", "candidate"):
                for stream in ("stderr", "stdout"):
                    log = path.parent / f"{label}.{stream}.log"
                    if log.is_file() and log.stat().st_size:
                        lines.append(f"{'参考' if label == 'reference' else '候选'}诊断：{log}")
            break
    if (run_dir / "input" / "pipeline.json").is_file():
        lines += ["代码有误时，修正原项目的候选并用原 spec 开始新运行；保留这里的失败记录。",
                  "若仅修正了运行环境，可重新评估这份固定输入（不调用 agent，自动创建新目录）：",
                  evaluate_command(run_dir / "input" / "pipeline.json")]
    else:
        lines += ["修正原因后，用原命令重新运行并省略 --run-dir，或指定新的目录。"]
    return lines
