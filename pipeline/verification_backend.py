from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import time
from typing import Any


@dataclass(slots=True)
class ExecutableRunRequest:
    label: str
    workspace: Path
    command: list[str] | str
    metrics_path: str
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float | None = 300
    shell: bool = False


class ExecutableVerificationBackend:
    """Run reference and candidate workspaces and write a structured verdict."""

    schema_version = "verification-report/v1"
    backend_name = "executable"

    def __init__(
        self,
        *,
        numerical_atol: float = 1e-5,
        numerical_rtol: float = 1e-4,
        stdout_limit: int = 20000,
    ) -> None:
        if any(not math.isfinite(value) or value < 0 for value in (numerical_atol, numerical_rtol)):
            raise ValueError("Numerical tolerances must be finite and non-negative.")
        self.numerical_atol = numerical_atol
        self.numerical_rtol = numerical_rtol
        self.stdout_limit = stdout_limit

    def verify(
        self,
        *,
        reference: ExecutableRunRequest,
        candidate: ExecutableRunRequest,
        artifact_dir: Path,
        score_metric: str,
        score_direction: str = "higher",
        metadata: dict[str, Any] | None = None,
        require_no_regression: bool = True,
    ) -> dict[str, Any]:
        if score_direction not in {"higher", "lower"}:
            raise ValueError("score_direction must be 'higher' or 'lower'.")
        artifact_dir.mkdir(parents=True, exist_ok=True)
        started_at = _now_iso()
        started = time.monotonic()

        reference_run = self._run(reference, artifact_dir)
        candidate_run = self._run(candidate, artifact_dir)

        correctness = _correctness(reference_run, candidate_run)
        numerical_error = _numerical_error(
            reference_run.get("metrics", {}),
            candidate_run.get("metrics", {}),
            atol=self.numerical_atol,
            rtol=self.numerical_rtol,
        )
        performance = _performance(
            reference_run.get("metrics", {}),
            candidate_run.get("metrics", {}),
            score_metric=score_metric,
            score_direction=score_direction,
        )
        failure_reason = _failure_reason(
            reference_run=reference_run,
            candidate_run=candidate_run,
            correctness=correctness,
            numerical_error=numerical_error,
            performance=performance,
        )
        if failure_reason == "performance_regressed" and not require_no_regression:
            failure_reason = None
        accepted = failure_reason is None
        report_path = artifact_dir / "verification_report.json"
        artifact_paths = {
            "report": str(report_path),
            "reference_stdout": str(reference_run.get("stdout_path", "")),
            "reference_stderr": str(reference_run.get("stderr_path", "")),
            "reference_metrics": str(reference_run.get("metrics_path", "")),
            "reference_workspace": str(reference.workspace),
            "candidate_stdout": str(candidate_run.get("stdout_path", "")),
            "candidate_stderr": str(candidate_run.get("stderr_path", "")),
            "candidate_metrics": str(candidate_run.get("metrics_path", "")),
            "candidate_workspace": str(candidate.workspace),
        }
        safe_metadata = metadata or {}
        report = {
            "require_no_regression": require_no_regression,
            "schema_version": self.schema_version,
            "backend": self.backend_name,
            "started_at": started_at,
            "finished_at": _now_iso(),
            "duration_seconds": round(time.monotonic() - started, 6),
            "metadata": safe_metadata,
            "bottleneck_region": _dict_value(safe_metadata.get("bottleneck_region")),
            "candidate_modifications": safe_metadata.get("candidate_modifications", []),
            "verdict": "keep" if accepted else "reject",
            "accepted": accepted,
            "failure_reason": failure_reason,
            "correctness": correctness,
            "numerical_error": numerical_error,
            "performance": performance,
            "reference": reference_run,
            "candidate": candidate_run,
            "evidence": _evidence(correctness, numerical_error, performance),
            "artifact_paths": artifact_paths,
        }
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return report

    def _run(self, request: ExecutableRunRequest, artifact_dir: Path) -> dict[str, Any]:
        workspace = request.workspace.resolve()
        metrics_path = _metrics_path(workspace, request.metrics_path)
        if metrics_path.exists() and metrics_path.is_file():
            metrics_path.unlink()
        stdout_path = artifact_dir / f"{request.label}.stdout.log"
        stderr_path = artifact_dir / f"{request.label}.stderr.log"
        started = time.monotonic()
        stdout = ""
        stderr = ""
        returncode: int | None = None
        timed_out = False
        exception = ""
        env = {**os.environ, **request.env}
        try:
            completed = subprocess.run(
                request.command,
                cwd=str(workspace),
                env=env,
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
                shell=request.shell,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            returncode = completed.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            returncode = None
            stdout = _decode_process_text(exc.stdout)
            stderr = _decode_process_text(exc.stderr)
            exception = f"Timed out after {request.timeout_seconds} seconds."
        except Exception as exc:  # pragma: no cover - defensive subprocess guard
            returncode = None
            exception = f"{type(exc).__name__}: {exc}"
            stderr = exception

        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        metrics = _read_metrics(metrics_path, stdout)
        return {
            "label": request.label,
            "command": request.command,
            "workspace": str(workspace),
            "returncode": returncode,
            "timed_out": timed_out,
            "exception": exception,
            "duration_seconds": round(time.monotonic() - started, 6),
            "stdout": _clip(stdout, self.stdout_limit),
            "stderr": _clip(stderr, self.stdout_limit),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "metrics_path": str(metrics_path),
            "metrics": metrics,
        }


def compact_verification_report(report: dict[str, Any]) -> dict[str, Any]:
    performance = _dict_value(report.get("performance"))
    numerical_error = _dict_value(report.get("numerical_error"))
    correctness = _dict_value(report.get("correctness"))
    artifact_paths = _dict_value(report.get("artifact_paths"))
    return {
        "backend": report.get("backend"),
        "schema_version": report.get("schema_version"),
        "verdict": report.get("verdict"),
        "accepted": bool(report.get("accepted")),
        "failure_reason": report.get("failure_reason"),
        "report_path": artifact_paths.get("report"),
        "correctness": {
            "passed": correctness.get("passed"),
            "reference_status": correctness.get("reference_status"),
            "candidate_status": correctness.get("candidate_status"),
        },
        "numerical_error": {
            "available": numerical_error.get("available"),
            "passed": numerical_error.get("passed"),
            "max_abs_error": numerical_error.get("max_abs_error"),
            "max_rel_error": numerical_error.get("max_rel_error"),
            "source": numerical_error.get("source"),
            "reason": numerical_error.get("reason"),
        },
        "performance": {
            "available": performance.get("available"),
            "passed": performance.get("passed"),
            "score_metric": performance.get("score_metric"),
            "score_direction": performance.get("score_direction"),
            "reference_score_value": performance.get("reference_score_value"),
            "candidate_score_value": performance.get("candidate_score_value"),
            "speedup": performance.get("speedup"),
            "reference_end_to_end_ms": performance.get("reference_end_to_end_ms"),
            "candidate_end_to_end_ms": performance.get("candidate_end_to_end_ms"),
        },
        "artifact_paths": artifact_paths,
        "bottleneck_region": _dict_value(report.get("bottleneck_region")),
        "candidate_modifications": report.get("candidate_modifications", []),
        "evidence": report.get("evidence", []),
    }


def _correctness(reference_run: dict[str, Any], candidate_run: dict[str, Any]) -> dict[str, Any]:
    reference_status = _correctness_status(reference_run)
    candidate_status = _correctness_status(candidate_run)
    return {
        "passed": reference_status == "PASS" and candidate_status == "PASS",
        "reference_status": reference_status,
        "candidate_status": candidate_status,
        "reference_returncode": reference_run.get("returncode"),
        "candidate_returncode": candidate_run.get("returncode"),
    }


def _correctness_status(run: dict[str, Any]) -> str:
    if run.get("returncode") != 0:
        return "ERROR"
    metrics = _dict_value(run.get("metrics"))
    if metrics.get("accepted") is False:
        return "FAIL"
    raw = str(metrics.get("correctness", "UNKNOWN")).strip().upper()
    if raw in {"FAIL", "FAILED", "FALSE", "ERROR", "CRASH"}:
        return "FAIL"
    if raw in {"PASS", "PASSED", "TRUE", "OK"}:
        return "PASS"
    return raw or "UNKNOWN"


def _numerical_error(
    reference_metrics: dict[str, Any],
    candidate_metrics: dict[str, Any],
    *,
    atol: float,
    rtol: float,
) -> dict[str, Any]:
    abs_keys = (
        "max_abs_error",
        "max_absolute_error",
        "max_abs_diff",
        "numerical_error",
        "numeric_error",
    )
    rel_keys = ("max_rel_error", "max_relative_error", "relative_error")
    reference_payload = _output_payload(reference_metrics)
    candidate_payload = _output_payload(candidate_metrics)
    # Actual outputs take precedence over an evaluator's error summary.
    if reference_payload is not None or candidate_payload is not None:
        if reference_payload is None or candidate_payload is None:
            return _missing_numerical_evidence(atol, rtol)
        reference_key, reference_value = reference_payload
        candidate_key, candidate_value = candidate_payload
        diff = _payload_diff(reference_value, candidate_value, atol=atol, rtol=rtol)
        return {
            "available": True,
            **diff,
            "atol": atol,
            "rtol": rtol,
            "source": f"{reference_key}:{candidate_key}",
        }

    # A full tensor comparison can report the largest elementwise tolerance ratio
    # without serializing gigabytes of model outputs. Require matching tolerances.
    if "max_normalized_error" in candidate_metrics:
        ratio = _metric_float(candidate_metrics, "max_normalized_error")
        valid = (ratio is not None and ratio >= 0
                 and _metric_float(candidate_metrics, "numerical_atol") == atol
                 and _metric_float(candidate_metrics, "numerical_rtol") == rtol
                 and type(candidate_metrics.get("compared_elements")) is int
                 and candidate_metrics["compared_elements"] > 0
                 and all(_metric_float(candidate_metrics, key) is not None
                         and _metric_float(candidate_metrics, key) >= 0
                         for key in abs_keys + rel_keys if key in candidate_metrics))
        return {"available": True, "passed": valid and ratio <= 1,
                "max_normalized_error": ratio,
                "max_abs_error": _metric_float(candidate_metrics, "max_abs_error"),
                "max_rel_error": _metric_float(candidate_metrics, "max_rel_error"),
                "atol": atol, "rtol": rtol, "source": "elementwise_tolerance_ratio",
                "reason": None if valid else "invalid_error_metric"}

    supplied = {key: _metric_float(candidate_metrics, key) for key in abs_keys + rel_keys if key in candidate_metrics}
    if supplied:
        valid = all(value is not None and value >= 0 for value in supplied.values())
        max_abs = max((supplied[key] for key in abs_keys if supplied.get(key) is not None), default=None)
        max_rel = max((supplied[key] for key in rel_keys if supplied.get(key) is not None), default=None)
        return {
            "available": True,
            "passed": valid and (
                (max_abs is not None and max_abs <= atol)
                or (max_rel is not None and max_rel <= rtol)
            ),
            "max_abs_error": max_abs,
            "max_rel_error": max_rel,
            "atol": atol,
            "rtol": rtol,
            "source": "candidate_metrics",
            "reason": None if valid else "invalid_error_metric",
        }

    return _missing_numerical_evidence(atol, rtol)


def _missing_numerical_evidence(atol: float, rtol: float) -> dict[str, Any]:
    return {
        "available": False,
        "passed": False,
        "max_abs_error": None,
        "max_rel_error": None,
        "atol": atol,
        "rtol": rtol,
        "source": "not_reported",
    }


def _performance(
    reference_metrics: dict[str, Any],
    candidate_metrics: dict[str, Any],
    *,
    score_metric: str,
    score_direction: str,
) -> dict[str, Any]:
    direction = "lower" if str(score_direction).lower() == "lower" else "higher"
    reference_score_raw = _metric_float(reference_metrics, score_metric)
    candidate_score_raw = _metric_float(candidate_metrics, score_metric)
    reference_score_value = _normalized_score(reference_score_raw, direction)
    candidate_score_value = _normalized_score(candidate_score_raw, direction)
    available = reference_score_value is not None and candidate_score_value is not None
    passed = available and candidate_score_value >= reference_score_value
    return {
        "available": available,
        "passed": passed,
        "score_metric": score_metric,
        "score_direction": direction,
        "reference_score_raw": reference_score_raw,
        "candidate_score_raw": candidate_score_raw,
        "reference_score_value": reference_score_value,
        "candidate_score_value": candidate_score_value,
        "speedup": _speedup(reference_metrics, candidate_metrics, score_metric=score_metric, direction=direction),
        "reference_end_to_end_ms": _metric_float(reference_metrics, "end_to_end_ms"),
        "candidate_end_to_end_ms": _metric_float(candidate_metrics, "end_to_end_ms"),
        "reference_throughput": _metric_float(reference_metrics, "throughput"),
        "candidate_throughput": _metric_float(candidate_metrics, "throughput"),
    }


def _failure_reason(
    *,
    reference_run: dict[str, Any],
    candidate_run: dict[str, Any],
    correctness: dict[str, Any],
    numerical_error: dict[str, Any],
    performance: dict[str, Any],
) -> str | None:
    if reference_run.get("returncode") != 0:
        return "reference_failed"
    if candidate_run.get("returncode") != 0:
        return "candidate_failed"
    if not correctness.get("passed"):
        if "UNKNOWN" in {correctness.get("reference_status"), correctness.get("candidate_status")}:
            return "correctness_evidence_missing"
        return "correctness_failed"
    if not numerical_error.get("passed"):
        if not numerical_error.get("available"):
            return "numerical_evidence_missing"
        if numerical_error.get("reason"):
            return str(numerical_error["reason"])
        return "numerical_error_exceeded"
    if not performance.get("passed"):
        if not performance.get("available"):
            return "performance_evidence_missing_or_invalid"
        return "performance_regressed"
    return None


def _evidence(
    correctness: dict[str, Any],
    numerical_error: dict[str, Any],
    performance: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "kind": "correctness",
            "passed": correctness.get("passed"),
            "reference_status": correctness.get("reference_status"),
            "candidate_status": correctness.get("candidate_status"),
        },
        {
            "kind": "numerical_error",
            "passed": numerical_error.get("passed"),
            "available": numerical_error.get("available"),
            "max_abs_error": numerical_error.get("max_abs_error"),
            "max_rel_error": numerical_error.get("max_rel_error"),
        },
        {
            "kind": "end_to_end_performance",
            "passed": performance.get("passed"),
            "speedup": performance.get("speedup"),
            "reference_score_value": performance.get("reference_score_value"),
            "candidate_score_value": performance.get("candidate_score_value"),
        },
    ]


def _metrics_path(workspace: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return workspace / path


def _read_metrics(path: Path, stdout: str) -> dict[str, Any]:
    if path.exists():
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _output_payload(metrics: dict[str, Any]) -> tuple[str, Any] | None:
    for key in (
        "output_value",
        "output",
        "result",
        "result_value",
        "sample_output",
        "output_checksum",
        "checksum",
    ):
        if key in metrics:
            return key, metrics[key]
    return None


def _payload_diff(reference: Any, candidate: Any, *, atol: float, rtol: float) -> dict[str, Any]:
    errors: list[tuple[float, float]] = []
    reasons: list[str] = []

    def compare(ref: Any, cand: Any) -> bool:
        if ref is None or cand is None:
            reasons.append("invalid_output")
            return False
        if isinstance(ref, bool) or isinstance(cand, bool):
            return type(ref) is type(cand) and ref == cand
        if isinstance(ref, (int, float)) and isinstance(cand, (int, float)):
            ref_value, cand_value = _finite_float(ref), _finite_float(cand)
            if ref_value is None or cand_value is None:
                reasons.append("non_finite_output")
                return False
            abs_error = abs(cand_value - ref_value)
            rel_error = abs_error / max(abs(ref_value), 1e-12)
            if not math.isfinite(abs_error) or not math.isfinite(rel_error):
                reasons.append("non_finite_output_error")
                return False
            errors.append((abs_error, rel_error))
            return abs_error <= atol + rtol * abs(ref_value)
        if type(ref) is not type(cand):
            reasons.append("output_structure_mismatch")
            return False
        if isinstance(ref, dict):
            if ref.keys() != cand.keys():
                reasons.append("output_structure_mismatch")
                return False
            return all([compare(ref[key], cand[key]) for key in ref])
        if isinstance(ref, list):
            if len(ref) != len(cand):
                reasons.append("output_structure_mismatch")
                return False
            return all([compare(a, b) for a, b in zip(ref, cand)])
        return ref == cand

    passed = compare(reference, candidate)
    return {
        "mode": "numeric" if errors else "exact",
        "passed": passed,
        "exact_match": passed and all(absolute == 0 for absolute, _ in errors),
        "max_abs_error": max((absolute for absolute, _ in errors), default=None),
        "max_rel_error": max((relative for _, relative in errors), default=None),
        "reason": reasons[0] if reasons else None,
    }


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _metric_float(metrics: dict[str, Any], key: str) -> float | None:
    return _first_float(metrics, key)


def _first_float(metrics: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = metrics.get(key)
        if value is None:
            continue
        parsed = _finite_float(value)
        if parsed is not None:
            return parsed
    return None


def _normalized_score(raw_score: float | None, direction: str) -> float | None:
    if raw_score is None:
        return None
    return raw_score if direction == "higher" else -raw_score


def _speedup(
    reference_metrics: dict[str, Any],
    candidate_metrics: dict[str, Any],
    *,
    score_metric: str,
    direction: str,
) -> float | None:
    ref_latency = _metric_float(reference_metrics, "end_to_end_ms")
    cand_latency = _metric_float(candidate_metrics, "end_to_end_ms")
    if ref_latency is not None and cand_latency is not None and cand_latency > 0:
        return ref_latency / cand_latency
    ref = _metric_float(reference_metrics, score_metric)
    cand = _metric_float(candidate_metrics, score_metric)
    if ref is None or cand is None:
        return None
    if direction == "higher" and ref != 0:
        return cand / ref
    if direction == "lower" and cand != 0:
        return ref / cand
    return None


def _decode_process_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
