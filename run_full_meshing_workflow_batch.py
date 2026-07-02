"""Run the HyperMesh meshing workflow in hmbatch/background mode.

This runner shares the meshing and classification logic from
``hypermesh_mcp_server.py``. Unlike ``run_full_meshing_workflow.py``, it does
not require a visible HyperMesh GUI listener: every phase is executed by
``hmbatch.exe`` and the working model is saved between phases.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import hypermesh_mcp_server as hm
import run_full_meshing_workflow as visible_runner


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


ROOT = Path(__file__).resolve().parent
RUNS_DIR = ROOT / "runs"
OUTPUTS_DIR = ROOT / "outputs"


def _now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _log(message: str) -> None:
    print(message, flush=True)


def _tcl_path(path: Path | str) -> str:
    return str(path).replace("\\", "/").replace('"', '\\"')


def _response_text(response: dict[str, Any]) -> str:
    return str(response.get("stdout", "") or "") + "\n" + str(response.get("stderr", "") or "")


def _batch_safe_script(script: str) -> str:
    lines: list[str] = []
    for line in str(script or "").splitlines():
        stripped = line.lstrip()
        if stripped.startswith("*writefile"):
            indent = line[: len(line) - len(stripped)]
            lines.append(f"{indent}catch {{hm_answernext yes}}")
            lines.append(
                f'{indent}if {{[catch {{{stripped}}} _mcp_write_err]}} '
                f'{{puts "MCP_BATCH_WRITEFILE_ERROR error=$_mcp_write_err"; error $_mcp_write_err}} '
                f'else {{puts "MCP_BATCH_WRITEFILE_OK"}}'
            )
            continue
        lines.append(line)
    return "\n".join(lines) + ("\n" if str(script or "").endswith("\n") else "")


def _execute_batch(
    script: str,
    *,
    model_path: Path | None,
    hmbatch_path: str | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    start = time.time()
    result = hm.execute_tcl(
        _batch_safe_script(script),
        hmbatch_path=hmbatch_path,
        model_path=str(model_path) if model_path else None,
        timeout_seconds=timeout_seconds,
        enforce_meshing_rules=False,
    )
    result["elapsed_seconds"] = round(time.time() - start, 2)
    result["response"] = _response_text(result)
    return result


def _write_batch_log(path: Path, response: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    command = response.get("command")
    lines = []
    if command:
        lines.append("COMMAND: " + " ".join(str(part) for part in command))
    lines.append(str(response.get("stdout", "") or ""))
    stderr = str(response.get("stderr", "") or "")
    if stderr:
        lines.append("\nSTDERR:\n" + stderr)
    path.write_text("\n".join(lines), encoding="utf-8", errors="replace")


def _write_popup_report(
    path: Path,
    summary: dict[str, Any],
    repair_summary: dict[str, Any],
) -> str:
    errors = summary.get("errors") or []
    probe_skipped = (summary.get("steps", {}).get("probe", {}) or {}).get("skipped_solids", []) or []
    quality_errors = [item for item in errors if item.get("step") == "tetra_quality"]
    by_solid: dict[int, dict[str, Any]] = {}
    for item in quality_errors:
        for solid in item.get("solids") or []:
            try:
                sid = int(solid.get("solid_id"))
            except (TypeError, ValueError):
                continue
            merged = by_solid.setdefault(sid, {"solid_id": sid})
            merged.update(solid)
    rolled_back = [by_solid[sid] for sid in sorted(by_solid)]
    sections: list[str] = []
    if probe_skipped:
        lines = [
            "后台探针跳过提示",
            "",
            f"有 {len(probe_skipped)} 个实体在探针阶段失败，后续已自动跳过划分：",
        ]
        for item in probe_skipped:
            lines.append(
                f"- solid {item.get('solid_id', '')} {item.get('component_name', '')}: "
                f"{item.get('reason', item.get('status', 'probe failed'))}"
            )
        sections.append("\n".join(lines))
    if rolled_back:
        sections.append(visible_runner._rolled_back_popup_message(rolled_back, repair_summary))
    if sections:
        text = "\n\n".join(sections)
    else:
        text = "网格退回提醒\n\n没有实体退回到 2D 面网格。"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8", errors="replace")
    return str(path)


def _append_save(script: str, working_model: Path) -> str:
    return script.rstrip() + f'\n*writefile "{_tcl_path(working_model)}" 1\n'


def _needs_ascii_stage(path: Path) -> bool:
    try:
        str(path).encode("ascii")
        return False
    except UnicodeEncodeError:
        return True


def _stage_cad_input_for_hmbatch(input_path: Path, stamp: str) -> Path:
    if not _needs_ascii_stage(input_path):
        return input_path
    staged = RUNS_DIR / f"batch_input_{stamp}{input_path.suffix.lower()}"
    staged.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(input_path, staged)
    _log(f"  - staged CAD input for hmbatch: {staged}")
    return staged


def _prepare_working_model(args: argparse.Namespace, stamp: str) -> tuple[Path, dict[str, Any]]:
    input_path = Path(args.input).resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input model was not found: {input_path}")
    working_model = RUNS_DIR / f"batch_working_{stamp}.hm"
    ext = input_path.suffix.lower()
    response: dict[str, Any]
    if ext == ".hm":
        script = f"""
puts "MCP_BATCH_OPEN_BEGIN path={_tcl_path(input_path)}"
*writefile "{_tcl_path(working_model)}" 1
puts "MCP_BATCH_OPEN_DONE output={_tcl_path(working_model)}"
""".lstrip()
        response = _execute_batch(
            script,
            model_path=input_path,
            hmbatch_path=args.hmbatch,
            timeout_seconds=args.import_timeout,
        )
    elif ext in {".stp", ".step"}:
        import_path = _stage_cad_input_for_hmbatch(input_path, stamp)
        script = f"""
puts "MCP_BATCH_IMPORT_BEGIN path={_tcl_path(import_path)} original={_tcl_path(input_path)}"
set mcp_batch_import_path [file normalize "{_tcl_path(import_path)}"]
if {{[catch {{*feinputwithdata2 "#Detect" $mcp_batch_import_path 1 0 -0.01 0 0 1 0 1 0}} err]}} {{
    puts "MCP_BATCH_IMPORT_ERROR $err"
    error $err
}}
*createmark solids 1 all
set mcp_batch_solid_count [llength [hm_getmark solids 1]]
*createmark surfs 1 all
set mcp_batch_surf_count [llength [hm_getmark surfs 1]]
puts "MCP_BATCH_IMPORT_COUNTS solids=$mcp_batch_solid_count surfs=$mcp_batch_surf_count"
*writefile "{_tcl_path(working_model)}" 1
puts "MCP_BATCH_IMPORT_DONE output={_tcl_path(working_model)}"
""".lstrip()
        response = _execute_batch(
            script,
            model_path=None,
            hmbatch_path=args.hmbatch,
            timeout_seconds=args.import_timeout,
        )
    else:
        raise ValueError("Background runner currently accepts .hm, .stp, and .step files.")

    response["output_hm_path"] = str(working_model)
    response["output_exists"] = working_model.exists()
    response_log_path = RUNS_DIR / f"workflow_batch_import_response_{stamp}.log"
    _write_batch_log(response_log_path, response)
    response["log_path"] = str(response_log_path)
    visible_runner._write_json_if_enabled(args.write_json, RUNS_DIR / f"workflow_batch_import_response_{stamp}.json", response)
    if not response.get("success") or not working_model.exists():
        raise RuntimeError("Failed to open/import input model in hmbatch. See batch import log.")
    return working_model, response


def _probe_script_for_hmbatch(generated: dict[str, Any]) -> str:
    probe_script = str(generated["script"]).rstrip()
    probe_return = "return $::mcp_probe_output"
    if probe_script.endswith(probe_return):
        return probe_script[: -len(probe_return)].rstrip() + "\nputs $::mcp_probe_output\n"
    return probe_script + "\ncatch {puts $::mcp_probe_output}\n"


def _probe_targets(
    *,
    args: argparse.Namespace,
    working_model: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    script = """
*createmark solids 1 all
set solids [lsort -integer [hm_getmark solids 1]]
puts "MCP_PROBE_TARGET_BEGIN count=[llength $solids]"
foreach sid $solids {
    set surf_count 0
    set comp_name ""
    catch {
        *createmark surfs 1 "by solids" $sid
        set surf_count [llength [hm_getmark surfs 1]]
    }
    catch {
        *createmark comps 1 "by solids" $sid
        set cids [hm_getmark comps 1]
        if {[llength $cids] > 0} {
            set cid [lindex $cids 0]
            set comp_name [hm_getvalue comps id=$cid dataname=name]
        }
    }
    set comp_name [string map {"\\n" " " "\\r" " " "\\t" " "} $comp_name]
    puts "MCP_PROBE_TARGET solid=$sid surf_count=$surf_count comp=$comp_name"
}
puts "MCP_PROBE_TARGET_END"
""".lstrip()
    response = _execute_batch(
        script,
        model_path=working_model,
        hmbatch_path=args.hmbatch,
        timeout_seconds=min(max(30, int(args.probe_timeout)), 180),
    )
    targets: list[dict[str, Any]] = []
    for line in _response_text(response).splitlines():
        match = re.match(r"^MCP_PROBE_TARGET solid=(\d+) surf_count=(\d+) comp=(.*)$", line.strip())
        if not match:
            continue
        targets.append(
            {
                "solid_id": int(match.group(1)),
                "surf_count": int(match.group(2)),
                "component_name": match.group(3).strip(),
            }
        )
    return targets, response


def _probe_solid_has_usable_data(lines: list[str], solid_id: int) -> bool:
    prefix = f"MCP_PROBE_SOLID id={int(solid_id)} "
    for line in lines:
        if not line.startswith(prefix):
            continue
        facts = _probe_line_facts(line)
        return facts.get("exists") != "0" and facts.get("bbox_ok") == "1"
    return False


def _probe_line_facts(line: str) -> dict[str, str]:
    return {
        token.split("=", 1)[0]: token.split("=", 1)[1]
        for token in line.split()
        if "=" in token
    }


def _skipped_solids_from_probe_lines(
    lines: list[str],
    targets: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    target_by_id = {int(item["solid_id"]): item for item in (targets or [])}
    skipped: list[dict[str, Any]] = []
    for line in lines:
        if not line.startswith("MCP_PROBE_SOLID"):
            continue
        facts = _probe_line_facts(line)
        try:
            sid = int(facts.get("id", "0"))
        except ValueError:
            continue
        if sid <= 0 or (facts.get("exists") != "0" and facts.get("bbox_ok") == "1"):
            continue
        target = target_by_id.get(sid, {})
        reason = "probe returned no usable bbox data"
        if "mesh_error" in facts:
            reason = "probe temporary surface mesh failed"
        elif facts.get("exists") == "0":
            reason = "solid no longer exists during probe"
        skipped.append(
            {
                "solid_id": sid,
                "component_name": target.get("component_name", ""),
                "surf_count": int(facts.get("surf_count", target.get("surf_count", 0)) or 0),
                "status": "probe_failed_skip_meshing",
                "reason": reason,
            }
        )
    return skipped


def _probe_failure_reason(response: dict[str, Any], lines: list[str], solid_id: int) -> str:
    prefix = f"MCP_PROBE_SOLID id={int(solid_id)} "
    for line in lines:
        if line.startswith(prefix) and "mesh_error=" in line:
            return "probe temporary surface mesh failed"
    text = _response_text(response)
    if "All surfaces failed to mesh" in text:
        return "All surfaces failed to mesh"
    if response.get("timeout"):
        return "probe timeout"
    if not response.get("success"):
        return "hmbatch probe command failed"
    return "probe returned no usable bbox data"


def _probe_model(args: argparse.Namespace, working_model: Path, stamp: str) -> tuple[str, dict[str, Any], Path]:
    generated = hm.generate_geometry_probe_tcl(
        probe_element_size=args.probe_element_size,
        min_element_size=args.probe_min_element_size,
        max_deviation=args.probe_max_deviation,
        max_feature_angle=args.probe_feature_angle,
        growth_rate=args.probe_growth_rate,
    )
    probe_script = _probe_script_for_hmbatch(generated)
    response = _execute_batch(
        probe_script,
        model_path=working_model,
        hmbatch_path=args.hmbatch,
        timeout_seconds=args.probe_timeout,
    )
    response_log_path = RUNS_DIR / f"workflow_probe_response_{stamp}.log"
    _write_batch_log(response_log_path, response)
    response["log_path"] = str(response_log_path)
    probe_text = "\n".join(hm._extract_probe_lines(_response_text(response)))
    probe_path = RUNS_DIR / f"workflow_probe_{stamp}.txt"
    if response.get("success") and probe_text.strip():
        probe_lines = probe_text.splitlines()
        skipped_solids = _skipped_solids_from_probe_lines(probe_lines)
        if skipped_solids:
            targets, target_response = _probe_targets(args=args, working_model=working_model)
            skipped_solids = _skipped_solids_from_probe_lines(probe_lines, targets)
            response["target_response"] = target_response
        response["probe_mode"] = "all_solids"
        response["skipped_solids"] = skipped_solids
        response["skipped_count"] = len(skipped_solids)
        probe_path.write_text(probe_text, encoding="utf-8")
        return probe_text, response, probe_path

    _log("  - full-model probe failed; retrying probe per solid and skipping failed solids")
    targets, target_response = _probe_targets(args=args, working_model=working_model)
    if not targets:
        probe_path.write_text(probe_text, encoding="utf-8")
        response["probe_mode"] = "all_solids_failed"
        response["target_response"] = target_response
        raise RuntimeError("Geometry probe failed and solid list could not be read.")

    aggregate_lines = [f"MCP_PROBE_BEGIN solid_count={len(targets)} probe_size={args.probe_element_size} mode=per_solid_skip_failed"]
    skipped_solids: list[dict[str, Any]] = []
    completed_solids: list[int] = []
    per_solid_logs: list[dict[str, Any]] = []
    for index, target in enumerate(targets, start=1):
        sid = int(target["solid_id"])
        _log(f"    probe solid {index}/{len(targets)} id={sid}")
        single = hm.generate_geometry_probe_tcl(
            solid_ids=[sid],
            probe_element_size=args.probe_element_size,
            min_element_size=args.probe_min_element_size,
            max_deviation=args.probe_max_deviation,
            max_feature_angle=args.probe_feature_angle,
            growth_rate=args.probe_growth_rate,
        )
        single_response = _execute_batch(
            _probe_script_for_hmbatch(single),
            model_path=working_model,
            hmbatch_path=args.hmbatch,
            timeout_seconds=args.probe_timeout,
        )
        single_lines = [
            line for line in hm._extract_probe_lines(_response_text(single_response))
            if line.startswith("MCP_PROBE_SOLID")
        ]
        if single_response.get("success") and _probe_solid_has_usable_data(single_lines, sid):
            aggregate_lines.extend(single_lines)
            completed_solids.append(sid)
        else:
            reason = _probe_failure_reason(single_response, single_lines, sid)
            skipped_solids.append(
                {
                    "solid_id": sid,
                    "component_name": target.get("component_name", ""),
                    "surf_count": target.get("surf_count", 0),
                    "status": "probe_failed_skip_meshing",
                    "reason": reason,
                }
            )
            aggregate_lines.append(
                "MCP_PROBE_SOLID "
                f"id={sid} exists=1 surf_count={int(target.get('surf_count') or 0)} "
                "elem_count=0 node_count=0 bbox_ok=0 "
                f"mesh_error={{{reason}}}"
            )
            _log(f"      skipped solid {sid}: {reason}")
        per_solid_logs.append(
            {
                "solid_id": sid,
                "success": bool(single_response.get("success")),
                "returncode": single_response.get("returncode"),
                "timeout": bool(single_response.get("timeout")),
                "line_count": len(single_lines),
                "stdout_tail": str(single_response.get("stdout", ""))[-1000:],
                "stderr_tail": str(single_response.get("stderr", ""))[-1000:],
            }
        )
    aggregate_lines.append("MCP_PROBE_END")
    probe_text = "\n".join(aggregate_lines)
    probe_path.write_text(probe_text, encoding="utf-8")

    response = {
        "success": bool(completed_solids),
        "probe_mode": "per_solid_skip_failed",
        "fallback_from_full_probe": True,
        "full_probe_response": response,
        "target_response": target_response,
        "completed_solids": completed_solids,
        "skipped_solids": skipped_solids,
        "skipped_count": len(skipped_solids),
        "solid_count": len(targets),
        "per_solid_logs": per_solid_logs,
        "log_path": str(response_log_path),
        "stdout": probe_text,
        "stderr": "",
        "response": probe_text,
    }
    _write_batch_log(response_log_path, response)
    if not completed_solids:
        raise RuntimeError("Geometry probe failed for every solid; no solids can be meshed.")
    return probe_text, response, probe_path


def _final_save_and_count(
    *,
    args: argparse.Namespace,
    working_model: Path,
    output_path: Path,
) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    script = f"""
puts "MCP_FINAL_SAVE_BEGIN"
*writefile "{_tcl_path(output_path)}" 1
puts "MCP_FINAL_SAVE_DONE path={_tcl_path(output_path)}"
*createmark elems 1 all
set all_elems [hm_getmark elems 1]
set shell_count 0
set tet4_count 0
set hex8_count 0
set other_count 0
foreach e $all_elems {{
    set cfg [hm_getvalue elems id=$e dataname=config]
    if {{$cfg == 103 || $cfg == 104 || $cfg == 106 || $cfg == 108}} {{incr shell_count}}
    if {{$cfg == 204}} {{incr tet4_count}}
    if {{$cfg == 208}} {{incr hex8_count}}
    if {{!($cfg == 103 || $cfg == 104 || $cfg == 106 || $cfg == 108 || $cfg == 204 || $cfg == 208)}} {{incr other_count}}
}}
puts "MCP_FINAL_COUNTS elems=[llength $all_elems] shells=$shell_count tet4=$tet4_count hex8=$hex8_count other=$other_count"
""".lstrip()
    result = _execute_batch(
        script,
        model_path=working_model,
        hmbatch_path=args.hmbatch,
        timeout_seconds=args.save_timeout,
    )
    result["output_hm_path"] = str(output_path)
    result["output_exists"] = output_path.exists()
    return result


def _stop_requested(args: argparse.Namespace) -> bool:
    stop_file = getattr(args, "stop_file", None)
    return bool(stop_file and Path(stop_file).exists())


def _write_stopped_workflow_summary(
    *,
    args: argparse.Namespace,
    workflow: dict[str, Any],
    stamp: str,
    stage: str,
    working_model: Path,
    output_path: Path,
    report_path: Path,
    diagnostic_log_path: Path,
    repair_summary: dict[str, Any] | None = None,
    probe_path: Path | None = None,
    gear_diag_path: Path | None = None,
) -> dict[str, Any]:
    _log(f"STOP requested at stage={stage}; saving current working model before exit.")
    workflow["stopped"] = True
    workflow["stop_stage"] = stage
    workflow["warnings"].append(f"User requested stop at {stage}; saved current working model and exited.")
    workflow["errors"].append({"step": "user_stop", "status": "stopped_after_current_stage", "stage": stage})

    final_save = _final_save_and_count(args=args, working_model=working_model, output_path=output_path)
    workflow["steps"]["final_save"] = final_save
    visible_runner._write_json_if_enabled(args.write_json, RUNS_DIR / f"workflow_final_save_response_{stamp}.json", final_save)
    workflow["success"] = False

    if repair_summary is None:
        repair_summary = {
            "repair_aggregate": {},
            "repair_by_solid": {},
            "tetra_attempted_count": 0,
            "tetra_done_count": 0,
            "tetra_failed_count": 0,
            "tetra_tet4_total": 0,
        }

    report_data = {
        "stamp": stamp,
        "success": False,
        "output_hm_path": str(output_path),
        "classification": workflow["steps"].get("classification", {}),
        "drag_count": int(workflow["steps"].get("drag_hex", {}).get("count") or 0),
        "drag": workflow["steps"].get("drag_hex", {}),
        "spin": workflow["steps"].get("spin_hex", {}),
        "tetra": workflow["steps"].get("tetra", {}),
        "final_save": final_save,
        "repair_summary": repair_summary,
        "errors": workflow["errors"],
        "warnings": workflow["warnings"],
        "parameters": vars(args),
        "part_parameters": [],
        "skipped_probe": workflow["steps"].get("probe", {}),
        "skipped_existing_mesh": workflow["steps"].get("existing_mesh_skip", {}),
        "generated_files": {
            "探测结果": str(probe_path) if probe_path else "",
            "中文报告": str(report_path),
            "最终模型": str(output_path),
            "实时诊断日志": str(diagnostic_log_path),
            "齿轮齿面识别诊断": str(gear_diag_path) if gear_diag_path else "",
        },
    }
    report = hm.write_chinese_meshing_workflow_report(report_data, output_path=str(report_path))
    workflow["report_path"] = report.get("report_path")
    workflow["popup_report_path"] = _write_popup_report(
        RUNS_DIR / f"workflow_popup_summary_{stamp}.txt",
        workflow,
        repair_summary,
    )
    visible_runner._write_json_if_enabled(args.write_json, RUNS_DIR / f"workflow_summary_{stamp}.json", workflow)
    visible_runner._write_json_if_enabled(args.write_json, RUNS_DIR / "workflow_latest_summary.json", workflow)
    visible_runner._diag(diagnostic_log_path, "batch_workflow_stopped", stage=stage, workflow=workflow)
    _log(f"Stopped after current stage: {stage}")
    _log(f"Chinese report: {workflow.get('report_path')}")
    return workflow


def _detect_existing_mesh_by_solid_batch(
    results: dict[str, dict[str, Any]],
    *,
    working_model: Path,
    hmbatch_path: str | None,
    timeout_seconds: int,
) -> dict[str, dict[str, Any]]:
    script = visible_runner._generate_existing_mesh_probe_tcl(results)
    if not script:
        return {}
    response = _execute_batch(
        script,
        model_path=working_model,
        hmbatch_path=hmbatch_path,
        timeout_seconds=timeout_seconds,
    )
    return visible_runner._parse_existing_mesh_probe(_response_text(response))


def run_workflow(args: argparse.Namespace) -> dict[str, Any]:
    stamp = args.stamp or _now_stamp()
    RUNS_DIR.mkdir(exist_ok=True)
    OUTPUTS_DIR.mkdir(exist_ok=True)
    output_path = Path(args.output).resolve() if args.output else OUTPUTS_DIR / f"full_mesh_batch_{stamp}.hm"
    report_path = RUNS_DIR / f"workflow_report_{stamp}.txt"
    diagnostic_log_path = RUNS_DIR / f"workflow_diagnostics_{stamp}.jsonl"

    workflow: dict[str, Any] = {
        "stamp": stamp,
        "mode": "hmbatch",
        "input_model_path": str(Path(args.input).resolve()),
        "output_hm_path": str(output_path),
        "continue_on_error": args.continue_on_error,
        "steps": {},
        "errors": [],
        "warnings": [],
        "diagnostic_log_path": str(diagnostic_log_path),
    }
    visible_runner._diag(diagnostic_log_path, "batch_workflow_start", workflow=workflow)

    _log(f"[0/6] Open/import model in hmbatch: {args.input}")
    working_model, import_response = _prepare_working_model(args, stamp)
    workflow["working_model_path"] = str(working_model)
    workflow["steps"]["import"] = import_response
    visible_runner._write_json_if_enabled(args.write_json, RUNS_DIR / f"workflow_batch_import_response_{stamp}.json", import_response)
    if _stop_requested(args):
        return _write_stopped_workflow_summary(
            args=args,
            workflow=workflow,
            stamp=stamp,
            stage="import",
            working_model=working_model,
            output_path=output_path,
            report_path=report_path,
            diagnostic_log_path=diagnostic_log_path,
        )

    _log("[1/6] Probe model")
    probe_text, probe_response, probe_path = _probe_model(args, working_model, stamp)
    workflow["steps"]["probe"] = {
        "success": probe_response.get("success"),
        "output_file": str(probe_path),
        "line_count": len(probe_text.splitlines()),
        "probe_mode": probe_response.get("probe_mode", "all_solids"),
        "skipped_count": int(probe_response.get("skipped_count") or 0),
        "skipped_solids": probe_response.get("skipped_solids", []) or [],
        "response": probe_response,
    }
    if workflow["steps"]["probe"]["skipped_solids"]:
        workflow["errors"].append(
            {
                "step": "probe",
                "status": "probe_failed_skip_meshing",
                "solid_count": workflow["steps"]["probe"]["skipped_count"],
                "solids": workflow["steps"]["probe"]["skipped_solids"],
            }
        )
        workflow["warnings"].append(
            f"probe skipped {workflow['steps']['probe']['skipped_count']} solid(s); they will not be meshed."
        )
    visible_runner._write_json_if_enabled(args.write_json, RUNS_DIR / f"workflow_probe_response_{stamp}.json", probe_response)
    if _stop_requested(args):
        return _write_stopped_workflow_summary(
            args=args,
            workflow=workflow,
            stamp=stamp,
            stage="probe",
            working_model=working_model,
            output_path=output_path,
            report_path=report_path,
            diagnostic_log_path=diagnostic_log_path,
            probe_path=probe_path,
        )

    _log("[2/6] Classify solids and run Phase 2")
    classification = hm.classify_all_solids_from_probe(
        probe_text,
        use_gear_tooth_refinement=args.use_gear_tooth_refinement,
    )
    visible_runner._write_json_if_enabled(args.write_json, RUNS_DIR / f"workflow_classification_{stamp}.json", classification)
    gear_diag_path = RUNS_DIR / f"gear_tooth_recognition_{stamp}.txt"
    gear_diag = hm.write_gear_tooth_recognition_report(
        classification_results=classification,
        output_path=str(gear_diag_path),
    )
    workflow["steps"]["gear_tooth_recognition"] = gear_diag
    workflow["steps"]["classification"] = {
        "success": classification.get("success"),
        "total_solids": classification.get("total_solids"),
        "strategy_counts": classification.get("strategy_counts"),
        "probe_skipped_count": workflow["steps"]["probe"].get("skipped_count", 0),
    }
    if not classification.get("success"):
        raise RuntimeError("Classification failed.")
    results = classification["results"]
    existing_mesh = _detect_existing_mesh_by_solid_batch(
        results,
        working_model=working_model,
        hmbatch_path=args.hmbatch,
        timeout_seconds=args.phase2_timeout,
    )
    skipped_existing_mesh = {
        sid: item for sid, item in existing_mesh.items()
        if item.get("has_existing_mesh")
    }
    for sid, item in skipped_existing_mesh.items():
        if sid in results:
            results[sid]["skip_meshing"] = True
            results[sid]["skip_reason"] = "已有网格，后台模式自动跳过划分"
            results[sid]["existing_element_count"] = item.get("element_count", 0)
    active_results = {sid: item for sid, item in results.items() if not item.get("skip_meshing")}
    workflow["steps"]["existing_mesh_skip"] = {
        "skipped_count": len(skipped_existing_mesh),
        "skipped_solids": [
            {
                "solid_id": int(sid),
                "component_name": results.get(sid, {}).get("component_name") or item.get("component_name", ""),
                "element_count": item.get("element_count", 0),
                "detection_mode": item.get("detection_mode", ""),
                "component_element_count": item.get("component_element_count", 0),
                "solid_element_count": item.get("solid_element_count", 0),
            }
            for sid, item in sorted(skipped_existing_mesh.items(), key=lambda pair: int(pair[0]))
        ],
        "solid_count": len(results),
    }
    workflow["steps"]["classification"]["skipped_existing_mesh_count"] = len(skipped_existing_mesh)
    if skipped_existing_mesh:
        skipped_text = ", ".join(
            f"{sid}({item.get('element_count', 0)} elems)"
            for sid, item in sorted(skipped_existing_mesh.items(), key=lambda pair: int(pair[0]))
        )
        _log(f"  - skipped existing-mesh solids: {skipped_text}")
    if _stop_requested(args):
        return _write_stopped_workflow_summary(
            args=args,
            workflow=workflow,
            stamp=stamp,
            stage="classification",
            working_model=working_model,
            output_path=output_path,
            report_path=report_path,
            diagnostic_log_path=diagnostic_log_path,
            probe_path=probe_path,
            gear_diag_path=gear_diag_path,
        )

    phase2_response = _execute_batch(
        _append_save(classification["phase2_finalize_script"], working_model),
        model_path=working_model,
        hmbatch_path=args.hmbatch,
        timeout_seconds=args.phase2_timeout,
    )
    phase2_log_path = RUNS_DIR / f"workflow_phase2_response_{stamp}.log"
    _write_batch_log(phase2_log_path, phase2_response)
    phase2_response["log_path"] = str(phase2_log_path)
    workflow["steps"]["phase2_finalize"] = phase2_response
    visible_runner._write_json_if_enabled(args.write_json, RUNS_DIR / f"workflow_phase2_response_{stamp}.json", phase2_response)
    if not phase2_response.get("success"):
        raise RuntimeError("Phase 2 finalization failed.")
    if _stop_requested(args):
        return _write_stopped_workflow_summary(
            args=args,
            workflow=workflow,
            stamp=stamp,
            stage="phase2_finalize",
            working_model=working_model,
            output_path=output_path,
            report_path=report_path,
            diagnostic_log_path=diagnostic_log_path,
            probe_path=probe_path,
            gear_diag_path=gear_diag_path,
        )

    _log("[3/6] Run drag-hex solids")
    drag_solids = visible_runner._build_drag_solids(active_results)
    drag_step: dict[str, Any] = {"count": len(drag_solids), "success": True}
    drag_response: dict[str, Any] | None = None
    if drag_solids:
        drag = hm.generate_batched_drag_hex_tcl(
            solids=drag_solids,
            output_hm_path=str(working_model),
            element_size=args.drag_element_size,
            element_size_min=args.drag_element_size_min,
            element_size_max=args.drag_element_size_max,
            fit_tolerance_ratio=args.drag_fit_tolerance_ratio,
            retry_count=args.drag_retry_count,
            drag_aspect_guard=args.drag_aspect_guard,
            drag_aspect_threshold=args.drag_aspect_threshold,
            drag_min_layers=args.drag_min_layers,
            pause_seconds_after_each_solid=args.drag_pause_seconds,
            checkpoint_every_n_solids=0,
        )
        drag_script_path = RUNS_DIR / f"workflow_drag_hex_{stamp}.tcl"
        drag_log_path = RUNS_DIR / f"workflow_drag_hex_{stamp}.log"
        drag_script_path.write_text(drag["script"], encoding="utf-8")
        drag_response = _execute_batch(
            drag["script"],
            model_path=working_model,
            hmbatch_path=args.hmbatch,
            timeout_seconds=args.drag_timeout,
        )
        _write_batch_log(drag_log_path, drag_response)
        drag_response["log_path"] = str(drag_log_path)
        drag_step.update(
            {
                "success": drag_response.get("success"),
                "script_path": str(drag_script_path),
                "log_path": str(drag_log_path),
                "one_layer_fallback": visible_runner._drag_one_layer_fallbacks(drag_solids, drag_response),
            }
        )
        drag_fallback_to_tetra = visible_runner._promote_drag_failures_to_tetra(
            drag_solids=drag_solids,
            drag_response=drag_response,
            active_results=active_results,
            results=results,
            trust_successful_results=bool(drag_response.get("success")),
        )
        drag_step["fallback_to_tetra"] = drag_fallback_to_tetra
        if drag_fallback_to_tetra:
            workflow["warnings"].append(
                "drag hex failed for some solids; they were promoted to later tetra batches."
            )
        visible_runner._write_json_if_enabled(args.write_json, RUNS_DIR / f"workflow_drag_hex_response_{stamp}.json", drag_response)
        if not drag_response.get("success"):
            workflow["errors"].append({"step": "drag_hex", "response": drag_response})
            if not args.continue_on_error:
                raise RuntimeError("Drag-hex step failed.")
    workflow["steps"]["drag_hex"] = drag_step
    if _stop_requested(args):
        return _write_stopped_workflow_summary(
            args=args,
            workflow=workflow,
            stamp=stamp,
            stage="drag_hex",
            working_model=working_model,
            output_path=output_path,
            report_path=report_path,
            diagnostic_log_path=diagnostic_log_path,
            probe_path=probe_path,
            gear_diag_path=gear_diag_path,
        )

    _log("[3.5/6] Run spin-hex solids")
    spin_solids = visible_runner._build_spin_solids(active_results)
    spin_step: dict[str, Any] = {"count": len(spin_solids), "completed": [], "fallback_to_tetra": [], "failed": []}
    spin_responses: dict[str, dict[str, Any]] = {}
    for spin_solid in spin_solids:
        sid = int(spin_solid["solid_id"])
        try:
            spin_size = float(args.spin_element_size or spin_solid.get("element_size") or args.drag_element_size)
            axis_point = spin_solid.get("spin_axis_point")
            split_normal = spin_solid.get("spin_split_plane_normal")
            split_point = spin_solid.get("spin_split_plane_point")
            if not axis_point or not split_normal or not split_point:
                raise RuntimeError(f"Spin solid {sid} is missing cut-section spin parameters.")
            spin = hm.generate_cutsection_spin_hex_tcl(
                solid_id=sid,
                component_name=str(spin_solid["component_name"]),
                split_plane_normal=list(split_normal),
                split_plane_point=list(split_point),
                spin_axis=str(spin_solid.get("spin_axis") or spin_solid.get("axis") or "z"),
                spin_axis_vector=spin_solid.get("spin_axis_vector"),
                spin_axis_point=list(axis_point),
                element_size=spin_size,
                density=args.spin_density_max,
                density_min=args.spin_density_min,
                density_max=args.spin_density_max,
                section_element_size_min=args.spin_section_element_size_min,
                section_element_size_max=args.spin_section_element_size_max,
                retry_count=args.spin_retry_count,
                section_edge_seed_counts=[100, 75],
                delete_existing_component_elements=True,
                output_hm_path=str(working_model),
            )
            spin_script_path = RUNS_DIR / f"workflow_spin_hex_s{sid}_{stamp}.tcl"
            spin_log_path = RUNS_DIR / f"workflow_spin_hex_s{sid}_{stamp}.log"
            spin_script_path.write_text(spin["script"], encoding="utf-8")
            spin_response = _execute_batch(
                spin["script"],
                model_path=working_model,
                hmbatch_path=args.hmbatch,
                timeout_seconds=args.spin_timeout,
            )
            _write_batch_log(spin_log_path, spin_response)
            spin_response["script_path"] = str(spin_script_path)
            spin_response["log_path"] = str(spin_log_path)
            parsed = visible_runner._parse_spin_results(_response_text(spin_response)).get(str(sid), {})
            spin_response["parsed_result"] = parsed
            spin_responses[str(sid)] = spin_response
            spun_3d_count = int(parsed.get("total3d") or parsed.get("hex8") or 0)
            if spin_response.get("success") and int(parsed.get("ok") or 0) == 1 and spun_3d_count > 0:
                spin_step["completed"].append({"solid_id": sid, **parsed, "script_path": str(spin_script_path), "log_path": str(spin_log_path)})
            else:
                fallback_ids = visible_runner._promote_spin_fallback_solids_to_tetra(
                    parsed=parsed,
                    spin_solid=spin_solid,
                    active_results=active_results,
                    results=results,
                )
                spin_step["fallback_to_tetra"].append(
                    {"solid_id": sid, **parsed, "fallback_solid_ids": fallback_ids, "script_path": str(spin_script_path), "log_path": str(spin_log_path)}
                )
        except Exception as exc:
            fallback_ids = visible_runner._promote_spin_fallback_solids_to_tetra(
                parsed={},
                spin_solid=spin_solid,
                active_results=active_results,
                results=results,
            )
            spin_step["failed"].append({"solid_id": sid, "fallback_solid_ids": fallback_ids, "error": str(exc)})
        if _stop_requested(args):
            spin_step["success"] = not spin_step["failed"]
            workflow["steps"]["spin_hex"] = spin_step
            visible_runner._write_json_if_enabled(args.write_json, RUNS_DIR / f"workflow_spin_hex_responses_{stamp}.json", spin_responses)
            return _write_stopped_workflow_summary(
                args=args,
                workflow=workflow,
                stamp=stamp,
                stage=f"spin_hex_s{sid}",
                working_model=working_model,
                output_path=output_path,
                report_path=report_path,
                diagnostic_log_path=diagnostic_log_path,
                probe_path=probe_path,
                gear_diag_path=gear_diag_path,
            )
    spin_step["success"] = not spin_step["failed"]
    workflow["steps"]["spin_hex"] = spin_step
    visible_runner._write_json_if_enabled(args.write_json, RUNS_DIR / f"workflow_spin_hex_responses_{stamp}.json", spin_responses)
    if _stop_requested(args):
        return _write_stopped_workflow_summary(
            args=args,
            workflow=workflow,
            stamp=stamp,
            stage="spin_hex",
            working_model=working_model,
            output_path=output_path,
            report_path=report_path,
            diagnostic_log_path=diagnostic_log_path,
            probe_path=probe_path,
            gear_diag_path=gear_diag_path,
        )

    _log("[4/6] Run tetra batches")
    tetra_batches = visible_runner._build_tetra_batches_from_results(active_results)
    plan_batches: list[dict[str, Any]] = []
    tetra_step: dict[str, Any] = {"batch_count": len(tetra_batches), "completed": [], "failed": []}
    for batch in tetra_batches:
        batch_index = int(batch["batch"])
        solid_ids = [int(value) for value in batch["solid_ids"]]
        solids = [results[str(sid)] for sid in solid_ids]
        generated = hm.generate_batched_plain_tetra_tcl(
            solids=solids,
            output_hm_path=str(working_model),
            pause_seconds_after_each_solid=args.tetra_pause_seconds,
            checkpoint_every_n_solids=0,
            default_element_size=args.tetra_element_size,
            default_min_element_size=args.tetra_min_element_size,
            max_deviation=args.tetra_max_deviation,
            feature_angle=args.tetra_feature_angle,
            growth_rate=args.tetra_growth_rate,
            fit_tolerance_ratio=args.tetra_fit_tolerance_ratio,
            target_vol_skew=args.tetra_target_vol_skew,
            repair_vol_skew=args.tetra_repair_vol_skew,
            chord_dev_degrade_delta=args.tetra_chord_dev_degrade_delta,
            element_size_min=args.tetra_element_size_min,
            element_size_max=args.tetra_element_size_max,
            min_element_size_min=args.tetra_min_element_size_min,
            min_element_size_max=args.tetra_min_element_size_max,
            delete_existing_component_elements=True,
            use_gear_tooth_refinement=args.use_gear_tooth_refinement,
            gear_tooth_element_size=args.gear_tooth_element_size,
            gear_tooth_element_size_min=args.gear_tooth_element_size_min,
            gear_tooth_element_size_max=args.gear_tooth_element_size_max,
            gear_tooth_min_element_size=args.gear_tooth_min_element_size,
            gear_tooth_min_element_size_min=args.gear_tooth_min_element_size_min,
            gear_tooth_min_element_size_max=args.gear_tooth_min_element_size_max,
            gear_tooth_feature_angle=args.gear_tooth_feature_angle,
        )
        script_path = RUNS_DIR / f"workflow_tetra_batch_{batch_index:02d}_{stamp}.tcl"
        log_path = RUNS_DIR / f"workflow_tetra_batch_{batch_index:02d}_{stamp}.log"
        script_path.write_text(generated["script"], encoding="utf-8")
        plan_batches.append(
            {
                "batch": batch_index,
                "solid_ids": solid_ids,
                "solid_names": {str(solid["solid_id"]): solid.get("component_name", "") for solid in solids},
                "script_path": str(script_path),
                "log_path": str(log_path),
                "reason": batch.get("reason", ""),
            }
        )
        _log(f"  - tetra batch {batch_index}/{len(tetra_batches)} solids={solid_ids}")
        response = _execute_batch(
            generated["script"],
            model_path=working_model,
            hmbatch_path=args.hmbatch,
            timeout_seconds=args.tetra_timeout,
        )
        _write_batch_log(log_path, response)
        status = "ok" if response.get("success") else "error"
        result = {
            "status": status,
            "success": response.get("success"),
            "elapsed_seconds": response.get("elapsed_seconds"),
            "tail": _response_text(response)[-6000:],
            "response": response,
        }
        visible_runner._write_json_if_enabled(args.write_json, RUNS_DIR / f"workflow_tetra_batch_{batch_index:02d}_response_{stamp}.json", result)
        if status == "ok":
            tetra_step["completed"].append(batch_index)
        else:
            failure = {"batch": batch_index, "solid_ids": solid_ids, "status": status}
            tetra_step["failed"].append(failure)
            workflow["errors"].append({"step": "tetra", **failure})
            if not args.continue_on_error:
                raise RuntimeError(f"Tetra batch {batch_index} failed: {status}")
        if _stop_requested(args):
            workflow["steps"]["tetra"] = tetra_step
            partial_plan = {
                "stamp": stamp,
                "drag_count": len(drag_solids),
                "spin_count": len(spin_solids),
                "spin_completed_count": len(spin_step.get("completed", [])),
                "spin_fallback_count": len(spin_step.get("fallback_to_tetra", [])) + len(spin_step.get("failed", [])),
                "tetra_batches": plan_batches,
                "final_save_path": str(output_path),
            }
            visible_runner._write_json_if_enabled(args.write_json, RUNS_DIR / f"workflow_execution_plan_{stamp}.json", partial_plan)
            partial_repair_summary = visible_runner._parse_repair_summary(partial_plan)
            return _write_stopped_workflow_summary(
                args=args,
                workflow=workflow,
                stamp=stamp,
                stage=f"tetra_batch_{batch_index:02d}",
                working_model=working_model,
                output_path=output_path,
                report_path=report_path,
                diagnostic_log_path=diagnostic_log_path,
                repair_summary=partial_repair_summary,
                probe_path=probe_path,
                gear_diag_path=gear_diag_path,
            )
    workflow["steps"]["tetra"] = tetra_step

    plan = {
        "stamp": stamp,
        "drag_count": len(drag_solids),
        "spin_count": len(spin_solids),
        "spin_completed_count": len(spin_step.get("completed", [])),
        "spin_fallback_count": len(spin_step.get("fallback_to_tetra", [])) + len(spin_step.get("failed", [])),
        "tetra_batches": plan_batches,
        "final_save_path": str(output_path),
    }
    visible_runner._write_json_if_enabled(args.write_json, RUNS_DIR / f"workflow_execution_plan_{stamp}.json", plan)

    _log("[5/6] Final save and element count")
    final_save = _final_save_and_count(args=args, working_model=working_model, output_path=output_path)
    workflow["steps"]["final_save"] = final_save
    visible_runner._write_json_if_enabled(args.write_json, RUNS_DIR / f"workflow_final_save_response_{stamp}.json", final_save)

    _log("[6/6] Write report")
    repair_summary = visible_runner._parse_repair_summary(plan)
    part_parameters = visible_runner._build_part_parameter_report(
        results=active_results,
        drag_solids=drag_solids,
        drag_response=drag_response,
        spin_solids=spin_solids,
        spin_responses=spin_responses,
        plan=plan,
        args=args,
    )
    workflow["steps"]["repair_summary"] = {
        **repair_summary["repair_aggregate"],
        "tetra_attempted_count": repair_summary["tetra_attempted_count"],
        "tetra_done_count": repair_summary["tetra_done_count"],
        "tetra_failed_count": repair_summary["tetra_failed_count"],
        "tetra_tet4_total": repair_summary["tetra_tet4_total"],
    }
    aggregate = repair_summary.get("repair_aggregate", {})
    solid_name_by_id = {
        str(sid): name
        for batch in plan_batches
        for sid, name in (batch.get("solid_names") or {}).items()
    }
    repair_by_solid = repair_summary.get("repair_by_solid", {})
    quality_error_keys = {
        "tetra_deleted_keep_surface_shells_count": ("rolled_back_to_surface_mesh", "tetra_deleted_keep_surface_shells"),
        "crash_guard_keep_surface_mesh_count": ("crash_guard_keep_surface_mesh", "crash_guard_keep_surface_mesh"),
        "extreme_aspect_keep_surface_mesh_count": ("extreme_aspect_keep_surface_mesh", "extreme_aspect_keep_surface_mesh"),
        "surface_fit_degraded_keep_surface_mesh_count": ("surface_fit_degraded_keep_surface_mesh", "surface_fit_degraded_keep_surface_mesh"),
        "surface_repair_timeout_keep_surface_mesh_count": ("surface_repair_timeout_keep_surface_mesh", "surface_repair_timeout_keep_surface_mesh"),
        "surface_backup_failed_keep_surface_mesh_count": ("surface_backup_failed_keep_surface_mesh", "surface_backup_failed_keep_surface_mesh"),
        "tetmesh_failed_keep_surface_mesh_count": ("tetmesh_failed_keep_surface_mesh", "tetmesh_failed_keep_surface_mesh"),
    }
    for key, (status, solid_flag) in quality_error_keys.items():
        count = int(aggregate.get(key) or 0)
        if count <= 0:
            continue
        solids = [
            {
                "solid_id": int(sid),
                "component_name": solid_name_by_id.get(str(sid), ""),
            }
            for sid, info in sorted(repair_by_solid.items(), key=lambda pair: int(pair[0]))
            if int(info.get(solid_flag) or 0) > 0
        ]
        workflow["errors"].append(
            {
                "step": "tetra_quality",
                "status": status,
                "solid_count": count,
                "solids": solids,
            }
        )
        workflow["warnings"].append(
            f"tetra quality guard: {count} solid(s) ended as {status}; see Chinese report for details."
        )
    tetra_result_failures = visible_runner._tetra_result_failures(repair_summary, plan_batches)
    if tetra_result_failures:
        workflow["errors"].append(
            {
                "step": "tetra_result",
                "status": "tetra_failed_or_missing",
                "solid_count": len(tetra_result_failures),
                "solids": tetra_result_failures,
            }
        )
        workflow["warnings"].append(
            f"tetra result guard: {len(tetra_result_failures)} solid(s) failed or did not report a final tetra result."
        )
    workflow["success"] = bool(final_save.get("success")) and bool(final_save.get("output_exists")) and not workflow["errors"]
    report_data = {
        "stamp": stamp,
        "success": workflow["success"],
        "output_hm_path": str(output_path),
        "classification": {
            **workflow["steps"]["classification"],
            "strategy_counts": {
                name: sum(1 for item in active_results.values() if item.get("strategy") == name)
                for name in sorted({str(item.get("strategy", "")) for item in active_results.values()})
            },
        },
        "drag_count": len(drag_solids),
        "drag": drag_step,
        "spin": spin_step,
        "tetra": tetra_step,
        "final_save": final_save,
        "repair_summary": repair_summary,
        "errors": workflow["errors"],
        "warnings": workflow["warnings"],
        "parameters": vars(args),
        "part_parameters": part_parameters,
        "skipped_probe": workflow["steps"].get("probe", {}),
        "skipped_existing_mesh": workflow["steps"].get("existing_mesh_skip", {}),
        "generated_files": {
            "探测结果": str(probe_path),
            "中文报告": str(report_path),
            "最终模型": str(output_path),
            "实时诊断日志": str(diagnostic_log_path),
            "齿轮齿面识别诊断": str(gear_diag_path),
        },
    }
    report = hm.write_chinese_meshing_workflow_report(report_data, output_path=str(report_path))
    workflow["report_path"] = report.get("report_path")
    workflow["popup_report_path"] = _write_popup_report(
        RUNS_DIR / f"workflow_popup_summary_{stamp}.txt",
        workflow,
        repair_summary,
    )
    visible_runner._write_json_if_enabled(args.write_json, RUNS_DIR / f"workflow_summary_{stamp}.json", workflow)
    visible_runner._write_json_if_enabled(args.write_json, RUNS_DIR / "workflow_latest_summary.json", workflow)
    visible_runner._diag(diagnostic_log_path, "batch_workflow_done", success=workflow["success"], workflow=workflow)
    _log(f"Chinese report: {workflow.get('report_path')}")
    return workflow


def build_arg_parser() -> argparse.ArgumentParser:
    parser = visible_runner.build_arg_parser()
    parser.description = "Run the HyperMesh meshing workflow in hmbatch/background mode."
    parser.add_argument("--input", required=True, help="Input .hm/.stp/.step model path.")
    parser.add_argument("--hmbatch", default=None, help="Optional explicit hmbatch.exe path.")
    parser.add_argument("--import-timeout", type=int, default=300)
    parser.add_argument("--probe-element-size", type=float, default=5.0)
    parser.add_argument("--probe-min-element-size", type=float, default=0.5)
    parser.add_argument("--probe-max-deviation", type=float, default=0.2)
    parser.add_argument("--probe-feature-angle", type=float, default=20.0)
    parser.add_argument("--probe-growth-rate", type=float, default=1.3)
    parser.add_argument("--stop-file", default=None, help="Optional flag file. If it appears, save current working HM and exit after the current stage.")
    return parser


def _normalize_args(args: argparse.Namespace) -> None:
    if args.spin_density_max is None:
        args.spin_density_max = args.spin_density
    if args.spin_density_min > args.spin_density_max:
        args.spin_density_min, args.spin_density_max = args.spin_density_max, args.spin_density_min
    if args.spin_section_element_size_min <= 0:
        args.spin_section_element_size_min = 0.2
    if args.spin_section_element_size_max <= 0:
        args.spin_section_element_size_max = 1.5
    if args.spin_section_element_size_min > args.spin_section_element_size_max:
        args.spin_section_element_size_min, args.spin_section_element_size_max = (
            args.spin_section_element_size_max,
            args.spin_section_element_size_min,
        )
    if args.drag_min_layers < 1:
        args.drag_min_layers = 1
    if args.drag_aspect_threshold <= 0:
        args.drag_aspect_threshold = 20.0
    if args.gear_tooth_element_size_min <= 0:
        args.gear_tooth_element_size_min = 1.2
    if args.gear_tooth_element_size_max <= 0:
        args.gear_tooth_element_size_max = args.gear_tooth_element_size_min
    if args.gear_tooth_element_size_min > args.gear_tooth_element_size_max:
        args.gear_tooth_element_size_min, args.gear_tooth_element_size_max = (
            args.gear_tooth_element_size_max,
            args.gear_tooth_element_size_min,
        )
    args.gear_tooth_element_size = min(
        max(args.gear_tooth_element_size, args.gear_tooth_element_size_min),
        args.gear_tooth_element_size_max,
    )
    if args.gear_tooth_min_element_size_min <= 0:
        args.gear_tooth_min_element_size_min = 0.2
    if args.gear_tooth_min_element_size_max <= 0:
        args.gear_tooth_min_element_size_max = args.gear_tooth_min_element_size_min
    if args.gear_tooth_min_element_size_min > args.gear_tooth_min_element_size_max:
        args.gear_tooth_min_element_size_min, args.gear_tooth_min_element_size_max = (
            args.gear_tooth_min_element_size_max,
            args.gear_tooth_min_element_size_min,
        )
    args.gear_tooth_min_element_size = min(
        max(args.gear_tooth_min_element_size, args.gear_tooth_min_element_size_min),
        args.gear_tooth_min_element_size_max,
    )
    if args.gear_tooth_feature_angle <= 0:
        args.gear_tooth_feature_angle = 15.0


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    _normalize_args(args)
    try:
        summary = run_workflow(args)
    except Exception as exc:
        _log(f"ERROR: {exc}")
        return 1

    counts = summary.get("steps", {}).get("final_save", {}).get("response", "")
    _log(f"Success: {summary.get('success')}")
    if counts:
        for line in str(counts).splitlines():
            if line.startswith("MCP_FINAL_COUNTS"):
                _log(line)
    _log(f"Output: {summary.get('output_hm_path')}")
    _log(f"Report: {summary.get('report_path')}")
    return 0 if summary.get("success") else 2


if __name__ == "__main__":
    sys.exit(main())
