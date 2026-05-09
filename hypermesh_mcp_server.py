from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP


APP_NAME = "hypermesh-mcp-server"
DEFAULT_HYPERMESH_DIR = Path(
    r"F:\Program Files\Altair\2020\hwdesktop\hw\bin\win64"
)
DEFAULT_HMBATCH = DEFAULT_HYPERMESH_DIR / "hmbatch.exe"
DEFAULT_HW = DEFAULT_HYPERMESH_DIR / "hw.exe"
DEFAULT_GUI_PORT = 47881
RUNS_DIR = Path(__file__).resolve().parent / "runs"

HYPERMESH_MESHING_STRATEGY = """
HyperMesh meshing strategy for this workstation:

1. Do not use solidmap for this workflow.
2. Use the existing b.hm-style size scale as the reference. For surface-deviation
   triangular surface mesh, use parameters close to: growth rate 1.23,
   minimum element size 0.5, maximum deviation 0.1, maximum feature angle 15,
   mesh type R-trias.
3. Tetra-volume parts must be meshed per object/component. Do not dump tetra
   elements from several objects into another component.
4. Flanges are tetra parts, not drag parts. A flange with bolt holes, stepped
   lips, bosses, or local cutouts must use surface-deviation R-trias followed
   by tetramesh, even if part of its outline looks circular.
5. For tetra strategy: first make 2D surface-deviation R-trias mesh, check/fix
   2D aspect > 10, then generate volume mesh with tetramesh, then check/fix
   volume skew > 0.99.
6. For simple straight tube/cylinder drag hex meshing, match logical edge seed
   counts, but do not blindly promote the whole source face to the largest outer
   edge count. If preview seed counts or edge lengths differ greatly, choose a
   balanced common count from the section scale (geometric-mean style) and use
   that one count for the mapped source face. Continue only when the source face
   is a mapped, 100% quad mesh. If that cannot be guaranteed, try a spin-hex
   strategy for suitable revolved bodies, otherwise fall back to tetra. The
   generated workflow must validate that real 3D hex elements were created; a
   leftover 2D section alone is a failure.
7. For obvious revolved bodies, prefer spin hex meshing, but never invent the
   section from guessed radii or from a side/end face. First split the solid with
   a real middle cutting plane, use only the newly created surfaces that lie on
   that cutting plane as 2D section sources, mesh those section surfaces as 100%
   quads, then spin to 3D. If the true cut section cannot be guaranteed as all
   quads, or if spin creates no valid 3D hex elements, clean up temporary shells
   and fall back to the tetra strategy.
8. Try a structured hex route before tetra when the geometry supports it:
   drag for simple constant-section extrusions, spin for clean true-section
   revolved solids, cut-section spin for stepped/recessed revolved solids.
   If the chosen hex route fails validation, fall back to tetra for that object.
   Clean bearing/ring-like revolved bodies should get a real cut-section spin
   attempt before tetra; direct surface-id spin is not enough unless the surface
   is already the true radial cross-section.
9. Component names should describe the physical object, not the mesh type.
   Examples: housing, shaft_ring, spacer_block_upper, support_flange.
10. Do not repair quality by blindly refining the whole mesh. Prefer strategy
    changes, local 3D smoothing/remesh, or sliver-tetra repair. If bad volume
    elements still cannot be fixed, keep them in the model and report them; do
    not delete unfixable quality-failed elements unless the user explicitly asks.
11. Gear, helical gear, or spline teeth are local fine-feature regions. Detect
    them only from true tooth geometry: alternating outer-radius peaks/valleys,
    repeated tooth flanks, twisted helical tooth faces, or explicit tooth/root
    surfaces. Do not treat smooth concentric bearing races, annular grooves, or
    cylindrical outer bands as gears. If exact tooth surface IDs are not known,
    auto-detect the outer gear band only after gear geometry evidence is present.
12. Surface-deviation tetra sizing policy: do not make complex/high-face-count
    solids coarser just because they are complex. Keep the requested nominal
    element size, but allow a smaller minimum element size so small faces,
    fillets, teeth, holes, and chamfers can be captured. Use a shell-count guard
    before *tetmesh so a dangerously dense surface mesh is reported instead of
    crashing HyperMesh.
    High-risk solids such as gears or very high-face-count chamfered bodies must
    not call *tetmesh in the same long GUI run unless explicitly allowed; stop
    after safe surface mesh and report MCP_PT_STOP instead.
13. Phase 2 finalization is mandatory. After classification, execute the
    generated finalize Tcl so components are renamed and colored by strategy
    before Phase 3 meshing.
"""

GENERIC_MESHING_RULES = {
    "tetra_surface_deviation_rtrias": {
        "use_when": [
            "flanges or flange-like bodies",
            "parts with bolt holes, local holes, bosses, protrusions, ribs, grooves, cutouts, or non-sweepable topology",
            "parts whose source face cannot be proven as 100% quads with matched edge seeds",
            "fallback for ambiguous geometry",
        ],
        "required_checks": [
            "2D surface mesh aspect cleanup before tetramesh",
            "for high surface counts, reduce min_element_size rather than increasing nominal element_size",
            "abort and report if surface shell count is above the crash-safety limit before tetramesh",
            "per-component tetramesh; do not mix several solids into one component",
            "3D volume quality check and local repair/report",
        ],
    },
    "drag_hex_guarded": {
        "use_when": [
            "simple straight extrusion or tube with constant section",
            "a real source face exists at one end of the extrusion",
            "all logical source-face edge groups can be forced to matched seed counts",
            "the source face meshes as 100% quads",
        ],
        "default_element_size": 1.5,
        "default_retry_count": 2,
        "element_size_rule": (
            "Drag hex element_size is clamped to 0.5-1.5 mm and must be chosen "
            "from both extrusion thickness and source-face size: min(requested, "
            "drag_distance/4, source_minor/3, source_major/8). Layer count is "
            "auto-computed as round(drag_distance / element_size). Minimum 1 "
            "layer. A thin solid with 1 layer is acceptable; do not fallback to "
            "tetra just because the drag direction is thin."
        ),
        "retry_policy": (
            "After each failed attempt, reduce element_size by 20% and retry. "
            "Default retry_count is 2 (3 total attempts). "
            "ONLY fallback to tetra if ALL retries fail."
        ),
        "source_face_rule": (
            "Source face MUST be at the NEGATIVE end of the drag axis (lowest "
            "centroid). Drag always goes POSITIVE into the solid. Example: z-axis "
            "solid at z=10~z=17. Pick the z闁?0 face, drag +z by 7. Picking z闁?7 "
            "face would drag elements outside the solid."
        ),
        "mandatory_batching": (
            "When processing multiple drag_hex solids that share the same drag axis, "
            "MUST use generate_batched_drag_hex_tcl to process all of them in ONE "
            "script. Do NOT call generate_guarded_drag_hex_tcl once per solid."
        ),
        "mandatory_source_shell_cleanup": (
            "After a successful hex drag, the 2D source-face shell elements MUST be "
            "deleted. The generated Tcl script includes this cleanup automatically."
        ),
        "seed_policy": (
            "Match logical source-face counts, but when preview counts or edge "
            "lengths are highly different, choose a balanced common count rather "
            "than forcing inner edges up to the outer-edge count."
        ),
        "fallback": "tetra_surface_deviation_rtrias",
    },
    "spin_hex_guarded": {
        "use_when": [
            "clean revolved solid",
            "the selected source surface is already a true cross-section",
            "the source section meshes as 100% quads",
        ],
        "fallback": "cutsection_spin_hex for stepped/recessed revolved solids; otherwise tetra_surface_deviation_rtrias",
    },
    "cutsection_spin_hex": {
        "use_when": [
            "stepped, recessed, or ambiguous revolved solid",
            "no existing face can be trusted as the spin section",
            "a middle cutting plane through the rotation axis can be defined",
        ],
        "method": [
            "split the actual solid with body_splitmerge_with_plane",
            "detect newly created surfaces that lie on the cutting plane",
            "accept only all-quad section meshes on that plane",
            "spin the accepted 2D section into 3D hex elements",
        ],
        "fallback": "tetra_surface_deviation_rtrias",
    },
    "gear_aware_tetra": {
        "use_when": [
            "gear, helical gear, pinion, spline, or many repeated radial/oblique teeth are present",
            "external tooth evidence is present: alternating outer-radius peaks/valleys, repeated flanks, or twisted tooth faces",
            "not a smooth bearing/ring with only concentric races or annular grooves",
            "tooth surfaces need a smaller local 2D size than shaft/hub surfaces",
            "the whole part is not safely sweepable as one structured hex block",
        ],
        "method": [
            "identify repeated tooth/flank/root surfaces as the gear region, or auto-detect the outer gear band",
            "surface mesh shaft/hub surfaces with the base size",
            "surface mesh tooth-region surfaces with a smaller gear size",
            "tetra mesh the solid from the mixed-size surface shell mesh",
        ],
        "fallback": "tetra_surface_deviation_rtrias with uniform base size",
    },
    "critical_rule_phase2_color_mandatory": {
        "rule": (
            "Phase 2 is not complete until components are renamed and assigned "
            "their Phase 2 colors. Execute the phase2_finalize_script returned "
            "by classify_all_solids_from_probe, or call generate_phase2_finalize_tcl. "
            "The Phase 2 color script preserves the original behavior: one "
            "visible unique HyperMesh color per renamed component."
        ),
    },
    "critical_rule_no_manual_tcl_injection": {
        "rule": (
            "NEVER inject hand-written Tcl code into or around scripts generated by "
            "generate_*_tcl tools. Every parameter on these tools exists for a reason. "
            "When a meshing problem occurs, you MUST: "
            "(1) re-read the FULL function signature of the relevant generate_*_tcl tool; "
            "(2) if a parameter already covers the scenario, use it; "
            "(3) only if NO existing parameter covers the case, modify the MCP tool itself."
        ),
    },
    "critical_rule_every_change_must_enforce": {
        "rule": (
            "Every modification to this MCP server MUST include enforcement in "
            "GENERIC_MESHING_RULES and/or _meshing_rule_violation. Rules written "
            "only in external files are invisible to other AI agents."
        ),
    },
}

# Strategy 闁?HyperMesh color id mapping (verified with HM 2020 *colormark)
FINALIZE_STRATEGY_COLORS = {
    "drag_hex": 1,
    "spin_hex": 7,
    "gear_aware_tetra": 3,
    "tetra_plain": 4,
    "surface_tetra": 4,
    "unknown": 4,
}

TETRA_COMPLEX_SURFACE_COUNT = 50
TETRA_VERY_COMPLEX_SURFACE_COUNT = 120
TETRA_COMPLEX_MIN_ELEMENT_SIZE = 0.25
TETRA_VERY_COMPLEX_MIN_ELEMENT_SIZE = 0.20
TETRA_MAX_SHELL_ELEMENTS = 8000
TETRA_COMPLEX_MAX_SHELL_ELEMENTS = 2500
TETRA_VERY_COMPLEX_MAX_SHELL_ELEMENTS = 1500
TETRA_DIRECT_TETMESH_SURFACE_COUNT_LIMIT = 50

# Generated-script boundary markers used by _meshing_rule_violation
MCP_SCRIPT_BEGIN = "# MCP_SCRIPT_BEGIN"
MCP_SCRIPT_END = "# MCP_SCRIPT_END"
TRUSTED_MESHING_GENERATORS = {
    "generate_surface_automesh_tcl",
    "generate_plain_tetra_tcl",
    "generate_guarded_drag_hex_tcl",
    "generate_batched_drag_hex_tcl",
    "generate_guarded_spin_hex_tcl",
    "generate_cutsection_spin_hex_tcl",
}
TRUSTED_NON_MESHING_GENERATORS = {
    "generate_phase2_finalize_tcl",
    "generate_finalize_components_tcl",
}
TRUSTED_GENERATORS = TRUSTED_MESHING_GENERATORS | TRUSTED_NON_MESHING_GENERATORS

SPECIAL_WORKFLOWS = {
    "visible_gui_mode": {
        "recommended": True,
        "listener_port": DEFAULT_GUI_PORT,
        "summary": (
            "Use create_gui_listener_tcl, manually source the generated Tcl in an "
            "already opened HyperMesh Tcl console when auto-launch does not work, "
            "then run execute_tcl_gui so the user can watch the model load, split, "
            "mesh, spin, and save in the visible GUI."
        ),
    },
    "cutsection_spin_hex": {
        "tool": "generate_cutsection_spin_hex_tcl",
        "method": [
            "Use this for stepped/recessed/ambiguous revolved solids where an existing face is not a trustworthy spin section.",
            "Split the target solid with body_splitmerge_with_plane using a user-provided middle plane.",
            "Detect the real section surfaces by meshing each new surface temporarily and checking node distance to the split plane.",
            "Accept only all-quad section meshes that lie on the split plane, then spin them 360 degrees about the x axis.",
            "Delete only the temporary 2D seed shell elements after spin; keep generated 3D hex elements.",
        ],
        "required_inputs": [
            "solid_id",
            "component_name",
            "split plane normal and point",
            "spin axis and axis point",
            "element size and spin density",
        ],
        "why": (
            "Direct surface-id spin is only safe when the selected surface is a "
            "true cross-section. For recessed or stepped rings, a real solid "
            "split is the reliable way to obtain that cross-section."
        ),
    },
    "quality_policy": {
        "policy": (
            "Try smoothing/sliver repair, but leave unfixable bad volume elements "
            "in the model and log their IDs. Do not delete them automatically."
        ),
    },
}

mcp = FastMCP(APP_NAME)


def _normalize_path(path: str | os.PathLike[str] | None) -> Path | None:
    if path is None or str(path).strip() == "":
        return None
    return Path(str(path).strip().strip('"')).expanduser()


def _candidate_hmbatch_paths() -> list[Path]:
    candidates: list[Path] = []

    env_path = _normalize_path(os.environ.get("HYPERMESH_BATCH_EXE"))
    if env_path:
        candidates.append(env_path)

    candidates.extend(
        [
            DEFAULT_HMBATCH,
            Path(r"F:\Program Files\Altair\2020\hwdesktop\hm\bin\win64\hmbatch.exe"),
            Path(r"C:\Program Files\Altair\2020\hwdesktop\hw\bin\win64\hmbatch.exe"),
            Path(r"C:\Program Files\Altair\2020\hwdesktop\hm\bin\win64\hmbatch.exe"),
        ]
    )

    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate).lower()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _candidate_hypermesh_gui_paths() -> list[Path]:
    candidates: list[Path] = []

    env_path = _normalize_path(os.environ.get("HYPERMESH_GUI_EXE"))
    if env_path:
        candidates.append(env_path)

    candidates.extend(
        [
            DEFAULT_HW,
            Path(r"F:\Program Files\Altair\2020\hwdesktop\hwx\bin\win64\hwx.exe"),
            Path(r"C:\Program Files\Altair\2020\hwdesktop\hw\bin\win64\hw.exe"),
            Path(r"C:\Program Files\Altair\2020\hwdesktop\hwx\bin\win64\hwx.exe"),
        ]
    )

    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate).lower()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _resolve_hmbatch(hmbatch_path: str | None = None) -> Path:
    explicit = _normalize_path(hmbatch_path)
    if explicit:
        if explicit.exists():
            return explicit
        raise FileNotFoundError(f"hmbatch.exe was not found: {explicit}")

    for candidate in _candidate_hmbatch_paths():
        if candidate.exists():
            return candidate

    raise FileNotFoundError(
        "Could not find hmbatch.exe. Set HYPERMESH_BATCH_EXE or pass hmbatch_path."
    )


def _resolve_hypermesh_gui(gui_path: str | None = None) -> Path:
    explicit = _normalize_path(gui_path)
    if explicit:
        if explicit.exists():
            return explicit
        raise FileNotFoundError(f"HyperMesh GUI executable was not found: {explicit}")

    for candidate in _candidate_hypermesh_gui_paths():
        if candidate.exists():
            return candidate

    raise FileNotFoundError(
        "Could not find hw.exe/hwx.exe. Set HYPERMESH_GUI_EXE or pass gui_path."
    )


def _quote_tcl_path(path: str | os.PathLike[str]) -> str:
    return str(Path(path)).replace("\\", "/").replace('"', '\\"')


def _balanced_seed_density(
    *,
    element_size: float,
    target_density: int | None,
    preview_edge_seed_counts: list[int] | None,
    source_edge_lengths: list[float] | None,
    ratio_threshold: float,
) -> tuple[int | None, str]:
    counts: list[int] = []
    if preview_edge_seed_counts:
        counts.extend(max(1, int(count)) for count in preview_edge_seed_counts)
    elif source_edge_lengths:
        counts.extend(
            max(1, round(float(length) / float(element_size)))
            for length in source_edge_lengths
        )

    if not counts:
        return target_density, "explicit" if target_density else "bbox_estimate"

    low = min(counts)
    high = max(counts)
    ratio = high / max(low, 1)
    if ratio >= ratio_threshold:
        balanced = round((low * high) ** 0.5)
        source = f"balanced_from_range_{low}_{high}"
    elif target_density:
        balanced = int(target_density)
        source = "explicit"
    else:
        balanced = round(sum(counts) / len(counts))
        source = f"average_from_preview_{low}_{high}"

    return max(4, min(120, int(balanced))), source


def _generated_by(script: str) -> str | None:
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("# mcp_generated_by="):
            return stripped.split("=", 1)[1].strip()
    return None


def _meshing_rule_violation(script: str) -> dict[str, Any] | None:
    """Reject raw meshing Tcl that bypasses MCP strategy generators.

    Generated scripts are identified by containing both MCP_SCRIPT_BEGIN and
    MCP_SCRIPT_END.  Forbidden commands are allowed inside those boundaries
    but rejected anywhere else.
    """
    lowered = script.lower()
    generator_name = _generated_by(script)

    # 闁冲厜鍋撻柍鍏夊亾 strip comments so we only scan real commands 闁冲厜鍋撻柍鍏夊亾闁冲厜鍋撻柍鍏夊亾闁冲厜鍋撻柍鍏夊亾闁冲厜鍋撻柍鍏夊亾闁冲厜鍋撻柍鍏夊亾闁冲厜鍋撻柍鍏夊亾闁冲厜鍋撻柍鍏夊亾
    clean_lines = []
    for line in script.split("\n"):
        pos = line.find("#")
        if pos >= 0:
            line = line[:pos]
        clean_lines.append(line)
    lowered_clean = "\n".join(clean_lines).lower()

    forbidden_tokens = [
        "*meshdragelements",
        "*meshspinelements",
        "*set_meshedgeparams",
        "*interactiveremeshedge",
        "*defaultmeshsurf_growth",
        "*tetmesh",
    ]

    has_generated_marker = generator_name is not None
    has_begin = MCP_SCRIPT_BEGIN.lower() in lowered
    has_end = MCP_SCRIPT_END.lower() in lowered

    if has_generated_marker:
        if generator_name not in TRUSTED_GENERATORS:
            return {
                "success": False,
                "policy_violation": True,
                "message": f"Generated Tcl came from an unknown MCP generator: {generator_name}.",
            }
        # 闁冲厜鍋撻柍鍏夊亾 generated script 闁冲厜鍋撻柍鍏夊亾 check structural integrity 闁冲厜鍋撻柍鍏夊亾闁冲厜鍋撻柍鍏夊亾闁冲厜鍋撻柍鍏夊亾闁冲厜鍋?
        if not has_begin or not has_end:
            return {
                "success": False,
                "policy_violation": True,
                "message": (
                    "Generated Tcl is missing MCP_SCRIPT_BEGIN or "
                    "MCP_SCRIPT_END boundary markers."
                ),
            }

        # Everything after the final MCP_SCRIPT_END must be clean. Composite MCP
        # scripts may contain nested generated-script markers.
        _, _, after_end = lowered.rpartition(MCP_SCRIPT_END.lower())
        if any(token in after_end for token in forbidden_tokens):
            return {
                "success": False,
                "policy_violation": True,
                "message": (
                    "Forbidden meshing Tcl was detected after MCP_SCRIPT_END. "
                    "Appending raw meshing commands to a generated script is not "
                    "allowed."
                ),
            }

        return None

    # 闁冲厜鍋撻柍鍏夊亾 non-generated script 闁冲厜鍋撻柍鍏夊亾 forbid all raw meshing commands 闁冲厜鍋撻柍鍏夊亾
    for token in forbidden_tokens:
        if token in lowered_clean:
            return {
                "success": False,
                "policy_violation": True,
                "blocked_command": token.lstrip("*"),
                "message": (
                    "Raw meshing Tcl is blocked. Use the corresponding "
                    "generate_*_tcl tool."
                ),
            }

    return None


def _script_has_generated_meshing(script: str) -> bool:
    generator_name = _generated_by(script)
    return generator_name in TRUSTED_MESHING_GENERATORS


def _phase2_finalization_violation(script: str) -> dict[str, Any] | None:
    if not _GLOBAL_CLASSIFICATION_RESULTS or _GLOBAL_PHASE2_FINALIZED:
        return None
    lowered = script.lower()
    if "mcp_finalize_components" in lowered or "mcp_finalize_ok" in lowered:
        return None
    if _script_has_generated_meshing(script):
        return {
            "success": False,
            "policy_violation": True,
            "blocked_step": "phase2_finalize_required",
            "message": (
                "Phase 2 finalization is mandatory before meshing. Execute the "
                "phase2_finalize_script returned by classify_all_solids_from_probe, "
                "or call generate_phase2_finalize_tcl and execute that script first."
            ),
        }
    return None


def _mark_phase2_finalized_from_result(result: dict[str, Any]) -> None:
    global _GLOBAL_PHASE2_FINALIZED
    response = str(result.get("response", ""))
    stdout = str(result.get("stdout", ""))
    if "MCP_FINALIZE_OK" in response or "MCP_FINALIZE_OK" in stdout:
        _GLOBAL_PHASE2_FINALIZED = True


def _ensure_runs_dir() -> Path:
    RUNS_DIR.mkdir(exist_ok=True)
    return RUNS_DIR


def _write_run_script(script: str) -> Path:
    run_dir = _ensure_runs_dir()
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    script_path = run_dir / f"hypermesh_mcp_{timestamp}_{os.getpid()}.tcl"
    script_path.write_text(script, encoding="utf-8")
    return script_path


def _tcl_escape_name(name: str) -> str:
    """Escape a string for safe use as a Tcl literal."""
    s = name.replace("\\", "\\\\")
    s = s.replace("{", "\\{").replace("}", "\\}")
    s = s.replace("[", "\\[").replace("]", "\\]")
    s = s.replace("$", "\\$").replace('"', '\\"')
    return s


def _classification_results_from_input(classification_results: dict | None = None) -> dict:
    """Accept flat, wrapped, or cached classification results."""
    target_results = {}
    if classification_results:
        if (
            "results" in classification_results
            and isinstance(classification_results["results"], dict)
            and classification_results["results"]
        ):
            target_results = classification_results["results"]
        elif "results" not in classification_results:
            target_results = classification_results

    if not target_results:
        target_results = _GLOBAL_CLASSIFICATION_RESULTS
    return target_results


def _finalize_assignments_from_classification(classification_results: dict | None = None) -> list[dict[str, Any]]:
    assignments: list[dict[str, Any]] = []
    for sid_str, info in _classification_results_from_input(classification_results).items():
        new_name = info.get("component_name", "")
        if not new_name:
            continue
        strategy = str(info.get("strategy", "unknown"))
        if strategy in {"tetra_surface_deviation_rtrias", "surface_tetra"}:
            strategy = "tetra_plain"
        assignments.append(
            {
                "solid_id": int(info.get("solid_id", sid_str)),
                "new_name": str(new_name),
                "strategy": strategy,
            }
        )
    return assignments


def _component_rename_tcl(
    *,
    component_id: int | None,
    old_name: str | None,
    new_name: str,
    solid_id: int | None = None,
) -> list[str]:
    """Generate Tcl lines to rename a single HyperMesh component."""
    safe_new_name = _tcl_escape_name(new_name)
    lines: list[str] = [f"# MCP rename -> {safe_new_name}"]

    if component_id is not None:
        lines.extend([
            f"*createmark components 1 {int(component_id)}",
            f'catch {{*renamecollector components {int(component_id)} "{safe_new_name}"}} _mcp_rename_err',
            'if {[info exists _mcp_rename_err] && $_mcp_rename_err ne ""} { puts "MCP_RENAME_WARN $_mcp_rename_err" }',
        ])
    elif old_name:
        safe_old_name = _tcl_escape_name(old_name)
        lines.extend([
            f'*createmark components 1 "{safe_old_name}"',
            f'catch {{*renamecollector components "{safe_old_name}" "{safe_new_name}"}} _mcp_rename_err',
            'if {[info exists _mcp_rename_err] && $_mcp_rename_err ne ""} { puts "MCP_RENAME_WARN $_mcp_rename_err" }',
        ])
    elif solid_id is not None:
        lines.extend([
            f'*createmark components 1 "by solids" {int(solid_id)}',
            "set _mcp_rename_cids [hm_getmark components 1]",
            "if {[llength $_mcp_rename_cids] > 0} {",
            "    set _mcp_rename_cid [lindex $_mcp_rename_cids 0]",
            f'    catch {{*renamecollector components $_mcp_rename_cid "{safe_new_name}"}} _mcp_rename_err',
            '    if {[info exists _mcp_rename_err] && $_mcp_rename_err ne ""} { puts "MCP_RENAME_WARN $_mcp_rename_err" }',
            "} else {",
            f'    puts "MCP_RENAME_FAIL solid_id={int(solid_id)} has no component"',
            "}",
        ])
    else:
        lines.append('puts "MCP_RENAME_FAIL missing component_id or old_name"')

    return lines


def _component_color_tcl(
    *,
    component_id: int | None,
    component_name: str | None,
    strategy: str,
    solid_id: int | None = None,
) -> list[str]:
    """Generate Tcl lines to color a single HyperMesh component by strategy."""
    color = FINALIZE_STRATEGY_COLORS.get(strategy, FINALIZE_STRATEGY_COLORS["unknown"])
    lines: list[str] = [f"# MCP color strategy={strategy} color={color}"]

    if component_name:
        safe_name = _tcl_escape_name(component_name)
        lines.extend([
            f'*createmark components 1 "{safe_name}"',
            (
                f'if {{[hm_marklength components 1] == 0 && "{solid_id or ""}" ne ""}} '
                f'{{*createmark components 1 "by solids" {int(solid_id) if solid_id is not None else 0}}}'
            ),
            f"catch {{*colormark components 1 {int(color)}}}",
        ])
    elif component_id is not None:
        lines.extend([
            f"*createmark components 1 {int(component_id)}",
            f"catch {{*colormark components 1 {int(color)}}}",
        ])
    elif solid_id is not None:
        lines.extend([
            f'*createmark components 1 "by solids" {int(solid_id)}',
            f"catch {{*colormark components 1 {int(color)}}}",
        ])
    else:
        lines.append('puts "MCP_COLOR_FAIL missing component_id or component_name"')

    return lines


def _wrap_generated_tcl(generator_name: str, body: str) -> str:
    """Wrap generated Tcl with boundary markers for policy enforcement."""
    return "\n".join([
        f"# MCP_GENERATED_BY={generator_name}",
        MCP_SCRIPT_BEGIN,
        body,
        MCP_SCRIPT_END,
    ])


def _parse_probe_facts(line: str):
    """Parse a PROBE: line into a dict with field aliases."""
    import re
    facts = {}
    line = line.strip()
    if line.startswith("PROBE:"):
        line = line[len("PROBE:"):].strip()
    elif line.startswith("MCP_PROBE_SOLID"):
        line = line[len("MCP_PROBE_SOLID"):].strip()
    tokens = re.findall(r'(?:[^\s"{}]+|"[^"]*"|\{[^}]*\})', line)
    for pair in tokens:
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        v = v.strip('"')
        try:
            if "." in v:
                facts[k] = float(v)
            else:
                facts[k] = int(v)
        except ValueError:
            facts[k] = v
    ALIASES = {"id": "solid_id", "solid": "solid_id", "sc": "surf_count", "comp": "component_name",
               "mn": "min_dim", "mx": "max_dim", "md": "mid_dim", "diag": "diagonal",
               "src_surf": "source_surface_id", "drag_axis": "drag_axis"}
    for old, new in ALIASES.items():
        if old in facts and new not in facts:
            facts[new] = facts[old]
    return facts


def _probe_lines_iter(probe_lines) -> list[str]:
    if probe_lines is None:
        return []
    if isinstance(probe_lines, str):
        return probe_lines.splitlines()
    if isinstance(probe_lines, (list, tuple)):
        return [str(line) for line in probe_lines]
    return str(probe_lines).splitlines()


def _generate_geometry_component_name(facts, strategy, suffix_solid_id=True):
    """Generate a geometry-driven component name from probe facts."""
    dx, dy, dz = facts.get("dx", 0), facts.get("dy", 0), facts.get("dz", 0)
    dims = sorted([dx, dy, dz])
    mn, md, mx = dims[0], dims[1], dims[2]
    sid = facts.get("solid_id", 0)
    sfx = "_s" + str(sid) if suffix_solid_id else ""
    if strategy == "gear_aware_tetra":
        return "gear_D" + str(round(mx)) + "_W" + str(round(mn)) + sfx
    elif strategy == "drag_hex":
        return "block_" + str(round(dx)) + "x" + str(round(dy)) + "x" + str(round(dz)) + sfx
    elif strategy == "spin_hex":
        return "disc_D" + str(round(md)) + "_t" + str(round(mn)) + sfx
    return "solid_" + str(round(dx)) + "x" + str(round(dy)) + "x" + str(round(dz)) + sfx


def _tetra_execution_batches(results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Return tetra batches ordered to reduce HyperMesh memory-crash risk."""
    tetra = [
        item for item in results.values()
        if item.get("strategy") in {"tetra_plain", "tetra_surface_deviation_rtrias", "surface_tetra"}
    ]

    def risk_score(item: dict[str, Any]) -> float:
        dims = item.get("dims", {})
        dx = float(dims.get("dx", 0))
        dy = float(dims.get("dy", 0))
        dz = float(dims.get("dz", 0))
        diag = (dx * dx + dy * dy + dz * dz) ** 0.5
        sc = float(item.get("surf_count", 0))
        min_size = max(0.2, float(item.get("min_element_size", 0.5)))
        return sc * max(diag, 1.0) / min_size

    batches: list[dict[str, Any]] = []
    current: list[int] = []
    current_score = 0.0
    batch_index = 1
    for item in sorted(tetra, key=lambda value: int(value.get("solid_id", 0))):
        sid = int(item.get("solid_id", 0))
        sc = int(item.get("surf_count", 0))
        score = risk_score(item)
        high_risk = sc >= TETRA_VERY_COMPLEX_SURFACE_COUNT or score >= 20000
        if high_risk:
            if current:
                batches.append({
                    "batch": batch_index,
                    "solid_ids": current,
                    "reason": "low/medium-risk tetra batch",
                    "pause_seconds_after_batch": 5,
                })
                batch_index += 1
                current = []
                current_score = 0.0
            batches.append({
                "batch": batch_index,
                "solid_ids": [sid],
                "reason": "high-risk tetra solid; run alone, then save/cool down before the next tetra batch",
                "pause_seconds_after_batch": 10,
            })
            batch_index += 1
            continue
        if current and (len(current) >= 4 or current_score + score > 18000):
            batches.append({
                "batch": batch_index,
                "solid_ids": current,
                "reason": "low/medium-risk tetra batch",
                "pause_seconds_after_batch": 5,
            })
            batch_index += 1
            current = []
            current_score = 0.0
        current.append(sid)
        current_score += score
    if current:
        batches.append({
            "batch": batch_index,
            "solid_ids": current,
            "reason": "low/medium-risk tetra batch",
            "pause_seconds_after_batch": 5,
        })
    return batches


def _gui_listener_script(host: str = "127.0.0.1", port: int = DEFAULT_GUI_PORT) -> str:
    return f"""
# HyperMesh MCP GUI listener.
# Source this file inside a visible HyperMesh session, or launch HyperMesh with it.
set ::mcp_hm_host "{host}"
set ::mcp_hm_port {int(port)}

proc ::mcp_hm_accept {{chan addr client_port}} {{
    fconfigure $chan -blocking 1 -translation binary -encoding utf-8
    set script [read $chan]
    if {{[string trim $script] eq ""}} {{
        puts $chan "ERROR\\nempty Tcl script"
        close $chan
        return
    }}

    # 濞ｅ洦绻傞悺銊╁储閻斿娼?puts闁挎稑鑻崹鍗烆嚈閻戞ê绀嬮柤楣冾棑婢ф寮?
    rename puts ::_mcp_orig_puts
    set ::mcp_capture ""
    proc puts args {{
        set len [llength $args]
        if {{$len == 1}} {{
            ::_mcp_orig_puts [lindex $args 0]
            append ::mcp_capture [lindex $args 0] "\\n"
        }} elseif {{$len == 2 && ([lindex $args 0] eq "stdout")}} {{
            ::_mcp_orig_puts stdout [lindex $args 1]
            append ::mcp_capture [lindex $args 1] "\\n"
        }} else {{
            eval [linsert $args 0 ::_mcp_orig_puts]
        }}
    }}

    set code [catch {{uplevel #0 $script}} result options]
    
    # 闁诡厹鍨归ˇ鏌ュ储閻斿娼?puts
    rename puts ""
    rename ::_mcp_orig_puts puts

    if {{$code == 0 || $code == 2}} {{
        puts $chan "OK"
        if {{$::mcp_capture ne ""}} {{
            puts $chan $::mcp_capture
        }}
        if {{$result ne ""}} {{
            puts $chan $result
        }}
    }} else {{
        puts $chan "ERROR"
        puts $chan $result
        if {{[dict exists $options -errorinfo]}} {{
            puts $chan [dict get $options -errorinfo]
        }}
    }}
    flush $chan
    close $chan
}}

if {{[info exists ::mcp_hm_server]}} {{
    catch {{close $::mcp_hm_server}}
}}
set ::mcp_hm_server [socket -server ::mcp_hm_accept -myaddr $::mcp_hm_host $::mcp_hm_port]
puts "MCP HyperMesh GUI listener is ready on $::mcp_hm_host:$::mcp_hm_port"
""".lstrip()


def _run_hypermesh_gui_script(
    *,
    script: str,
    host: str = "127.0.0.1",
    port: int = DEFAULT_GUI_PORT,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    if not script.strip():
        raise ValueError("script cannot be empty.")

    with socket.create_connection((host, int(port)), timeout=max(1, int(timeout_seconds))) as sock:
        sock.settimeout(max(1, int(timeout_seconds)))
        sock.sendall(script.encode("utf-8"))
        sock.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)

    response = b"".join(chunks).decode("utf-8", errors="replace")
    return {
        "success": response.startswith("OK"),
        "host": host,
        "port": int(port),
        "response": response,
    }


def _run_hmbatch(
    *,
    hmbatch_path: str | None,
    script: str,
    model_path: str | None = None,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    exe = _resolve_hmbatch(hmbatch_path)
    script_path = _write_run_script(script)
    command = [str(exe), "-noexit", "-tcl", str(script_path)]

    model = _normalize_path(model_path)
    if model:
        if not model.exists():
            raise FileNotFoundError(f"Model file was not found: {model}")
        command.append(str(model))

    env = os.environ.copy()
    env.setdefault("ALTAIR_HOME", str(DEFAULT_HYPERMESH_DIR.parents[4]))

    try:
        completed = subprocess.run(
            command,
            cwd=str(script_path.parent),
            env=env,
            capture_output=True,
            text=True,
            timeout=max(1, int(timeout_seconds)),
        )
        return {
            "success": completed.returncode == 0,
            "returncode": completed.returncode,
            "command": command,
            "script_path": str(script_path),
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "success": False,
            "timeout": True,
            "command": command,
            "script_path": str(script_path),
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "message": f"hmbatch did not finish within {timeout_seconds} seconds.",
        }


def _extract_probe_lines(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith(("MCP_PROBE_BEGIN", "MCP_PROBE_SOLID", "MCP_PROBE_END"))
    ]


_PROBE_TCL_TEMPLATE = r"""
proc find_best_source_surface {solid_id drag_axis sbb_min sbb_max} {
    *createmark surfaces 1 "by solids" $solid_id
    set all_surfs [hm_getmark surfaces 1]
    set best_surf -1
    set best_score 999999

    # 闁哄秷顫夊畵?drag_axis 缁绢収鍠栭悾?bbox 闁轰焦澹嗙划宥夋儍?index
    if {$drag_axis eq "x"} {
        set ax_idx 0; set ax_idx_max 3
        set ax1_idx 1; set ax1_max 4
        set ax2_idx 2; set ax2_max 5
    } else { if {$drag_axis eq "y"} {
        set ax_idx 1; set ax_idx_max 4
        set ax1_idx 0; set ax1_max 3
        set ax2_idx 2; set ax2_max 5
    } else {
        set ax_idx 2; set ax_idx_max 5
        set ax1_idx 0; set ax1_max 3
        set ax2_idx 1; set ax2_max 4
    }}

    set solid_ax_len [expr {abs([lindex $sbb_max $ax_idx] \
                           - [lindex $sbb_min $ax_idx])}]
    set tolerance [expr {$solid_ax_len * 0.05}]

    foreach sid $all_surfs {
        *createmark surfaces 2 $sid
        if {[catch {hm_getboundingbox surfaces 2 0 0 0} s_sbb]} {continue}

        # === 濡ょ姴鐭侀惁?1闁挎稒宀稿鏉库柦?drag_axis 闁哄倻鎳撻幃婊堝储濮橆剙顔?闁?0 ===
        set s_thick [expr {abs([lindex $s_sbb $ax_idx_max] \
                          - [lindex $s_sbb $ax_idx])}]
        if {$s_thick > $tolerance} {continue}

        # === 濡ょ姴鐭侀惁?2闁挎稒宀稿浼存煂瀹ュ懐濡囬柛?solid 闁?drag_axis 缂佹棏鍨抽崑?===
        set s_center [expr {([lindex $s_sbb $ax_idx] \
                        + [lindex $s_sbb $ax_idx_max]) / 2.0}]
        set dist_to_min [expr {abs($s_center - [lindex $sbb_min $ax_idx])}]
        set dist_to_max [expr {abs($s_center - [lindex $sbb_max $ax_idx])}]
        if {$dist_to_min > $tolerance && $dist_to_max > $tolerance} {continue}

        # === 濡ょ姴鐭侀惁?3闁挎稒宀稿浼村捶閵娿儱寰撳ù鐘崇墧鐞氳鲸绋夐鍡樻▕閹艰揪濡囧▓鎴犱焊閸濆嫷鍤熼柛鏍х秺閸?solid 闁规惌浜?===
        set s_ax1 [expr {abs([lindex $s_sbb $ax1_max] \
                        - [lindex $s_sbb $ax1_idx])}]
        set s_ax2 [expr {abs([lindex $s_sbb $ax2_max] \
                        - [lindex $s_sbb $ax2_idx])}]
        set sol_ax1 [expr {abs([lindex $sbb_max $ax1_idx] \
                          - [lindex $sbb_min $ax1_idx])}]
        set sol_ax2 [expr {abs([lindex $sbb_max $ax2_idx] \
                          - [lindex $sbb_min $ax2_idx])}]
        set r1 [expr {$s_ax1 / max($sol_ax1, 0.001)}]
        set r2 [expr {$s_ax2 / max($sol_ax2, 0.001)}]
        if {$r1 < 0.9 || $r2 < 0.9} {continue}

        # === 閻犲洤瀚崹搴ㄦ晬濮橆剙绲块柡鍫氬亾濞达絽鍟跨亸顕€鏌?===
        set score [expr {min($dist_to_min, $dist_to_max) \
                    + abs(1.0-$r1)*10 + abs(1.0-$r2)*10}]
        if {$score < $best_score} {set best_score $score; set best_surf $sid}
    }
    return $best_surf
}

set f [open "__OUTPUT_PATH__" w]
*createmark solids 1 "all"
set solid_ids [hm_getmark solids 1]
foreach sid $solid_ids {
    set comp_name "NONE"
    *createmark components 1 "by solids" $sid
    set cids [hm_getmark components 1]
    if {[llength $cids] > 0} { catch {set comp_name [hm_getvalue components id=[lindex $cids 0] dataname=name]} }
    *createmark surfaces 1 "by solids" $sid
    set surf_ids [hm_getmark surfaces 1]
    set sc [llength $surf_ids]
    set bb [hm_getboundingbox surfaces 1]
    set dx [expr {[lindex $bb 3] - [lindex $bb 0]}]
    set dy [expr {[lindex $bb 4] - [lindex $bb 1]}]
    set dz [expr {[lindex $bb 5] - [lindex $bb 2]}]
    if {$dx <= $dy && $dx <= $dz} { set mn $dx; if {$dy <= $dz} {set md $dy; set mx $dz} else {set md $dz; set mx $dy} } else { if {$dy <= $dx && $dy <= $dz} { set mn $dy; if {$dx <= $dz} {set md $dx; set mx $dz} else {set md $dz; set mx $dx} } else { set mn $dz; if {$dx <= $dy} {set md $dx; set mx $dy} else {set md $dy; set mx $dx} } }
    set slender 1.0; if {$mn > 0.001} {set slender [expr {$mx / $mn}]}
    set diag [expr {sqrt($dx*$dx + $dy*$dy + $dz*$dz)}]
    # 婵犙勫姍濞兼澘螞閳ь剙霉鐎ｅ墎绐楅柟鍨劤閸ゎ參寮甸埀顒勬寘閸曨剚鐓欓柛姘灱缁€瀣博椤栨粍鐣遍梻?
    set drag_axis ""
    if {$mn == $dx} {set drag_axis "x"} elseif {$mn == $dy} {set drag_axis "y"} else {set drag_axis "z"}
    
    set sbb_min [list [lindex $bb 0] [lindex $bb 1] [lindex $bb 2]]
    set sbb_max [list [lindex $bb 3] [lindex $bb 4] [lindex $bb 5]]
    set src_surf [find_best_source_surface $sid $drag_axis $sbb_min $sbb_max]
    
    puts $f "PROBE: solid=$sid comp=\"$comp_name\" sc=$sc dx=[format %.3f $dx] dy=[format %.3f $dy] dz=[format %.3f $dz] mn=[format %.3f $mn] mx=[format %.3f $mx] md=[format %.3f $md] slender=[format %.2f $slender] diag=[format %.3f $diag] src_surf=$src_surf drag_axis=$drag_axis"
}
close $f
"""


@mcp.tool()
def run_geometry_probe_gui(
    host: str = "127.0.0.1",
    port: int = DEFAULT_GUI_PORT,
    output_path: str | None = None,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """MANDATORY Phase 1: probe EVERY solid in the current HyperMesh model via GUI listener."""
    out = _normalize_path(output_path) if output_path else (
        Path(os.environ.get("TEMP", "/tmp")) / "hypermesh_probe_output.txt"
    )
    tcl = _PROBE_TCL_TEMPLATE.replace("__OUTPUT_PATH__", str(out).replace("\\", "/"))
    (Path(str(out)).parent / "_debug_tcl.txt").write_text(tcl, encoding="utf-8")
    result = execute_tcl_gui(script=tcl, host=host, port=port, timeout_seconds=timeout_seconds, enforce_meshing_rules=False)
    print("DEBUG_PROBE result:", result)  # 闁告梻濮剧换鏍偘?

    probe_lines = ""
    if out.exists():
        probe_lines = out.read_text(encoding="utf-8", errors="replace")
    return {
        "success": result.get("success", False),
        "phase": "Phase 1",
        "output_file": str(out),
        "probe_lines": probe_lines,
        "_debug_result": result,
    }


# Global state to prevent AI from truncating the large JSON payload inside MCP tool calls
_GLOBAL_CLASSIFICATION_RESULTS = {}
_GLOBAL_PHASE2_FINALIZED = False

@mcp.tool()
def classify_all_solids_from_probe(probe_lines, visual_observations=None):
    """MANDATORY Phase 2: classify every probed solid from probe_lines."""
    global _GLOBAL_CLASSIFICATION_RESULTS, _GLOBAL_PHASE2_FINALIZED
    results = {}
    for line in probe_lines.splitlines():
        stripped = line.strip()
        if not stripped or not (
            stripped.startswith("PROBE:")
            or stripped.startswith("MCP_PROBE_SOLID")
        ):
            continue
        f = _parse_probe_facts(line)
        sid = f.get("solid_id", 0)
        if sid <= 0 or f.get("dx", 0) <= 0:
            continue
        dx, dy, dz = f["dx"], f["dy"], f["dz"]
        slender = f.get("slender", 1)
        sc = f.get("surf_count", 6)
        dims = sorted([dx, dy, dz])
        mn, md, mx = dims[0], dims[1], dims[2]
        is_circular = (dx > 0 and dy > 0 and abs(dx - dy) / max(dx, dy) < 0.20)
        strategy = "tetra_plain"
        evidence = []
        elem_size = 1.0
        min_elem_size = 0.5
        src_surf = f.get("source_surface_id", -1)
        
        if sc == 6 and mx > 0 and mn / mx < 0.40 and src_surf > 0:
            strategy = "drag_hex"
            evidence.append("6-face thin -> drag")
        elif is_circular and slender > 2.5 and mx > 2 * md and sc <= 10:
            strategy = "spin_hex"
            evidence.append("shaft -> spin")
        elif is_circular and slender < 3 and mx < 2 * md and 6 < sc <= 10:
            strategy = "spin_hex"
            evidence.append("compact -> spin")
        elif slender > 15 and mn < 2.0:
            strategy = "tetra_plain"
            evidence.append("thin plate")
        elif slender > 4 and mx > 3 * md:
            strategy = "tetra_plain"
            evidence.append("shaft -> tetra")
            elem_size = min(max(0.6, md / 6.0), 2.0)
        elif sc >= 20:
            strategy = "tetra_plain"
            evidence.append("complex")
            if sc >= TETRA_VERY_COMPLEX_SURFACE_COUNT:
                min_elem_size = TETRA_VERY_COMPLEX_MIN_ELEMENT_SIZE
                evidence.append("very high surface count -> smaller surface-deviation min size")
            elif sc >= TETRA_COMPLEX_SURFACE_COUNT:
                min_elem_size = TETRA_COMPLEX_MIN_ELEMENT_SIZE
                evidence.append("high surface count -> smaller surface-deviation min size")
        else:
            strategy = "tetra_plain"
            evidence.append("general")
        if strategy == "tetra_plain":
            if sc >= TETRA_VERY_COMPLEX_SURFACE_COUNT:
                min_elem_size = min(min_elem_size, TETRA_VERY_COMPLEX_MIN_ELEMENT_SIZE)
            elif sc >= TETRA_COMPLEX_SURFACE_COUNT:
                min_elem_size = min(min_elem_size, TETRA_COMPLEX_MIN_ELEMENT_SIZE)
            min_elem_size = max(0.20, min(0.50, min_elem_size))
            if sc >= TETRA_VERY_COMPLEX_SURFACE_COUNT:
                max_shell_before_tetmesh = TETRA_VERY_COMPLEX_MAX_SHELL_ELEMENTS
            elif sc >= TETRA_COMPLEX_SURFACE_COUNT:
                max_shell_before_tetmesh = TETRA_COMPLEX_MAX_SHELL_ELEMENTS
            else:
                max_shell_before_tetmesh = TETRA_MAX_SHELL_ELEMENTS
            allow_tetmesh = True
            allow_surface_mesh = True
        else:
            max_shell_before_tetmesh = TETRA_MAX_SHELL_ELEMENTS
            allow_tetmesh = True
            allow_surface_mesh = True
        name = _generate_geometry_component_name(f, strategy, suffix_solid_id=True)
        results[str(sid)] = {
            "solid_id": sid, "strategy": strategy, "component_name": name,
            "element_size": round(elem_size, 2),
            "min_element_size": round(min_elem_size, 3),
            "max_shell_elements_before_tetmesh": max_shell_before_tetmesh,
            "allow_tetmesh": allow_tetmesh,
            "allow_surface_mesh": allow_surface_mesh,
            "evidence": evidence,
            "source_surface_id": f.get("source_surface_id", -1),
            "drag_axis": f.get("drag_axis", ""),
            "gear_axis": f.get("drag_axis", ""),
            "geometry_confirms_gear_teeth": strategy == "gear_aware_tetra",
            "surf_count": sc,
            "dims": {"dx": dx, "dy": dy, "dz": dz, "mn": mn, "md": md, "mx": mx},
        }
    counts = {}
    for r in results.values():
        counts[r["strategy"]] = counts.get(r["strategy"], 0) + 1
    tetra_batches = _tetra_execution_batches(results)
        
    # Save to global cache so downstream tools don't require the AI to pass back a massive dict
    _GLOBAL_CLASSIFICATION_RESULTS.clear()
    _GLOBAL_CLASSIFICATION_RESULTS.update(results)
    _GLOBAL_PHASE2_FINALIZED = False
    
    finalize = generate_phase2_finalize_tcl({"results": results})
    return {
        "success": True,
        "phase": "Phase 2",
        "total_solids": len(results),
        "strategy_counts": counts,
        "results": results,
        "phase3_tetra_batches": tetra_batches,
        "phase3_tetra_batching_required": True,
        "phase2_finalize_script": finalize.get("script", ""),
        "phase2_finalize_required": True,
        "required_next_step": (
            "Execute phase2_finalize_script with execute_tcl_gui before Phase 3 meshing. "
            "During Phase 3, process tetra solids in phase3_tetra_batches order; do not "
            "run high-risk tetra solids back-to-back in solid-id order."
        ),
        "completion_token": "MCP_FINALIZE_OK",
    }


@mcp.tool()
def suggest_component_names(probe_lines):
    """Generate geometry-based component names from probe_lines."""
    names = {}
    for line in probe_lines.splitlines():
        stripped = line.strip()
        if not stripped or not stripped.startswith("PROBE:"):
            continue
        f = _parse_probe_facts(line)
        sid = f.get("solid_id", 0)
        if sid <= 0 or f.get("dx", 0) <= 0:
            continue
        names[str(sid)] = _generate_geometry_component_name(f, "tetra_plain")
    return {"success": True, "count": len(names), "names": names}


@mcp.tool()
def generate_rename_components_tcl(classification_results: dict | None = None):
    """Generate Tcl to rename HyperMesh components from classification results."""
    lines = ["# Rename components Tcl", "set renamed 0"]
    target_results = _classification_results_from_input(classification_results)

    for sid_str, info in target_results.items():
        name = info.get("component_name", "")
        if not name:
            continue
        lines.append('*createmark components 1 "by solids" ' + sid_str)
        lines.append("set cids [hm_getmark components 1]")
        lines.append("if {[llength $cids] > 0} {")
        lines.append("    set cid [lindex $cids 0]")
        lines.append("    set oldname [hm_getvalue components id=$cid dataname=name]")
        lines.append('    catch {*renamecollector components "$oldname" "' + name + '"}')
        lines.append("    incr renamed")
        lines.append("}")
    lines.append('puts "RENAMED: $renamed components"')
    return {"success": True, "tcl_script": "\n".join(lines)}


@mcp.tool()
def generate_component_colors_tcl(classification_results: dict | None = None):
    """Generate Tcl to assign unique colors to each component."""
    lines = ["# Color components uniquely", "set colored 0"]
    target_results = _classification_results_from_input(classification_results)

    # Keep the original Phase 2 behavior: assign one visible unique color per
    # renamed component, cycling through HyperMesh color ids.
    color_idx = 0
    for sid_str, info in target_results.items():
        name = info.get("component_name", "")
        if name:
            color = (color_idx % 64) + 1
            lines.append(f'*createmark components 1 "{name}"')
            lines.append(f"catch {{*colormark components 1 {color}}}")
            color_idx += 1
    lines.append(f"set colored [expr {{$colored + {color_idx}}}]")
    lines.append('puts "COLORED: $colored components"')
    return {"success": True, "tcl_script": "\n".join(lines)}


@mcp.tool()
def generate_finalize_components_tcl(assignments: list[dict[str, Any]]) -> dict[str, Any]:
    """Generate Tcl that finalizes components after meshing.

    This step is mandatory after meshing.
    It renames components, colors them by strategy, and emits MCP_FINALIZE_OK.

    assignments example:
    [
        {
            "component_id": 12,
            "old_name": "component12",
            "new_name": "shaft_ring_s12",
            "strategy": "drag_hex"
        }
    ]
    """
    if not assignments:
        return {"success": False, "message": "assignments cannot be empty."}

    lines: list[str] = [
        "# MCP finalization",
        "# MCP_FINALIZE_COMPONENTS",
        'puts "MCP_FINALIZE_BEGIN"',
    ]

    finalized_count = 0

    for item in assignments:
        component_id_raw = item.get("component_id")
        component_id = int(component_id_raw) if component_id_raw not in (None, "") else None
        solid_id_raw = item.get("solid_id")
        solid_id = int(solid_id_raw) if solid_id_raw not in (None, "") else None
        old_name = item.get("old_name")
        new_name = item.get("new_name")
        strategy = str(item.get("strategy", "unknown"))
        if strategy in {"tetra_surface_deviation_rtrias", "surface_tetra"}:
            strategy = "tetra_plain"

        if not new_name:
            return {"success": False, "message": f"new_name is required for assignment: {item}"}

        lines.extend(
            _component_rename_tcl(
                component_id=component_id,
                old_name=str(old_name) if old_name else None,
                new_name=str(new_name),
                solid_id=solid_id,
            )
        )
        lines.extend(
            _component_color_tcl(
                component_id=component_id,
                component_name=str(new_name),
                strategy=strategy,
                solid_id=solid_id,
            )
        )

        safe_new_name = _tcl_escape_name(str(new_name))
        lines.append(f'puts "MCP_FINALIZED_COMP name={safe_new_name} strategy={strategy}"')
        finalized_count += 1

    lines.append(f'puts "MCP_FINALIZE_OK count={finalized_count}"')

    return {
        "success": True,
        "script": "\n".join(lines),
        "required_next_step": "execute_tcl_gui or execute_tcl",
        "completion_token": "MCP_FINALIZE_OK",
    }


@mcp.tool()
def generate_phase2_finalize_tcl(classification_results: dict | None = None) -> dict[str, Any]:
    """Generate mandatory Phase 2 Tcl from cached or supplied classification results."""
    target_results = _classification_results_from_input(classification_results)
    if not target_results:
        return {
            "success": False,
            "message": "No classification results are available. Run classify_all_solids_from_probe first.",
        }
    rename = generate_rename_components_tcl({"results": target_results})
    colors = generate_component_colors_tcl({"results": target_results})
    body = "\n".join(
        [
            "# MCP Phase 2 finalization",
            "# MCP_FINALIZE_COMPONENTS",
            'puts "MCP_FINALIZE_BEGIN"',
            rename["tcl_script"],
            colors["tcl_script"],
            'puts "MCP_FINALIZE_OK"',
        ]
    )
    return {
        "success": True,
        "script": _wrap_generated_tcl("generate_phase2_finalize_tcl", body),
        "phase": "Phase 2 finalization",
        "required": True,
        "required_next_step": "execute_tcl_gui or execute_tcl",
        "completion_token": "MCP_FINALIZE_OK",
    }


@mcp.tool()
def get_hypermesh_meshing_strategy() -> dict[str, Any]:
    """Return the local HyperMesh meshing strategy requested by the user."""
    return {
        "success": True,
        "strategy": HYPERMESH_MESHING_STRATEGY.strip(),
        "generic_rules": GENERIC_MESHING_RULES,
        "special_workflows": SPECIAL_WORKFLOWS,
        "default_hmbatch": str(DEFAULT_HMBATCH),
    }


@mcp.tool()
def get_meshing_rules() -> dict[str, Any]:
    """Return generic HyperMesh meshing rules without hard-coded component names."""
    return {
        "success": True,
        "generic_rules": GENERIC_MESHING_RULES,
        "special_workflows": SPECIAL_WORKFLOWS,
        "notes": [
            "CRITICAL: See generic_rules['critical_rule_no_manual_tcl_injection'] 闁?NEVER inject hand-written Tcl when a generate_*_tcl parameter already covers the scenario.",
            "CRITICAL: See generic_rules['critical_rule_every_change_must_enforce'] 闁?every MCP change MUST include enforcement in GENERIC_MESHING_RULES and/or _meshing_rule_violation.",
            "CRITICAL: See generic_rules['critical_rule_phase2_color_mandatory'] - execute Phase 2 finalization before meshing.",
            "Do not decide tetra/drag/spin by component name.",
            "Classify by geometry: holes/flanges/bosses/cutouts -> tetra; simple constant extrusions with matched quad source face -> drag; clean true cross-section revolved bodies -> spin.",
            "For stepped or recessed revolved solids, use the generic cut-section spin workflow rather than guessed surface-id spin.",
            "quality cleanup should prefer strategy changes and local repair; do not blindly refine or delete unfixable bad elements.",
            "visible GUI mode changes where the Tcl runs; meshing logic and input/output paths remain explicit.",
        ],
    }


@mcp.tool()
def get_cutsection_spin_workflow() -> dict[str, Any]:
    """Return the generic cut-section spin workflow for stepped/recessed revolved solids."""
    return {
        "success": True,
        "workflow": SPECIAL_WORKFLOWS["cutsection_spin_hex"],
        "gui_mode": SPECIAL_WORKFLOWS["visible_gui_mode"],
        "quality_policy": SPECIAL_WORKFLOWS["quality_policy"],
    }


@mcp.tool()
def generate_geometry_probe_tcl(
    solid_ids: list[int] | None = None,
    probe_element_size: float = 5.0,
    min_element_size: float = 0.5,
    max_deviation: float = 0.2,
    max_feature_angle: float = 20.0,
    growth_rate: float = 1.3,
) -> dict[str, Any]:
    """Generate Tcl that temporarily meshes each solid to report bbox/size facts."""
    if probe_element_size <= 0:
        raise ValueError("probe_element_size must be greater than 0.")
    if min_element_size <= 0:
        raise ValueError("min_element_size must be greater than 0.")
    if max_deviation < 0:
        raise ValueError("max_deviation must be non-negative.")

    if solid_ids:
        ids = " ".join(str(int(value)) for value in solid_ids)
        solid_setup = [f"set target_solids {{{ids}}}"]
    else:
        solid_setup = [
            '*createmark solids 1 "all"',
            "set target_solids [hm_getmark solids 1]",
        ]

    max_size = max(float(probe_element_size) * 2.0, float(min_element_size))
    lines = [
        "# HyperMesh MCP geometry probe",
        "# Temporary coarse surface mesh for geometry inspection; final mesh is not kept.",
        f"set probe_size {float(probe_element_size)}",
        f"set probe_min_size {float(min_element_size)}",
        f"set probe_max_size {max_size}",
        f"set probe_max_dev {float(max_deviation)}",
        f"set probe_feature_angle {float(max_feature_angle)}",
        f"set probe_growth {float(growth_rate)}",
        'set ::mcp_probe_output ""',
        "proc mcp_probe_line {line} {append ::mcp_probe_output $line \"\\n\"}",
        "proc mcp_mark_count {entity mark_id} {",
        "    if {[catch {hm_marklength $entity $mark_id} n]} {return 0}",
        "    return $n",
        "}",
        "proc mcp_all_elems {} {",
        '    *createmark elems 1 "all"',
        "    return [hm_getmark elems 1]",
        "}",
        "proc mcp_all_nodes {} {",
        '    *createmark nodes 1 "all"',
        "    return [hm_getmark nodes 1]",
        "}",
        "proc mcp_list_subtract {a b} {",
        "    array set seen {}",
        "    foreach x $b {set seen($x) 1}",
        "    set out {}",
        "    foreach x $a {if {![info exists seen($x)]} {lappend out $x}}",
        "    return $out",
        "}",
        "proc mcp_delete_elems {elems} {",
        "    if {[llength $elems] == 0} {return}",
        "    eval *createmark elems 1 $elems",
        "    catch {*deletemark elems 1}",
        "}",
        "proc mcp_delete_nodes {nodes} {",
        "    if {[llength $nodes] == 0} {return}",
        "    eval *createmark nodes 1 $nodes",
        "    catch {*deletemark nodes 1}",
        "}",
        "proc mcp_best_source_surface {solid_id drag_axis solid_bb} {",
        "    *createmark surfaces 1 \"by solids\" $solid_id",
        "    set all_surfs [hm_getmark surfaces 1]",
        "    set best_surf -1",
        "    set best_score 1.0e30",
        "    if {$drag_axis eq \"x\"} {",
        "        set ax_min 0; set ax_max 3; set a1_min 1; set a1_max 4; set a2_min 2; set a2_max 5",
        "    } elseif {$drag_axis eq \"y\"} {",
        "        set ax_min 1; set ax_max 4; set a1_min 0; set a1_max 3; set a2_min 2; set a2_max 5",
        "    } else {",
        "        set ax_min 2; set ax_max 5; set a1_min 0; set a1_max 3; set a2_min 1; set a2_max 4",
        "    }",
        "    set solid_ax_len [expr {abs([lindex $solid_bb $ax_max] - [lindex $solid_bb $ax_min])}]",
        "    set tolerance [expr {max($solid_ax_len * 0.08, 0.05)}]",
        "    set solid_a1 [expr {abs([lindex $solid_bb $a1_max] - [lindex $solid_bb $a1_min])}]",
        "    set solid_a2 [expr {abs([lindex $solid_bb $a2_max] - [lindex $solid_bb $a2_min])}]",
        "    foreach surf_id $all_surfs {",
        "        *createmark surfaces 2 $surf_id",
        "        if {[catch {hm_getboundingbox surfaces 2 0 0 0} sbb] || [llength $sbb] < 6} {continue}",
        "        set surf_thick [expr {abs([lindex $sbb $ax_max] - [lindex $sbb $ax_min])}]",
        "        if {$surf_thick > $tolerance} {continue}",
        "        set surf_center [expr {([lindex $sbb $ax_min] + [lindex $sbb $ax_max]) / 2.0}]",
        "        set dist_min [expr {abs($surf_center - [lindex $solid_bb $ax_min])}]",
        "        set dist_max [expr {abs($surf_center - [lindex $solid_bb $ax_max])}]",
        "        if {$dist_min > $tolerance && $dist_max > $tolerance} {continue}",
        "        set surf_a1 [expr {abs([lindex $sbb $a1_max] - [lindex $sbb $a1_min])}]",
        "        set surf_a2 [expr {abs([lindex $sbb $a2_max] - [lindex $sbb $a2_min])}]",
        "        set r1 [expr {$surf_a1 / max($solid_a1, 0.001)}]",
        "        set r2 [expr {$surf_a2 / max($solid_a2, 0.001)}]",
        "        if {$r1 < 0.85 || $r2 < 0.85} {continue}",
        "        set score [expr {min($dist_min, $dist_max) + abs(1.0 - $r1) * 10.0 + abs(1.0 - $r2) * 10.0}]",
        "        if {$score < $best_score} {set best_score $score; set best_surf $surf_id}",
        "    }",
        "    return $best_surf",
        "}",
        *solid_setup,
        'mcp_probe_line "MCP_PROBE_BEGIN solid_count=[llength $target_solids] probe_size=$probe_size"',
        "foreach sid $target_solids {",
        "    set before_elems [mcp_all_elems]",
        "    set before_nodes [mcp_all_nodes]",
        "    *createmark solids 1 $sid",
        "    if {[mcp_mark_count solids 1] == 0} {",
        '        mcp_probe_line "MCP_PROBE_SOLID id=$sid exists=0"',
        "        continue",
        "    }",
        "    *createmark surfs 1 \"by solids\" $sid",
        "    set surf_count [mcp_mark_count surfs 1]",
        "    if {$surf_count == 0} {",
        '        mcp_probe_line "MCP_PROBE_SOLID id=$sid exists=1 surf_count=0 elem_count=0 bbox_ok=0"',
        "        continue",
        "    }",
        "    set mesh_err \"\"",
        "    *createarray 3 0 0 0",
        "    if {[catch {*defaultmeshsurf_growth 1 $probe_size 3 3 2 1 1 1 35 0 $probe_min_size $probe_max_size $probe_max_dev $probe_feature_angle $probe_growth 1 3 1 0} mesh_err]} {",
        '        mcp_probe_line "MCP_PROBE_SOLID id=$sid exists=1 surf_count=$surf_count elem_count=0 bbox_ok=0 mesh_error={$mesh_err}"',
        "        set failed_elems [mcp_list_subtract [mcp_all_elems] $before_elems]",
        "        set failed_nodes [mcp_list_subtract [mcp_all_nodes] $before_nodes]",
        "        mcp_delete_elems $failed_elems",
        "        mcp_delete_nodes $failed_nodes",
        "        continue",
        "    }",
        "    set new_elems [mcp_list_subtract [mcp_all_elems] $before_elems]",
        "    set new_nodes [mcp_list_subtract [mcp_all_nodes] $before_nodes]",
        "    set tri_count 0",
        "    set quad_count 0",
        "    foreach eid $new_elems {",
        "        if {[catch {hm_getvalue elems id=$eid dataname=config} cfg]} {continue}",
        "        if {$cfg == 103 || $cfg == 106} {incr tri_count}",
        "        if {$cfg == 104 || $cfg == 108} {incr quad_count}",
        "    }",
        "    set bbox_ok 0",
        "    set dx 0.0; set dy 0.0; set dz 0.0; set diag 0.0; set slender 0.0",
        "    if {[llength $new_elems] > 0} {",
        "        eval *createmark elems 2 $new_elems",
        "        if {![catch {hm_getboundingbox elems 2 0 0 0} bb] && [llength $bb] >= 6} {",
        "            set bbox_ok 1",
        "            set dx [expr {abs([lindex $bb 3] - [lindex $bb 0])}]",
        "            set dy [expr {abs([lindex $bb 4] - [lindex $bb 1])}]",
        "            set dz [expr {abs([lindex $bb 5] - [lindex $bb 2])}]",
        "            set diag [expr {sqrt($dx*$dx + $dy*$dy + $dz*$dz)}]",
        "            set min_dim [expr {min($dx, min($dy, $dz))}]",
        "            set max_dim [expr {max($dx, max($dy, $dz))}]",
        "            if {$min_dim > 0} {set slender [expr {$max_dim / $min_dim}]}",
        "        }",
        "    }",
        "    set drag_axis \"\"",
        "    set src_surf -1",
        "    if {$bbox_ok} {",
        "        if {$dx <= $dy && $dx <= $dz} {set drag_axis \"x\"} elseif {$dy <= $dx && $dy <= $dz} {set drag_axis \"y\"} else {set drag_axis \"z\"}",
        "        set src_surf [mcp_best_source_surface $sid $drag_axis $bb]",
        "    }",
        '    mcp_probe_line "MCP_PROBE_SOLID id=$sid exists=1 surf_count=$surf_count elem_count=[llength $new_elems] node_count=[llength $new_nodes] tri_count=$tri_count quad_count=$quad_count bbox_ok=$bbox_ok dx=$dx dy=$dy dz=$dz diag=$diag slender=$slender src_surf=$src_surf drag_axis=$drag_axis"',
        "    mcp_delete_elems $new_elems",
        "    mcp_delete_nodes $new_nodes",
        "}",
        'mcp_probe_line "MCP_PROBE_END"',
        "return $::mcp_probe_output",
    ]
    return {
        "success": True,
        "script": "\n".join(lines) + "\n",
        "strategy": "Temporary geometry probe only; generated shells/nodes are deleted before returning.",
    }


@mcp.tool()
def recommend_tetra_sizes_from_probe_lines(
    probe_lines: list[str],
    base_element_size: float = 4.0,
    min_element_size: float = 0.6,
    thin_slender_threshold: float = 8.0,
    thin_dimension_factor: float = 0.2,
) -> dict[str, Any]:
    """Recommend per-solid tetra sizes from MCP_PROBE_SOLID geometry facts."""
    if base_element_size <= 0:
        raise ValueError("base_element_size must be greater than 0.")
    if min_element_size <= 0:
        raise ValueError("min_element_size must be greater than 0.")
    if thin_slender_threshold <= 0:
        raise ValueError("thin_slender_threshold must be greater than 0.")
    if thin_dimension_factor <= 0:
        raise ValueError("thin_dimension_factor must be greater than 0.")

    recommendations: list[dict[str, Any]] = []
    for line in probe_lines:
        stripped = line.strip()
        if not (stripped.startswith("MCP_PROBE_SOLID") or stripped.startswith("PROBE:")):
            continue
        facts: dict[str, str] = {}
        for token in stripped.split()[1:]:
            if "=" in token:
                key, value = token.split("=", 1)
                facts[key] = value
        if stripped.startswith("PROBE:"):
            try:
                solid_id = int(facts.get("solid", facts.get("id", "0")))
            except (KeyError, ValueError):
                continue
        else:
            try:
                solid_id = int(facts["id"])
            except (KeyError, ValueError):
                continue
        if facts.get("exists") == "0" or facts.get("bbox_ok") == "0":
            recommendations.append(
                {
                    "solid_id": solid_id,
                    "strategy": "inspect_manually",
                    "reason": "Probe could not obtain a usable temporary mesh bbox.",
                }
            )
            continue
        dims = []
        for key in ("dx", "dy", "dz"):
            try:
                dims.append(float(facts.get(key, "0")))
            except ValueError:
                dims.append(0.0)
        positive_dims = [value for value in dims if value > 0]
        min_dim = min(positive_dims) if positive_dims else 0.0
        max_dim = max(positive_dims) if positive_dims else 0.0
        try:
            slender = float(facts.get("slender", "0"))
        except ValueError:
            slender = 0.0
        try:
            surf_count = int(facts.get("surf_count", "0"))
        except ValueError:
            surf_count = 0

        size = float(base_element_size)
        reasons = ["default base size"]
        is_thin = slender >= thin_slender_threshold
        is_small_complex = surf_count >= 10 and min_dim > 0 and min_dim < base_element_size * 2.5
        if min_dim > 0 and (is_thin or is_small_complex or min_dim < base_element_size * 1.5):
            size = min(size, max(float(min_element_size), min_dim * float(thin_dimension_factor)))
            reasons = [
                "thin/small or small-complex feature detected from probe bbox",
                f"min_dim={min_dim:g}",
                f"slender={slender:g}",
                f"surf_count={surf_count}",
            ]
        if max_dim > 0 and size > max_dim / 4.0:
            size = max(float(min_element_size), max_dim / 4.0)
            reasons.append("limited by max dimension")

        recommendations.append(
            {
                "solid_id": solid_id,
                "strategy": "tetra_surface_deviation_rtrias",
                "recommended_element_size": round(size, 4),
                "min_dimension": round(min_dim, 4),
                "max_dimension": round(max_dim, 4),
                "slender": round(slender, 4),
                "surf_count": surf_count,
                "reason": "; ".join(reasons),
            }
        )

    return {
        "success": True,
        "count": len(recommendations),
        "recommendations": recommendations,
    }


@mcp.tool()
def locate_hypermesh() -> dict[str, Any]:
    """Locate candidate HyperMesh batch and visible-GUI executables."""
    batch_found = [str(path) for path in _candidate_hmbatch_paths() if path.exists()]
    gui_found = [str(path) for path in _candidate_hypermesh_gui_paths() if path.exists()]
    selected = batch_found[0] if batch_found else None
    selected_gui = gui_found[0] if gui_found else None
    return {
        "success": selected is not None or selected_gui is not None,
        "selected": selected,
        "selected_gui": selected_gui,
        "found": batch_found,
        "found_gui": gui_found,
        "hint": (
            "Set HYPERMESH_BATCH_EXE for background batch mode, or "
            "HYPERMESH_GUI_EXE for visible GUI mode."
        ),
    }


@mcp.tool()
def create_gui_listener_tcl(
    host: str = "127.0.0.1",
    port: int = DEFAULT_GUI_PORT,
) -> dict[str, Any]:
    """Create the Tcl listener script used by a visible HyperMesh GUI session."""
    script_path = _write_run_script(_gui_listener_script(host=host, port=port))
    return {
        "success": True,
        "script_path": str(script_path),
        "host": host,
        "port": int(port),
        "how_to_use": (
            "Open HyperMesh visibly, then source this Tcl file in the Tcl command "
            "window. After that, execute_tcl_gui can send Tcl into the visible session."
        ),
    }


@mcp.tool()
def start_hypermesh_gui_listener(
    gui_path: str | None = None,
    model_path: str | None = None,
    host: str = "127.0.0.1",
    port: int = DEFAULT_GUI_PORT,
) -> dict[str, Any]:
    """Start visible HyperMesh and ask it to source the MCP GUI listener Tcl."""
    exe = _resolve_hypermesh_gui(gui_path)
    script_path = _write_run_script(_gui_listener_script(host=host, port=port))
    command = [str(exe), "-tcl", str(script_path)]

    model = _normalize_path(model_path)
    if model:
        if not model.exists():
            raise FileNotFoundError(f"Model file was not found: {model}")
        command.append(str(model))

    env = os.environ.copy()
    env.setdefault("ALTAIR_HOME", str(DEFAULT_HYPERMESH_DIR.parents[4]))
    process = subprocess.Popen(
        command,
        cwd=str(script_path.parent),
        env=env,
        close_fds=True,
    )
    return {
        "success": True,
        "pid": process.pid,
        "command": command,
        "script_path": str(script_path),
        "host": host,
        "port": int(port),
        "note": (
            "HyperMesh should open visibly. If this HyperMesh version ignores "
            "-tcl for GUI startup, open HyperMesh manually and source script_path."
        ),
    }


@mcp.tool()
def check_hypermesh_connection(hmbatch_path: str | None = None) -> dict[str, Any]:
    """Check whether hmbatch.exe can be found and started."""
    exe = _resolve_hmbatch(hmbatch_path)
    command = [str(exe), "-help"]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return {
            "success": True,
            "executable": str(exe),
            "returncode": completed.returncode,
            "stdout": completed.stdout[:4000],
            "stderr": completed.stderr[:4000],
        }
    except subprocess.TimeoutExpired:
        return {
            "success": True,
            "executable": str(exe),
            "warning": (
                "Process started but did not exit within 20 seconds. "
                "This can happen when license or GUI startup blocks batch probing."
            ),
        }


@mcp.tool()
def generate_surface_automesh_tcl(
    element_size: float,
    surface_ids: list[int] | None = None,
    output_hm_path: str | None = None,
) -> dict[str, Any]:
    """Generate Tcl for a simple 2D surface automesh on existing HyperMesh surfaces."""
    if element_size <= 0:
        raise ValueError("element_size must be greater than 0.")

    if surface_ids:
        ids = " ".join(str(int(value)) for value in surface_ids)
        mark_line = f"*createmark surfaces 1 {ids}"
    else:
        mark_line = '*createmark surfaces 1 "all"'

    lines = [
        "# HyperMesh MCP generated surface automesh script",
        "# Review recorded local commands if your solver profile needs custom options.",
        'catch {*beginhistorystate "MCP surface automesh"}',
        mark_line,
        f"set elem_size {float(element_size)}",
        "*interactiveremeshsurf 1 $elem_size 2 2 2 1 1",
        "*automesh 0 2 2",
        "*storemeshtodatabase 1",
        "*ameshclearsurface",
        'catch {*endhistorystate "MCP surface automesh"}',
    ]
    if output_hm_path:
        lines.append(f'*writefile "{_quote_tcl_path(output_hm_path)}" 1')

    return {"success": True, "script": _wrap_generated_tcl("generate_surface_automesh_tcl", "\n".join(lines))}


@mcp.tool()
def generate_plain_tetra_tcl(
    solid_id: int,
    component_name: str,
    element_size: float,
    output_hm_path: str | None = None,
    min_element_size: float = 0.5,
    max_deviation: float = 0.05,
    feature_angle: float = 15,
    growth_rate: float = 1.23,
    max_shell_elements_before_tetmesh: int = TETRA_MAX_SHELL_ELEMENTS,
    allow_tetmesh: bool = True,
    allow_surface_mesh: bool = True,
    fit_tolerance_ratio: float = 0.01,
    target_vol_skew: float = 0.70,
    repair_vol_skew: float = 0.99,
    delete_existing_component_elements: bool = True,
) -> dict[str, Any]:
    """Generate Tcl for surface-deviation R-trias plus tetra meshing on one solid."""
    if solid_id <= 0:
        raise ValueError("solid_id must be > 0.")
    if element_size <= 0:
        raise ValueError("element_size must be > 0.")
    if min_element_size <= 0:
        raise ValueError("min_element_size must be > 0.")
    if max_shell_elements_before_tetmesh <= 0:
        raise ValueError("max_shell_elements_before_tetmesh must be > 0.")
    if fit_tolerance_ratio <= 0:
        raise ValueError("fit_tolerance_ratio must be > 0.")
    if not (0 < target_vol_skew <= 1):
        raise ValueError("target_vol_skew must be in (0, 1].")
    if not (0 < repair_vol_skew <= 1):
        raise ValueError("repair_vol_skew must be in (0, 1].")
    if not component_name.strip():
        raise ValueError("component_name cannot be empty.")
    comp = _tcl_escape_name(component_name)
    clamped_min = max(0.20, min(float(min_element_size), 0.50))
    clamped_size = max(1.5, min(float(element_size), 2.0))
    lines = [
        "# HyperMesh MCP generated tetra script: surface deviation R-trias -> smooth-pyramid tetra",
        f"set target_solid {int(solid_id)}",
        f"set target_component {{{comp}}}",
        f"set requested_elem_size {clamped_size}",
        f"set requested_min_size {clamped_min}",
        f"set max_dev {float(max_deviation)}",
        f"set feat_angle {float(feature_angle)}",
        f"set growth {float(growth_rate)}",
        f"set max_shell_before_tetmesh {int(max_shell_elements_before_tetmesh)}",
        f"set allow_tetmesh {1 if allow_tetmesh else 0}",
        f"set allow_surface_mesh {1 if allow_surface_mesh else 0}",
        f"set fit_tol_ratio {float(fit_tolerance_ratio)}",
        f"set target_vol_skew {float(target_vol_skew)}",
        f"set repair_vol_skew {float(repair_vol_skew)}",
        f"set delete_existing_component_elements {1 if delete_existing_component_elements else 0}",
        "set retry_count 4",
        "set ok 0",
        "set unfixed_aspect_report 0",
        "set unrepaired_vol_skew_report 0",
        "set final_bad_solid 0",
        "set final_bad_vol_count 0",
        "set target_vol_skew_report -1",
        "proc mcp_all_elems {} {",
        '    *createmark elems 1 "all"',
        "    return [hm_getmark elems 1]",
        "}",
        "proc mcp_list_subtract {a b} {",
        "    array set seen {}",
        "    foreach x $b {set seen($x) 1}",
        "    set out {}",
        "    foreach x $a {if {![info exists seen($x)]} {lappend out $x}}",
        "    return $out",
        "}",
        "proc mcp_delete_elems {ids} {",
        "    if {[llength $ids] == 0} {return}",
        "    eval *createmark elems 1 $ids",
        "    catch {*deletemark elems 1}",
        "    eval *createmark elements 1 $ids",
        "    catch {*deletemark elements 1}",
        "}",
        "proc mcp_delete_marked_component_elems {comp} {",
        '    *createmark elems 1 "by comp" $comp',
        "    set ids [hm_getmark elems 1]",
        "    if {[llength $ids] > 0} {mcp_delete_elems $ids}",
        "    return [llength $ids]",
        "}",
        "proc mcp_node_xyz {nid} {",
        "    set x [hm_getvalue nodes id=$nid dataname=x]",
        "    set y [hm_getvalue nodes id=$nid dataname=y]",
        "    set z [hm_getvalue nodes id=$nid dataname=z]",
        "    return [list $x $y $z]",
        "}",
        "proc mcp_dist3 {p q} {",
        "    set dx [expr {[lindex $p 0] - [lindex $q 0]}]",
        "    set dy [expr {[lindex $p 1] - [lindex $q 1]}]",
        "    set dz [expr {[lindex $p 2] - [lindex $q 2]}]",
        "    return [expr {sqrt($dx*$dx + $dy*$dy + $dz*$dz)}]",
        "}",
        "proc mcp_tri_area {p1 p2 p3} {",
        "    set ax [expr {[lindex $p2 0] - [lindex $p1 0]}]",
        "    set ay [expr {[lindex $p2 1] - [lindex $p1 1]}]",
        "    set az [expr {[lindex $p2 2] - [lindex $p1 2]}]",
        "    set bx [expr {[lindex $p3 0] - [lindex $p1 0]}]",
        "    set by [expr {[lindex $p3 1] - [lindex $p1 1]}]",
        "    set bz [expr {[lindex $p3 2] - [lindex $p1 2]}]",
        "    set cx [expr {$ay*$bz - $az*$by}]",
        "    set cy [expr {$az*$bx - $ax*$bz}]",
        "    set cz [expr {$ax*$by - $ay*$bx}]",
        "    return [expr {0.5 * sqrt($cx*$cx + $cy*$cy + $cz*$cz)}]",
        "}",
        "proc mcp_shell_aspect {eid} {",
        "    if {[catch {hm_getvalue elems id=$eid dataname=nodes} nodes]} {return 0.0}",
        "    set pts {}",
        "    foreach nid $nodes {lappend pts [mcp_node_xyz $nid]}",
        "    set n [llength $pts]",
        "    if {$n < 3} {return 1.0e30}",
        "    set lengths {}",
        "    for {set i 0} {$i < $n} {incr i} {",
        "        set j [expr {($i + 1) % $n}]",
        "        lappend lengths [mcp_dist3 [lindex $pts $i] [lindex $pts $j]]",
        "    }",
        "    set sorted [lsort -real $lengths]",
        "    set min_edge [lindex $sorted 0]",
        "    set max_edge [lindex $sorted end]",
        "    if {$min_edge <= 1.0e-9} {return 1.0e30}",
        "    if {$n == 3} {",
        "        set area [mcp_tri_area [lindex $pts 0] [lindex $pts 1] [lindex $pts 2]]",
        "        if {$area <= 1.0e-12} {return 1.0e30}",
        "        set min_alt [expr {2.0 * $area / $max_edge}]",
        "        if {$min_alt <= 1.0e-9} {return 1.0e30}",
        "        return [expr {$max_edge / $min_alt}]",
        "    }",
        "    return [expr {$max_edge / $min_edge}]",
        "}",
        "proc mcp_bad_shell_aspect_ids {ids threshold} {",
        "    set out {}",
        "    foreach eid $ids {",
        "        set aspect [mcp_shell_aspect $eid]",
        "        if {$aspect > $threshold} {lappend out $eid}",
        "    }",
        "    return $out",
        "}",
        "proc mcp_tetra_ids_in_component {component_name} {",
        "    set out {}",
        "    *createmark elems 1 \"by comp\" \"$component_name\"",
        "    foreach eid [hm_getmark elems 1] {",
        "        set c [hm_getvalue elems id=$eid dataname=config]",
        "        if {$c==204 || $c==205 || $c==210 || $c==547} {lappend out $eid}",
        "    }",
        "    return $out",
        "}",
        '*currentcollector components "$target_component"',
        'if {$delete_existing_component_elements} {',
        '    set removed_existing [mcp_delete_marked_component_elems $target_component]',
        '    if {$removed_existing > 0} {puts "MCP_PT_INFO solid=$target_solid removed_existing_component_elems=$removed_existing"}',
        '}',
        '*createmark surfaces 1 "by solids" $target_solid',
        'if {[hm_marklength surfaces 1] == 0} { puts "MCP_PT_FAIL solid=$target_solid no_surfaces"; return }',
        'set all_surfs [hm_getmark surfaces 1]',
        'set surf_count [llength $all_surfs]',
        '*createmark surfaces 2 "by solids" $target_solid',
        'set sbb [hm_getboundingbox surfaces 2]',
        'set sdx [expr {abs([lindex $sbb 3]-[lindex $sbb 0])}]',
        'set sdy [expr {abs([lindex $sbb 4]-[lindex $sbb 1])}]',
        'set sdz [expr {abs([lindex $sbb 5]-[lindex $sbb 2])}]',
        'set dims_sorted [lsort -real [list $sdx $sdy $sdz]]',
        'set min_dim [lindex $dims_sorted 0]',
        'set mid_dim [lindex $dims_sorted 1]',
        'set max_dim [lindex $dims_sorted 2]',
        'set solid_bb [list [lindex $sbb 0] [lindex $sbb 1] [lindex $sbb 2] [lindex $sbb 3] [lindex $sbb 4] [lindex $sbb 5]]',
        'set solid_diag [expr {sqrt(pow($sdx,2)+pow($sdy,2)+pow($sdz,2))}]',
        'set auto_elem_size [expr {min(2.0, max(1.5, $mid_dim/4.0))}]',
        'set elem_size [expr {min(2.0, max(1.5, min($requested_elem_size, $auto_elem_size)))}]',
        'set complexity_min [expr {0.50 - min(0.30, max(0.0, ($surf_count - 20) / 100.0 * 0.30))}]',
        'set dim_min [expr {max(0.20, min(0.50, $min_dim/8.0))}]',
        'set base_min_size [expr {max(0.20, min(0.50, min($requested_min_size, min($complexity_min, $dim_min))))}]',
        'puts "MCP_PT_START solid=$target_solid surf_count=$surf_count elem_size=$elem_size min_size=$base_min_size max_dev=$max_dev fit_tol_ratio=$fit_tol_ratio target_vol_skew=$target_vol_skew"',
        'if {!$allow_surface_mesh} {',
        '    puts "MCP_PT_STOP solid=$target_solid surface_mesh_disabled_for_high_risk_geometry before_surface_mesh"',
        '    return',
        '}',
        'for {set at 0} {$at < $retry_count && !$ok} {incr at} {',
        '    set cs [expr {max(1.5, $elem_size * pow(0.90, $at))}]',
        '    set mn_size [expr {max(0.20, $base_min_size * pow(0.80, $at))}]',
        '    set max_size [expr {max($cs * 1.50, $mn_size + 0.05)}]',
        '    *createmark surfaces 1 "by solids" $target_solid',
        '    set all_surfs [hm_getmark surfaces 1]',
        '    *createmark elems 1 "by surface" $all_surfs',
        '    set stale_shells [hm_getmark elems 1]',
        '    if {[llength $stale_shells] > 0} {',
        '        puts "MCP_PT_INFO solid=$target_solid cleanup_stale_shells=[llength $stale_shells] before_attempt=$at"',
        '        mcp_delete_elems $stale_shells',
        '    }',
        '    set before_surface_elems [mcp_all_elems]',
        '    puts "MCP_PT_SURFACE_ATTEMPT solid=$target_solid attempt=$at size=$cs min=$mn_size max=$max_size"',
        '    *createarray 3 0 0 0',
        '    if {[catch {*defaultmeshsurf_growth 1 $cs 3 3 2 1 1 1 35 0 $mn_size $max_size $max_dev $feat_angle $growth 1 3 1 0} surf_err]} {',
        '        puts "MCP_PT_WARN solid=$target_solid surface_mesh_failed=$surf_err attempt=$at"',
        '        *createmark elems 1 "by surface" $all_surfs',
        '        set failed_shells [hm_getmark elems 1]',
        '        mcp_delete_elems $failed_shells',
        '        continue',
        '    }',
        '    *storemeshtodatabase 1',
        '    set shell_ids [mcp_list_subtract [mcp_all_elems] $before_surface_elems]',
        '    if {[llength $shell_ids] == 0} {',
        '        *createmark elems 1 "by surface" $all_surfs',
        '        set shell_ids [hm_getmark elems 1]',
        '    }',
        '    set shell_count [llength $shell_ids]',
        '    if {$shell_count == 0} { puts "MCP_PT_WARN solid=$target_solid no_shells attempt=$at"; continue }',
        '    set missing_surf_list {}',
        '    foreach sid $all_surfs {',
        '        *createmark elems 2 "by surface" $sid',
        '        if {[hm_marklength elems 2] == 0} {',
        '            lappend missing_surf_list $sid',
        '            puts "MCP_PT_WARN solid=$target_solid missing_mesh_surface=$sid"',
        '        }',
        '    }',
        '    eval *createmark elems 2 $shell_ids',
        '    set shell_bb [hm_getboundingbox elems 2 0 0 0]',
        '    set fit_tol [expr {max($cs * 0.25, $solid_diag * $fit_tol_ratio)}]',
        '    set fit_ok 1',
        '    set fit_max_diff 0.0',
        '    set fit_max_index -1',
        '    for {set i 0} {$i < 6} {incr i} {',
        '        set fit_diff [expr {abs([lindex $shell_bb $i] - [lindex $solid_bb $i])}]',
        '        if {$fit_diff > $fit_max_diff} {set fit_max_diff $fit_diff; set fit_max_index $i}',
        '        if {$fit_diff > $fit_tol} {set fit_ok 0}',
        '    }',
        '    if {[llength $missing_surf_list] > 0 || !$fit_ok} {',
        '        puts "MCP_PT_WARN solid=$target_solid surface_fit_failed missing=[llength $missing_surf_list] fit_ok=$fit_ok fit_tol=$fit_tol max_diff=$fit_max_diff max_index=$fit_max_index attempt=$at"',
        '        mcp_delete_elems $shell_ids',
        '        continue',
        '    }',
        '    if {$shell_count > $max_shell_before_tetmesh} {',
        '        puts "MCP_PT_WARN solid=$target_solid shell_count=$shell_count exceeds_guard=$max_shell_before_tetmesh continuing_to_tetmesh"',
        '    }',
        '    set bad_aspect_ids {}',
        '    set bad_aspect_ids [mcp_bad_shell_aspect_ids $shell_ids 10.0]',
        '    puts "MCP_PT_INFO solid=$target_solid aspect_bad_local=[llength $bad_aspect_ids]"',
        '    set repair_at 0',
        '    while {[llength $bad_aspect_ids] > 0 && $repair_at < 3} {',
        '        eval *createmark elems 1 $bad_aspect_ids',
        '        if {$repair_at == 0} {',
        '            puts "MCP_PT_INFO solid=$target_solid aspect_repair=triangle_cleanup count=[llength $bad_aspect_ids]"',
        '            catch {*triangle_clean_up elems 1 "aspect=10.0 height=0.2"}',
        '        } elseif {$repair_at == 1} {',
        '            puts "MCP_PT_INFO solid=$target_solid aspect_repair=smooth_5 count=[llength $bad_aspect_ids]"',
        '            catch {*smooth elems 1 5}',
        '        } else {',
        '            puts "MCP_PT_INFO solid=$target_solid aspect_repair=smooth_15 count=[llength $bad_aspect_ids]"',
        '            catch {*smooth elems 1 15}',
        '        }',
        '        set bad_aspect_ids [mcp_bad_shell_aspect_ids $shell_ids 10.0]',
        '        incr repair_at',
        '    }',
        '    set unfixed_aspect_report [llength $bad_aspect_ids]',
        '    if {$unfixed_aspect_report > 0} {',
        '        puts "MCP_PT_WARN solid=$target_solid aspect_unfixed=$unfixed_aspect_report continue_to_tetmesh_keep_surface_mesh=1"',
        '    }',
        '    if {!$allow_tetmesh} {',
        '        puts "MCP_PT_STOP solid=$target_solid shell_count=$shell_count tetmesh_disabled_for_high_risk_geometry before_tetmesh"',
        '        mcp_delete_elems $shell_ids',
        '        return',
        '    }',
        '    set before_tetmesh_elems [mcp_all_elems]',
        '    eval *createmark elems 1 $shell_ids',
        '    set tet_max [expr {max($cs * 1.90, $mn_size + 0.10)}]',
        '    *createstringarray 2 "tet: 547 1.2 2 $tet_max 0.8 $mn_size 0" "pars: pre_cln=1 post_cln=1 shell_validation=1 use_optimizer=1 skip_aflr3=1 feature_angle=30 niter=30 fix_comp_bdr=1 fix_top_bdr=1 shell_swap=1 shell_remesh=1 upd_shell=1 shell_dev=0.0,0.0 vol_skew=\'0.70,0.70,0.70,1\'"',
        '    set tet_rc [catch {*tetmesh elems 1 1 elems 0 -1 1 2} tet_err]',
        '    if {$tet_rc} {',
        '        puts "MCP_PT_WARN solid=$target_solid tetmesh_failed=$tet_err surface_not_closed_or_tetmesh_rejected=1"',
        '        set failed_new_elems [mcp_list_subtract [mcp_all_elems] $before_tetmesh_elems]',
        '        mcp_delete_elems $failed_new_elems',
        '        mcp_delete_elems $shell_ids',
        '        continue',
        '    }',
        '    set comp_elems [mcp_tetra_ids_in_component $target_component]',
        '    if {[llength $comp_elems] == 0} { puts "MCP_PT_WARN solid=$target_solid no_tetra_after_tetmesh_keep_surface_mesh=1"; continue }',
        '    puts "MCP_PT_INFO solid=$target_solid tetmesh_generation_vol_skew_target=$target_vol_skew"',
        '    eval *createmark elems 1 $comp_elems',
        '    set repair_rc [catch {*elementtestvolumetricskew elems 1 $repair_vol_skew 1 0 ""} repair_err]',
        '    if {$repair_rc && $repair_err ne "0"} {',
        '        puts "MCP_PT_WARN solid=$target_solid vol_skew_test_failed=$repair_err"',
        '    } else {',
        '        set bad_vol_ids [hm_getmark elems 1]',
        '        if {[llength $bad_vol_ids] == [llength $comp_elems]} {',
        '            puts "MCP_PT_WARN solid=$target_solid vol_skew_test_unfiltered count=[llength $bad_vol_ids] preserving_volume_mesh=1"',
        '            set bad_vol_ids {}',
        '        }',
        '        set vol_repair_at 0',
        '        while {[llength $bad_vol_ids] > 0 && $vol_repair_at < 4} {',
        '            eval *createmark elems 1 $bad_vol_ids',
        '            if {$vol_repair_at == 0} {',
        '                puts "MCP_PT_INFO solid=$target_solid vol_skew_repair=solid_mesh_optimization count=[llength $bad_vol_ids] threshold=$repair_vol_skew"',
        '                *clearmark elements 2',
        '                *createstringarray 2 "tet: 256 1.2 2 0.0 0.8 0.0 0" "pars: fix_comp_bdr= 1 fix_top_bdr= 0 shell_swap=0 shell_remesh=0 use_optimizer=1 skip_aflr3=1 feature_angle=35.0 niter=3 upd_shell=0 vol_skew=\'0.99,0.60,0.10,1.0\'"',
        '                catch {*tetmesh elements 1 6 elements 2 1 1 2} opt_err',
        '            } elseif {$vol_repair_at == 1} {',
        '                puts "MCP_PT_INFO solid=$target_solid vol_skew_repair=smooth_3 count=[llength $bad_vol_ids]"',
        '                catch {*smooth elems 1 3}',
        '            } elseif {$vol_repair_at == 2} {',
        '                puts "MCP_PT_INFO solid=$target_solid vol_skew_repair=smooth_8 count=[llength $bad_vol_ids]"',
        '                catch {*smooth elems 1 8}',
        '            } else {',
        '                puts "MCP_PT_INFO solid=$target_solid vol_skew_repair=smooth_15 count=[llength $bad_vol_ids]"',
        '                catch {*smooth elems 1 15}',
        '            }',
        '            set comp_elems [mcp_tetra_ids_in_component $target_component]',
        '            eval *createmark elems 1 $comp_elems',
        '            set repair_rc [catch {*elementtestvolumetricskew elems 1 $repair_vol_skew 1 0 ""} repair_err]',
        '            if {$repair_rc && $repair_err ne "0"} {',
        '                set bad_vol_ids {}',
        '            } else {',
        '                set bad_vol_ids [hm_getmark elems 1]',
        '                if {[llength $bad_vol_ids] == [llength $comp_elems]} {set bad_vol_ids {}}',
        '            }',
        '            incr vol_repair_at',
        '        }',
        '        set unrepaired_vol_skew_report [llength $bad_vol_ids]',
        '    }',
        '    if {$unrepaired_vol_skew_report > 0} {',
        '        puts "MCP_PT_FAIL solid=$target_solid unrepaired_vol_skew_over_$repair_vol_skew=$unrepaired_vol_skew_report delete_tetra_keep_surface_shells=1"',
        '        set final_bad_solid $target_solid',
        '        set final_bad_vol_count $unrepaired_vol_skew_report',
        '        mcp_delete_elems [mcp_tetra_ids_in_component $target_component]',
        '        set ok 0',
        '        break',
        '    }',
        '    mcp_delete_elems $shell_ids',
        '    set ok 1',
        '}',
        '*createmark elems 1 "by comp" "$target_component"',
        'set final_elems [hm_getmark elems 1]',
        'set final_count [llength $final_elems]',
        'set t4 0; set t10 0',
        'foreach eid $final_elems {',
        '    set c [hm_getvalue elems id=$eid dataname=config]',
        '    if {$c==204 || $c==205} {incr t4}',
        '    if {$c==210 || $c==547} {incr t10}',
        '}',
        'if {!$ok && $final_bad_vol_count > 0} { puts "MCP_PT_QUALITY_FAIL solid=$final_bad_solid bad_volume_elements=$final_bad_vol_count kept_surface_shells=1" }',
        'if {!$ok && $final_bad_vol_count == 0} { puts "MCP_PT_FAIL solid=$target_solid no_accepted_tetra_after_retries" }',
        'puts "MCP_PT_DONE solid=$target_solid ok=$ok total=$final_count tet4=$t4 tet10=$t10 unfixed_aspect=$unfixed_aspect_report tetmesh_generation_vol_skew_target=$target_vol_skew unrepaired_vol_skew_over_$repair_vol_skew=$unrepaired_vol_skew_report"',
    ]
    if output_hm_path:
        lines.append(f'*writefile "{_quote_tcl_path(output_hm_path)}" 1')
    return {
        "success": True,
        "script": _wrap_generated_tcl("generate_plain_tetra_tcl", "\n".join(lines)),
    }


@mcp.tool()
def generate_guarded_drag_hex_tcl(
    source_surface_id: int,
    drag_distance: float,
    element_size: float,
    component_name: str,
    axis: str = "z",
    solid_id: int | None = None,
    fit_tolerance_ratio: float = 0.05,
    retry_count: int = 2,
    layer_count: int | None = None,
    matched_edge_groups: list[list[int]] | None = None,
    target_density: int | None = None,
    preview_edge_seed_counts: list[int] | None = None,
    source_edge_lengths: list[float] | None = None,
    seed_balance_ratio_threshold: float = 1.6,
    output_hm_path: str | None = None,
) -> dict[str, Any]:
    """Generate guarded drag-hex Tcl: match edge seeds, then all-quad source face or no drag."""
    if drag_distance <= 0:
        raise ValueError("drag_distance must be greater than 0.")
    if element_size <= 0:
        raise ValueError("element_size must be greater than 0.")
    if not component_name.strip():
        raise ValueError("component_name cannot be empty.")
    if target_density is not None and target_density <= 0:
        raise ValueError("target_density must be greater than 0.")
    if solid_id is not None and solid_id <= 0:
        raise ValueError("solid_id must be greater than 0 when supplied.")
    if fit_tolerance_ratio <= 0:
        raise ValueError("fit_tolerance_ratio must be greater than 0.")
    if retry_count < 0:
        raise ValueError("retry_count cannot be negative.")
    if seed_balance_ratio_threshold <= 1.0:
        raise ValueError("seed_balance_ratio_threshold must be greater than 1.0.")
    if preview_edge_seed_counts and any(int(count) <= 0 for count in preview_edge_seed_counts):
        raise ValueError("preview_edge_seed_counts must contain positive integers.")
    if source_edge_lengths and any(float(length) <= 0 for length in source_edge_lengths):
        raise ValueError("source_edge_lengths must contain positive values.")

    axis_key = axis.strip().lower()
    vectors = {
        "x": (1, 0, 0),
        "y": (0, 1, 0),
        "z": (0, 0, 1),
    }
    if axis_key not in vectors:
        raise ValueError("axis must be one of: x, y, z.")

    axis_indices = {"x": (0, 3), "y": (1, 4), "z": (2, 5)}
    ax_min, ax_max = axis_indices[axis_key]

    vx, vy, vz = vectors[axis_key]
    layers = int(layer_count) if layer_count and layer_count > 0 else 0
    comp = component_name.replace('"', '\\"')
    balanced_density, density_source = _balanced_seed_density(
        element_size=float(element_size),
        target_density=target_density,
        preview_edge_seed_counts=preview_edge_seed_counts,
        source_edge_lengths=source_edge_lengths,
        ratio_threshold=float(seed_balance_ratio_threshold),
    )
    group_lines: list[str] = []
    if matched_edge_groups:
        group_text = " ".join(
            "{" + " ".join(str(int(edge)) for edge in group) + "}"
            for group in matched_edge_groups
        )
        group_lines.extend(
            [
                f"set matched_edge_groups {{{group_text}}}",
                f"set target_density {int(balanced_density) if balanced_density else 0}",
                f'set target_density_source "{density_source}"',
                f"set seed_balance_ratio_threshold {float(seed_balance_ratio_threshold)}",
                "if {$target_density <= 0} {",
                "    *createmark surfaces 2 $source_surface",
                "    set bb [hm_getboundingbox surfaces 2 0 0 0]",
                "    set dx [expr {abs([lindex $bb 3] - [lindex $bb 0])}]",
                "    set dy [expr {abs([lindex $bb 4] - [lindex $bb 1])}]",
                "    set dz [expr {abs([lindex $bb 5] - [lindex $bb 2])}]",
                "    set dims [lsort -real [list $dx $dy $dz]]",
                "    set major [lindex $dims 2]",
                "    set target_density [expr {int(round($major / $elem_size))}]",
                "    if {$target_density < 4} { set target_density 4 }",
                "    if {$target_density > 120} { set target_density 120 }",
                '    set target_density_source "bbox_estimate"',
                "}",
                'puts "MCP guarded drag source-face target_density=$target_density source=$target_density_source balance_ratio_threshold=$seed_balance_ratio_threshold"',
                "foreach edge_group $matched_edge_groups {",
                "    foreach edge_index $edge_group {",
                "        # edge_index is 0-based in the order shown by HyperMesh automesh.",
                "        catch {*set_meshedgeparams $edge_index $target_density 1 0 0 0 $elem_size 0 0}",
                "    }",
                "}",
            ]
        )
    else:
        group_lines.extend(
            [
                "# No explicit matched_edge_groups were supplied.",
                "# *setedgedensitylink 1 is still enabled, but exact source-face uniform",
                "# seeding requires passing all logical edge indices and target_density.",
            ]
        )
    lines = [
        "# HyperMesh MCP generated guarded drag-hex script",
        "# Precondition: all logical edge groups of the drag source face must share",
        "# one compatible target_density, but it should be balanced when inner/outer",
        "# preview counts or edge lengths differ greatly; do not blindly use the largest",
        "# outer-edge count for the whole section.",
        "# If the source face is not mapped 100% quads after uniform seeding, skip drag.",
        f'set drag_component "{comp}"',
        f"set source_surface {int(source_surface_id)}",
        f"set target_solid {int(solid_id) if solid_id is not None else 0}",
        f"set requested_elem_size {float(element_size)}",
        f"set elem_size {float(element_size)}",
        f"set drag_distance {float(drag_distance)}",
        f"set drag_layers {int(layers)}",
        f"set fit_tol_ratio {float(fit_tolerance_ratio)}",
        f"set retry_count {int(retry_count)}",
        "proc mcp_all_elems {} {",
        '    *createmark elems 1 "all"',
        "    return [hm_getmark elems 1]",
        "}",
        "proc mcp_list_subtract {a b} {",
        "    array set seen {}",
        "    foreach x $b {set seen($x) 1}",
        "    set out {}",
        "    foreach x $a {if {![info exists seen($x)]} {lappend out $x}}",
        "    return $out",
        "}",
        "proc mcp_count_hex8 {elems} {",
        "    set hexes 0",
        "    foreach eid $elems {",
        "        if {[catch {hm_getvalue elems id=$eid dataname=config} cfg]} {continue}",
        "        if {$cfg == 208} {incr hexes}",
        "    }",
        "    return $hexes",
        "}",
        "proc mcp_bbox_fit_ok {elems solid_id fit_ratio elem_size} {",
        "    if {$solid_id <= 0} {return 1}",
        "    if {[llength $elems] == 0} {return 0}",
        "    eval *createmark elems 2 $elems",
        "    *createmark surfaces 2 \"by solids\" $solid_id",
        "    if {[catch {hm_getboundingbox elems 2 0 0 0} ebb]} {return 0}",
        "    if {[catch {hm_getboundingbox surfaces 2 0 0 0} sbb]} {return 0}",
        "    set sx [expr {abs([lindex $sbb 3] - [lindex $sbb 0])}]",
        "    set sy [expr {abs([lindex $sbb 4] - [lindex $sbb 1])}]",
        "    set sz [expr {abs([lindex $sbb 5] - [lindex $sbb 2])}]",
        "    set diag [expr {sqrt($sx*$sx + $sy*$sy + $sz*$sz)}]",
        "    set tol [expr {max($elem_size * 1.5, $diag * $fit_ratio)}]",
        "    for {set i 0} {$i < 6} {incr i} {",
        "        if {abs([lindex $ebb $i] - [lindex $sbb $i]) > $tol} {return 0}",
        "    }",
        "    return 1",
        "}",
        'catch {*beginhistorystate "MCP guarded drag hex"}',
        '*currentcollector components "$drag_component"',
        "catch {*setedgedensitylinkwithaspectratio -1}",
        "*setedgedensitylink 1",
        "*createmark surfaces 2 $source_surface",
        "set size_bb [hm_getboundingbox surfaces 2 0 0 0]",
        "set size_dims [lsort -real [list [expr {abs([lindex $size_bb 3]-[lindex $size_bb 0])}] [expr {abs([lindex $size_bb 4]-[lindex $size_bb 1])}] [expr {abs([lindex $size_bb 5]-[lindex $size_bb 2])}]]]",
        "set source_minor [lindex $size_dims 1]",
        "set source_major [lindex $size_dims 2]",
        "set thickness_size [expr {$drag_distance/4.0}]",
        "set source_size [expr {min($source_minor/3.0, $source_major/8.0)}]",
        "set elem_size [expr {max(0.5, min(1.5, min($requested_elem_size, $thickness_size, $source_size)))}]",
        "if {$drag_layers <= 0} {set drag_layers [expr {max(1, round($drag_distance/$elem_size))}]}",
        'puts "MCP_DRAG_SIZE single solid=$target_solid thickness=$drag_distance source_minor=$source_minor source_major=$source_major requested=$requested_elem_size thickness_size=$thickness_size source_size=$source_size chosen=$elem_size layers=$drag_layers limits=0.5..1.5"',
        "*createmark surfaces 1 $source_surface",
        "*interactiveremeshsurf 1 $elem_size 1 1 1 1 1",
        "*set_meshfaceparams 0 5 1 0 0 1 0.5 1 1",
        *group_lines,
        "*automesh 0 5 1",
        "*storemeshtodatabase 1",
        "*ameshclearsurface",
        '*createmark elems 1 "by surface" $source_surface',
        "set source_shells [hm_getmark elems 1]",
        "set quad_count 0",
        "foreach eid $source_shells {",
        "    set cfg [hm_getvalue elems id=$eid dataname=config]",
        "    if {$cfg == 104 || $cfg == 108} { incr quad_count }",
        "}",
        "if {[llength $source_shells] == 0 || $quad_count != [llength $source_shells]} {",
        '    puts "MCP guarded drag skipped: source face is not all quads."',
        "    if {[llength $source_shells] > 0} { eval *createmark elems 1 $source_shells; catch {*deletemark elems 1} }",
        '    puts "MCP guarded drag skipped tetra fallback: removed_old_tetra_path=1"',
        "} else {",
        "    set hex_success 0",
        "    set attempt 0",
        "    while {$attempt <= $retry_count && !$hex_success} {",
        '        puts "MCP guarded drag attempt=$attempt elem_size=$elem_size"',
        "        set before_elems [mcp_all_elems]",
        "        # auto-flip: ensure drag goes from source face INTO solid",
        "        *createmark surfaces 2 $source_surface",
        "        set sb [hm_getboundingbox surfaces 2 0 0 0]",
        f"        set sc [expr {{([lindex $sb {ax_min}]+[lindex $sb {ax_max}])/2.0}}]",
        "        *createmark surfaces 2 \"by solids\" $target_solid",
        "        set bb [hm_getboundingbox surfaces 2 0 0 0]",
        f"        set dir_x {vx}; set dir_y {vy}; set dir_z {vz}",
        "        if {$target_solid > 0} {",
        f"            set smin [lindex $bb {ax_min}]; set smax [lindex $bb {ax_max}]",
        "            if {[expr {$sc-$smin}] < [expr {$smax-$sc}]} {",
        "                # face at bottom, drag +direction",
        "            } else {",
        "                # face at top, flip",
        f"                set dir_x [expr {{-1*{vx}}}]; set dir_y [expr {{-1*{vy}}}]; set dir_z [expr {{-1*{vz}}}]",
        "            }",
        "        }",
        "        *createvector 1 $dir_x $dir_y $dir_z",
        '        set _mcp_drag_err ""',
        "        if {[catch {",
        "            *meshdragelements2 1 1 $drag_distance $drag_layers 0 0.0 0",
        '        } _mcp_drag_err]} {',
        '            puts "MCP_DRAG_FAIL stage=meshdragelements_error solid=$target_solid error=$_mcp_drag_err"',
        "            if {[llength $source_shells] > 0} { eval *createmark elems 1 $source_shells; catch {*deletemark elems 1} }",
        "            set elem_size [expr {$elem_size * 0.8}]",
        "            if {$elem_size < 0.5} {set elem_size 0.5}",
        "            incr attempt",
        "            continue",
        "        }",
        "        set new_elems [mcp_list_subtract [mcp_all_elems] $before_elems]",
        '        puts "MCP_DRAG_NEW_ELEMS single solid=$target_solid count=[llength $new_elems]"',
        "        if {[llength $new_elems] == 0} {",
        '            puts "MCP_DRAG_FAIL stage=meshdragelements_empty single solid=$target_solid"',
        "            if {[llength $source_shells] > 0} { eval *createmark elems 1 $source_shells; catch {*deletemark elems 1} }",
        "            set elem_size [expr {$elem_size * 0.8}]",
        "            if {$elem_size < 0.5} {set elem_size 0.5}",
        "            incr attempt",
        "            continue",
        "        }",
        "        set hex_count [mcp_count_hex8 $new_elems]",
        "        set fit_ok [mcp_bbox_fit_ok $new_elems $target_solid $fit_tol_ratio $elem_size]",
        "        if {[llength $new_elems] > 0 && $hex_count == [llength $new_elems] && $fit_ok} {",
        "            set hex_success 1",
        '            puts "MCP guarded drag completed: hex8=$hex_count fit_ok=$fit_ok"',
        "        } else {",
        '            puts "MCP guarded drag invalid: new_elements=[llength $new_elems] hex8=$hex_count fit_ok=$fit_ok; cleaning and retrying/falling back."',
        "            if {[llength $new_elems] > 0} { eval *createmark elems 1 $new_elems; catch {*deletemark elems 1} }",
        "        }",
        "        incr attempt",
        "    }",
        "    eval *createmark elems 1 $source_shells",
        "    catch {*deletemark elems 1}",
        '    if {!$hex_success} { puts "MCP guarded drag failed: tetra fallback removed_old_tetra_path=1" }',
        "}",
        'catch {*endhistorystate "MCP guarded drag hex"}',
    ]
    if output_hm_path:
        lines.append(f'*writefile "{_quote_tcl_path(output_hm_path)}" 1')

    return {
        "success": True,
        "script": _wrap_generated_tcl("generate_guarded_drag_hex_tcl", "\n".join(lines)),
        "strategy": (
            "Use this only for simple straight tubes/extrusions. Before drag, "
            "force corresponding edge groups to a compatible target_density. "
            "When preview counts or edge lengths are highly different, this "
            "uses a balanced common count instead of promoting every edge to "
            "the largest outer count. Never use it for flanges with bolt holes."
        ),
    }


@mcp.tool()
def generate_batched_drag_hex_tcl(
    solids: list[dict[str, Any]],
    element_size: float = 1.5,
    fit_tolerance_ratio: float = 0.05,
    retry_count: int = 2,
    matched_edge_groups: list[list[int]] | None = None,
    pause_seconds_after_each_solid: float = 1.0,
    checkpoint_every_n_solids: int = 4,
    checkpoint_hm_path: str | None = None,
    output_hm_path: str | None = None,
) -> dict[str, Any]:
    """Generate ONE Tcl script that processes multiple drag-hex solids in batch.

    solids: list of dicts, each with keys:
        solid_id (int), source_surface_id (int), drag_distance (float),
        component_name (str), axis (str: "x"/"y"/"z")
    """
    if not solids:
        raise ValueError("solids list cannot be empty.")
    if element_size <= 0:
        raise ValueError("element_size must be > 0.")
    if retry_count < 0:
        raise ValueError("retry_count cannot be negative.")
    if pause_seconds_after_each_solid < 0:
        raise ValueError("pause_seconds_after_each_solid cannot be negative.")
    if checkpoint_every_n_solids < 0:
        raise ValueError("checkpoint_every_n_solids cannot be negative.")

    checkpoint_path = _quote_tcl_path(checkpoint_hm_path) if checkpoint_hm_path else ""

    batch_items: list[str] = []
    for s in solids:
        sid_val = int(s.get("solid_id", 0))
        surf_val = int(s.get("source_surface_id", 0))
        dd_val = float(s.get("drag_distance", 0))
        comp_val = _tcl_escape_name(str(s.get("component_name", "")))
        ax_val = str(s.get("axis", "z")).strip().lower()
        if sid_val <= 0 or surf_val <= 0 or dd_val <= 0 or not comp_val:
            raise ValueError(f"Invalid solid spec: {s}")
        if ax_val not in ("x", "y", "z"):
            raise ValueError(f"axis must be x/y/z, got: {ax_val}")
        batch_items.append(f"{sid_val} {surf_val} {dd_val} {{{comp_val}}} {ax_val}")

    batch_tcl = "\n".join(f"    {item}" for item in batch_items)

    lines: list[str] = [
        "# HyperMesh MCP generated batched drag-hex script",
        f"set elem_size {float(element_size)}",
        f"set fit_tol_ratio {float(fit_tolerance_ratio)}",
        f"set retry_count {int(retry_count)}",
        f"set pause_ms_after_each_solid {int(float(pause_seconds_after_each_solid) * 1000)}",
        f"set checkpoint_every_n_solids {int(checkpoint_every_n_solids)}",
        f'set checkpoint_hm_path "{checkpoint_path}"',
        "set completed_drag_solids 0",
    ]
    if matched_edge_groups:
        group_text = " ".join(
            "{" + " ".join(str(int(e)) for e in g) + "}" for g in matched_edge_groups
        )
        lines.append(f"set matched_edge_groups {{{group_text}}}")
    else:
        lines.append("set matched_edge_groups {}")

    lines.extend([
        "proc b_all {} { *createmark elems 1 \"all\"; return [hm_getmark elems 1] }",
        "proc b_sub {a b} { array set x {}; foreach i $b {set x($i) 1}; set o {}; foreach i $a {if {![info exists x($i)]} {lappend o $i}}; return $o }",
        "proc b_hex {e} { set c 0; foreach i $e { if {![catch {hm_getvalue elems id=$i dataname=config} g]} {if {$g==208} {incr c}} }; return $c }",
        "proc b_fit {e id r z} {",
        "    if {$id<=0} {return 1}; if {[llength $e]==0} {return 0}",
        "    eval *createmark elems 2 $e; *createmark surfaces 2 \"by solids\" $id",
        "    if {[catch {hm_getboundingbox elems 2 0 0 0} eb]} {return 0}",
        "    if {[catch {hm_getboundingbox surfaces 2 0 0 0} sb]} {return 0}",
        "    set d [expr {sqrt(pow(abs([lindex $sb 3]-[lindex $sb 0]),2)+pow(abs([lindex $sb 4]-[lindex $sb 1]),2)+pow(abs([lindex $sb 5]-[lindex $sb 2]),2))}]",
        "    set t [expr {max($z*1.5,$d*$r)}]",
        "    for {set i 0} {$i<6} {incr i} {if {abs([lindex $eb $i]-[lindex $sb $i])>$t} {return 0}}",
        "    return 1",
        "}",
        "set batch {",
        batch_tcl,
        "}",
        "foreach {sid surf dd dc ax} $batch {",
        "    catch {*beginhistorystate \"MCP guarded drag hex batch s$sid\"}",
        '    *currentcollector components "$dc"',
        "    catch {*setedgedensitylinkwithaspectratio -1}; *setedgedensitylink 1",
        "    *createmark surfaces 2 $surf",
        "    set size_bb [hm_getboundingbox surfaces 2 0 0 0]",
        "    set size_dims [lsort -real [list [expr {abs([lindex $size_bb 3]-[lindex $size_bb 0])}] [expr {abs([lindex $size_bb 4]-[lindex $size_bb 1])}] [expr {abs([lindex $size_bb 5]-[lindex $size_bb 2])}]]]",
        "    set source_minor [lindex $size_dims 1]",
        "    set source_major [lindex $size_dims 2]",
        "    set thickness_size [expr {$dd/4.0}]",
        "    set source_size [expr {min($source_minor/3.0, $source_major/8.0)}]",
        "    set cs [expr {max(0.5, min(1.5, min($elem_size, $thickness_size, $source_size)))}]",
        '    puts "MCP_DRAG_SIZE solid=$sid thickness=$dd source_minor=$source_minor source_major=$source_major requested=$elem_size thickness_size=$thickness_size source_size=$source_size chosen=$cs limits=0.5..1.5"',
        "    set hs 0; set at 0",
        "    while {$at<=$retry_count&&!$hs} {",
        "        *createmark surfaces 1 $surf",
        "        *interactiveremeshsurf 1 $cs 1 1 1 1 1",
        "        *set_meshfaceparams 0 5 1 0 0 1 0.5 1 1",
        "        *createmark surfaces 2 $surf",
        "        set bb [hm_getboundingbox surfaces 2 0 0 0]",
        "        set mj [lindex [lsort -real [list [expr {abs([lindex $bb 3]-[lindex $bb 0])}] [expr {abs([lindex $bb 4]-[lindex $bb 1])}] [expr {abs([lindex $bb 5]-[lindex $bb 2])}]]] 2]",
        "        set td [expr {int(round($mj/$cs))}]; if {$td<4} {set td 4}",
        "        if {[llength $matched_edge_groups] > 0} {",
        "            foreach edge_group $matched_edge_groups {",
        "                foreach edge_index $edge_group {",
        "                    catch {*set_meshedgeparams $edge_index $td 1 0 0 0 $cs 0 0}",
        "                }",
        "            }",
        "        } else {",
        "            foreach e {0 1 2 3} { catch {*set_meshedgeparams $e $td 1 0 0 0 $cs 0 0} }",
        "        }",
        "        *automesh 0 5 1; *storemeshtodatabase 1; *ameshclearsurface",
        '        puts "MCP_DRAG_ASPECT_CHECK_SKIPPED solid=$sid"',
        '        *createmark elems 1 "by surface" $surf',
        '        set source_shells [hm_getmark elems 1]',
        '        puts "MCP_DRAG_SOURCE_SHELLS solid=$sid surf=$surf count=[llength $source_shells]"',
        '        if {[llength $source_shells] == 0} {',
        '            puts "MCP_DRAG_FAIL stage=source_shell_empty solid=$sid surf=$surf"',
        '            set cs [expr {$cs*0.8}]',
        '            if {$cs<0.5} {set cs 0.5}',
        '            incr at',
        '            continue',
        '        }',
        "        set ss $source_shells; set qc 0",
        "        foreach i $ss { set cf [hm_getvalue elems id=$i dataname=config]; if {$cf==104||$cf==108} {incr qc} }",
        "        if {[llength $ss]==0||$qc!=[llength $ss]} {",
        "            if {[llength $ss]>0} {eval *createmark elems 1 $ss; catch {*deletemark elems 1}}",
        "            puts \"MCP_DRAG_SKIP_TETRA solid=$sid reason=non_quad_source removed_old_tetra_path=1\"",
        "            break",
        "        }",
        "        set dl [expr {max(1,round($dd/$cs))}]",
        "        set be [b_all]",
        "        *createmark surfaces 2 $surf",
        "        set sb [hm_getboundingbox surfaces 2 0 0 0]",
        "        *createmark surfaces 2 \"by solids\" $sid",
        "        set bb [hm_getboundingbox surfaces 2 0 0 0]",
        "        if {$ax eq \"x\"} {set dvx 1; set dvy 0; set dvz 0; set sc [expr {([lindex $sb 0]+[lindex $sb 3])/2.0}]; set smin [lindex $bb 0]; set smax [lindex $bb 3]} else { if {$ax eq \"y\"} {set dvx 0; set dvy 1; set dvz 0; set sc [expr {([lindex $sb 1]+[lindex $sb 4])/2.0}]; set smin [lindex $bb 1]; set smax [lindex $bb 4]} else {set dvx 0; set dvy 0; set dvz 1; set sc [expr {([lindex $sb 2]+[lindex $sb 5])/2.0}]; set smin [lindex $bb 2]; set smax [lindex $bb 5]} }",
        "        if {[expr {$sc-$smin}] > [expr {$smax-$sc}]} { set dvx [expr {-1*$dvx}]; set dvy [expr {-1*$dvy}]; set dvz [expr {-1*$dvz}] }",
        "        *createvector 1 $dvx $dvy $dvz",
        "        set _mcp_drag_err \"\"",
        "        eval *createmark elems 1 $source_shells",
        "        if {[catch {",
        "            *meshdragelements2 1 1 $dd $dl 0 0.0 0",
        "        } _mcp_drag_err]} {",
        "            puts \"MCP_DRAG_FAIL stage=meshdragelements_error solid=$sid error=$_mcp_drag_err\"",
        "            eval *createmark elems 1 $source_shells",
        "            catch {*deletemark elems 1}",
        "            set cs [expr {$cs*0.8}]",
        "            if {$cs<0.5} {set cs 0.5}",
        "            incr at",
        "            continue",
        "        }",
        "        set after_drag [b_all]",
        "        set new_drag_elems [b_sub $after_drag $be]",
        "        puts \"MCP_DRAG_NEW_ELEMS solid=$sid count=[llength $new_drag_elems]\"",
        "        if {[llength $new_drag_elems] == 0} {",
        "            puts \"MCP_DRAG_FAIL stage=meshdragelements_empty solid=$sid\"",
        "            eval *createmark elems 1 $source_shells",
        "            catch {*deletemark elems 1}",
        "            set cs [expr {$cs*0.8}]",
        "            if {$cs<0.5} {set cs 0.5}",
        "            incr at",
        "            continue",
        "        }",
        "        set ne $new_drag_elems; set hc [b_hex $ne]; set fo [b_fit $ne $sid $fit_tol_ratio $cs]",
        "        if {[llength $ne]>0&&$hc==[llength $ne]&&$fo} { set hs 1 } else {",
        "            if {[llength $ne]>0} {eval *createmark elems 1 $ne; catch {*deletemark elems 1}}",
        "            eval *createmark elems 1 $source_shells; catch {*deletemark elems 1}",
        "            set cs [expr {$cs*0.8}]; if {$cs<0.5} {set cs 0.5}",
        "        }; incr at",
        "    }",
        "    if {!$hs} {",
        "        puts \"MCP_DRAG_SKIP_TETRA solid=$sid reason=drag_failed removed_old_tetra_path=1\"",
        "    }",
        "    if {$hs&&[info exists source_shells]&&[llength $source_shells]>0} {eval *createmark elems 1 $source_shells; catch {*deletemark elems 1}}",
        "    catch {*endhistorystate \"MCP guarded drag hex batch s$sid\"}",
        "    incr completed_drag_solids",
        "    if {$checkpoint_hm_path ne \"\" && $checkpoint_every_n_solids > 0 && ($completed_drag_solids % $checkpoint_every_n_solids) == 0} {",
        "        puts \"MCP_DRAG_CHECKPOINT after_solids=$completed_drag_solids path=$checkpoint_hm_path\"",
        "        catch {*writefile \"$checkpoint_hm_path\" 1}",
        "    }",
        "    catch {update}",
        "    if {$pause_ms_after_each_solid > 0} {after $pause_ms_after_each_solid}",
        "}",
    ])
    if output_hm_path:
        lines.append(f'*writefile "{_quote_tcl_path(output_hm_path)}" 1')

    return {
        "success": True,
        "script": _wrap_generated_tcl("generate_batched_drag_hex_tcl", "\n".join(lines)),
        "strategy": "Batched drag-hex: processes multiple solids in one script.",
    }


@mcp.tool()
def generate_guarded_spin_hex_tcl(
    source_surface_id: int,
    element_size: float,
    component_name: str,
    axis: str = "z",
    solid_id: int | None = None,
    fit_tolerance_ratio: float = 0.05,
    retry_count: int = 1,
    angle_degrees: float = 360.0,
    density: int = 96,
    output_hm_path: str | None = None,
) -> dict[str, Any]:
    """Generate guarded spin-hex Tcl: all-quad section or no spin."""
    if element_size <= 0:
        raise ValueError("element_size must be greater than 0.")
    if density <= 0:
        raise ValueError("density must be greater than 0.")
    if not component_name.strip():
        raise ValueError("component_name cannot be empty.")
    if solid_id is not None and solid_id <= 0:
        raise ValueError("solid_id must be greater than 0 when supplied.")
    if fit_tolerance_ratio <= 0:
        raise ValueError("fit_tolerance_ratio must be greater than 0.")
    if retry_count < 0:
        raise ValueError("retry_count cannot be negative.")

    axis_key = axis.strip().lower()
    normals = {
        "x": (1, 0, 0),
        "y": (0, 1, 0),
        "z": (0, 0, 1),
    }
    if axis_key not in normals:
        raise ValueError("axis must be one of: x, y, z.")

    nx, ny, nz = normals[axis_key]
    comp = component_name.replace('"', '\\"')
    lines = [
        "# HyperMesh MCP generated guarded spin-hex script",
        "# Use for clean revolved bodies. Do not use for flanges with bolt holes or protrusions.",
        "# Precondition: the selected source section should have matched edge seeds and be all quads.",
        f'set spin_component "{comp}"',
        f"set source_surface {int(source_surface_id)}",
        f"set target_solid {int(solid_id) if solid_id is not None else 0}",
        f"set elem_size {float(element_size)}",
        f"set spin_angle {float(angle_degrees)}",
        f"set spin_density {int(density)}",
        f"set fit_tol_ratio {float(fit_tolerance_ratio)}",
        f"set retry_count {int(retry_count)}",
        "proc mcp_all_elems {} {",
        '    *createmark elems 1 "all"',
        "    return [hm_getmark elems 1]",
        "}",
        "proc mcp_list_subtract {a b} {",
        "    array set seen {}",
        "    foreach x $b {set seen($x) 1}",
        "    set out {}",
        "    foreach x $a {if {![info exists seen($x)]} {lappend out $x}}",
        "    return $out",
        "}",
        "proc mcp_count_hex8 {elems} {",
        "    set hexes 0",
        "    foreach eid $elems {",
        "        if {[catch {hm_getvalue elems id=$eid dataname=config} cfg]} {continue}",
        "        if {$cfg == 208} {incr hexes}",
        "    }",
        "    return $hexes",
        "}",
        "proc mcp_bbox_fit_ok {elems solid_id fit_ratio elem_size} {",
        "    if {$solid_id <= 0} {return 1}",
        "    if {[llength $elems] == 0} {return 0}",
        "    eval *createmark elems 2 $elems",
        "    *createmark surfaces 2 \"by solids\" $solid_id",
        "    if {[catch {hm_getboundingbox elems 2 0 0 0} ebb]} {return 0}",
        "    if {[catch {hm_getboundingbox surfaces 2 0 0 0} sbb]} {return 0}",
        "    set sx [expr {abs([lindex $sbb 3] - [lindex $sbb 0])}]",
        "    set sy [expr {abs([lindex $sbb 4] - [lindex $sbb 1])}]",
        "    set sz [expr {abs([lindex $sbb 5] - [lindex $sbb 2])}]",
        "    set diag [expr {sqrt($sx*$sx + $sy*$sy + $sz*$sz)}]",
        "    set tol [expr {max($elem_size * 1.5, $diag * $fit_ratio)}]",
        "    for {set i 0} {$i < 6} {incr i} {",
        "        if {abs([lindex $ebb $i] - [lindex $sbb $i]) > $tol} {return 0}",
        "    }",
        "    return 1",
        "}",
        'catch {*beginhistorystate "MCP guarded spin hex"}',
        '*currentcollector components "$spin_component"',
        "*createmark surfaces 1 $source_surface",
        "*createmark surfaces 2 $source_surface",
        "set bb [hm_getboundingbox surfaces 2 0 0 0]",
        "set cx [expr {([lindex $bb 0] + [lindex $bb 3]) / 2.0}]",
        "set cy [expr {([lindex $bb 1] + [lindex $bb 4]) / 2.0}]",
        "set cz [expr {([lindex $bb 2] + [lindex $bb 5]) / 2.0}]",
        "*interactiveremeshsurf 1 $elem_size 4 4 2 1 1",
        "*set_meshfaceparams 0 4 1 0 0 1 0.5 1 1",
        "*automesh 0 4 1",
        "*storemeshtodatabase 1",
        "*ameshclearsurface",
        '*createmark elems 1 "by surface" $source_surface',
        "set source_shells [hm_getmark elems 1]",
        "set quad_count 0",
        "foreach eid $source_shells {",
        "    set cfg [hm_getvalue elems id=$eid dataname=config]",
        "    if {$cfg == 104 || $cfg == 108} { incr quad_count }",
        "}",
        "if {[llength $source_shells] == 0 || $quad_count != [llength $source_shells]} {",
        '    puts "MCP guarded spin skipped: source section is not all quads."',
        "    if {[llength $source_shells] > 0} { eval *createmark elems 1 $source_shells; catch {*deletemark elems 1} }",
        '    puts "MCP guarded spin skipped tetra fallback: removed_old_tetra_path=1"',
        "} else {",
        "    set hex_success 0",
        "    set attempt 0",
        "    while {$attempt <= $retry_count && !$hex_success} {",
        '        puts "MCP guarded spin attempt=$attempt elem_size=$elem_size"',
        "        set before_elems [mcp_all_elems]",
        f"        *createplane 1 {nx} {ny} {nz} $cx $cy $cz",
        "        *meshspinelements2 1 1 $spin_angle $spin_density 1 0.0 0",
        "        set new_elems [mcp_list_subtract [mcp_all_elems] $before_elems]",
        "        set hex_count [mcp_count_hex8 $new_elems]",
        "        set fit_ok [mcp_bbox_fit_ok $new_elems $target_solid $fit_tol_ratio $elem_size]",
        "        if {[llength $new_elems] > 0 && $hex_count == [llength $new_elems] && $fit_ok} {",
        "            set hex_success 1",
        '            puts "MCP guarded spin completed: hex8=$hex_count fit_ok=$fit_ok"',
        "        } else {",
        '            puts "MCP guarded spin invalid: new_elements=[llength $new_elems] hex8=$hex_count fit_ok=$fit_ok; cleaning and retrying/falling back."',
        "            if {[llength $new_elems] > 0} { eval *createmark elems 1 $new_elems; catch {*deletemark elems 1} }",
        "        }",
        "        incr attempt",
        "    }",
        "    eval *createmark elems 1 $source_shells",
        "    catch {*deletemark elems 1}",
        '    if {!$hex_success} { puts "MCP guarded spin failed: tetra fallback removed_old_tetra_path=1" }',
        "}",
        'catch {*endhistorystate "MCP guarded spin hex"}',
    ]
    if output_hm_path:
        lines.append(f'*writefile "{_quote_tcl_path(output_hm_path)}" 1')

    return {
        "success": True,
        "script": _wrap_generated_tcl("generate_guarded_spin_hex_tcl", "\n".join(lines)),
        "strategy": (
            "Use spin for clean revolved bodies when the source section can be "
            "meshed as 100% quads. Fall back to tetra for flanges, protrusions, "
            "or any non-quad section. If the selected surface is not a true "
            "cross-section of the solid, use generate_cutsection_spin_hex_tcl."
        ),
    }


@mcp.tool()
def generate_cutsection_spin_hex_tcl(
    solid_id: int,
    component_name: str,
    split_plane_normal: list[float],
    split_plane_point: list[float],
    spin_axis: str = "x",
    spin_axis_point: list[float] | None = None,
    element_size: float = 0.7,
    density: int = 96,
    plane_tolerance: float = 0.02,
    fit_tolerance_ratio: float = 0.05,
    retry_count: int = 1,
    include_existing_section_surfaces: bool = True,
    allow_quad_only_fallback: bool = True,
    delete_existing_component_elements: bool = True,
    output_hm_path: str | None = None,
) -> dict[str, Any]:
    """Generate generic real cut-section spin-hex Tcl for a stepped/recessed revolved solid."""
    if solid_id <= 0:
        raise ValueError("solid_id must be greater than 0.")
    if element_size <= 0:
        raise ValueError("element_size must be greater than 0.")
    if density <= 0:
        raise ValueError("density must be greater than 0.")
    if plane_tolerance <= 0:
        raise ValueError("plane_tolerance must be greater than 0.")
    if fit_tolerance_ratio <= 0:
        raise ValueError("fit_tolerance_ratio must be greater than 0.")
    if retry_count < 0:
        raise ValueError("retry_count cannot be negative.")
    if not component_name.strip():
        raise ValueError("component_name cannot be empty.")
    if len(split_plane_normal) != 3 or len(split_plane_point) != 3:
        raise ValueError("split_plane_normal and split_plane_point must contain 3 numbers.")

    axis_key = spin_axis.strip().lower()
    axis_normals = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}
    if axis_key not in axis_normals:
        raise ValueError("spin_axis must be one of: x, y, z.")

    nx, ny, nz = [float(v) for v in split_plane_normal]
    px, py, pz = [float(v) for v in split_plane_point]
    normal_length = (nx * nx + ny * ny + nz * nz) ** 0.5
    if normal_length <= 0:
        raise ValueError("split_plane_normal cannot be the zero vector.")
    if spin_axis_point is None:
        raise ValueError(
            "spin_axis_point is required and must be a point on the real rotation axis. "
            "Do not reuse split_plane_point unless it is also on that axis."
        )
    if len(spin_axis_point) != 3:
        raise ValueError("spin_axis_point must contain 3 numbers.")
    ax, ay, az = [float(v) for v in spin_axis_point]
    snx, sny, snz = axis_normals[axis_key]
    axis_dot = abs(nx * snx + ny * sny + nz * snz) / normal_length
    if axis_dot > 0.05:
        raise ValueError(
            "For cut-section spin, the split plane must contain the spin axis, "
            "so split_plane_normal must be nearly perpendicular to spin_axis. "
            "If the cut plane is perpendicular to the axis, use drag for a "
            "constant-section body or tetra fallback for complex topology."
        )
    comp = component_name.replace('"', '\\"')

    delete_existing = "1" if delete_existing_component_elements else "0"
    include_existing = "1" if include_existing_section_surfaces else "0"
    quad_fallback = "1" if allow_quad_only_fallback else "0"
    lines = [
        "# HyperMesh MCP generated cut-section spin-hex script",
        "# Use for stepped/recessed revolved solids where an existing face is not a reliable section.",
        "# The spin axis point must lie on the true rotation axis; the split-plane point alone is not enough.",
        f'set target_component "{comp}"',
        f"set target_solid {int(solid_id)}",
        f"set elem_size {float(element_size)}",
        f"set spin_density {int(density)}",
        f"set plane_tol {float(plane_tolerance)}",
        f"set fit_tol_ratio {float(fit_tolerance_ratio)}",
        f"set retry_count {int(retry_count)}",
        f"set include_existing_section_surfaces {include_existing}",
        f"set allow_quad_only_fallback {quad_fallback}",
        f"set delete_existing_component_elements {delete_existing}",
        f"set split_nx {nx}",
        f"set split_ny {ny}",
        f"set split_nz {nz}",
        f"set split_px {px}",
        f"set split_py {py}",
        f"set split_pz {pz}",
        f"set axis_px {ax}",
        f"set axis_py {ay}",
        f"set axis_pz {az}",
        "proc mcp_mark_count {entity mark_id} {",
        "    if {[catch {hm_marklength $entity $mark_id} n]} {return 0}",
        "    return $n",
        "}",
        "proc mcp_all_surfs {} {",
        '    *createmark surfs 1 "all"',
        "    return [hm_getmark surfs 1]",
        "}",
        "proc mcp_all_elems {} {",
        '    *createmark elems 1 "all"',
        "    return [hm_getmark elems 1]",
        "}",
        "proc mcp_list_subtract {a b} {",
        "    array set seen {}",
        "    foreach x $b {set seen($x) 1}",
        "    set out {}",
        "    foreach x $a {if {![info exists seen($x)]} {lappend out $x}}",
        "    return $out",
        "}",
        "proc mcp_unique_append {base additions} {",
        "    array set seen {}",
        "    set out {}",
        "    foreach x $base {",
        "        if {![info exists seen($x)]} {set seen($x) 1; lappend out $x}",
        "    }",
        "    foreach x $additions {",
        "        if {![info exists seen($x)]} {set seen($x) 1; lappend out $x}",
        "    }",
        "    return $out",
        "}",
        "proc mcp_delete_elems {elems} {",
        "    if {[llength $elems] == 0} {return}",
        "    eval *createmark elems 1 $elems",
        "    catch {*deletemark elems 1}",
        "}",
        "proc mcp_hex8_count {elems} {",
        "    set hexes 0",
        "    foreach eid $elems {",
        "        if {[catch {hm_getvalue elems id=$eid dataname=config} cfg]} {continue}",
        "        if {$cfg == 208} {incr hexes}",
        "    }",
        "    return $hexes",
        "}",
        "proc mcp_bbox_fit_ok {elems solid_id fit_ratio elem_size} {",
        "    if {[llength $elems] == 0} {return 0}",
        "    eval *createmark elems 2 $elems",
        "    *createmark surfaces 2 \"by solids\" $solid_id",
        "    if {[mcp_mark_count elems 2] == 0 || [mcp_mark_count surfaces 2] == 0} {return 0}",
        "    if {[catch {hm_getboundingbox elems 2 0 0 0} ebb]} {return 0}",
        "    if {[catch {hm_getboundingbox surfaces 2 0 0 0} sbb]} {return 0}",
        "    set sx [expr {abs([lindex $sbb 3] - [lindex $sbb 0])}]",
        "    set sy [expr {abs([lindex $sbb 4] - [lindex $sbb 1])}]",
        "    set sz [expr {abs([lindex $sbb 5] - [lindex $sbb 2])}]",
        "    set diag [expr {sqrt($sx*$sx + $sy*$sy + $sz*$sz)}]",
        "    set tol [expr {max($elem_size * 1.5, $diag * $fit_ratio)}]",
        "    for {set i 0} {$i < 6} {incr i} {",
        "        set diff [expr {abs([lindex $ebb $i] - [lindex $sbb $i])}]",
        "        if {$diff > $tol} {",
        '            puts "MCP mesh-solid fit failed: bbox_index=$i mesh=[lindex $ebb $i] solid=[lindex $sbb $i] diff=$diff tol=$tol"',
        "            return 0",
        "        }",
        "    }",
        "    return 1",
        "}",
        "proc mcp_node_plane_dist {nid nx ny nz px py pz} {",
        "    set x [hm_getvalue nodes id=$nid dataname=x]",
        "    set y [hm_getvalue nodes id=$nid dataname=y]",
        "    set z [hm_getvalue nodes id=$nid dataname=z]",
        "    set d [expr {$nx * ($x - $px) + $ny * ($y - $py) + $nz * ($z - $pz)}]",
        "    if {$d < 0} {set d [expr {-$d}]}",
        "    return $d",
        "}",
        "proc mcp_mesh_true_section {sid elem_size nx ny nz px py pz plane_tol} {",
        "    set mesh_modes {{1 5}}",
        "    if {$::mcp_allow_quad_only_fallback} {lappend mesh_modes {4 4}}",
        "    foreach mode_pair $mesh_modes {",
        "        set interactive_mode [lindex $mode_pair 0]",
        "        set face_mode [lindex $mode_pair 1]",
        "        *createmark surfaces 1 $sid",
        "        catch {*setedgedensitylinkwithaspectratio -1}",
        "        *setedgedensitylink 1",
        "        *interactiveremeshsurf 1 $elem_size $interactive_mode $face_mode 2 1 1",
        "        *set_meshfaceparams 0 $face_mode 1 0 0 1 0.5 1 1",
        "        *automesh 0 $face_mode 1",
        "        *storemeshtodatabase 1",
        "        *ameshclearsurface",
        '        *createmark elems 1 "by surface" $sid',
        "        set shells [hm_getmark elems 1]",
        "        if {[llength $shells] == 0} {continue}",
        "        set quads 0",
        "        set maxdist 0.0",
        "        foreach eid $shells {",
        "            set cfg [hm_getvalue elems id=$eid dataname=config]",
        "            if {$cfg == 104 || $cfg == 108} {incr quads}",
        "            foreach nid [hm_getvalue elems id=$eid dataname=nodes] {",
        "                set d [mcp_node_plane_dist $nid $nx $ny $nz $px $py $pz]",
        "                if {$d > $maxdist} {set maxdist $d}",
        "            }",
        "        }",
        "        if {$quads == [llength $shells] && $maxdist <= $plane_tol} {",
        '            puts "MCP accepted true section surface=$sid mesh_mode=$face_mode shells=[llength $shells] maxdist=$maxdist plane_tol=$plane_tol"',
        "            return $shells",
        "        }",
        "        mcp_delete_elems $shells",
        "    }",
        "    return {}",
        "}",
        'catch {*beginhistorystate "MCP cut-section spin hex"}',
        '*currentcollector components "$target_component"',
        "set ::mcp_allow_quad_only_fallback $allow_quad_only_fallback",
        "set hex_success 0",
        "if {$delete_existing_component_elements} {",
        '    *createmark elems 1 "by comp name" $target_component',
        "    if {[mcp_mark_count elems 1] > 0} {catch {*deletemark elems 1}}",
        "}",
        "set before_surfs [mcp_all_surfs]",
        "*createmark solids 1 $target_solid",
        "if {[mcp_mark_count solids 1] == 0} {",
        '    puts "MCP cut-section spin skipped: solid is missing."',
        "} else {",
        "    *createplane 1 $split_nx $split_ny $split_nz $split_px $split_py $split_pz",
        "    if {[catch {*body_splitmerge_with_plane solids 1 1} split_err]} {",
        '        puts "MCP cut-section split failed: $split_err"',
        "    } else {",
        "        set new_surfs [lsort -integer [mcp_list_subtract [mcp_all_surfs] $before_surfs]]",
        '        puts "MCP cut-section new_surfs=$new_surfs"',
        "        set candidate_surfs $new_surfs",
        "        if {$include_existing_section_surfaces} {",
        "            *createmark surfs 2 \"by solids\" $target_solid",
        "            set solid_surfs [hm_getmark surfs 2]",
        "            set candidate_surfs [mcp_unique_append $candidate_surfs $solid_surfs]",
        "        }",
        '        puts "MCP cut-section candidate_surfs=$candidate_surfs"',
        "        set attempt 0",
        "        while {$attempt <= $retry_count && !$hex_success} {",
        "            set attempt_size $elem_size",
        "            set effective_plane_tol [expr {max($plane_tol, $attempt_size * 0.05)}]",
        '            puts "MCP cut-section spin attempt=$attempt elem_size=$attempt_size"',
        "            set seed_shells {}",
        "            foreach sid $candidate_surfs {",
        "                set shells [mcp_mesh_true_section $sid $attempt_size $split_nx $split_ny $split_nz $split_px $split_py $split_pz $effective_plane_tol]",
        "                foreach e $shells {lappend seed_shells $e}",
        "            }",
        "            if {[llength $seed_shells] == 0} {",
        '                puts "MCP cut-section spin attempt failed: no true all-quad section surfaces were found."',
        "                incr attempt",
        "                continue",
        "            }",
        "            set before_elems [mcp_all_elems]",
        "            eval *createmark elems 1 $seed_shells",
        f"            *createplane 1 {snx} {sny} {snz} $axis_px $axis_py $axis_pz",
        "            if {[catch {*meshspinelements2 1 1 360 $spin_density 1 0.0 0} spin_err]} {",
        '                puts "MCP cut-section spin attempt failed: $spin_err"',
        "            } else {",
        "                set new_elems [mcp_list_subtract [mcp_all_elems] $before_elems]",
        "                set hex_count [mcp_hex8_count $new_elems]",
        "                set fit_ok [mcp_bbox_fit_ok $new_elems $target_solid $fit_tol_ratio $attempt_size]",
        "                if {[llength $new_elems] > 0 && $hex_count == [llength $new_elems] && $fit_ok} {",
        "                    eval *createmark elems 1 $new_elems",
        "                    catch {*movemark elems 1 $target_component}",
        "                    set hex_success 1",
        '                    puts "MCP cut-section spin completed: hex8=$hex_count fit_ok=$fit_ok"',
        "                } else {",
        '                    puts "MCP cut-section spin invalid: new_elements=[llength $new_elems] hex8=$hex_count fit_ok=$fit_ok; cleaning and retrying/falling back."',
        "                    mcp_delete_elems $new_elems",
        "                }",
        "            }",
        "            mcp_delete_elems $seed_shells",
        "            incr attempt",
        "        }",
        "    }",
        "}",
        'if {!$hex_success} { puts "MCP cut-section spin failed: tetra fallback removed_old_tetra_path=1" }',
        'catch {*endhistorystate "MCP cut-section spin hex"}',
    ]
    if output_hm_path:
        lines.append(f'*writefile "{_quote_tcl_path(output_hm_path)}" 1')

    return {
        "success": True,
        "script": _wrap_generated_tcl("generate_cutsection_spin_hex_tcl", "namespace eval ::mcp_meshing {\n" + "\n".join(lines) + "\n}\n"),
        "strategy": (
            "Generic cut-section spin: split the actual solid first, accept only "
            "new all-quad surfaces that lie on that cut plane, then spin those "
            "2D shells. This is intended for stepped/recessed revolved solids."
        ),
    }


@mcp.tool()
def execute_tcl(
    script: str,
    hmbatch_path: str | None = None,
    model_path: str | None = None,
    timeout_seconds: int = 120,
    enforce_meshing_rules: bool = True,
) -> dict[str, Any]:
    """Execute a raw HyperMesh Tcl script with hmbatch."""
    if not script.strip():
        raise ValueError("script cannot be empty.")
    if enforce_meshing_rules:
        violation = _meshing_rule_violation(script)
        if violation:
            violation["execution_mode"] = "batch"
            return violation
        violation = _phase2_finalization_violation(script)
        if violation:
            violation["execution_mode"] = "batch"
            return violation
    result = _run_hmbatch(
        hmbatch_path=hmbatch_path,
        model_path=model_path,
        script=script,
        timeout_seconds=timeout_seconds,
    )
    _mark_phase2_finalized_from_result(result)
    return result


@mcp.tool()
def execute_tcl_gui(
    script: str,
    host: str = "127.0.0.1",
    port: int = DEFAULT_GUI_PORT,
    model_path: str | None = None,
    output_hm_path: str | None = None,
    timeout_seconds: int = 120,
    enforce_meshing_rules: bool = True,
) -> dict[str, Any]:
    """Execute Tcl inside an already visible HyperMesh GUI listener session."""
    if not script.strip():
        raise ValueError("script cannot be empty.")
    if enforce_meshing_rules:
        violation = _meshing_rule_violation(script)
        if violation:
            violation["execution_mode"] = "visible_gui"
            return violation
        violation = _phase2_finalization_violation(script)
        if violation:
            violation["execution_mode"] = "visible_gui"
            return violation

    prefix: list[str] = []
    model = _normalize_path(model_path)
    if model:
        if not model.exists():
            raise FileNotFoundError(f"Model file was not found: {model}")
        prefix.append(f'*readfile "{_quote_tcl_path(model)}"')

    suffix: list[str] = []
    if output_hm_path:
        suffix.append(f'*writefile "{_quote_tcl_path(output_hm_path)}" 1')

    gui_script = "\n".join(prefix + [script] + suffix)
    if not gui_script.endswith("\n"):
        gui_script += "\n"

    try:
        result = _run_hypermesh_gui_script(
            script=gui_script,
            host=host,
            port=port,
            timeout_seconds=timeout_seconds,
        )
        _mark_phase2_finalized_from_result(result)
        return result
    except OSError as exc:
        return {
            "success": False,
            "host": host,
            "port": int(port),
            "message": (
                "Could not connect to the visible HyperMesh GUI listener. "
                "Run start_hypermesh_gui_listener, or open HyperMesh and source "
                "the Tcl file returned by create_gui_listener_tcl."
            ),
            "error": str(exc),
        }


@mcp.tool()
def make_recorded_tcl_wrapper(
    recorded_tcl_path: str,
    replacements: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Load a HyperMesh-recorded Tcl command file and optionally apply replacements."""
    path = _normalize_path(recorded_tcl_path)
    if not path or not path.exists():
        raise FileNotFoundError(f"Recorded Tcl file was not found: {recorded_tcl_path}")

    script = path.read_text(encoding="utf-8", errors="replace")
    for old, new in (replacements or {}).items():
        script = script.replace(str(old), str(new))
    return {"success": True, "script": script}


@mcp.tool()
def automesh_surfaces(
    input_hm_path: str,
    output_hm_path: str,
    element_size: float,
    surface_ids: list[int] | None = None,
    hmbatch_path: str | None = None,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Run a simple surface automesh on an existing .hm model and save a new .hm file."""
    generated = generate_surface_automesh_tcl(
        element_size=element_size,
        surface_ids=surface_ids,
        output_hm_path=output_hm_path,
    )
    result = _run_hmbatch(
        hmbatch_path=hmbatch_path,
        model_path=input_hm_path,
        script=generated["script"],
        timeout_seconds=timeout_seconds,
    )
    result["output_hm_path"] = output_hm_path
    return result


@mcp.tool()
def automesh_surfaces_gui(
    input_hm_path: str,
    output_hm_path: str,
    element_size: float,
    surface_ids: list[int] | None = None,
    host: str = "127.0.0.1",
    port: int = DEFAULT_GUI_PORT,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Run surface automesh inside the visible HyperMesh GUI and save a new file."""
    generated = generate_surface_automesh_tcl(
        element_size=element_size,
        surface_ids=surface_ids,
    )
    result = execute_tcl_gui(
        script=generated["script"],
        host=host,
        port=port,
        model_path=input_hm_path,
        output_hm_path=output_hm_path,
        timeout_seconds=timeout_seconds,
    )
    result["output_hm_path"] = output_hm_path
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="HyperMesh MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default=os.environ.get("MCP_TRANSPORT", "stdio"),
        help="Transport mode: stdio (default, for Codex) or sse (for Cowork HTTP)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("MCP_HOST", "127.0.0.1"),
        help="Host for SSE mode (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MCP_PORT", "8742")),
        help="Port for SSE mode (default: 8742)",
    )
    args = parser.parse_args()

    if args.transport == "sse":
        print(f"Starting HyperMesh MCP server in SSE mode on {args.host}:{args.port}")
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="sse")
    else:
        mcp.run()
