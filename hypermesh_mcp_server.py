from __future__ import annotations

import os
import json
import socket
import subprocess
import time
import uuid
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
GUI_REALTIME_DIAG_PATH = RUNS_DIR / "mcp_gui_realtime_diagnostics_latest.jsonl"

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
7. For obvious revolved bodies, prefer spin meshing, but never invent the
   section from guessed radii or from a side/end face. First split the solid with
   a real middle cutting plane, use only the newly created surfaces that lie on
   that cutting plane as 2D section sources, mesh one section surface, then spin
   it to 3D. The section mesh may be mixed, but its size is chosen and checked
   from the actual section dimensions; if the section mesh is too coarse, clean
   up temporary shells and fall back to the tetra strategy.
8. Try a structured hex route before tetra when the geometry supports it:
   drag for simple constant-section extrusions and cut-section spin for
   revolved solids.
   If the chosen hex route fails validation, fall back to tetra for that object.
   Clean bearing/ring-like revolved bodies should get a real cut-section spin
   attempt before tetra; do not use direct surface-id spin.
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
    fillets, teeth, holes, and chamfers can be captured. Log shell-count guard
    warnings before *tetmesh for crash diagnosis, but do not stop complex solids;
    isolate high-risk tetra solids into separate Phase 3 batches instead.
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
            "2D shells with aspect > 2000 are treated as extreme geometry warnings: exclude them from automatic 2D repair, repair the remaining bad shells, and report them prominently",
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
            "auto-computed as round(drag_distance / element_size). Agent-side "
            "generation defaults to at least 3 layers with a native hex aspect "
            "check; offline panel keeps the older behavior unless the checkbox is enabled."
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
    "cutsection_spin_hex": {
        "use_when": [
            "stepped, recessed, or ambiguous revolved solid",
            "a middle cutting plane through the rotation axis can be defined",
        ],
        "method": [
            "split the actual solid with body_splitmerge_with_plane",
            "detect the two newly created cross-section surfaces",
            "try meshing one cross-section surface twice with a size chosen from that section's bbox",
            "mixed 2D section meshes are allowed when the mesh density is reasonable for the section size",
            "spin the accepted 2D section mesh into 3D elements",
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
# Avoid color id 1 because it renders as black in common HyperMesh themes and
# makes black mesh edges almost invisible.
FINALIZE_STRATEGY_COLORS = {
    "drag_hex": 8,
    "spin_hex": 7,
    "gear_aware_tetra": 3,
    "tetra_plain": 4,
    "surface_tetra": 4,
    "unknown": 4,
}
FINALIZE_UNIQUE_COLOR_IDS = [
    8, 7, 3, 4, 5, 6, 9, 10, 11, 12, 13, 14, 15, 16,
    17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28,
    29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40,
    41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52,
    53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64,
]

TETRA_COMPLEX_SURFACE_COUNT = 50
TETRA_VERY_COMPLEX_SURFACE_COUNT = 120
TETRA_COMPLEX_MIN_ELEMENT_SIZE = 0.25
TETRA_VERY_COMPLEX_MIN_ELEMENT_SIZE = 0.20
TETRA_MAX_SHELL_ELEMENTS = 8000
TETRA_COMPLEX_MAX_SHELL_ELEMENTS = 2500
TETRA_VERY_COMPLEX_MAX_SHELL_ELEMENTS = 1500
TETRA_CRASH_GUARD_SHELL_ELEMENTS = 150000
TETRA_DIRECT_TETMESH_SURFACE_COUNT_LIMIT = 50
TETRA_FATAL_SURFACE_ASPECT = 2000.0
TETRA_SURFACE_CHORD_DEV = 0.1
GEAR_TOOTH_DEFAULT_SIZE_SCALE = 0.70
GEAR_TOOTH_PREVIEW_COMPONENT = "__mcp_gear_tooth_preview"

# Generated-script boundary markers used by _meshing_rule_violation
MCP_SCRIPT_BEGIN = "# MCP_SCRIPT_BEGIN"
MCP_SCRIPT_END = "# MCP_SCRIPT_END"
TRUSTED_MESHING_GENERATORS = {
    "generate_surface_automesh_tcl",
    "generate_plain_tetra_tcl",
    "generate_batched_plain_tetra_tcl",
    "generate_gear_tooth_preview_tcl",
    "generate_guarded_drag_hex_tcl",
    "generate_batched_drag_hex_tcl",
    "generate_cutsection_spin_hex_tcl",
}
TRUSTED_NON_MESHING_GENERATORS = {
    "generate_phase2_finalize_tcl",
    "generate_finalize_components_tcl",
    "generate_delete_gear_tooth_preview_tcl",
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
            "Use this for revolved solids; do not use a guessed existing surface as the spin section.",
            "Split the target solid with body_splitmerge_with_plane using a user-provided middle plane.",
            "Detect the real section surfaces by meshing each new surface temporarily and checking node distance to the split plane.",
            "Select one cut-section surface and try meshing it twice.",
            "Continue only when the 2D section is all quads, then spin it around the real rotation axis.",
        ],
        "required_inputs": [
            "solid_id",
            "component_name",
            "split plane normal and point",
            "spin axis and axis point",
            "element size and spin density",
        ],
        "why": (
            "A real solid split is the reliable way to obtain the radial "
            "cross-section for recessed or stepped rings."
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
            Path(r"E:\Program Files\Altair\2020\hwdesktop\hw\bin\win64\hmbatch.exe"),
            Path(r"E:\Program Files\Altair\2020\hwdesktop\hm\bin\win64\hmbatch.exe"),
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
            Path(r"E:\Program Files\Altair\2020\hwdesktop\hw\bin\win64\hw.exe"),
            Path(r"E:\Program Files\Altair\2020\hwdesktop\hwx\bin\win64\hwx.exe"),
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


def _altair_home_from_exe(exe: Path) -> Path:
    resolved = exe.resolve()
    parts = [part.lower() for part in resolved.parts]
    if "hm" in parts:
        index = parts.index("hm")
        if index > 0:
            return Path(*resolved.parts[:index])
    if "hw" in parts:
        index = parts.index("hw")
        if index > 0:
            return Path(*resolved.parts[:index])
    return DEFAULT_HYPERMESH_DIR.parents[3]


def _hmbatch_environment(exe: Path) -> dict[str, str]:
    env = os.environ.copy()
    altair_home = _altair_home_from_exe(exe)
    env["ALTAIR_HOME"] = str(altair_home)
    env.setdefault("HW_ROOTDIR", str(altair_home))

    bin_paths = [
        altair_home / "hm" / "bin" / "win64",
        altair_home / "hw" / "bin" / "win64",
    ]
    existing_bins = [str(path) for path in bin_paths if path.exists()]
    if existing_bins:
        env["PATH"] = ";".join(existing_bins + [env.get("PATH", "")])

    tcl_library = altair_home / "hw" / "tcl" / "tcl8.5.9" / "win64" / "lib" / "tcl8.5"
    if tcl_library.exists():
        env["TCL_LIBRARY"] = str(tcl_library)
    return env


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


def _append_gui_realtime_diag(event: str, **payload: Any) -> None:
    try:
        RUNS_DIR.mkdir(exist_ok=True)
        record = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event": event,
            **payload,
        }
        with GUI_REALTIME_DIAG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            handle.flush()
    except Exception:
        pass


def _script_diag_summary(script: str) -> dict[str, Any]:
    markers = [
        line.strip()
        for line in str(script or "").splitlines()
        if "MCP_" in line or line.strip().startswith(("puts", "*tetmesh", "*meshdragelements", "*meshspinelements"))
    ]
    return {
        "script_chars": len(script or ""),
        "script_lines": len(str(script or "").splitlines()),
        "first_markers": markers[:8],
        "last_markers": markers[-8:],
    }


def _gui_response_reason(result: dict[str, Any] | None) -> str:
    if not isinstance(result, dict):
        return "no_response"
    text = str(result.get("response", "") or result.get("stdout", "") or result.get("error", "") or result.get("message", ""))
    lower = text.lower()
    if result.get("success"):
        return "ok"
    if "software caused connection abort" in lower or "connection abort" in lower:
        return "hypermesh_connection_abort_or_crash"
    if "connection refused" in lower or "could not connect" in lower:
        return "listener_not_available_or_hypermesh_closed"
    if "timed out" in lower or "timeout" in lower:
        return "timeout_or_hypermesh_hang"
    if "error" in lower:
        return "tcl_or_hypermesh_error"
    return "failed"


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


def _unwrap_generated_tcl(script: str) -> str:
    """Return body inside generated-script markers."""
    text = str(script)
    if MCP_SCRIPT_BEGIN in text and MCP_SCRIPT_END in text:
        return text.split(MCP_SCRIPT_BEGIN, 1)[1].split(MCP_SCRIPT_END, 1)[0].strip()
    return text


def _estimate_tetra_timeout_seconds(
    *,
    surf_count: int = 0,
    min_element_size: float = 0.6,
    diagonal: float = 0.0,
    batch_size: int = 1,
) -> int:
    min_size = max(0.2, float(min_element_size or 0.5))
    complexity = max(1.0, float(surf_count or 1)) * max(1.0, float(diagonal or 1.0)) / min_size
    return int(max(300, min(7200, 240 + complexity * 0.03 + max(1, batch_size) * 120)))


def _recommended_timeout_from_script(script: str) -> int | None:
    for line in str(script).splitlines()[:20]:
        stripped = line.strip()
        if stripped.startswith("# MCP_RECOMMENDED_TIMEOUT_SECONDS="):
            try:
                return int(float(stripped.split("=", 1)[1].strip()))
            except ValueError:
                return None
    return None


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
               "src_surf": "source_surface_id", "drag_axis": "drag_axis",
               "src_loops": "source_loop_count", "src_inner_loops": "source_inner_loop_count",
               "src_boundary_edges": "source_boundary_edge_count",
               "src_boundary_nodes": "source_boundary_node_count"}
    for old, new in ALIASES.items():
        if old in facts and new not in facts:
            facts[new] = facts[old]
    return facts


def _probe_int_list_value(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            try:
                out.append(int(item))
            except (TypeError, ValueError):
                continue
        return out
    text = str(value).strip().strip("{}")
    if not text or text.lower() in {"none", "null", "-"}:
        return []
    out = []
    for token in text.replace(",", " ").split():
        try:
            out.append(int(token))
        except ValueError:
            continue
    return out


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


def _spin_cutsection_params_from_probe(facts: dict[str, Any]) -> dict[str, Any]:
    """Build a cut-section spin plan from bbox facts.

    The spin axis is the smallest bbox direction. The split plane must contain
    that axis, so its normal is chosen from one of the two radial directions.
    """
    axis = str(facts.get("drag_axis", "") or "z").lower()
    if axis not in {"x", "y", "z"}:
        axis = "z"
    x0, y0, z0 = (float(facts.get(k, 0.0) or 0.0) for k in ("x0", "y0", "z0"))
    x1, y1, z1 = (float(facts.get(k, 0.0) or 0.0) for k in ("x1", "y1", "z1"))
    cx = (x0 + x1) / 2.0
    cy = (y0 + y1) / 2.0
    cz = (z0 + z1) / 2.0
    normal_by_axis = {
        "x": [0.0, 1.0, 0.0],
        "y": [1.0, 0.0, 0.0],
        "z": [1.0, 0.0, 0.0],
    }
    return {
        "spin_method": "cutsection",
        "spin_axis": axis,
        "spin_axis_point": [round(cx, 6), round(cy, 6), round(cz, 6)],
        "spin_split_plane_normal": normal_by_axis[axis],
        "spin_split_plane_point": [round(cx, 6), round(cy, 6), round(cz, 6)],
    }


def _spin_params_from_probe(facts: dict[str, Any]) -> dict[str, Any]:
    # Spin is intentionally based on a real cross-section cut through the
    # rotation axis. Do not reuse drag end faces or guessed longitudinal faces.
    return _spin_cutsection_params_from_probe(facts)


def _gear_reason_from_probe(facts: dict[str, Any]) -> str | None:
    """Return a gear classification reason from geometry facts only."""
    min_repeated_tooth_faces = 12
    sc = int(facts.get("surf_count", 0) or 0)
    dx, dy, dz = float(facts.get("dx", 0) or 0), float(facts.get("dy", 0) or 0), float(facts.get("dz", 0) or 0)
    dims = sorted([dx, dy, dz])
    mn, md, mx = dims[0], dims[1], dims[2]
    if mx <= 0 or md <= 0:
        return None
    src_surf = int(facts.get("source_surface_id", -1) or -1)
    mn_mx = mn / mx
    md_mx = md / mx
    mn_md = mn / md
    mx_md = mx / md
    tooth_count = int(facts.get("gear_tooth_count", 0) or 0)
    tooth_density = tooth_count / max(sc, 1)
    src_inner_loops = int(facts.get("source_inner_loop_count", 0) or 0)
    src_boundary_nodes = int(facts.get("source_boundary_node_count", 0) or 0)

    # Small closed gears can have too few separate side surfaces for the
    # tooth-face probe to count them, but their source section exposes a dense
    # looped boundary. Keep this path narrow so ordinary flanges stay out.
    if 70 <= sc <= 100 and src_surf > 0 and md_mx >= 0.95 and 0.30 <= mn_mx <= 0.42 and src_inner_loops >= 3 and src_boundary_nodes >= 100:
        return "gear small looped circular repeated-boundary body"

    # Source-based gears should expose one repeated tooth ring. Multi-inner-loop
    # source sections are usually housings/covers with circular arrays of ribs,
    # bosses, or holes; they can produce many repeated faces but are not gears.
    dense_thin_repeated_ring = (
        tooth_count >= min_repeated_tooth_faces
        and tooth_density >= 0.70
        and md_mx >= 0.95
        and mn_mx <= 0.30
    )
    if src_surf > 0 and src_inner_loops > 2 and not dense_thin_repeated_ring:
        return None

    # Internal ring gears are thin circular rings with a single dense repeated
    # inner boundary. Their tooth faces sit on the inside, so the usual outer
    # band density is lower than for external gears.
    if src_surf > 0 and sc >= 180 and tooth_count >= 40 and md_mx >= 0.95 and 0.10 <= mn_mx <= 0.22 and src_inner_loops == 1 and src_boundary_nodes >= 200:
        return "gear internal ring: single dense repeated inner tooth ring"

    # Small sun gears can be compact and closed enough that the surface probe is
    # conservative. The source boundary still carries a dense single tooth loop.
    if src_surf > 0 and 100 <= sc <= 170 and tooth_count >= min_repeated_tooth_faces and md_mx >= 0.95 and 0.38 <= mn_mx <= 0.46 and src_inner_loops == 1 and src_boundary_nodes >= 150:
        return "gear compact sun: single dense repeated boundary body"

    # Some bearing/cam-like thin circular parts expose one very dense source
    # loop, but their repeated faces do not form a sufficiently dense tooth
    # band. Real small gears in this size class have boundary-node/surface-count
    # ratios near 1.0, while these false positives are much higher.
    boundary_surface_density = src_boundary_nodes / max(sc, 1)
    if (
        src_surf > 0
        and src_inner_loops == 1
        and 120 <= sc <= 179
        and 0.20 <= mn_mx <= 0.32
        and md_mx >= 0.95
        and src_boundary_nodes >= 240
        and boundary_surface_density >= 1.45
        and tooth_density < 0.70
    ):
        return None

    # Compact circular bodies are gear-like only when the tooth-band probe
    # already sees a dense repeated band. A plain circular plate with many holes
    # can otherwise look deceptively similar by bbox and surface count.
    if sc >= 100 and tooth_count >= min_repeated_tooth_faces and tooth_density >= 0.45 and md_mx >= 0.95 and 0.12 <= mn_mx <= 0.35:
        return "gear compact circular dense repeated-tooth body"

    # Tooth candidates alone are not enough: large side plates can have many
    # repeated ribs/chamfers. Keep this path limited to gear-like envelopes.
    if sc >= 100 and tooth_count >= 40 and tooth_density >= 0.25 and md_mx >= 0.95 and 0.25 <= mn_mx <= 0.60:
        return "gear thin circular repeated-tooth body"
    if src_surf <= 0 and sc >= 100 and tooth_count >= 40 and tooth_density >= 0.20 and mn_mx >= 0.70 and md_mx >= 0.70 and mx_md <= 1.40:
        return "gear thick near-round repeated-tooth body"
    if sc >= 100 and tooth_count >= 40 and tooth_density >= 0.45 and 0.40 <= mn_mx <= 0.54 and 0.60 <= md_mx <= 0.75 and mx_md <= 1.60:
        return "gear oblong repeated-tooth body"

    if src_surf <= 0 and 40 <= sc <= 90 and tooth_count >= 20 and tooth_density >= 0.45 and mn_md >= 0.85 and mx_md <= 2.10:
        return "gear compact no-source external gear: dense repeated tooth band"

    if src_surf <= 0 and sc >= 100 and tooth_count >= 40 and tooth_density >= 0.45 and md_mx >= 0.95 and 0.18 <= mn_mx <= 0.50:
        return "gear disk/body: circular envelope with dense repeated-tooth band"
    if src_surf <= 0 and sc >= 400 and tooth_count >= 30 and tooth_density >= 0.06 and md_mx >= 0.95 and 0.18 <= mn_mx <= 0.50:
        return "gear high-surface external ring: repeated tooth band on circular envelope"
    if src_surf <= 0 and sc >= 180 and tooth_count >= 40 and tooth_density >= 0.25 and md_mx >= 0.70 and mn_mx >= 0.70:
        return "gear rounded/chamfer body: compact envelope with repeated-tooth band"
    if src_surf <= 0 and 40 <= sc <= 90 and tooth_count >= 20 and tooth_density >= 0.45 and mn_md >= 0.85 and 2.5 <= mx_md <= 5.5:
        return "gear shaft/end slender body: dense repeated tooth band"
    if src_surf <= 0 and 80 <= sc <= 140 and tooth_count >= 20 and tooth_density >= 0.10 and mn_md >= 0.90 and 2.5 <= mx_md <= 5.5:
        return "gear shaft/tooth-related slender body: repeated-feature surface count with round cross-section"
    return None


def _tetra_execution_batches(results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Return tetra batches ordered to reduce HyperMesh memory-crash risk."""
    tetra = [
        item for item in results.values()
        if item.get("strategy") in {"tetra_plain", "tetra_surface_deviation_rtrias", "surface_tetra", "gear_aware_tetra"}
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
    current_group = ""
    batch_index = 1

    def batch_reason(group: str) -> str:
        if group == "gear":
            return "gear-aware tetra batch"
        return "low/medium-risk tetra batch"

    def item_group(item: dict[str, Any]) -> str:
        return "gear" if item.get("strategy") == "gear_aware_tetra" else "tetra"

    grouped_items: list[dict[str, Any]] = []
    grouped_by_id: dict[str, list[dict[str, Any]]] = {}
    ungrouped: list[dict[str, Any]] = []
    for item in tetra:
        tetra_group_id = str(item.get("tetra_group_id") or "")
        if tetra_group_id:
            grouped_by_id.setdefault(tetra_group_id, []).append(item)
        else:
            ungrouped.append(item)
    for tetra_group_id, members in grouped_by_id.items():
        ordered_members = sorted(
            members,
            key=lambda value: (
                int(value.get("tetra_group_order", 0) or 0),
                int(value.get("solid_id", 0)),
            ),
        )
        grouped_items.append(
            {
                "items": ordered_members,
                "sort_key": (
                    0 if any(value.get("strategy") == "gear_aware_tetra" for value in ordered_members) else 1,
                    min(int(value.get("solid_id", 0)) for value in ordered_members),
                ),
                "group_id": tetra_group_id,
            }
        )
    for item in ungrouped:
        grouped_items.append(
            {
                "items": [item],
                "sort_key": (0 if item.get("strategy") == "gear_aware_tetra" else 1, int(item.get("solid_id", 0))),
                "group_id": "",
            }
        )

    for group_entry in sorted(grouped_items, key=lambda value: value["sort_key"]):
        group_items = group_entry["items"]
        solid_ids = [int(value.get("solid_id", 0)) for value in group_items]
        score = sum(risk_score(value) for value in group_items)
        group = item_group(group_items[0])
        high_risk = (
            any(int(value.get("surf_count", 0)) >= TETRA_VERY_COMPLEX_SURFACE_COUNT for value in group_items)
            or score >= 20000
        )
        if current and group != current_group:
            batches.append({
                "batch": batch_index,
                "solid_ids": current,
                "reason": batch_reason(current_group),
                "pause_seconds_after_batch": 5,
            })
            batch_index += 1
            current = []
            current_score = 0.0
            current_group = ""
        if high_risk:
            if current:
                batches.append({
                    "batch": batch_index,
                    "solid_ids": current,
                    "reason": batch_reason(current_group),
                    "pause_seconds_after_batch": 5,
                })
                batch_index += 1
                current = []
                current_score = 0.0
                current_group = ""
            batches.append({
                "batch": batch_index,
                "solid_ids": solid_ids,
                "reason": f"high-risk {batch_reason(group)}; run alone, then save/cool down before the next tetra batch",
                "pause_seconds_after_batch": 10,
            })
            batch_index += 1
            continue
        if current and (len(current) + len(solid_ids) > 4 or current_score + score > 18000):
            batches.append({
                "batch": batch_index,
                "solid_ids": current,
                "reason": batch_reason(current_group),
                "pause_seconds_after_batch": 5,
            })
            batch_index += 1
            current = []
            current_score = 0.0
            current_group = ""
        current.extend(solid_ids)
        current_score += score
        current_group = group
    if current:
        batches.append({
            "batch": batch_index,
            "solid_ids": current,
            "reason": batch_reason(current_group),
            "pause_seconds_after_batch": 5,
        })
    return batches


def _gui_listener_script(host: str = "127.0.0.1", port: int = DEFAULT_GUI_PORT) -> str:
    return f"""
# HyperMesh MCP GUI listener.
# Source this file inside a visible HyperMesh session, or launch HyperMesh with it.
set ::mcp_hm_host "{host}"
set ::mcp_hm_port {int(port)}

proc ::mcp_hm_restore_puts {{}} {{
    if {{[llength [info commands ::_mcp_orig_puts]] > 0}} {{
        catch {{rename puts ""}}
        catch {{rename ::_mcp_orig_puts puts}}
    }}
    if {{[llength [info commands ::_mcp_base_puts]] > 0}} {{
        catch {{rename puts ""}}
        catch {{rename ::_mcp_base_puts puts}}
    }}
}}
::mcp_hm_restore_puts

proc ::mcp_hm_run_async {{job_id script log_path}} {{
    set ::mcp_async_status($job_id) "running"
    set f [open $log_path a]
    puts $f "MCP_ASYNC_START job=$job_id"
    close $f
    set ::mcp_async_log_path $log_path

    proc ::mcp_hm_async_log {{text}} {{
        if {{![info exists ::mcp_async_log_path] || $::mcp_async_log_path eq ""}} {{return}}
        if {{[catch {{
            set lf [open $::mcp_async_log_path a]
            puts $lf $text
            close $lf
        }}]}} {{
            catch {{close $lf}}
        }}
    }}

    proc ::mcp_hm_async_console_summary {{text}} {{
        if {{[string match "MCP_CONSOLE *" $text] || [string match "MCP_ASYNC_*" $text]}} {{
            ::_mcp_base_puts $text
        }}
    }}

    ::mcp_hm_restore_puts
    rename puts ::_mcp_base_puts
    set old_capture ""
    if {{[info exists ::mcp_capture]}} {{set old_capture $::mcp_capture}}
    set ::mcp_capture ""
    proc puts args {{
        set len [llength $args]
        if {{$len == 1}} {{
            set msg [lindex $args 0]
            ::mcp_hm_async_log $msg
            ::mcp_hm_async_console_summary $msg
        }} elseif {{$len == 2 && ([lindex $args 0] eq "stdout")}} {{
            set msg [lindex $args 1]
            ::mcp_hm_async_log $msg
            ::mcp_hm_async_console_summary $msg
        }} else {{
            eval [linsert $args 0 ::_mcp_base_puts]
        }}
    }}

    set code [catch {{uplevel #0 $script}} result options]

    catch {{rename puts ""}}
    catch {{rename ::_mcp_base_puts puts}}

    set f [open $log_path a]
    if {{$code == 0 || $code == 2}} {{
        set ::mcp_async_status($job_id) "ok"
        puts $f "MCP_ASYNC_OK job=$job_id"
    }} else {{
        set ::mcp_async_status($job_id) "error"
        puts $f "MCP_ASYNC_ERROR job=$job_id result=$result"
        if {{[dict exists $options -errorinfo]}} {{
            puts $f [dict get $options -errorinfo]
        }}
    }}
    if {{$::mcp_capture ne ""}} {{puts $f $::mcp_capture}}
    if {{$result ne ""}} {{puts $f $result}}
    close $f
    set ::mcp_capture $old_capture
    set ::mcp_async_log_path ""
}}

proc ::mcp_hm_accept {{chan addr client_port}} {{
    fconfigure $chan -blocking 1 -translation binary -encoding utf-8
    set script [read $chan]
    if {{[string trim $script] eq ""}} {{
        catch {{puts $chan "ERROR\\nempty Tcl script"}}
        catch {{flush $chan}}
        catch {{close $chan}}
        return
    }}

    ::mcp_hm_restore_puts
    # Capture puts output for the Python client without leaving stale hooks.
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
    
    catch {{rename puts ""}}
    catch {{rename ::_mcp_orig_puts puts}}

    if {{[catch {{
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
    }} reply_err]}} {{
        # The Python side may legitimately time out or disconnect after a long
        # HyperMesh operation. Do not surface socket write/flush failures as GUI
        # Tcl error popups; the workflow logs already carry the real status.
        catch {{puts "MCP_SOCKET_WARN client=$addr:$client_port write_failed=$reply_err"}}
    }}
    catch {{close $chan}}
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


def _run_hypermesh_gui_script_async(
    *,
    script: str,
    job_id: str,
    log_path: str,
    host: str = "127.0.0.1",
    port: int = DEFAULT_GUI_PORT,
    timeout_seconds: int = 10,
) -> dict[str, Any]:
    """Queue a Tcl script inside the GUI listener and return before it executes."""
    wrapper = f"""
set ::mcp_async_job_id "{job_id}"
set ::mcp_async_log_path "{_quote_tcl_path(log_path)}"
set ::mcp_async_script {{{script}}}
set ::mcp_async_status($::mcp_async_job_id) "queued"
set ::mcp_async_log($::mcp_async_job_id) $::mcp_async_log_path
set _mcp_f [open $::mcp_async_log_path w]
puts $_mcp_f "MCP_ASYNC_QUEUED job=$::mcp_async_job_id"
close $_mcp_f
after 1 [list ::mcp_hm_run_async $::mcp_async_job_id $::mcp_async_script $::mcp_async_log_path]
puts "MCP_ASYNC_ACCEPTED job=$::mcp_async_job_id log=$::mcp_async_log_path"
""".lstrip()
    return _run_hypermesh_gui_script(
        script=wrapper,
        host=host,
        port=port,
        timeout_seconds=timeout_seconds,
    )


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

    env = _hmbatch_environment(exe)

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
proc mcp_all_elems {} {
    *createmark elems 1 "all"
    return [hm_getmark elems 1]
}

proc mcp_all_nodes {} {
    *createmark nodes 1 "all"
    return [hm_getmark nodes 1]
}

proc mcp_list_subtract {a b} {
    array set seen {}
    foreach x $b {set seen($x) 1}
    set out {}
    foreach x $a {if {![info exists seen($x)]} {lappend out $x}}
    return $out
}

proc mcp_delete_elems {elems} {
    if {[llength $elems] == 0} {return}
    eval *createmark elems 1 $elems
    catch {*deletemark elems 1}
}

proc mcp_delete_nodes {nodes} {
    if {[llength $nodes] == 0} {return}
    eval *createmark nodes 1 $nodes
    catch {*deletemark nodes 1}
}

proc mcp_shell_loop_info {elems} {
    if {[llength $elems] == 0} {return {0 0 0}}
    array set edge_count {}
    array set edge_nodes {}
    foreach eid $elems {
        if {[catch {hm_getvalue elems id=$eid dataname=nodes} nodes]} {continue}
        set n [llength $nodes]
        if {$n < 3} {continue}
        for {set i 0} {$i < $n} {incr i} {
            set a [lindex $nodes $i]
            set b [lindex $nodes [expr {($i + 1) % $n}]]
            if {$a eq "" || $b eq "" || $a == $b} {continue}
            if {$a < $b} {
                set key "$a,$b"
                set pair [list $a $b]
            } else {
                set key "$b,$a"
                set pair [list $b $a]
            }
            if {![info exists edge_count($key)]} {set edge_count($key) 0}
            incr edge_count($key)
            set edge_nodes($key) $pair
        }
    }
    array set adj {}
    set boundary_edges 0
    foreach key [array names edge_count] {
        if {$edge_count($key) != 1} {continue}
        incr boundary_edges
        set pair $edge_nodes($key)
        set a [lindex $pair 0]
        set b [lindex $pair 1]
        lappend adj($a) $b
        lappend adj($b) $a
    }
    array set seen {}
    set loops 0
    foreach node [array names adj] {
        if {[info exists seen($node)]} {continue}
        incr loops
        set stack [list $node]
        set seen($node) 1
        while {[llength $stack] > 0} {
            set cur [lindex $stack end]
            set stack [lrange $stack 0 end-1]
            foreach nb $adj($cur) {
                if {![info exists seen($nb)]} {
                    set seen($nb) 1
                    lappend stack $nb
                }
            }
        }
    }
    return [list $loops $boundary_edges [array size adj]]
}

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

proc mcp_gear_axis_from_bbox {solid_bb fallback_axis} {
    set dx [expr {abs([lindex $solid_bb 3] - [lindex $solid_bb 0])}]
    set dy [expr {abs([lindex $solid_bb 4] - [lindex $solid_bb 1])}]
    set dz [expr {abs([lindex $solid_bb 5] - [lindex $solid_bb 2])}]
    set dxy [expr {abs($dx - $dy) / max($dx, max($dy, 0.001))}]
    set dxz [expr {abs($dx - $dz) / max($dx, max($dz, 0.001))}]
    set dyz [expr {abs($dy - $dz) / max($dy, max($dz, 0.001))}]
    set best $dxy
    set axis "z"
    if {$dxz < $best} {set best $dxz; set axis "y"}
    if {$dyz < $best} {set best $dyz; set axis "x"}
    if {$best <= 0.18} {return $axis}
    return $fallback_axis
}

proc mcp_angle_span_deg {angles} {
    set n [llength $angles]
    if {$n <= 1} {return 0.0}
    set vals [lsort -real $angles]
    set max_gap 0.0
    for {set i 0} {$i < $n} {incr i} {
        set a [lindex $vals $i]
        if {$i == [expr {$n - 1}]} {
            set b [expr {[lindex $vals 0] + 360.0}]
        } else {
            set b [lindex $vals [expr {$i + 1}]]
        }
        set gap [expr {$b - $a}]
        if {$gap > $max_gap} {set max_gap $gap}
    }
    return [expr {360.0 - $max_gap}]
}

proc mcp_gear_tooth_surfaces {solid_id gear_axis solid_bb} {
    if {$gear_axis eq "x"} {
        set ax_min 0; set ax_max 3; set r1_min 1; set r1_max 4; set r2_min 2; set r2_max 5
    } elseif {$gear_axis eq "y"} {
        set ax_min 1; set ax_max 4; set r1_min 0; set r1_max 3; set r2_min 2; set r2_max 5
    } else {
        set ax_min 2; set ax_max 5; set r1_min 0; set r1_max 3; set r2_min 1; set r2_max 4
    }
    set c1 [expr {([lindex $solid_bb $r1_min] + [lindex $solid_bb $r1_max]) / 2.0}]
    set c2 [expr {([lindex $solid_bb $r2_min] + [lindex $solid_bb $r2_max]) / 2.0}]
    set axis_len [expr {abs([lindex $solid_bb $ax_max] - [lindex $solid_bb $ax_min])}]
    set len1 [expr {abs([lindex $solid_bb $r1_max] - [lindex $solid_bb $r1_min])}]
    set len2 [expr {abs([lindex $solid_bb $r2_max] - [lindex $solid_bb $r2_min])}]
    set max_radial_span [expr {max($len1, $len2)}]
    set min_radial_span [expr {min($len1, $len2)}]
    set radial_roundness [expr {$min_radial_span / max($max_radial_span, 0.001)}]
    set solid_rmax [expr {sqrt($len1*$len1 + $len2*$len2) / 2.0}]
    if {$solid_rmax <= 0.001} {return {}}
    set solid_rnom [expr {$max_radial_span / 2.0}]
    if {$solid_rnom <= 0.001} {set solid_rnom $solid_rmax}

    *createmark surfaces 1 "by solids" $solid_id
    set all_surfs [hm_getmark surfaces 1]
    set high_count_gear [expr {[llength $all_surfs] >= 400}]
    set shaft_end_gear [expr {$axis_len > $max_radial_span * 2.5 && [llength $all_surfs] <= 120}]
    set shaft_inline_gear [expr {$axis_len > $max_radial_span * 2.3 && $radial_roundness >= 0.80 && [llength $all_surfs] >= 80 && [llength $all_surfs] <= 160}]
    set compact_no_source_gear [expr {$axis_len > $max_radial_span * 1.30 && $axis_len <= $max_radial_span * 2.20 && $radial_roundness >= 0.85 && [llength $all_surfs] >= 40 && [llength $all_surfs] <= 90}]
    set small_loop_gear [expr {$axis_len <= $max_radial_span * 0.38 && [llength $all_surfs] >= 70 && [llength $all_surfs] <= 100}]
    set full_thickness_compact_gear [expr {$axis_len <= $max_radial_span * 0.46 && [llength $all_surfs] >= 100 && [llength $all_surfs] <= 300}]
    set compact_thin_gear [expr {$axis_len <= $max_radial_span * 0.35 && [llength $all_surfs] <= 300}]
    set candidates {}
    foreach surf_id $all_surfs {
        *createmark surfaces 2 $surf_id
        if {[catch {hm_getboundingbox surfaces 2 0 0 0} sbb] || [llength $sbb] < 6} {continue}
        set rmin 1.0e30
        set rmax -1.0
        set angles {}
        foreach v1 [list [lindex $sbb $r1_min] [lindex $sbb $r1_max]] {
            foreach v2 [list [lindex $sbb $r2_min] [lindex $sbb $r2_max]] {
                set d1 [expr {$v1 - $c1}]
                set d2 [expr {$v2 - $c2}]
                set rr [expr {sqrt($d1*$d1 + $d2*$d2)}]
                if {$rr < $rmin} {set rmin $rr}
                if {$rr > $rmax} {set rmax $rr}
                set ang [expr {atan2($d2, $d1) * 180.0 / acos(-1)}]
                if {$ang < 0} {set ang [expr {$ang + 360.0}]}
                lappend angles $ang
            }
        }
        set sc1 [expr {([lindex $sbb $r1_min] + [lindex $sbb $r1_max]) / 2.0}]
        set sc2 [expr {([lindex $sbb $r2_min] + [lindex $sbb $r2_max]) / 2.0}]
        set center_radius [expr {sqrt(($sc1-$c1)*($sc1-$c1) + ($sc2-$c2)*($sc2-$c2))}]
        set center_ratio [expr {$center_radius / $solid_rmax}]
        set outer_ratio [expr {$rmax / $solid_rmax}]
        set radial_span [expr {$rmax - $rmin}]
        set radial_span_ratio [expr {$radial_span / $solid_rmax}]
        set center_nom_ratio [expr {$center_radius / $solid_rnom}]
        set inner_nom_ratio [expr {$rmin / $solid_rnom}]
        set outer_nom_ratio [expr {$rmax / $solid_rnom}]
        set radial_span_nom_ratio [expr {$radial_span / $solid_rnom}]
        set surf_axis_span [expr {abs([lindex $sbb $ax_max] - [lindex $sbb $ax_min])}]
        set surf_r1_span [expr {abs([lindex $sbb $r1_max] - [lindex $sbb $r1_min])}]
        set surf_r2_span [expr {abs([lindex $sbb $r2_max] - [lindex $sbb $r2_min])}]
        set axis_ratio [expr {$surf_axis_span / max($axis_len, 0.001)}]
        set local_radial_span [expr {max($surf_r1_span, $surf_r2_span)}]
        set local_span_ratio [expr {$local_radial_span / max($max_radial_span, 0.001)}]
        set angle_span [mcp_angle_span_deg $angles]
        set ax_center [expr {([lindex $sbb $ax_min] + [lindex $sbb $ax_max]) / 2.0}]
        set ax_center_ratio [expr {($ax_center - [lindex $solid_bb $ax_min]) / max($axis_len, 0.001)}]

        # Tooth faces are local repeated flank/top/root faces. The center radius
        # prevents inner holes/slots from being selected just because their bbox
        # corners reach outward.
        if {$small_loop_gear} {
            if {$center_ratio >= 0.60 && $outer_ratio >= 0.68 && $axis_ratio >= 0.55 && $axis_ratio <= 0.75 && $local_span_ratio <= 0.14 && $angle_span <= 18.0} {
                lappend candidates $surf_id
            }
        } elseif {$high_count_gear && $axis_len <= $max_radial_span * 0.42 && $radial_roundness >= 0.85} {
            set high_count_external_tooth [expr {$center_ratio >= 0.66 && $outer_ratio >= 0.70 && $axis_ratio <= 1.05 && $local_span_ratio <= 0.10 && $angle_span <= 16.0 && $radial_span_ratio <= 0.12}]
            set high_count_external_side [expr {$center_ratio >= 0.62 && $outer_ratio >= 0.70 && $axis_ratio <= 1.05 && $local_span_ratio <= 0.06 && $angle_span <= 10.0 && $radial_span_ratio >= 0.006 && $radial_span_ratio <= 0.08}]
            set high_count_radial_flank [expr {$axis_len > $max_radial_span * 0.18 && $center_ratio >= 0.62 && $outer_ratio >= 0.78 && $axis_ratio >= 0.08 && $axis_ratio <= 1.05 && $local_span_ratio <= 0.18 && $angle_span <= 14.0 && $radial_span_ratio >= 0.08 && $radial_span_ratio <= 0.55}]
            set high_count_nominal_outer_tip [expr {$axis_len > $max_radial_span * 0.18 && $center_ratio >= 0.62 && $center_nom_ratio >= 0.78 && $outer_nom_ratio >= 0.92 && $axis_ratio >= 0.06 && $axis_ratio <= 1.05 && $local_span_ratio <= 0.30 && $angle_span <= 34.0 && $radial_span_nom_ratio <= 0.34}]
            set high_count_nominal_outer_flank [expr {$axis_len > $max_radial_span * 0.18 && $center_ratio >= 0.62 && $center_nom_ratio >= 0.64 && $inner_nom_ratio >= 0.62 && $outer_nom_ratio >= 0.90 && $axis_ratio >= 0.06 && $axis_ratio <= 1.05 && $local_span_ratio <= 0.48 && $angle_span <= 82.0 && $radial_span_nom_ratio >= 0.04 && $radial_span_nom_ratio <= 0.98}]
            set high_count_nominal_outer_axial_side [expr {$axis_len > $max_radial_span * 0.18 && $center_nom_ratio >= 0.88 && $outer_nom_ratio >= 0.98 && $axis_ratio >= 0.015 && $axis_ratio <= 0.05 && $local_span_ratio <= 0.12 && $angle_span <= 14.0 && $radial_span_nom_ratio >= 0.10 && $radial_span_nom_ratio <= 0.22}]
            if {$high_count_external_tooth || $high_count_external_side || $high_count_radial_flank || $high_count_nominal_outer_tip || $high_count_nominal_outer_flank || $high_count_nominal_outer_axial_side} {
                lappend candidates $surf_id
            }
        } elseif {$compact_thin_gear || $full_thickness_compact_gear} {
            set compact_full_span [expr {$center_ratio >= 0.62 && $outer_ratio >= 0.66 && $axis_ratio >= 0.80 && $local_span_ratio <= 0.12 && $angle_span <= 10.0 && $radial_span_ratio >= 0.0}]
            set compact_flank_band [expr {$center_ratio >= 0.62 && $outer_ratio >= 0.66 && $axis_ratio >= 0.35 && $axis_ratio <= 0.55 && $local_span_ratio <= 0.08 && $angle_span <= 8.0 && $radial_span_ratio >= 0.0}]
            set compact_full_tooth_band [expr {$full_thickness_compact_gear && $center_ratio >= 0.55 && $outer_ratio >= 0.60 && $axis_ratio >= 0.70 && $local_span_ratio <= 0.22 && $angle_span <= 26.0 && $radial_span_ratio >= 0.0}]
            set compact_thick_tooth_band [expr {$full_thickness_compact_gear && $axis_len > $max_radial_span * 0.36 && [llength $all_surfs] >= 100 && [llength $all_surfs] <= 170 && $center_ratio >= 0.50 && $outer_ratio >= 0.55 && $axis_ratio >= 0.65 && $local_span_ratio <= 0.30 && $angle_span <= 35.0 && $radial_span_ratio >= 0.0}]
            set compact_oblique_tooth_band [expr {$full_thickness_compact_gear && $axis_len > $max_radial_span * 0.36 && [llength $all_surfs] >= 100 && [llength $all_surfs] <= 170 && $radial_roundness >= 0.95 && $outer_ratio >= 0.45 && $axis_ratio >= 0.05 && $local_span_ratio <= 0.95 && $angle_span <= 125.0}]
            set compact_high_count_tooth_band [expr {$high_count_gear && $center_ratio >= 0.55 && $outer_ratio >= 0.58 && $axis_ratio >= 0.12 && $axis_ratio <= 1.05 && $local_span_ratio <= 0.45 && $angle_span <= 70.0 && $radial_span_ratio >= 0.0}]
            if {$compact_full_span || $compact_flank_band || $compact_full_tooth_band || $compact_thick_tooth_band || $compact_oblique_tooth_band || $compact_high_count_tooth_band} {
                lappend candidates $surf_id
            }
        } elseif {$compact_no_source_gear} {
            set compact_no_source_outer_flank [expr {$center_ratio >= 0.58 && $outer_ratio >= 0.63 && $axis_ratio >= 0.50 && $axis_ratio <= 0.75 && $local_span_ratio <= 0.30 && $angle_span <= 35.0 && $radial_span_ratio >= 0.008}]
            set compact_no_source_root_flank [expr {$center_ratio >= 0.49 && $outer_ratio >= 0.52 && $axis_ratio >= 0.50 && $axis_ratio <= 0.75 && $local_span_ratio <= 0.18 && $angle_span <= 22.0 && $radial_span_ratio >= 0.03}]
            set compact_no_source_tip_sliver [expr {$center_ratio >= 0.70 && $outer_ratio >= 0.70 && $axis_ratio >= 0.50 && $axis_ratio <= 0.75 && $local_span_ratio <= 0.05 && $angle_span <= 6.0}]
            if {$compact_no_source_outer_flank || $compact_no_source_root_flank || $compact_no_source_tip_sliver} {
                lappend candidates $surf_id
            }
        } elseif {$shaft_inline_gear} {
            set shaft_inline_flank [expr {$center_ratio >= 0.32 && $outer_ratio >= 0.40 && $axis_ratio >= 0.035 && $axis_ratio <= 0.60 && $local_span_ratio <= 0.34 && $angle_span <= 55.0 && $radial_span_ratio >= 0.008}]
            set shaft_inline_top_root [expr {$center_ratio >= 0.28 && $outer_ratio >= 0.36 && $axis_ratio >= 0.015 && $axis_ratio <= 0.80 && $local_span_ratio <= 0.55 && $angle_span <= 82.0 && $radial_span_ratio >= 0.0}]
            if {$shaft_inline_flank || $shaft_inline_top_root} {
                lappend candidates $surf_id
            }
        } elseif {$shaft_end_gear} {
            if {($ax_center_ratio <= 0.15 || $ax_center_ratio >= 0.85) && $center_ratio >= 0.35 && $outer_ratio >= 0.45 && $axis_ratio >= 0.05 && $axis_ratio <= 0.16 && $local_span_ratio <= 0.16 && $angle_span <= 35.0 && $radial_span_ratio >= 0.03} {
                lappend candidates $surf_id
            }
        } elseif {$high_count_gear} {
            if {$center_ratio >= 0.60 && $outer_ratio >= 0.65 && $axis_ratio <= 0.12 && $local_span_ratio <= 0.14 && $angle_span <= 18.0 && $radial_span_ratio >= 0.04} {
                lappend candidates $surf_id
            }
        } else {
            if {$radial_roundness < 0.70} {
                set ordinary_local [expr {$center_ratio >= 0.54 && $outer_ratio >= 0.55 && $local_span_ratio <= 0.075 && $angle_span <= 8.0 && $radial_span_ratio >= 0.0 && $radial_span_ratio <= 0.080 && $axis_ratio >= 0.08 && $axis_ratio <= 0.25}]
                set ordinary_root_local [expr {$center_ratio >= 0.535 && $outer_ratio >= 0.545 && $local_span_ratio <= 0.025 && $angle_span <= 4.0 && $radial_span_ratio >= 0.0 && $radial_span_ratio <= 0.080 && $axis_ratio >= 0.08 && $axis_ratio <= 0.25}]
                set ordinary_mid_flank 0
            } else {
                set ordinary_local [expr {$center_ratio >= 0.58 && $outer_ratio >= 0.63 && $local_span_ratio <= 0.30 && $angle_span <= 35.0 && $radial_span_ratio >= 0.008 && $axis_ratio <= 0.65}]
                set ordinary_mid_flank [expr {$center_ratio >= 0.49 && $outer_ratio >= 0.52 && $local_span_ratio <= 0.30 && $angle_span <= 40.0 && $axis_ratio <= 0.50}]
                set ordinary_root_local 0
            }
            set ordinary_end_sliver [expr {$center_ratio >= 0.48 && $outer_ratio >= 0.52 && $radial_span_ratio >= 0.025 && $local_span_ratio <= 0.08 && $angle_span <= 12.5 && $axis_ratio <= 0.03}]
            if {$ordinary_local || $ordinary_root_local || $ordinary_mid_flank || $ordinary_end_sliver} {
                lappend candidates $surf_id
            }
        }
    }
    return $candidates
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
    set gear_axis [mcp_gear_axis_from_bbox $bb $drag_axis]
    
    set sbb_min [list [lindex $bb 0] [lindex $bb 1] [lindex $bb 2]]
    set sbb_max [list [lindex $bb 3] [lindex $bb 4] [lindex $bb 5]]
    set src_surf [find_best_source_surface $sid $drag_axis $sbb_min $sbb_max]
    set src_loop_count 0
    set src_inner_loop_count 0
    set src_boundary_edge_count 0
    set src_boundary_node_count 0
    if {$src_surf > 0 && $diag > 0.001} {
        set before_elems [mcp_all_elems]
        set before_nodes [mcp_all_nodes]
        set source_probe_size [expr {max(min($diag / 12.0, 5.0), 0.5)}]
        set source_probe_min [expr {max($source_probe_size / 4.0, 0.2)}]
        set source_probe_max [expr {$source_probe_size * 2.0}]
        *createmark surfaces 1 $src_surf
        *createarray 3 0 0 0
        catch {*defaultmeshsurf_growth 1 $source_probe_size 3 3 2 1 1 1 35 0 $source_probe_min $source_probe_max 0.2 20.0 1.3 1 3 1 0}
        set source_elems [mcp_list_subtract [mcp_all_elems] $before_elems]
        set source_nodes [mcp_list_subtract [mcp_all_nodes] $before_nodes]
        if {[llength $source_elems] > 0} {
            set loop_info [mcp_shell_loop_info $source_elems]
            set src_loop_count [lindex $loop_info 0]
            set src_boundary_edge_count [lindex $loop_info 1]
            set src_boundary_node_count [lindex $loop_info 2]
            if {$src_loop_count > 1} {set src_inner_loop_count [expr {$src_loop_count - 1}]}
        }
        mcp_delete_elems $source_elems
        mcp_delete_nodes $source_nodes
    }
    set gear_tooth_surfs [mcp_gear_tooth_surfaces $sid $gear_axis $bb]
    set gear_tooth_csv [join $gear_tooth_surfs ","]
    
    puts $f "PROBE: solid=$sid comp=\"$comp_name\" sc=$sc x0=[format %.6f [lindex $bb 0]] y0=[format %.6f [lindex $bb 1]] z0=[format %.6f [lindex $bb 2]] x1=[format %.6f [lindex $bb 3]] y1=[format %.6f [lindex $bb 4]] z1=[format %.6f [lindex $bb 5]] dx=[format %.3f $dx] dy=[format %.3f $dy] dz=[format %.3f $dz] mn=[format %.3f $mn] mx=[format %.3f $mx] md=[format %.3f $md] slender=[format %.2f $slender] diag=[format %.3f $diag] src_surf=$src_surf drag_axis=$drag_axis gear_axis=$gear_axis gear_tooth_surfs=$gear_tooth_csv gear_tooth_count=[llength $gear_tooth_surfs] src_loops=$src_loop_count src_inner_loops=$src_inner_loop_count src_boundary_edges=$src_boundary_edge_count src_boundary_nodes=$src_boundary_node_count"
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
def classify_all_solids_from_probe(
    probe_lines,
    visual_observations=None,
    use_gear_tooth_refinement: bool = True,
):
    """MANDATORY Phase 2: classify every probed solid from probe_lines."""
    global _GLOBAL_CLASSIFICATION_RESULTS, _GLOBAL_PHASE2_FINALIZED
    results = {}
    for line in _probe_lines_iter(probe_lines):
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
        drag_axis = str(f.get("drag_axis", "") or "").lower()
        if drag_axis == "x":
            cross_a, cross_b = dy, dz
        elif drag_axis == "y":
            cross_a, cross_b = dx, dz
        else:
            cross_a, cross_b = dx, dy
        cross_ratio = min(cross_a, cross_b) / max(cross_a, cross_b, 0.001)
        is_axisymmetric_bbox = cross_ratio >= 0.85
        strategy = "tetra_plain"
        evidence = []
        elem_size = 1.0
        min_elem_size = 0.5
        src_surf = f.get("source_surface_id", -1)
        source_inner_loops = int(f.get("source_inner_loop_count", 0) or 0)
        gear_reason = _gear_reason_from_probe(f) if use_gear_tooth_refinement else None
        gear_tooth_surface_ids = (
            _probe_int_list_value(f.get("gear_tooth_surfs") or f.get("gear_tooth_surface_ids"))
            if use_gear_tooth_refinement
            else []
        )
        gear_tooth_count = int(f.get("gear_tooth_count", len(gear_tooth_surface_ids)) or 0) if use_gear_tooth_refinement else 0
        
        if gear_reason:
            strategy = "gear_aware_tetra"
            evidence.append(gear_reason)
            if gear_tooth_surface_ids:
                evidence.append(f"gear tooth/outer-band surface candidates: {gear_tooth_count}")
        elif sc == 6 and mx > 0 and mn / mx <= 0.75 and src_surf > 0 and source_inner_loops <= 1:
            strategy = "drag_hex"
            evidence.append("6-face constant-section solid/ring, including short thick rings -> drag")
        elif sc == 4 and mx > 0 and src_surf > 0 and cross_ratio >= 0.75 and mn / mx <= 0.60 and source_inner_loops <= 1:
            strategy = "drag_hex"
            evidence.append("4-surface short cylinder -> drag")
        elif is_axisymmetric_bbox and src_surf > 0 and source_inner_loops == 1 and mn / mx <= 0.55 and 6 <= sc <= 28:
            strategy = "spin_hex"
            evidence.append("axisymmetric recessed/hollow body -> cut-section spin")
        elif is_axisymmetric_bbox and src_surf > 0 and source_inner_loops > 1 and mn / mx <= 0.55 and 6 <= sc <= 28:
            evidence.append("multi-hole thin ring/source section -> tetra")
        elif is_axisymmetric_bbox and src_surf > 0 and mn / mx <= 0.55 and 6 <= sc <= 28:
            evidence.append("axisymmetric but source section has no inner loop -> tetra")
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
        if strategy in {"tetra_plain", "gear_aware_tetra"}:
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
        spin_plan = _spin_params_from_probe(f) if strategy == "spin_hex" else {}
        results[str(sid)] = {
            "solid_id": sid, "strategy": strategy, "component_name": name,
            "raw_probe_line": stripped,
            "element_size": round(elem_size, 2),
            "min_element_size": round(min_elem_size, 3),
            "max_shell_elements_before_tetmesh": max_shell_before_tetmesh,
            "allow_tetmesh": allow_tetmesh,
            "allow_surface_mesh": allow_surface_mesh,
            "evidence": evidence,
            "source_surface_id": f.get("source_surface_id", -1),
            "source_loop_count": int(f.get("source_loop_count", 0) or 0),
            "source_inner_loop_count": source_inner_loops,
            "source_boundary_edge_count": int(f.get("source_boundary_edge_count", 0) or 0),
            "source_boundary_node_count": int(f.get("source_boundary_node_count", 0) or 0),
            "drag_axis": f.get("drag_axis", ""),
            "gear_axis": f.get("gear_axis", f.get("drag_axis", "")),
            "geometry_confirms_gear_teeth": strategy == "gear_aware_tetra",
            "gear_detection_enabled": bool(use_gear_tooth_refinement),
            "gear_probe_reason": gear_reason or "",
            "gear_probe_tooth_surface_ids": gear_tooth_surface_ids,
            "gear_probe_tooth_surface_count": gear_tooth_count,
            "gear_tooth_surface_ids": gear_tooth_surface_ids if strategy == "gear_aware_tetra" else [],
            "gear_tooth_surface_count": gear_tooth_count if strategy == "gear_aware_tetra" else 0,
            "surf_count": sc,
            "dims": {"dx": dx, "dy": dy, "dz": dz, "mn": mn, "md": md, "mx": mx},
            **spin_plan,
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
            "During Phase 3, use phase3_tetra_batches as model-agnostic batching "
            "guidance: preserve solid-id order, but isolate high-risk tetra solids "
            "so they are not run back-to-back inside one heavy command sequence."
        ),
        "completion_token": "MCP_FINALIZE_OK",
    }


@mcp.tool()
def suggest_component_names(probe_lines):
    """Generate geometry-based component names from probe_lines."""
    names = {}
    for line in _probe_lines_iter(probe_lines):
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
    # renamed component, cycling through HyperMesh color ids while skipping
    # black so black mesh edges remain visible.
    color_idx = 0
    color_ids = FINALIZE_UNIQUE_COLOR_IDS or [2]
    for sid_str, info in target_results.items():
        name = info.get("component_name", "")
        if name:
            color = color_ids[color_idx % len(color_ids)]
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
            "Classify by geometry: holes/flanges/bosses/cutouts -> tetra; simple constant extrusions with matched quad source face -> drag; revolved bodies -> cut-section spin.",
            "For revolved solids, use the generic cut-section spin workflow rather than guessed surface-id spin.",
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
        "proc mcp_shell_loop_info {elems} {",
        "    if {[llength $elems] == 0} {return {0 0 0}}",
        "    array set edge_count {}",
        "    array set edge_nodes {}",
        "    foreach eid $elems {",
        "        if {[catch {hm_getvalue elems id=$eid dataname=nodes} nodes]} {continue}",
        "        set n [llength $nodes]",
        "        if {$n < 3} {continue}",
        "        for {set i 0} {$i < $n} {incr i} {",
        "            set a [lindex $nodes $i]",
        "            set b [lindex $nodes [expr {($i + 1) % $n}]]",
        "            if {$a eq \"\" || $b eq \"\" || $a == $b} {continue}",
        "            if {$a < $b} {",
        "                set key \"$a,$b\"",
        "                set pair [list $a $b]",
        "            } else {",
        "                set key \"$b,$a\"",
        "                set pair [list $b $a]",
        "            }",
        "            if {![info exists edge_count($key)]} {set edge_count($key) 0}",
        "            incr edge_count($key)",
        "            set edge_nodes($key) $pair",
        "        }",
        "    }",
        "    array set adj {}",
        "    set boundary_edges 0",
        "    foreach key [array names edge_count] {",
        "        if {$edge_count($key) != 1} {continue}",
        "        incr boundary_edges",
        "        set pair $edge_nodes($key)",
        "        set a [lindex $pair 0]",
        "        set b [lindex $pair 1]",
        "        lappend adj($a) $b",
        "        lappend adj($b) $a",
        "    }",
        "    array set seen {}",
        "    set loops 0",
        "    foreach node [array names adj] {",
        "        if {[info exists seen($node)]} {continue}",
        "        incr loops",
        "        set stack [list $node]",
        "        set seen($node) 1",
        "        while {[llength $stack] > 0} {",
        "            set cur [lindex $stack end]",
        "            set stack [lrange $stack 0 end-1]",
        "            foreach nb $adj($cur) {",
        "                if {![info exists seen($nb)]} {",
        "                    set seen($nb) 1",
        "                    lappend stack $nb",
        "                }",
        "            }",
        "        }",
        "    }",
        "    return [list $loops $boundary_edges [array size adj]]",
        "}",
        "proc mcp_surface_loop_info {surface_id} {",
        "    if {$surface_id <= 0} {return {0 0 0}}",
        "    *createmark elems 1 \"by surface\" $surface_id",
        "    return [mcp_shell_loop_info [hm_getmark elems 1]]",
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
        "proc mcp_gear_axis_from_bbox {solid_bb fallback_axis} {",
        "    set dx [expr {abs([lindex $solid_bb 3] - [lindex $solid_bb 0])}]",
        "    set dy [expr {abs([lindex $solid_bb 4] - [lindex $solid_bb 1])}]",
        "    set dz [expr {abs([lindex $solid_bb 5] - [lindex $solid_bb 2])}]",
        "    set dxy [expr {abs($dx - $dy) / max($dx, max($dy, 0.001))}]",
        "    set dxz [expr {abs($dx - $dz) / max($dx, max($dz, 0.001))}]",
        "    set dyz [expr {abs($dy - $dz) / max($dy, max($dz, 0.001))}]",
        "    set best $dxy",
        "    set axis \"z\"",
        "    if {$dxz < $best} {set best $dxz; set axis \"y\"}",
        "    if {$dyz < $best} {set best $dyz; set axis \"x\"}",
        "    if {$best <= 0.18} {return $axis}",
        "    return $fallback_axis",
        "}",
        "proc mcp_angle_span_deg {angles} {",
        "    set n [llength $angles]",
        "    if {$n <= 1} {return 0.0}",
        "    set vals [lsort -real $angles]",
        "    set max_gap 0.0",
        "    for {set i 0} {$i < $n} {incr i} {",
        "        set a [lindex $vals $i]",
        "        if {$i == [expr {$n - 1}]} {",
        "            set b [expr {[lindex $vals 0] + 360.0}]",
        "        } else {",
        "            set b [lindex $vals [expr {$i + 1}]]",
        "        }",
        "        set gap [expr {$b - $a}]",
        "        if {$gap > $max_gap} {set max_gap $gap}",
        "    }",
        "    return [expr {360.0 - $max_gap}]",
        "}",
        "proc mcp_gear_tooth_surfaces {solid_id gear_axis solid_bb} {",
        "    if {$gear_axis eq \"x\"} {",
        "        set ax_min 0; set ax_max 3; set r1_min 1; set r1_max 4; set r2_min 2; set r2_max 5",
        "    } elseif {$gear_axis eq \"y\"} {",
        "        set ax_min 1; set ax_max 4; set r1_min 0; set r1_max 3; set r2_min 2; set r2_max 5",
        "    } else {",
        "        set ax_min 2; set ax_max 5; set r1_min 0; set r1_max 3; set r2_min 1; set r2_max 4",
        "    }",
        "    set c1 [expr {([lindex $solid_bb $r1_min] + [lindex $solid_bb $r1_max]) / 2.0}]",
        "    set c2 [expr {([lindex $solid_bb $r2_min] + [lindex $solid_bb $r2_max]) / 2.0}]",
        "    set axis_len [expr {abs([lindex $solid_bb $ax_max] - [lindex $solid_bb $ax_min])}]",
        "    set len1 [expr {abs([lindex $solid_bb $r1_max] - [lindex $solid_bb $r1_min])}]",
        "    set len2 [expr {abs([lindex $solid_bb $r2_max] - [lindex $solid_bb $r2_min])}]",
        "    set max_radial_span [expr {max($len1, $len2)}]",
        "    set min_radial_span [expr {min($len1, $len2)}]",
        "    set radial_roundness [expr {$min_radial_span / max($max_radial_span, 0.001)}]",
        "    set solid_rmax [expr {sqrt($len1*$len1 + $len2*$len2) / 2.0}]",
        "    if {$solid_rmax <= 0.001} {return {}}",
        "    set solid_rnom [expr {$max_radial_span / 2.0}]",
        "    if {$solid_rnom <= 0.001} {set solid_rnom $solid_rmax}",
        "    *createmark surfaces 1 \"by solids\" $solid_id",
        "    set all_surfs [hm_getmark surfaces 1]",
        "    set high_count_gear [expr {[llength $all_surfs] >= 400}]",
        "    set shaft_end_gear [expr {$axis_len > $max_radial_span * 2.5 && [llength $all_surfs] <= 120}]",
        "    set shaft_inline_gear [expr {$axis_len > $max_radial_span * 2.3 && $radial_roundness >= 0.80 && [llength $all_surfs] >= 80 && [llength $all_surfs] <= 160}]",
        "    set compact_no_source_gear [expr {$axis_len > $max_radial_span * 1.30 && $axis_len <= $max_radial_span * 2.20 && $radial_roundness >= 0.85 && [llength $all_surfs] >= 40 && [llength $all_surfs] <= 90}]",
        "    set small_loop_gear [expr {$axis_len <= $max_radial_span * 0.38 && [llength $all_surfs] >= 70 && [llength $all_surfs] <= 100}]",
        "    set full_thickness_compact_gear [expr {$axis_len <= $max_radial_span * 0.46 && [llength $all_surfs] >= 100 && [llength $all_surfs] <= 300}]",
        "    set compact_thin_gear [expr {$axis_len <= $max_radial_span * 0.35 && [llength $all_surfs] <= 300}]",
        "    set candidates {}",
        "    foreach surf_id $all_surfs {",
        "        *createmark surfaces 2 $surf_id",
        "        if {[catch {hm_getboundingbox surfaces 2 0 0 0} sbb] || [llength $sbb] < 6} {continue}",
        "        set rmin 1.0e30",
        "        set rmax -1.0",
        "        set angles {}",
        "        foreach v1 [list [lindex $sbb $r1_min] [lindex $sbb $r1_max]] {",
        "            foreach v2 [list [lindex $sbb $r2_min] [lindex $sbb $r2_max]] {",
        "                set d1 [expr {$v1 - $c1}]",
        "                set d2 [expr {$v2 - $c2}]",
        "                set rr [expr {sqrt($d1*$d1 + $d2*$d2)}]",
        "                if {$rr < $rmin} {set rmin $rr}",
        "                if {$rr > $rmax} {set rmax $rr}",
        "                set ang [expr {atan2($d2, $d1) * 180.0 / acos(-1)}]",
        "                if {$ang < 0} {set ang [expr {$ang + 360.0}]}",
        "                lappend angles $ang",
        "            }",
        "        }",
        "        set sc1 [expr {([lindex $sbb $r1_min] + [lindex $sbb $r1_max]) / 2.0}]",
        "        set sc2 [expr {([lindex $sbb $r2_min] + [lindex $sbb $r2_max]) / 2.0}]",
        "        set center_radius [expr {sqrt(($sc1-$c1)*($sc1-$c1) + ($sc2-$c2)*($sc2-$c2))}]",
        "        set center_ratio [expr {$center_radius / $solid_rmax}]",
        "        set outer_ratio [expr {$rmax / $solid_rmax}]",
        "        set radial_span [expr {$rmax - $rmin}]",
        "        set radial_span_ratio [expr {$radial_span / $solid_rmax}]",
        "        set center_nom_ratio [expr {$center_radius / $solid_rnom}]",
        "        set inner_nom_ratio [expr {$rmin / $solid_rnom}]",
        "        set outer_nom_ratio [expr {$rmax / $solid_rnom}]",
        "        set radial_span_nom_ratio [expr {$radial_span / $solid_rnom}]",
        "        set surf_axis_span [expr {abs([lindex $sbb $ax_max] - [lindex $sbb $ax_min])}]",
        "        set surf_r1_span [expr {abs([lindex $sbb $r1_max] - [lindex $sbb $r1_min])}]",
        "        set surf_r2_span [expr {abs([lindex $sbb $r2_max] - [lindex $sbb $r2_min])}]",
        "        set axis_ratio [expr {$surf_axis_span / max($axis_len, 0.001)}]",
        "        set local_radial_span [expr {max($surf_r1_span, $surf_r2_span)}]",
        "        set local_span_ratio [expr {$local_radial_span / max($max_radial_span, 0.001)}]",
        "        set angle_span [mcp_angle_span_deg $angles]",
        "        set ax_center [expr {([lindex $sbb $ax_min] + [lindex $sbb $ax_max]) / 2.0}]",
        "        set ax_center_ratio [expr {($ax_center - [lindex $solid_bb $ax_min]) / max($axis_len, 0.001)}]",
        "        if {$small_loop_gear} {",
        "            if {$center_ratio >= 0.60 && $outer_ratio >= 0.68 && $axis_ratio >= 0.55 && $axis_ratio <= 0.75 && $local_span_ratio <= 0.14 && $angle_span <= 18.0} {",
        "                lappend candidates $surf_id",
        "            }",
        "        } elseif {$high_count_gear && $axis_len <= $max_radial_span * 0.42 && $radial_roundness >= 0.85} {",
        "            set high_count_external_tooth [expr {$center_ratio >= 0.66 && $outer_ratio >= 0.70 && $axis_ratio <= 1.05 && $local_span_ratio <= 0.10 && $angle_span <= 16.0 && $radial_span_ratio <= 0.12}]",
        "            set high_count_external_side [expr {$center_ratio >= 0.62 && $outer_ratio >= 0.70 && $axis_ratio <= 1.05 && $local_span_ratio <= 0.06 && $angle_span <= 10.0 && $radial_span_ratio >= 0.006 && $radial_span_ratio <= 0.08}]",
        "            set high_count_radial_flank [expr {$axis_len > $max_radial_span * 0.18 && $center_ratio >= 0.62 && $outer_ratio >= 0.78 && $axis_ratio >= 0.08 && $axis_ratio <= 1.05 && $local_span_ratio <= 0.18 && $angle_span <= 14.0 && $radial_span_ratio >= 0.08 && $radial_span_ratio <= 0.55}]",
        "            set high_count_nominal_outer_tip [expr {$axis_len > $max_radial_span * 0.18 && $center_ratio >= 0.62 && $center_nom_ratio >= 0.78 && $outer_nom_ratio >= 0.92 && $axis_ratio >= 0.06 && $axis_ratio <= 1.05 && $local_span_ratio <= 0.30 && $angle_span <= 34.0 && $radial_span_nom_ratio <= 0.34}]",
        "            set high_count_nominal_outer_flank [expr {$axis_len > $max_radial_span * 0.18 && $center_ratio >= 0.62 && $center_nom_ratio >= 0.64 && $inner_nom_ratio >= 0.62 && $outer_nom_ratio >= 0.90 && $axis_ratio >= 0.06 && $axis_ratio <= 1.05 && $local_span_ratio <= 0.48 && $angle_span <= 82.0 && $radial_span_nom_ratio >= 0.04 && $radial_span_nom_ratio <= 0.98}]",
        "            set high_count_nominal_outer_axial_side [expr {$axis_len > $max_radial_span * 0.18 && $center_nom_ratio >= 0.88 && $outer_nom_ratio >= 0.98 && $axis_ratio >= 0.015 && $axis_ratio <= 0.05 && $local_span_ratio <= 0.12 && $angle_span <= 14.0 && $radial_span_nom_ratio >= 0.10 && $radial_span_nom_ratio <= 0.22}]",
        "            if {$high_count_external_tooth || $high_count_external_side || $high_count_radial_flank || $high_count_nominal_outer_tip || $high_count_nominal_outer_flank || $high_count_nominal_outer_axial_side} {",
        "                lappend candidates $surf_id",
        "            }",
        "        } elseif {$compact_thin_gear || $full_thickness_compact_gear} {",
        "            set compact_full_span [expr {$center_ratio >= 0.62 && $outer_ratio >= 0.66 && $axis_ratio >= 0.80 && $local_span_ratio <= 0.12 && $angle_span <= 10.0 && $radial_span_ratio >= 0.0}]",
        "            set compact_flank_band [expr {$center_ratio >= 0.62 && $outer_ratio >= 0.66 && $axis_ratio >= 0.35 && $axis_ratio <= 0.55 && $local_span_ratio <= 0.08 && $angle_span <= 8.0 && $radial_span_ratio >= 0.0}]",
        "            set compact_full_tooth_band [expr {$full_thickness_compact_gear && $center_ratio >= 0.55 && $outer_ratio >= 0.60 && $axis_ratio >= 0.70 && $local_span_ratio <= 0.22 && $angle_span <= 26.0 && $radial_span_ratio >= 0.0}]",
        "            set compact_thick_tooth_band [expr {$full_thickness_compact_gear && $axis_len > $max_radial_span * 0.36 && [llength $all_surfs] >= 100 && [llength $all_surfs] <= 170 && $center_ratio >= 0.50 && $outer_ratio >= 0.55 && $axis_ratio >= 0.65 && $local_span_ratio <= 0.30 && $angle_span <= 35.0 && $radial_span_ratio >= 0.0}]",
        "            set compact_oblique_tooth_band [expr {$full_thickness_compact_gear && $axis_len > $max_radial_span * 0.36 && [llength $all_surfs] >= 100 && [llength $all_surfs] <= 170 && $radial_roundness >= 0.95 && $outer_ratio >= 0.45 && $axis_ratio >= 0.05 && $local_span_ratio <= 0.95 && $angle_span <= 125.0}]",
        "            set compact_high_count_tooth_band [expr {$high_count_gear && $center_ratio >= 0.55 && $outer_ratio >= 0.58 && $axis_ratio >= 0.12 && $axis_ratio <= 1.05 && $local_span_ratio <= 0.45 && $angle_span <= 70.0 && $radial_span_ratio >= 0.0}]",
        "            if {$compact_full_span || $compact_flank_band || $compact_full_tooth_band || $compact_thick_tooth_band || $compact_oblique_tooth_band || $compact_high_count_tooth_band} {",
        "                lappend candidates $surf_id",
        "            }",
        "        } elseif {$compact_no_source_gear} {",
        "            set compact_no_source_outer_flank [expr {$center_ratio >= 0.58 && $outer_ratio >= 0.63 && $axis_ratio >= 0.50 && $axis_ratio <= 0.75 && $local_span_ratio <= 0.30 && $angle_span <= 35.0 && $radial_span_ratio >= 0.008}]",
        "            set compact_no_source_root_flank [expr {$center_ratio >= 0.49 && $outer_ratio >= 0.52 && $axis_ratio >= 0.50 && $axis_ratio <= 0.75 && $local_span_ratio <= 0.18 && $angle_span <= 22.0 && $radial_span_ratio >= 0.03}]",
        "            set compact_no_source_tip_sliver [expr {$center_ratio >= 0.70 && $outer_ratio >= 0.70 && $axis_ratio >= 0.50 && $axis_ratio <= 0.75 && $local_span_ratio <= 0.05 && $angle_span <= 6.0}]",
        "            if {$compact_no_source_outer_flank || $compact_no_source_root_flank || $compact_no_source_tip_sliver} {",
        "                lappend candidates $surf_id",
        "            }",
        "        } elseif {$shaft_inline_gear} {",
        "            set shaft_inline_flank [expr {$center_ratio >= 0.32 && $outer_ratio >= 0.40 && $axis_ratio >= 0.035 && $axis_ratio <= 0.60 && $local_span_ratio <= 0.34 && $angle_span <= 55.0 && $radial_span_ratio >= 0.008}]",
        "            set shaft_inline_top_root [expr {$center_ratio >= 0.28 && $outer_ratio >= 0.36 && $axis_ratio >= 0.015 && $axis_ratio <= 0.80 && $local_span_ratio <= 0.55 && $angle_span <= 82.0 && $radial_span_ratio >= 0.0}]",
        "            if {$shaft_inline_flank || $shaft_inline_top_root} {",
        "                lappend candidates $surf_id",
        "            }",
        "        } elseif {$shaft_end_gear} {",
        "            if {($ax_center_ratio <= 0.15 || $ax_center_ratio >= 0.85) && $center_ratio >= 0.35 && $outer_ratio >= 0.45 && $axis_ratio >= 0.05 && $axis_ratio <= 0.16 && $local_span_ratio <= 0.16 && $angle_span <= 35.0 && $radial_span_ratio >= 0.03} {",
        "                lappend candidates $surf_id",
        "            }",
        "        } elseif {$high_count_gear} {",
        "            if {$center_ratio >= 0.60 && $outer_ratio >= 0.65 && $axis_ratio <= 0.12 && $local_span_ratio <= 0.14 && $angle_span <= 18.0 && $radial_span_ratio >= 0.04} {",
        "                lappend candidates $surf_id",
        "            }",
        "        } else {",
        "            if {$radial_roundness < 0.70} {",
        "                set ordinary_local [expr {$center_ratio >= 0.54 && $outer_ratio >= 0.55 && $local_span_ratio <= 0.075 && $angle_span <= 8.0 && $radial_span_ratio >= 0.0 && $radial_span_ratio <= 0.080 && $axis_ratio >= 0.08 && $axis_ratio <= 0.25}]",
        "                set ordinary_root_local [expr {$center_ratio >= 0.535 && $outer_ratio >= 0.545 && $local_span_ratio <= 0.025 && $angle_span <= 4.0 && $radial_span_ratio >= 0.0 && $radial_span_ratio <= 0.080 && $axis_ratio >= 0.08 && $axis_ratio <= 0.25}]",
        "                set ordinary_mid_flank 0",
        "            } else {",
        "                set ordinary_local [expr {$center_ratio >= 0.58 && $outer_ratio >= 0.63 && $local_span_ratio <= 0.30 && $angle_span <= 35.0 && $radial_span_ratio >= 0.008 && $axis_ratio <= 0.65}]",
        "                set ordinary_mid_flank [expr {$center_ratio >= 0.49 && $outer_ratio >= 0.52 && $local_span_ratio <= 0.30 && $angle_span <= 40.0 && $axis_ratio <= 0.50}]",
        "                set ordinary_root_local 0",
        "            }",
        "            set ordinary_end_sliver [expr {$center_ratio >= 0.48 && $outer_ratio >= 0.52 && $radial_span_ratio >= 0.025 && $local_span_ratio <= 0.08 && $angle_span <= 12.5 && $axis_ratio <= 0.03}]",
        "            if {$ordinary_local || $ordinary_root_local || $ordinary_mid_flank || $ordinary_end_sliver} {",
        "                lappend candidates $surf_id",
        "            }",
        "        }",
        "    }",
        "    return $candidates",
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
        "    set gear_axis \"\"",
        "    set src_surf -1",
        "    set src_loop_count 0",
        "    set src_inner_loop_count 0",
        "    set src_boundary_edge_count 0",
        "    set src_boundary_node_count 0",
        "    if {$bbox_ok} {",
        "        if {$dx <= $dy && $dx <= $dz} {set drag_axis \"x\"} elseif {$dy <= $dx && $dy <= $dz} {set drag_axis \"y\"} else {set drag_axis \"z\"}",
        "        set gear_axis [mcp_gear_axis_from_bbox $bb $drag_axis]",
        "        set src_surf [mcp_best_source_surface $sid $drag_axis $bb]",
        "        set loop_info [mcp_surface_loop_info $src_surf]",
        "        set src_loop_count [lindex $loop_info 0]",
        "        set src_boundary_edge_count [lindex $loop_info 1]",
        "        set src_boundary_node_count [lindex $loop_info 2]",
        "        if {$src_loop_count > 1} {set src_inner_loop_count [expr {$src_loop_count - 1}]}",
        "    }",
        "    set gear_tooth_surfs [mcp_gear_tooth_surfaces $sid $gear_axis $bb]",
        "    set gear_tooth_csv [join $gear_tooth_surfs \",\"]",
        '    mcp_probe_line "MCP_PROBE_SOLID id=$sid exists=1 surf_count=$surf_count elem_count=[llength $new_elems] node_count=[llength $new_nodes] tri_count=$tri_count quad_count=$quad_count bbox_ok=$bbox_ok x0=[lindex $bb 0] y0=[lindex $bb 1] z0=[lindex $bb 2] x1=[lindex $bb 3] y1=[lindex $bb 4] z1=[lindex $bb 5] dx=$dx dy=$dy dz=$dz diag=$diag slender=$slender src_surf=$src_surf drag_axis=$drag_axis gear_axis=$gear_axis gear_tooth_surfs=$gear_tooth_csv gear_tooth_count=[llength $gear_tooth_surfs] src_loops=$src_loop_count src_inner_loops=$src_inner_loop_count src_boundary_edges=$src_boundary_edge_count src_boundary_nodes=$src_boundary_node_count"',
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

    env = _hmbatch_environment(exe)
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
            env=_hmbatch_environment(exe),
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
    min_element_size: float = 0.6,
    max_deviation: float = 0.1,
    feature_angle: float = 30,
    growth_rate: float = 1.23,
    max_shell_elements_before_tetmesh: int = TETRA_MAX_SHELL_ELEMENTS,
    allow_tetmesh: bool = True,
    allow_surface_mesh: bool = True,
    fit_tolerance_ratio: float = 0.01,
    target_vol_skew: float = 0.70,
    repair_vol_skew: float = 0.99,
    chord_dev_degrade_delta: float = 0.20,
    element_size_min: float = 1.5,
    element_size_max: float = 2.0,
    min_element_size_min: float = 0.4,
    min_element_size_max: float = 0.6,
    delete_existing_component_elements: bool = True,
    use_gear_tooth_refinement: bool = True,
    gear_tooth_surface_ids: list[int] | None = None,
    gear_tooth_element_size: float | None = 1.6,
    gear_tooth_element_size_min: float | None = 1.2,
    gear_tooth_element_size_max: float | None = 1.6,
    gear_tooth_min_element_size: float | None = 0.3,
    gear_tooth_min_element_size_min: float | None = 0.2,
    gear_tooth_min_element_size_max: float | None = 0.3,
    gear_tooth_feature_angle: float | None = 15.0,
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
    if chord_dev_degrade_delta < 0:
        raise ValueError("chord_dev_degrade_delta must be >= 0.")
    if not (0 < target_vol_skew <= 1):
        raise ValueError("target_vol_skew must be in (0, 1].")
    if not (0 < repair_vol_skew <= 1):
        raise ValueError("repair_vol_skew must be in (0, 1].")
    if element_size_min <= 0 or element_size_max <= 0 or element_size_min > element_size_max:
        raise ValueError("element_size_min/max must be positive and min <= max.")
    if min_element_size_min <= 0 or min_element_size_max <= 0 or min_element_size_min > min_element_size_max:
        raise ValueError("min_element_size_min/max must be positive and min <= max.")
    if not component_name.strip():
        raise ValueError("component_name cannot be empty.")
    comp = _tcl_escape_name(component_name)
    clamped_min = max(float(min_element_size_min), min(float(min_element_size), float(min_element_size_max)))
    clamped_size = max(float(element_size_min), min(float(element_size), float(element_size_max)))
    raw_gear_tooth_ids = gear_tooth_surface_ids or []
    safe_gear_tooth_ids: list[int] = []
    for value in raw_gear_tooth_ids:
        try:
            sid_value = int(value)
        except (TypeError, ValueError):
            continue
        if sid_value > 0 and sid_value not in safe_gear_tooth_ids:
            safe_gear_tooth_ids.append(sid_value)
    default_tooth_size = clamped_size * GEAR_TOOTH_DEFAULT_SIZE_SCALE
    default_tooth_min_size = clamped_min * GEAR_TOOTH_DEFAULT_SIZE_SCALE
    tooth_size_min = float(gear_tooth_element_size_min) if gear_tooth_element_size_min is not None else default_tooth_size
    tooth_size_max = float(gear_tooth_element_size_max) if gear_tooth_element_size_max is not None else tooth_size_min
    tooth_min_size_min = float(gear_tooth_min_element_size_min) if gear_tooth_min_element_size_min is not None else default_tooth_min_size
    tooth_min_size_max = float(gear_tooth_min_element_size_max) if gear_tooth_min_element_size_max is not None else tooth_min_size_min
    if tooth_size_min <= 0 or tooth_size_max <= 0 or tooth_size_min > tooth_size_max:
        raise ValueError("gear_tooth_element_size_min/max must be positive and min <= max.")
    if tooth_min_size_min <= 0 or tooth_min_size_max <= 0 or tooth_min_size_min > tooth_min_size_max:
        raise ValueError("gear_tooth_min_element_size_min/max must be positive and min <= max.")
    requested_tooth_size = float(gear_tooth_element_size) if gear_tooth_element_size is not None else tooth_size_max
    requested_tooth_min_size = float(gear_tooth_min_element_size) if gear_tooth_min_element_size is not None else tooth_min_size_max
    if requested_tooth_size <= 0:
        raise ValueError("gear_tooth_element_size must be > 0.")
    if requested_tooth_min_size <= 0:
        raise ValueError("gear_tooth_min_element_size must be > 0.")
    tooth_clamped_size = max(tooth_size_min, min(requested_tooth_size, tooth_size_max))
    tooth_clamped_min = max(tooth_min_size_min, min(requested_tooth_min_size, tooth_min_size_max))
    tooth_feature_angle = float(gear_tooth_feature_angle) if gear_tooth_feature_angle is not None else float(feature_angle) * GEAR_TOOTH_DEFAULT_SIZE_SCALE
    if tooth_feature_angle <= 0:
        raise ValueError("gear_tooth_feature_angle must be > 0.")
    gear_tooth_tcl_list = " ".join(str(value) for value in safe_gear_tooth_ids)
    lines = [
        "# HyperMesh MCP generated tetra script: surface deviation R-trias -> smooth-pyramid tetra",
        f"# MCP_RECOMMENDED_TIMEOUT_SECONDS={_estimate_tetra_timeout_seconds(surf_count=max_shell_elements_before_tetmesh, min_element_size=clamped_min, batch_size=1)}",
        f"set target_solid {int(solid_id)}",
        f"set target_component {{{comp}}}",
        f"set surface_backup_component {{__mcp_2d_backup_s{int(solid_id)}}}",
        f"set requested_elem_size {clamped_size}",
        f"set requested_min_size {clamped_min}",
        f"set elem_size_min {float(element_size_min)}",
        f"set elem_size_max {float(element_size_max)}",
        f"set min_size_min {float(min_element_size_min)}",
        f"set min_size_max {float(min_element_size_max)}",
        f"set use_gear_tooth_refinement {1 if use_gear_tooth_refinement and safe_gear_tooth_ids else 0}",
        f"set gear_tooth_surfs_requested {{{gear_tooth_tcl_list}}}",
        f"set gear_tooth_elem_size {tooth_clamped_size}",
        f"set gear_tooth_elem_size_min {tooth_size_min}",
        f"set gear_tooth_elem_size_max {tooth_size_max}",
        f"set gear_tooth_min_size {tooth_clamped_min}",
        f"set gear_tooth_min_size_min {tooth_min_size_min}",
        f"set gear_tooth_min_size_max {tooth_min_size_max}",
        f"set gear_tooth_feat_angle {tooth_feature_angle}",
        f"set max_dev {float(max_deviation)}",
        f"set feat_angle {float(feature_angle)}",
        f"set growth {float(growth_rate)}",
        f"set max_shell_before_tetmesh {int(max_shell_elements_before_tetmesh)}",
        f"set crash_guard_shell_limit {int(TETRA_CRASH_GUARD_SHELL_ELEMENTS)}",
        "set surface_repair_timeout_ms 300000",
        "set conservative_repair_shell_limit 80000",
        "set conservative_repair_bad_aspect_limit 500",
        "set ultra_conservative_repair_bad_aspect_limit 2000",
        f"set allow_tetmesh {1 if allow_tetmesh else 0}",
        f"set allow_surface_mesh {1 if allow_surface_mesh else 0}",
        f"set fit_tol_ratio {float(fit_tolerance_ratio)}",
        f"set target_vol_skew {float(target_vol_skew)}",
        f"set repair_vol_skew {float(repair_vol_skew)}",
        f"set delete_existing_component_elements {1 if delete_existing_component_elements else 0}",
        "set surface_aspect_threshold 10.0",
        f"set fatal_surface_aspect_threshold {float(TETRA_FATAL_SURFACE_ASPECT)}",
        f"set surface_chord_dev_threshold {float(TETRA_SURFACE_CHORD_DEV)}",
        f"set surface_chord_dev_degrade_delta {float(chord_dev_degrade_delta)}",
        "set surface_growth_limit 1.23",
        "set surface_max_to_min_ratio 6.0",
        "set retry_count 2",
        "set ok 0",
        "set unfixed_aspect_report 0",
        "set unrepaired_vol_skew_report 0",
        "set extreme_surface_aspect_count 0",
        "set final_bad_solid 0",
        "set final_bad_vol_count 0",
        "set kept_surface_shell_count 0",
        "set last_param_key \"\"",
        "set target_vol_skew_report -1",
        "set repair_fit_degrade_ratio 2.0",
        "set surface_fit_degraded_keep_surface 0",
        "set surface_fit_degraded_before 0.0",
        "set surface_fit_degraded_after 0.0",
        "set surface_fit_degraded_tol 0.0",
        "set surface_fit_degraded_ratio 0.0",
        "set surface_fit_degraded_reason none",
        "set surface_fit_degraded_chord_before 0.0",
        "set surface_fit_degraded_chord_after 0.0",
        "set surface_fit_degraded_chord_delta 0.0",
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
        "proc mcp_list_intersect {a b} {",
        "    array set seen {}",
        "    foreach x $b {set seen($x) 1}",
        "    set out {}",
        "    foreach x $a {if {[info exists seen($x)]} {lappend out $x}}",
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
        "proc mcp_ensure_component {comp color} {",
        "    if {[catch {hm_entityinfo exist comps $comp -byname} exists]} {set exists 0}",
        "    if {!$exists} {catch {*createentity comps name=$comp color=$color}}",
        "    return $comp",
        "}",
        "proc mcp_set_current_component {comp color} {",
        "    mcp_ensure_component $comp $color",
        "    if {[catch {*currentcollector components \"$comp\"} err]} {",
        "        puts \"MCP_PT_WARN currentcollector_failed component=$comp error=$err\"",
        "        return 0",
        "    }",
        "    return 1",
        "}",
        "proc mcp_delete_component_if_empty {comp} {",
        "    if {[catch {hm_entityinfo exist comps $comp -byname} exists] || !$exists} {return 0}",
        "    *createmark elems 1 \"by comp\" $comp",
        "    if {[hm_marklength elems 1] > 0} {return 0}",
        "    *createmark components 1 $comp",
        "    catch {*deletemark components 1}",
        "    return 1",
        "}",
        "proc mcp_cleanup_surface_backup {backup_ids backup_comp} {",
        "    if {[llength $backup_ids] > 0} {mcp_delete_elems $backup_ids}",
        "    set removed [mcp_delete_component_if_empty $backup_comp]",
        "    if {$removed} {puts \"MCP_PT_INFO surface_backup_component_deleted component=$backup_comp\"}",
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
        "    if {[llength $ids] == 0} {return {}}",
        "    catch {*clearmark elements 2}",
        "    eval *createmark elements 1 $ids",
        "    set rc [catch {*elementtestaspect elements 1 $threshold 2 2 0 \"  2D Aspect Ratio  \"} err]",
        "    if {!$rc} {",
        "        set failed [mcp_list_intersect [hm_getmark elements 2] $ids]",
        "        return $failed",
        "    }",
        "    puts \"MCP_PT_WARN surface_aspect_native_test_failed=$err fallback=coordinate_aspect\"",
        "    set out {}",
        "    foreach eid $ids {",
        "        set aspect [mcp_shell_aspect $eid]",
        "        if {$aspect > $threshold} {lappend out $eid}",
        "    }",
        "    return $out",
        "}",
        "proc mcp_bad_shell_chord_dev_ids {ids threshold} {",
        "    if {[llength $ids] == 0} {return {}}",
        "    catch {*clearmark elements 2}",
        "    eval *createmark elements 1 $ids",
        "    set rc [catch {*elementtestchordaldeviation elements 1 $threshold 2 0 \"  2D Chordal Deviation  \"} err]",
        "    if {!$rc} {",
        "        set failed [mcp_list_intersect [hm_getmark elements 2] $ids]",
        "        return $failed",
        "    }",
        "    puts \"MCP_PT_WARN surface_chord_dev_native_test_failed=$err disabled=1\"",
        "    return {}",
        "}",
        "proc mcp_max_shell_chord_dev {ids} {",
        "    if {[llength $ids] == 0} {return 0.0}",
        "    set low 0.0",
        "    set high 0.1",
        "    set guard 0",
        "    set bad [llength [mcp_bad_shell_chord_dev_ids $ids $high]]",
        "    while {$bad > 0 && $guard < 30} {",
        "        set low $high",
        "        set high [expr {$high * 2.0}]",
        "        set bad [llength [mcp_bad_shell_chord_dev_ids $ids $high]]",
        "        incr guard",
        "    }",
        "    for {set i 0} {$i < 18} {incr i} {",
        "        set mid [expr {($low + $high) / 2.0}]",
        "        set bad [llength [mcp_bad_shell_chord_dev_ids $ids $mid]]",
        "        if {$bad > 0} {set low $mid} else {set high $mid}",
        "    }",
        "    return $high",
        "}",
        "proc mcp_sliver_bad_shell_count {ids min_edge_limit aspect_fast_threshold} {",
        "    set count 0",
        "    foreach eid $ids {",
        "        if {[catch {hm_getvalue elems id=$eid dataname=nodes} nodes]} {continue}",
        "        if {[llength $nodes] != 3} {continue}",
        "        set p0 [mcp_node_xyz [lindex $nodes 0]]; set p1 [mcp_node_xyz [lindex $nodes 1]]; set p2 [mcp_node_xyz [lindex $nodes 2]]",
        "        set d01 [mcp_dist3 $p0 $p1]; set d12 [mcp_dist3 $p1 $p2]; set d20 [mcp_dist3 $p2 $p0]",
        "        set min_edge [expr {min($d01, min($d12, $d20))}]",
        "        set max_edge [expr {max($d01, max($d12, $d20))}]",
        "        if {$min_edge <= 0.000001} {incr count; continue}",
        "        set edge_aspect [expr {$max_edge / $min_edge}]",
        "        if {$min_edge < $min_edge_limit || $edge_aspect >= $aspect_fast_threshold} {incr count}",
        "    }",
        "    return $count",
        "}",
        "proc mcp_shells_on_surfaces {surfs} {",
        "    set out {}",
        "    foreach sid $surfs {",
        "        *createmark elems 2 \"by surface\" $sid",
        "        foreach eid [hm_getmark elems 2] {lappend out $eid}",
        "    }",
        "    return $out",
        "}",
        "proc mcp_surfaces_from_elems {elems fallback_surfs} {",
        "    if {[llength $elems] == 0} {return {}}",
        "    if {![catch {eval *createmark elems 1 $elems; *createmark surfaces 1 \"by elems\"}]} {",
        "        set surfs [hm_getmark surfaces 1]",
        "        if {[llength $surfs] > 0} {return $surfs}",
        "    }",
        "    array set bad {}",
        "    foreach eid $elems {set bad($eid) 1}",
        "    set out {}",
        "    foreach sid $fallback_surfs {",
        "        *createmark elems 2 \"by surface\" $sid",
        "        set surface_elems [hm_getmark elems 2]",
        "        foreach se $surface_elems {",
        "            if {[info exists bad($se)]} {lappend out $sid; break}",
        "        }",
        "    }",
        "    return [lsort -unique -integer $out]",
        "}",
        "proc mcp_local_remesh_bad_shells {bad_ids protected_ids fallback_surfs elem_size min_size max_size max_dev feat_angle growth} {",
        "    set target_surfs [mcp_surfaces_from_elems $bad_ids $fallback_surfs]",
        "    set protected_surfs [mcp_surfaces_from_elems $protected_ids $fallback_surfs]",
        "    if {[llength $protected_surfs] > 0} {",
        "        set before_filter_count [llength $target_surfs]",
        "        set target_surfs [mcp_list_subtract $target_surfs $protected_surfs]",
        "        set skipped_count [expr {$before_filter_count - [llength $target_surfs]}]",
        "        if {$skipped_count > 0} {",
        "            puts \"MCP_PT_WARN local_remesh_skipped_surfaces_with_extreme_aspect=$skipped_count protected_extreme_elements=[llength $protected_ids]\"",
        "        }",
        "    }",
        "    if {[llength $target_surfs] == 0} {return 0}",
        "    set before [mcp_all_elems]",
        "    foreach sid $target_surfs {",
        "        *createmark elems 1 \"by surface\" $sid",
        "        set old [hm_getmark elems 1]",
        "        if {[llength $old] > 0} {mcp_delete_elems $old}",
        "        *createmark surfaces 1 $sid",
        "        *createarray 3 0 0 0",
        "        catch {*defaultmeshsurf_growth 1 $elem_size 3 3 2 1 1 1 35 0 $min_size $max_size $max_dev $feat_angle $growth 1 3 1 0}",
        "        catch {*storemeshtodatabase 1}",
        "    }",
        "    set after [mcp_all_elems]",
        "    set new_ids [mcp_list_subtract $after $before]",
        "    if {[llength $new_ids] == 0} {set new_ids [mcp_shells_on_surfaces $target_surfs]}",
        "    return [llength $new_ids]",
        "}",
        "proc mcp_replace_bad_triangle_nodes {bad_ids} {",
        "    set changed 0",
        "    foreach eid $bad_ids {",
        "        if {[catch {hm_getvalue elems id=$eid dataname=nodes} nodes]} {continue}",
        "        if {[llength $nodes] != 3} {continue}",
        "        set n0 [lindex $nodes 0]; set n1 [lindex $nodes 1]; set n2 [lindex $nodes 2]",
        "        set p0 [mcp_node_xyz $n0]; set p1 [mcp_node_xyz $n1]; set p2 [mcp_node_xyz $n2]",
        "        set d01 [mcp_dist3 $p0 $p1]; set d12 [mcp_dist3 $p1 $p2]; set d20 [mcp_dist3 $p2 $p0]",
        "        set keep $n0; set move $n1; set dst $p0",
        "        if {$d12 < $d01 && $d12 <= $d20} {set keep $n1; set move $n2; set dst $p1}",
        "        if {$d20 < $d01 && $d20 < $d12} {set keep $n2; set move $n0; set dst $p2}",
        "        hm_answernext yes",
        "        if {[catch {*replacenodes $move $keep 1 0} replace_err]} {",
        "            puts \"MCP_PT_WARN replace_nodes_failed elem=$eid move=$move keep=$keep error=$replace_err\"",
        "            continue",
        "        }",
        "        incr changed",
        "    }",
        "    return $changed",
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
        "proc mcp_bad_vol_skew_ids {elems threshold} {",
        "    if {[llength $elems] == 0} {return {}}",
        "    catch {*clearmark elems 2}",
        "    eval *createmark elems 1 $elems",
        "    set rc [catch {*elementtestvolumeareaskew elems 1 $threshold 2 4 0 \"\"} err]",
        "    set failed {}",
        "    if {!$rc} {",
        "        set failed [mcp_list_intersect [hm_getmark elems 2] $elems]",
        "        return $failed",
        "    }",
        "    puts \"MCP_PT_WARN volumearea_skew_mark2_test_failed=$err fallback=usermark\"",
        "    if {$rc} {",
        "        catch {*clearmark elems 2}",
        "        eval *createmark elems 1 $elems",
        "        set rc2 [catch {*elementtestvolumeareaskew elems 1 $threshold 0 4 0 \"\"} err2]",
        "        if {!$rc2} {set failed [mcp_list_intersect [hm_getusermark elems] $elems]} else {puts \"MCP_PT_WARN volumearea_skew_usermark_test_failed=$err2\"}",
        "    }",
        "    return $failed",
        "}",
        "proc mcp_shell_ids_in_component {component_name} {",
        "    set out {}",
        "    *createmark elems 1 \"by comp\" \"$component_name\"",
        "    foreach eid [hm_getmark elems 1] {",
        "        set c [hm_getvalue elems id=$eid dataname=config]",
        "        if {$c==103 || $c==104 || $c==106 || $c==108} {lappend out $eid}",
        "    }",
        "    return $out",
        "}",
        "proc mcp_backup_surface_shells {ids backup_comp target_comp} {",
        "    if {[llength $ids] == 0} {return {}}",
        "    mcp_ensure_component $backup_comp 61",
        "    set old_backup_count [mcp_delete_marked_component_elems $backup_comp]",
        "    if {$old_backup_count > 0} {puts \"MCP_PT_INFO backup_removed_old_shells=$old_backup_count component=$backup_comp\"}",
        "    mcp_delete_component_if_empty $backup_comp",
        "    mcp_ensure_component $backup_comp 61",
        "    set before [mcp_all_elems]",
        "    if {![mcp_set_current_component $backup_comp 61]} {return {}}",
        "    eval *createmark elems 1 $ids",
        "    set dup_rc [catch {*duplicatemark elems 1 1} dup_err]",
        "    if {![mcp_set_current_component $target_comp 7]} {return {}}",
        "    if {$dup_rc} {puts \"MCP_PT_WARN surface_backup_duplicate_failed=$dup_err component=$backup_comp\"; return {}}",
        "    set copied [mcp_list_subtract [mcp_all_elems] $before]",
        "    if {[llength $copied] > 0} {eval *createmark elems 1 $copied; catch {*movemark elems 1 \"$backup_comp\"}}",
        "    catch {*createmark components 1 \"$backup_comp\"; *hideentitybymark components 1}",
        "    return $copied",
        "}",
        "proc mcp_restore_surface_backup {backup_ids backup_comp target_comp all_surfs} {",
        "    if {[llength $backup_ids] == 0} {return {}}",
        "    set current_shells [mcp_list_subtract [mcp_shells_on_surfaces $all_surfs] $backup_ids]",
        "    if {[llength $current_shells] > 0} {mcp_delete_elems $current_shells}",
        "    set before [mcp_all_elems]",
        "    if {![mcp_set_current_component $target_comp 7]} {return {}}",
        "    eval *createmark elems 1 $backup_ids",
        "    set dup_rc [catch {*duplicatemark elems 1 1} dup_err]",
        "    if {$dup_rc} {puts \"MCP_PT_WARN surface_backup_restore_failed=$dup_err component=$backup_comp\"; return {}}",
        "    set restored [mcp_list_subtract [mcp_all_elems] $before]",
        "    if {[llength $restored] > 0} {eval *createmark elems 1 $restored; catch {*movemark elems 1 \"$target_comp\"}}",
        "    mcp_delete_elems $backup_ids",
        "    mcp_delete_component_if_empty $backup_comp",
        "    return $restored",
        "}",
        'if {![mcp_set_current_component $target_component 7]} { puts "MCP_PT_FAIL solid=$target_solid currentcollector_failed component=$target_component"; return }',
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
        'set auto_elem_size [expr {min($elem_size_max, max($elem_size_min, $mid_dim/4.0))}]',
        'set elem_size [expr {min($elem_size_max, max($elem_size_min, min($requested_elem_size, $auto_elem_size)))}]',
        'set min_size_span [expr {$min_size_max - $min_size_min}]',
        'set complexity_min [expr {$min_size_max - min($min_size_span, max(0.0, ($surf_count - 20) / 100.0 * $min_size_span))}]',
        'set dim_min [expr {max($min_size_min, min($min_size_max, $min_dim/8.0))}]',
        'set base_min_size [expr {max($min_size_min, min($min_size_max, min($requested_min_size, min($complexity_min, $dim_min))))}]',
        'set gear_tooth_surfs [mcp_list_intersect $gear_tooth_surfs_requested $all_surfs]',
        'if {!$use_gear_tooth_refinement} {set gear_tooth_surfs {}}',
        'set gear_tooth_surface_count [llength $gear_tooth_surfs]',
        'puts "MCP_PT_START solid=$target_solid surf_count=$surf_count elem_size=$elem_size min_size=$base_min_size max_dev=$max_dev fit_tol_ratio=$fit_tol_ratio target_vol_skew=$target_vol_skew gear_tooth_refinement=$use_gear_tooth_refinement gear_tooth_surfaces=$gear_tooth_surface_count gear_tooth_elem_size=$gear_tooth_elem_size gear_tooth_min_size=$gear_tooth_min_size gear_tooth_feat_angle=$gear_tooth_feat_angle"',
        'puts "MCP_CONSOLE solid=$target_solid stage=2d_start surf_count=$surf_count elem_size=$elem_size min_size=$base_min_size"',
        'if {!$allow_surface_mesh} {',
        '    puts "MCP_PT_STOP solid=$target_solid surface_mesh_disabled_for_high_risk_geometry before_surface_mesh"',
        '    return',
        '}',
        'for {set at 0} {$at < $retry_count && !$ok} {incr at} {',
        '    set cs [expr {max($elem_size_min, $elem_size * pow(0.90, $at))}]',
        '    set mn_size [expr {max($min_size_min, $base_min_size * pow(0.80, $at))}]',
        '    set effective_growth [expr {min($growth, $surface_growth_limit)}]',
        '    set max_size [expr {max($cs * 1.35, $mn_size + 0.05)}]',
        '    set max_size [expr {min($max_size, max($mn_size + 0.05, $mn_size * $surface_max_to_min_ratio))}]',
        '    set max_size [expr {max($max_size, $cs)}]',
        '    set param_key [format "%.4f|%.4f|%.4f|%.4f|%.4f" $cs $mn_size $max_size $max_dev $effective_growth]',
        '    if {$param_key eq $last_param_key} {',
        '        puts "MCP_PT_STOP solid=$target_solid duplicate_retry_params attempt=$at params=$param_key keep_previous_surface_mesh=1"',
        '        break',
        '    }',
        '    set last_param_key $param_key',
        '    *createmark surfaces 1 "by solids" $target_solid',
        '    set all_surfs [hm_getmark surfaces 1]',
        '    set gear_tooth_surfs [mcp_list_intersect $gear_tooth_surfs_requested $all_surfs]',
        '    if {!$use_gear_tooth_refinement} {set gear_tooth_surfs {}}',
        '    set body_surfs $all_surfs',
        '    if {[llength $gear_tooth_surfs] > 0} {set body_surfs [mcp_list_subtract $all_surfs $gear_tooth_surfs]}',
        '    *createmark elems 1 "by surface" $all_surfs',
        '    set stale_shells [hm_getmark elems 1]',
        '    if {[llength $stale_shells] > 0} {',
        '        puts "MCP_PT_INFO solid=$target_solid cleanup_stale_shells=[llength $stale_shells] before_attempt=$at"',
        '        mcp_delete_elems $stale_shells',
        '    }',
        '    set before_surface_elems [mcp_all_elems]',
        '    set tooth_cs [expr {max($gear_tooth_elem_size_min, min($gear_tooth_elem_size_max, $gear_tooth_elem_size * pow(0.90, $at)))}]',
        '    set tooth_mn_size [expr {max($gear_tooth_min_size_min, min($gear_tooth_min_size_max, $gear_tooth_min_size * pow(0.80, $at)))}]',
        '    set tooth_max_size [expr {max($tooth_cs * 1.35, $tooth_mn_size + 0.05)}]',
        '    set tooth_max_size [expr {min($tooth_max_size, max($tooth_mn_size + 0.05, $tooth_mn_size * $surface_max_to_min_ratio))}]',
        '    set tooth_max_size [expr {max($tooth_max_size, $tooth_cs)}]',
        '    puts "MCP_PT_SURFACE_ATTEMPT solid=$target_solid attempt=$at size=$cs min=$mn_size max=$max_size growth=$effective_growth max_to_min_ratio=$surface_max_to_min_ratio gear_tooth_surfaces=[llength $gear_tooth_surfs] gear_size=$tooth_cs gear_min=$tooth_mn_size gear_max=$tooth_max_size gear_feat_angle=$gear_tooth_feat_angle"',
        '    set surface_mesh_failed 0',
        '    set surface_mesh_error ""',
        '    if {[llength $gear_tooth_surfs] > 0} {',
        '        eval *createmark surfaces 1 $gear_tooth_surfs',
        '        *createarray 3 0 0 0',
        '        if {[catch {*defaultmeshsurf_growth 1 $tooth_cs 3 3 2 1 1 1 35 0 $tooth_mn_size $tooth_max_size $max_dev $gear_tooth_feat_angle $effective_growth 1 3 1 0} tooth_err]} {',
        '            set surface_mesh_failed 1',
        '            set surface_mesh_error $tooth_err',
        '        } else {',
        '            catch {*storemeshtodatabase 1}',
        '        }',
        '    }',
        '    if {!$surface_mesh_failed && [llength $body_surfs] > 0} {',
        '        eval *createmark surfaces 1 $body_surfs',
        '        *createarray 3 0 0 0',
        '        if {[catch {*defaultmeshsurf_growth 1 $cs 3 3 2 1 1 1 35 0 $mn_size $max_size $max_dev $feat_angle $effective_growth 1 3 1 0} surf_err]} {',
        '            set surface_mesh_failed 1',
        '            set surface_mesh_error $surf_err',
        '        } else {',
        '            catch {*storemeshtodatabase 1}',
        '        }',
        '    }',
        '    if {$surface_mesh_failed} {',
        '        puts "MCP_PT_WARN solid=$target_solid surface_mesh_failed=$surface_mesh_error attempt=$at"',
        '        *createmark elems 1 "by surface" $all_surfs',
        '        set failed_shells [hm_getmark elems 1]',
        '        mcp_delete_elems $failed_shells',
        '        continue',
        '    }',
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
        '    set pre_repair_fit_tol $fit_tol',
        '    set pre_repair_fit_max_diff $fit_max_diff',
        '    set pre_repair_fit_max_index $fit_max_index',
        '    set pre_repair_chord_dev_max [mcp_max_shell_chord_dev $shell_ids]',
        '    puts "MCP_PT_INFO solid=$target_solid surface_fit_before_repair max_diff=$pre_repair_fit_max_diff fit_tol=$pre_repair_fit_tol max_index=$pre_repair_fit_max_index chord_dev_max=$pre_repair_chord_dev_max chord_delta_limit=$surface_chord_dev_degrade_delta attempt=$at"',
        '    if {$shell_count > $max_shell_before_tetmesh} {',
        '        puts "MCP_PT_WARN solid=$target_solid shell_count=$shell_count exceeds_guard=$max_shell_before_tetmesh continuing_to_tetmesh"',
        '    }',
        '    set fatal_aspect_ids [mcp_bad_shell_aspect_ids $shell_ids $fatal_surface_aspect_threshold]',
        '    set fatal_aspect_count [llength $fatal_aspect_ids]',
        '    if {$fatal_aspect_count > 0} {',
        '        puts "MCP_PT_WARN solid=$target_solid extreme_surface_aspect_unrepaired count=$fatal_aspect_count threshold=$fatal_surface_aspect_threshold shell_count=$shell_count continue_repair_other_shells=1"',
        '        set extreme_surface_aspect_count $fatal_aspect_count',
        '    }',
        '    set bad_aspect_ids {}',
        '    set bad_aspect_ids [mcp_list_subtract [mcp_bad_shell_aspect_ids $shell_ids $surface_aspect_threshold] $fatal_aspect_ids]',
        '    puts "MCP_PT_INFO solid=$target_solid aspect_bad_local=[llength $bad_aspect_ids]"',
        '    set initial_aspect_bad_count [llength $bad_aspect_ids]',
        '    set repair_at 0',
        '    set repair_path_label "standard"',
        '    set repair_start_ms [clock milliseconds]',
        '    set repair_timed_out 0',
        '    set conservative_2d_repair 0',
        '    set ultra_conservative_2d_repair 0',
        '    set bad_aspect_count_now [llength $bad_aspect_ids]',
        '    if {$shell_count > $crash_guard_shell_limit || $bad_aspect_count_now > $ultra_conservative_repair_bad_aspect_limit} {',
        '        set ultra_conservative_2d_repair 1',
        '        set conservative_2d_repair 1',
        '        set repair_at 3',
        '        set repair_path_label "ultra_conservative_suspected_overlap_replace_only"',
        '        puts "MCP_PT_WARN solid=$target_solid suspected_overlap_or_broken_surface mode=ultra_conservative shell_count=$shell_count bad_aspect=$bad_aspect_count_now extreme_aspect=$fatal_aspect_count skip_triangle_cleanup=1 skip_smooth=1 skip_local_remesh=1 timeout_ms=$surface_repair_timeout_ms"',
        '    } elseif {$shell_count > $conservative_repair_shell_limit || $bad_aspect_count_now > $conservative_repair_bad_aspect_limit || $fatal_aspect_count > 0} {',
        '        set conservative_2d_repair 1',
        '        set repair_at 2',
        '        set repair_path_label "conservative_suspected_overlap_local_remesh_first"',
        '        puts "MCP_PT_WARN solid=$target_solid suspected_overlap_or_broken_surface mode=conservative shell_count=$shell_count bad_aspect=$bad_aspect_count_now extreme_aspect=$fatal_aspect_count skip_triangle_cleanup=1 skip_smooth=1 timeout_ms=$surface_repair_timeout_ms"',
        '    }',
        '    set sliver_fast_count 0',
        '    if {[llength $bad_aspect_ids] > 0} {',
        '        set sliver_min_edge_limit [expr {max(0.0001, $mn_size * 0.50)}]',
        '        set sliver_fast_count [mcp_sliver_bad_shell_count $bad_aspect_ids $sliver_min_edge_limit 25.0]',
        '        if {$sliver_fast_count > 0 && $sliver_fast_count >= int(ceil([llength $bad_aspect_ids] * 0.50))} {',
        '            puts "MCP_PT_INFO solid=$target_solid aspect_repair_fast_path=replace_nodes reason=sliver_short_edge sliver_count=$sliver_fast_count total=[llength $bad_aspect_ids] min_edge_limit=$sliver_min_edge_limit"',
        '            set repair_path_label "sliver_replace_fast_path"',
        '            set repair_at 3',
        '        }',
        '    }',
        '    while {[llength $bad_aspect_ids] > 0 && $repair_at < 4} {',
        '        set repair_elapsed_ms [expr {[clock milliseconds] - $repair_start_ms}]',
        '        if {$repair_elapsed_ms > $surface_repair_timeout_ms} {',
        '            set repair_timed_out 1',
        '            set repair_path_label "${repair_path_label}_timeout"',
        '            puts "MCP_PT_STOP solid=$target_solid surface_repair_timeout elapsed_ms=$repair_elapsed_ms timeout_ms=$surface_repair_timeout_ms shell_count=$shell_count remaining_aspect=[llength $bad_aspect_ids] keep_repaired_surface_mesh=1"',
        '            break',
        '        }',
        '        if {$ultra_conservative_2d_repair && $repair_at < 3} {set repair_at 3}',
        '        if {$conservative_2d_repair && !$ultra_conservative_2d_repair && $repair_at < 2} {set repair_at 2}',
        '        eval *createmark elems 1 $bad_aspect_ids',
        '        set before_repair_count [llength $bad_aspect_ids]',
        '        if {$repair_at == 0} {',
        '            puts "MCP_PT_INFO solid=$target_solid aspect_repair=triangle_cleanup count=[llength $bad_aspect_ids]"',
        '            catch {*triangle_clean_up elems 1 "aspect=6.0 height=0.3"}',
        '        } elseif {$repair_at == 1} {',
        '            puts "MCP_PT_INFO solid=$target_solid aspect_repair=smooth_5 count=[llength $bad_aspect_ids]"',
        '            catch {*smooth elems 1 5}',
        '        } elseif {$repair_at == 2} {',
        '            puts "MCP_PT_INFO solid=$target_solid aspect_repair=local_remesh count=[llength $bad_aspect_ids]"',
        '            set local_new [mcp_local_remesh_bad_shells $bad_aspect_ids $fatal_aspect_ids $all_surfs $cs $mn_size $max_size $max_dev $feat_angle $effective_growth]',
        '            puts "MCP_PT_INFO solid=$target_solid local_remesh_new_shells=$local_new"',
        '            set shell_ids [mcp_shells_on_surfaces $all_surfs]',
        '        } else {',
        '            puts "MCP_PT_INFO solid=$target_solid aspect_repair=replace_nodes count=[llength $bad_aspect_ids]"',
        '            set replaced [mcp_replace_bad_triangle_nodes $bad_aspect_ids]',
        '            puts "MCP_PT_INFO solid=$target_solid replace_nodes_changed=$replaced"',
        '            set shell_ids [mcp_shells_on_surfaces $all_surfs]',
        '        }',
        '        set fatal_aspect_ids [mcp_bad_shell_aspect_ids $shell_ids $fatal_surface_aspect_threshold]',
        '        set bad_aspect_ids [mcp_list_subtract [mcp_bad_shell_aspect_ids $shell_ids $surface_aspect_threshold] $fatal_aspect_ids]',
        '        set after_repair_count [llength $bad_aspect_ids]',
        '        puts "MCP_PT_INFO solid=$target_solid aspect_repair_after before=$before_repair_count after=$after_repair_count threshold=$surface_aspect_threshold"',
        '        set next_repair_at [expr {$repair_at + 1}]',
        '        if {$after_repair_count == 0} {',
        '            set next_repair_at 4',
        '        } elseif {$repair_at == 0 && $after_repair_count >= $before_repair_count} {',
        '            puts "MCP_PT_INFO solid=$target_solid aspect_repair_smart_skip=smooth_5 reason=triangle_cleanup_no_improvement before=$before_repair_count after=$after_repair_count"',
        '            set repair_path_label "smart_skip_to_local_remesh"',
        '            set next_repair_at 2',
        '        } elseif {$repair_at == 2 && $after_repair_count >= $before_repair_count} {',
        '            puts "MCP_PT_INFO solid=$target_solid aspect_repair_smart_skip=remaining_to_replace_nodes reason=local_remesh_no_improvement before=$before_repair_count after=$after_repair_count"',
        '            set repair_path_label "smart_replace_after_local_remesh"',
        '            set next_repair_at 3',
        '        }',
        '        set repair_elapsed_ms [expr {[clock milliseconds] - $repair_start_ms}]',
        '        if {$repair_elapsed_ms > $surface_repair_timeout_ms && $next_repair_at < 4} {',
        '            set repair_timed_out 1',
        '            set repair_path_label "${repair_path_label}_timeout"',
        '            puts "MCP_PT_STOP solid=$target_solid surface_repair_timeout elapsed_ms=$repair_elapsed_ms timeout_ms=$surface_repair_timeout_ms shell_count=$shell_count remaining_aspect=$after_repair_count keep_repaired_surface_mesh=1"',
        '            break',
        '        }',
        '        set repair_at $next_repair_at',
        '    }',
        '    if {!$repair_timed_out && [llength $bad_aspect_ids] > 0} {',
        '        set repair_elapsed_ms [expr {[clock milliseconds] - $repair_start_ms}]',
        '        if {$repair_elapsed_ms > $surface_repair_timeout_ms} {',
        '            set repair_timed_out 1',
        '            set repair_path_label "${repair_path_label}_timeout"',
        '            puts "MCP_PT_STOP solid=$target_solid surface_repair_timeout elapsed_ms=$repair_elapsed_ms timeout_ms=$surface_repair_timeout_ms shell_count=$shell_count remaining_aspect=[llength $bad_aspect_ids] keep_repaired_surface_mesh=1"',
        '        }',
        '    }',
        '    if {!$repair_timed_out && [llength $bad_aspect_ids] > 0} {',
        '        eval *createmark elems 1 $bad_aspect_ids',
        '        set before_repair_count [llength $bad_aspect_ids]',
        '        puts "MCP_PT_INFO solid=$target_solid aspect_repair=replace_nodes_extra count=[llength $bad_aspect_ids]"',
        '        set replaced [mcp_replace_bad_triangle_nodes $bad_aspect_ids]',
        '        puts "MCP_PT_INFO solid=$target_solid replace_nodes_extra_changed=$replaced"',
        '        set shell_ids [mcp_shells_on_surfaces $all_surfs]',
        '        set fatal_aspect_ids [mcp_bad_shell_aspect_ids $shell_ids $fatal_surface_aspect_threshold]',
        '        set bad_aspect_ids [mcp_list_subtract [mcp_bad_shell_aspect_ids $shell_ids $surface_aspect_threshold] $fatal_aspect_ids]',
        '        set after_repair_count [llength $bad_aspect_ids]',
        '        puts "MCP_PT_INFO solid=$target_solid aspect_repair_after before=$before_repair_count after=$after_repair_count threshold=$surface_aspect_threshold"',
        '        set repair_path_label "${repair_path_label}_extra_replace"',
        '    }',
        '    set shell_ids [mcp_shells_on_surfaces $all_surfs]',
        '    set shell_count [llength $shell_ids]',
        '    if {$shell_count == 0} {',
        '        puts "MCP_PT_WARN solid=$target_solid no_shells_after_2d_repair attempt=$at action=retry_or_fail"',
        '        continue',
        '    }',
        '    eval *createmark elems 2 $shell_ids',
        '    set post_shell_bb [hm_getboundingbox elems 2 0 0 0]',
        '    set post_repair_fit_max_diff 0.0',
        '    set post_repair_fit_max_index -1',
        '    for {set i 0} {$i < 6} {incr i} {',
        '        set fit_diff [expr {abs([lindex $post_shell_bb $i] - [lindex $solid_bb $i])}]',
        '        if {$fit_diff > $post_repair_fit_max_diff} {set post_repair_fit_max_diff $fit_diff; set post_repair_fit_max_index $i}',
        '    }',
        '    set repair_fit_ratio [expr {$post_repair_fit_max_diff / max($pre_repair_fit_max_diff, 1.0e-9)}]',
        '    set post_repair_chord_dev_max [mcp_max_shell_chord_dev $shell_ids]',
        '    set repair_chord_delta [expr {$post_repair_chord_dev_max - $pre_repair_chord_dev_max}]',
        '    set repair_chord_degraded [expr {$repair_chord_delta > $surface_chord_dev_degrade_delta}]',
        '    set repair_bbox_degraded [expr {$post_repair_fit_max_diff > $pre_repair_fit_tol && $repair_fit_ratio >= $repair_fit_degrade_ratio}]',
        '    set repair_degrade_reason none',
        '    if {$repair_bbox_degraded && $repair_chord_degraded} {set repair_degrade_reason bbox+chord} elseif {$repair_bbox_degraded} {set repair_degrade_reason bbox} elseif {$repair_chord_degraded} {set repair_degrade_reason chord}',
        '    puts "MCP_PT_INFO solid=$target_solid surface_fit_after_repair reason=$repair_degrade_reason bbox_before=$pre_repair_fit_max_diff bbox_after=$post_repair_fit_max_diff fit_tol=$pre_repair_fit_tol max_index=$post_repair_fit_max_index ratio=$repair_fit_ratio chord_max_before=$pre_repair_chord_dev_max chord_max_after=$post_repair_chord_dev_max chord_delta=$repair_chord_delta chord_delta_limit=$surface_chord_dev_degrade_delta attempt=$at"',
        '    if {$repair_bbox_degraded || $repair_chord_degraded} {',
        '        set next_attempt [expr {$at + 1}]',
        '        if {$next_attempt < $retry_count} {',
        '            set next_cs [expr {max($elem_size_min, $elem_size * pow(0.90, $next_attempt))}]',
        '            set next_mn_size [expr {max($min_size_min, $base_min_size * pow(0.80, $next_attempt))}]',
        '            set next_max_size [expr {max($next_cs * 1.35, $next_mn_size + 0.05)}]',
        '            set next_max_size [expr {min($next_max_size, max($next_mn_size + 0.05, $next_mn_size * $surface_max_to_min_ratio))}]',
        '            set next_max_size [expr {max($next_max_size, $next_cs)}]',
        '            set next_param_key [format "%.4f|%.4f|%.4f|%.4f|%.4f" $next_cs $next_mn_size $next_max_size $max_dev $effective_growth]',
        '            if {$next_param_key ne $param_key} {',
        '                puts "MCP_PT_WARN solid=$target_solid surface_fit_degraded_after_repair reason=$repair_degrade_reason bbox_before=$pre_repair_fit_max_diff bbox_after=$post_repair_fit_max_diff fit_tol=$pre_repair_fit_tol ratio=$repair_fit_ratio limit=$repair_fit_degrade_ratio chord_max_before=$pre_repair_chord_dev_max chord_max_after=$post_repair_chord_dev_max chord_delta=$repair_chord_delta chord_delta_limit=$surface_chord_dev_degrade_delta attempt=$at action=retry_surface_mesh next_params=$next_param_key"',
        '                mcp_delete_elems $shell_ids',
        '                continue',
        '            }',
        '            puts "MCP_PT_WARN solid=$target_solid duplicate_retry_params_after_fit_degrade attempt=$at next_attempt=$next_attempt params=$next_param_key action=keep_current_surface_mesh"',
        '        }',
        '        puts "MCP_PT_WARN solid=$target_solid surface_fit_degraded_after_repair reason=$repair_degrade_reason bbox_before=$pre_repair_fit_max_diff bbox_after=$post_repair_fit_max_diff fit_tol=$pre_repair_fit_tol ratio=$repair_fit_ratio limit=$repair_fit_degrade_ratio chord_max_before=$pre_repair_chord_dev_max chord_max_after=$post_repair_chord_dev_max chord_delta=$repair_chord_delta chord_delta_limit=$surface_chord_dev_degrade_delta attempt=$at action=final_no_replace_repair"',
        '        set fit_retry_bad_ids [mcp_list_subtract [mcp_bad_shell_aspect_ids $shell_ids $surface_aspect_threshold] [mcp_bad_shell_aspect_ids $shell_ids $fatal_surface_aspect_threshold]]',
        '        if {[llength $fit_retry_bad_ids] > 0 && !$conservative_2d_repair && !$repair_timed_out} {',
        '            eval *createmark elems 1 $fit_retry_bad_ids',
        '            set before_repair_count [llength $fit_retry_bad_ids]',
        '            puts "MCP_PT_INFO solid=$target_solid aspect_repair=fit_degrade_final_no_replace count=$before_repair_count"',
        '            catch {*triangle_clean_up elems 1 "aspect=6.0 height=0.3"}',
        '            catch {*smooth elems 1 5}',
        '            set shell_ids [mcp_shells_on_surfaces $all_surfs]',
        '            set fatal_aspect_ids [mcp_bad_shell_aspect_ids $shell_ids $fatal_surface_aspect_threshold]',
        '            set fit_retry_after_ids [mcp_list_subtract [mcp_bad_shell_aspect_ids $shell_ids $surface_aspect_threshold] $fatal_aspect_ids]',
        '            puts "MCP_PT_INFO solid=$target_solid aspect_repair_after before=$before_repair_count after=[llength $fit_retry_after_ids] threshold=$surface_aspect_threshold"',
        '            set repair_path_label "${repair_path_label}_fit_no_replace"',
        '        } elseif {[llength $fit_retry_bad_ids] > 0} {',
        '            puts "MCP_PT_WARN solid=$target_solid aspect_repair=fit_degrade_final_no_replace_skipped reason=conservative_or_timeout count=[llength $fit_retry_bad_ids] conservative=$conservative_2d_repair timeout=$repair_timed_out"',
        '        }',
        '        set shell_ids [mcp_shells_on_surfaces $all_surfs]',
        '        set shell_count [llength $shell_ids]',
        '        if {$shell_count == 0} {',
        '            puts "MCP_PT_WARN solid=$target_solid no_shells_after_fit_degrade_final_no_replace attempt=$at"',
        '            continue',
        '        }',
        '        eval *createmark elems 2 $shell_ids',
        '        set post_shell_bb [hm_getboundingbox elems 2 0 0 0]',
        '        set post_repair_fit_max_diff 0.0',
        '        set post_repair_fit_max_index -1',
        '        for {set i 0} {$i < 6} {incr i} {',
        '            set fit_diff [expr {abs([lindex $post_shell_bb $i] - [lindex $solid_bb $i])}]',
        '            if {$fit_diff > $post_repair_fit_max_diff} {set post_repair_fit_max_diff $fit_diff; set post_repair_fit_max_index $i}',
        '        }',
        '        set repair_fit_ratio [expr {$post_repair_fit_max_diff / max($pre_repair_fit_max_diff, 1.0e-9)}]',
        '        set post_repair_chord_dev_max [mcp_max_shell_chord_dev $shell_ids]',
        '        set repair_chord_delta [expr {$post_repair_chord_dev_max - $pre_repair_chord_dev_max}]',
        '        set repair_chord_degraded [expr {$repair_chord_delta > $surface_chord_dev_degrade_delta}]',
        '        set repair_bbox_degraded [expr {$post_repair_fit_max_diff > $pre_repair_fit_tol && $repair_fit_ratio >= $repair_fit_degrade_ratio}]',
        '        set repair_degrade_reason none',
        '        if {$repair_bbox_degraded && $repair_chord_degraded} {set repair_degrade_reason bbox+chord} elseif {$repair_bbox_degraded} {set repair_degrade_reason bbox} elseif {$repair_chord_degraded} {set repair_degrade_reason chord}',
        '        puts "MCP_PT_INFO solid=$target_solid surface_fit_after_final_no_replace_repair reason=$repair_degrade_reason bbox_before=$pre_repair_fit_max_diff bbox_after=$post_repair_fit_max_diff fit_tol=$pre_repair_fit_tol max_index=$post_repair_fit_max_index ratio=$repair_fit_ratio chord_max_before=$pre_repair_chord_dev_max chord_max_after=$post_repair_chord_dev_max chord_delta=$repair_chord_delta chord_delta_limit=$surface_chord_dev_degrade_delta attempt=$at"',
        '        if {$repair_bbox_degraded || $repair_chord_degraded} {',
        '            set surface_fit_degraded_keep_surface 1',
        '            set surface_fit_degraded_before $pre_repair_fit_max_diff',
        '            set surface_fit_degraded_after $post_repair_fit_max_diff',
        '            set surface_fit_degraded_tol $pre_repair_fit_tol',
        '            set surface_fit_degraded_ratio $repair_fit_ratio',
        '            set surface_fit_degraded_reason $repair_degrade_reason',
        '            set surface_fit_degraded_chord_before $pre_repair_chord_dev_max',
        '            set surface_fit_degraded_chord_after $post_repair_chord_dev_max',
        '            set surface_fit_degraded_chord_delta $repair_chord_delta',
        '        } else {',
        '            puts "MCP_PT_INFO solid=$target_solid surface_fit_degrade_recovered_after_final_no_replace bbox_before=$pre_repair_fit_max_diff bbox_after=$post_repair_fit_max_diff fit_tol=$pre_repair_fit_tol ratio=$repair_fit_ratio chord_max_before=$pre_repair_chord_dev_max chord_max_after=$post_repair_chord_dev_max chord_delta=$repair_chord_delta chord_delta_limit=$surface_chord_dev_degrade_delta attempt=$at"',
        '        }',
        '    }',
        '    set fatal_aspect_ids [mcp_bad_shell_aspect_ids $shell_ids $fatal_surface_aspect_threshold]',
        '    set extreme_surface_aspect_count [llength $fatal_aspect_ids]',
        '    set bad_aspect_ids [mcp_list_subtract [mcp_bad_shell_aspect_ids $shell_ids $surface_aspect_threshold] $fatal_aspect_ids]',
        '    set normal_aspect_bad_count [llength $bad_aspect_ids]',
        '    set unfixed_aspect_report [expr {$normal_aspect_bad_count + $extreme_surface_aspect_count}]',
        '    puts "MCP_PT_INFO solid=$target_solid repaired_surface_mesh_ready shell_count=$shell_count final_aspect_bad=$unfixed_aspect_report normal_aspect_bad=$normal_aspect_bad_count extreme_surface_aspect=$extreme_surface_aspect_count extreme_threshold=$fatal_surface_aspect_threshold"',
        '    puts "MCP_CONSOLE solid=$target_solid stage=2d_result initial_bad=$initial_aspect_bad_count final_bad=$unfixed_aspect_report repair_path=$repair_path_label shell_count=$shell_count"',
        '    if {$unfixed_aspect_report > 0} {',
        '        puts "MCP_PT_WARN solid=$target_solid aspect_unfixed=$unfixed_aspect_report continue_to_tetmesh_keep_surface_mesh=1"',
        '    }',
        '    if {$repair_timed_out} {',
        '        puts "MCP_PT_STOP solid=$target_solid surface_repair_timeout_skip_tetmesh shell_count=$shell_count final_aspect_bad=$unfixed_aspect_report timeout_ms=$surface_repair_timeout_ms keep_repaired_surface_mesh=1"',
        '        puts "MCP_CONSOLE solid=$target_solid stage=3d_skip reason=surface_repair_timeout shell_count=$shell_count final_aspect_bad=$unfixed_aspect_report timeout_ms=$surface_repair_timeout_ms"',
        '        set kept_surface_shell_count $shell_count',
        '        set ok 0',
        '        break',
        '    }',
        '    if {$shell_count > $crash_guard_shell_limit} {',
        '        puts "MCP_PT_STOP solid=$target_solid crash_guard_skip_tetmesh shell_count=$shell_count limit=$crash_guard_shell_limit unfixed_aspect=$unfixed_aspect_report keep_repaired_surface_mesh=1"',
        '        puts "MCP_CONSOLE solid=$target_solid stage=3d_skip reason=crash_guard shell_count=$shell_count limit=$crash_guard_shell_limit unfixed_aspect=$unfixed_aspect_report"',
        '        set kept_surface_shell_count $shell_count',
        '        set ok 0',
        '        break',
        '    }',
        '    if {$extreme_surface_aspect_count > 0} {',
        '        puts "MCP_PT_STOP solid=$target_solid extreme_aspect_skip_tetmesh shell_count=$shell_count final_aspect_bad=$unfixed_aspect_report extreme_aspect=$extreme_surface_aspect_count threshold=$fatal_surface_aspect_threshold keep_repaired_surface_mesh=1"',
        '        puts "MCP_CONSOLE solid=$target_solid stage=3d_skip reason=extreme_aspect shell_count=$shell_count final_aspect_bad=$unfixed_aspect_report extreme_aspect=$extreme_surface_aspect_count threshold=$fatal_surface_aspect_threshold"',
        '        set kept_surface_shell_count $shell_count',
        '        set ok 0',
        '        break',
        '    }',
        '    if {$surface_fit_degraded_keep_surface} {',
        '        puts "MCP_PT_STOP solid=$target_solid surface_fit_degraded_after_repair reason=$surface_fit_degraded_reason bbox_before=$surface_fit_degraded_before bbox_after=$surface_fit_degraded_after fit_tol=$surface_fit_degraded_tol ratio=$surface_fit_degraded_ratio limit=$repair_fit_degrade_ratio chord_max_before=$surface_fit_degraded_chord_before chord_max_after=$surface_fit_degraded_chord_after chord_delta=$surface_fit_degraded_chord_delta chord_delta_limit=$surface_chord_dev_degrade_delta action=keep_surface_no_tetra shell_count=$shell_count final_aspect_bad=$unfixed_aspect_report keep_repaired_surface_mesh=1"',
        '        puts "MCP_CONSOLE solid=$target_solid stage=3d_skip reason=surface_fit_degraded degrade_reason=$surface_fit_degraded_reason shell_count=$shell_count final_aspect_bad=$unfixed_aspect_report bbox_before=$surface_fit_degraded_before bbox_after=$surface_fit_degraded_after chord_max_before=$surface_fit_degraded_chord_before chord_max_after=$surface_fit_degraded_chord_after chord_delta=$surface_fit_degraded_chord_delta chord_delta_limit=$surface_chord_dev_degrade_delta"',
        '        set kept_surface_shell_count $shell_count',
        '        set ok 0',
        '        break',
        '    }',
        '    if {!$allow_tetmesh} {',
        '        puts "MCP_PT_STOP solid=$target_solid shell_count=$shell_count tetmesh_disabled_for_high_risk_geometry keep_surface_mesh=1"',
        '        set kept_surface_shell_count $shell_count',
        '        return',
        '    }',
        '    set surface_backup_ids [mcp_backup_surface_shells $shell_ids $surface_backup_component $target_component]',
        '    set surface_backup_count [llength $surface_backup_ids]',
        '    puts "MCP_PT_INFO solid=$target_solid surface_backup_created shell_count=$shell_count backup_count=$surface_backup_count component=$surface_backup_component"',
        '    if {$surface_backup_count != $shell_count} {',
        '        puts "MCP_PT_STOP solid=$target_solid surface_backup_failed shell_count=$shell_count backup_count=$surface_backup_count keep_repaired_surface_mesh=1"',
        '        set kept_surface_shell_count $shell_count',
        '        set ok 0',
        '        break',
        '    }',
        '    set before_tetmesh_elems [mcp_all_elems]',
        '    puts "MCP_CONSOLE solid=$target_solid stage=3d_start shell_count=$shell_count target_vol_skew=$target_vol_skew"',
        '    *freesimulation',
        '    *createstringarray 2 "pars: upd_shell=0 fix_comp_bdr vol_skew=\'0.700000,0.800000,0.600000,1.000000,0.860000,0.990000\'" "tet: 67 1.3 -1 0 0.8 -1 -1"',
        '    *createmark components 2 "$target_component"',
        '    if {[hm_marklength components 2] == 0} {',
        '        puts "MCP_PT_WARN solid=$target_solid tetmesh_failed=no_component_mark component=$target_component"',
        '        mcp_cleanup_surface_backup $surface_backup_ids $surface_backup_component',
        '        set kept_surface_shell_count $shell_count',
        '        continue',
        '    }',
        '    catch {update}',
        '    set tet_rc [catch {*tetmesh components 2 1 elements 0 -1 1 2} tet_err]',
        '    catch {update}',
        '    if {$tet_rc} {',
        '        puts "MCP_PT_WARN solid=$target_solid tetmesh_failed=$tet_err manual_component_tetmesh_rejected=1"',
        '        set failed_new_elems [mcp_list_subtract [mcp_all_elems] $before_tetmesh_elems]',
        '        mcp_delete_elems $failed_new_elems',
        '        mcp_cleanup_surface_backup $surface_backup_ids $surface_backup_component',
        '        set kept_surface_shell_count $shell_count',
        '        continue',
        '    }',
        '    set comp_elems [mcp_tetra_ids_in_component $target_component]',
        '    if {[llength $comp_elems] == 0} { puts "MCP_PT_WARN solid=$target_solid no_tetra_after_tetmesh_keep_surface_mesh=1"; mcp_cleanup_surface_backup $surface_backup_ids $surface_backup_component; set kept_surface_shell_count $shell_count; continue }',
        '    puts "MCP_PT_INFO solid=$target_solid tetmesh_generation_vol_skew_target=$target_vol_skew"',
        '    set bad_vol_ids [mcp_bad_vol_skew_ids $comp_elems $repair_vol_skew]',
        '    if {[llength $bad_vol_ids] < 0} {',
        '        puts "MCP_PT_WARN solid=$target_solid vol_skew_test_failed=unknown"',
        '    } else {',
        '        if {[llength $bad_vol_ids] == [llength $comp_elems]} {',
        '            puts "MCP_PT_WARN solid=$target_solid vol_skew_test_all_component_tetras_flagged count=[llength $bad_vol_ids] continue_repair=1"',
        '        }',
        '        puts "MCP_PT_INFO solid=$target_solid vol_skew_bad_initial=[llength $bad_vol_ids] threshold=$repair_vol_skew"',
        '        set vol_repair_at 0',
        '        while {[llength $bad_vol_ids] > 0 && $vol_repair_at < 4} {',
        '            eval *createmark elems 1 $bad_vol_ids',
        '            set before_vol_repair_count [llength $bad_vol_ids]',
        '            if {$vol_repair_at == 0} {',
        '                puts "MCP_PT_INFO solid=$target_solid vol_skew_repair=solid_mesh_optimization count=[llength $bad_vol_ids] threshold=$repair_vol_skew"',
        '                puts "MCP_PT_INFO solid=$target_solid solid_mesh_optimization_input=all_component_tetras count=[llength $comp_elems]"',
        '                *clearmark elements 1',
        '                *clearmark elements 2',
        '                catch {*elementchecksettings -1 0 0 1 0 0 0 1 0 1 1 0 0 0 0 0 0 0 0 0 0 0 0}',
        '                *createstringarray 2 "tet: 256 1.2 2 0.0 0.8 0.0 0" "pars: fix_comp_bdr= 1 fix_top_bdr= 0 shell_swap=0 shell_remesh=0 use_optimizer=1 skip_aflr3=1 feature_angle=35.0 niter=3 upd_shell=0 vol_skew=\'0.99,0.60,0.10,1.0\'"',
        '                eval *createmark elements 1 $comp_elems',
        '                catch {*clearmark elements 2}',
        '                catch {update}',
        '                set opt_rc [catch {*tetmesh elements 1 6 elements 2 1 1 2} opt_err]',
        '                catch {update}',
        '                if {$opt_rc} {puts "MCP_PT_WARN solid=$target_solid solid_mesh_optimization_failed=$opt_err"}',
        '                catch {*elementchecksettings -1 0 0 1 0 0 0 1 0 1 1 0 0 0 0 0 0 0 0 0 0 0 0}',
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
        '            set bad_vol_ids [mcp_bad_vol_skew_ids $comp_elems $repair_vol_skew]',
        '            if {[llength $bad_vol_ids] == [llength $comp_elems]} {',
        '                puts "MCP_PT_WARN solid=$target_solid vol_skew_retest_all_component_tetras_flagged count=[llength $bad_vol_ids] continue_repair=1"',
        '            }',
        '            puts "MCP_PT_INFO solid=$target_solid vol_skew_repair_after before=$before_vol_repair_count after=[llength $bad_vol_ids] threshold=$repair_vol_skew"',
        '            incr vol_repair_at',
        '        }',
        '        set unrepaired_vol_skew_report [llength $bad_vol_ids]',
        '    }',
        '    if {$unrepaired_vol_skew_report > 0} {',
        '        puts "MCP_PT_FAIL solid=$target_solid unrepaired_vol_skew_over_$repair_vol_skew=$unrepaired_vol_skew_report delete_tetra_keep_surface_shells=1"',
        '        puts "MCP_PT_INFO solid=$target_solid rollback_keep_surface_mesh=1 shell_count=$shell_count"',
        '        set final_bad_solid $target_solid',
        '        set final_bad_vol_count $unrepaired_vol_skew_report',
        '        mcp_delete_elems [mcp_tetra_ids_in_component $target_component]',
        '        set rollback_shell_ids [mcp_restore_surface_backup $surface_backup_ids $surface_backup_component $target_component $all_surfs]',
        '        puts "MCP_PT_INFO solid=$target_solid surface_backup_restored restored_count=[llength $rollback_shell_ids] expected=$surface_backup_count component=$surface_backup_component"',
        '        if {[llength $rollback_shell_ids] == 0} {',
        '            puts "MCP_PT_WARN solid=$target_solid surface_backup_restore_empty fallback=current_surface_shells"',
        '            set rollback_shell_ids [mcp_shells_on_surfaces $all_surfs]',
        '        }',
        '        set rollback_extreme_ids [mcp_bad_shell_aspect_ids $rollback_shell_ids $fatal_surface_aspect_threshold]',
        '        set rollback_normal_ids [mcp_list_subtract [mcp_bad_shell_aspect_ids $rollback_shell_ids $surface_aspect_threshold] $rollback_extreme_ids]',
        '        set shell_count [llength $rollback_shell_ids]',
        '        set extreme_surface_aspect_count [llength $rollback_extreme_ids]',
        '        set normal_aspect_bad_count [llength $rollback_normal_ids]',
        '        set unfixed_aspect_report [expr {$normal_aspect_bad_count + $extreme_surface_aspect_count}]',
        '        puts "MCP_PT_INFO solid=$target_solid rollback_surface_quality shell_count=$shell_count normal_aspect_bad=$normal_aspect_bad_count extreme_surface_aspect=$extreme_surface_aspect_count total_aspect_bad=$unfixed_aspect_report extreme_threshold=$fatal_surface_aspect_threshold"',
        '        set ok 0',
        '        break',
        '    }',
        '    mcp_cleanup_surface_backup $surface_backup_ids $surface_backup_component',
        '    mcp_delete_elems $shell_ids',
        '    set leftover_shells [mcp_shell_ids_in_component $target_component]',
        '    if {[llength $leftover_shells] > 0} {',
        '        puts "MCP_PT_INFO solid=$target_solid cleanup_leftover_component_shells=[llength $leftover_shells]"',
        '        mcp_delete_elems $leftover_shells',
        '    }',
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
        'if {!$ok && $final_bad_vol_count == 0} { puts "MCP_PT_FAIL solid=$target_solid no_accepted_tetra_after_retries kept_surface_shells=$kept_surface_shell_count" }',
        'puts "MCP_PT_DONE solid=$target_solid ok=$ok total=$final_count tet4=$t4 tet10=$t10 unfixed_aspect=$unfixed_aspect_report extreme_surface_aspect=$extreme_surface_aspect_count tetmesh_generation_vol_skew_target=$target_vol_skew unrepaired_vol_skew_over_$repair_vol_skew=$unrepaired_vol_skew_report"',
        'puts "MCP_CONSOLE solid=$target_solid stage=3d_result ok=$ok tet4=$t4 tet10=$t10 unfixed_aspect=$unfixed_aspect_report bad_vol=$unrepaired_vol_skew_report"',
    ]
    if output_hm_path:
        lines.append(f'*writefile "{_quote_tcl_path(output_hm_path)}" 1')
    return {
        "success": True,
        "script": _wrap_generated_tcl("generate_plain_tetra_tcl", "\n".join(lines)),
    }


@mcp.tool()
def generate_batched_plain_tetra_tcl(
    solids: list[dict[str, Any]],
    output_hm_path: str | None = None,
    pause_seconds_after_each_solid: float = 5.0,
    checkpoint_every_n_solids: int = 0,
    checkpoint_hm_path: str | None = None,
    default_element_size: float = 1.5,
    default_min_element_size: float = 0.6,
    max_deviation: float = 0.1,
    feature_angle: float = 30,
    growth_rate: float = 1.23,
    fit_tolerance_ratio: float = 0.01,
    target_vol_skew: float = 0.70,
    repair_vol_skew: float = 0.99,
    chord_dev_degrade_delta: float = 0.20,
    element_size_min: float = 1.5,
    element_size_max: float = 2.0,
    min_element_size_min: float = 0.4,
    min_element_size_max: float = 0.6,
    delete_existing_component_elements: bool = True,
    use_gear_tooth_refinement: bool = True,
    gear_tooth_element_size: float | None = 1.6,
    gear_tooth_element_size_min: float | None = 1.2,
    gear_tooth_element_size_max: float | None = 1.6,
    gear_tooth_min_element_size: float | None = 0.3,
    gear_tooth_min_element_size_min: float | None = 0.2,
    gear_tooth_min_element_size_max: float | None = 0.3,
    gear_tooth_feature_angle: float | None = 15.0,
) -> dict[str, Any]:
    """Generate one compact Tcl script for a small batch of plain tetra solids."""
    if not solids:
        raise ValueError("solids list cannot be empty.")
    if len(solids) > 4:
        raise ValueError("Keep tetra batches small: pass at most 4 solids.")
    if pause_seconds_after_each_solid < 0:
        raise ValueError("pause_seconds_after_each_solid cannot be negative.")
    if checkpoint_every_n_solids < 0:
        raise ValueError("checkpoint_every_n_solids cannot be negative.")
    if default_element_size <= 0:
        raise ValueError("default_element_size must be > 0.")
    if default_min_element_size <= 0:
        raise ValueError("default_min_element_size must be > 0.")
    if chord_dev_degrade_delta < 0:
        raise ValueError("chord_dev_degrade_delta must be >= 0.")

    max_timeout = 300
    body_lines: list[str] = [
        "# HyperMesh MCP generated batched tetra script",
        "# MCP_RECOMMENDED_TIMEOUT_SECONDS=__MCP_TIMEOUT_PLACEHOLDER__",
        f"set mcp_tetra_batch_count {len(solids)}",
        f"set mcp_tetra_pause_ms {int(float(pause_seconds_after_each_solid) * 1000)}",
        f"set mcp_tetra_checkpoint_every {int(checkpoint_every_n_solids)}",
        f'set mcp_tetra_checkpoint_path "{_quote_tcl_path(checkpoint_hm_path) if checkpoint_hm_path else ""}"',
        "set mcp_tetra_completed 0",
    ]
    summaries: list[dict[str, Any]] = []
    common_end = 0

    for index, item in enumerate(solids):
        sid = int(item.get("solid_id", 0))
        comp = str(item.get("component_name", "")).strip()
        if sid <= 0 or not comp:
            raise ValueError(f"Invalid tetra solid spec: {item}")
        elem = float(item.get("element_size", default_element_size))
        mn = float(item.get("min_element_size", default_min_element_size))
        max_shell = int(item.get("max_shell_elements_before_tetmesh", TETRA_MAX_SHELL_ELEMENTS))
        allow_tet = bool(item.get("allow_tetmesh", True))
        allow_surf = bool(item.get("allow_surface_mesh", True))
        surf_count = int(item.get("surf_count", 0) or 0)
        gear_tooth_ids = item.get("gear_tooth_surface_ids", []) or []
        dims = item.get("dims", {}) if isinstance(item.get("dims", {}), dict) else {}
        diagonal = float((float(dims.get("dx", 0)) ** 2 + float(dims.get("dy", 0)) ** 2 + float(dims.get("dz", 0)) ** 2) ** 0.5)
        recommended_timeout = _estimate_tetra_timeout_seconds(
            surf_count=surf_count,
            min_element_size=mn,
            diagonal=diagonal,
            batch_size=len(solids),
        )
        max_timeout = max(max_timeout, recommended_timeout)
        summaries.append({
            "solid_id": sid,
            "component_name": comp,
            "surf_count": surf_count,
            "recommended_timeout_seconds": recommended_timeout,
        })

        script_obj = generate_plain_tetra_tcl(
            solid_id=sid,
            component_name=comp,
            element_size=elem,
            min_element_size=mn,
            max_deviation=max_deviation,
            feature_angle=feature_angle,
            growth_rate=growth_rate,
            max_shell_elements_before_tetmesh=max_shell,
            allow_tetmesh=allow_tet,
            allow_surface_mesh=allow_surf,
            fit_tolerance_ratio=fit_tolerance_ratio,
            target_vol_skew=target_vol_skew,
            repair_vol_skew=repair_vol_skew,
            chord_dev_degrade_delta=chord_dev_degrade_delta,
            element_size_min=element_size_min,
            element_size_max=element_size_max,
            min_element_size_min=min_element_size_min,
            min_element_size_max=min_element_size_max,
            delete_existing_component_elements=delete_existing_component_elements,
            use_gear_tooth_refinement=use_gear_tooth_refinement and str(item.get("strategy", "")) == "gear_aware_tetra",
            gear_tooth_surface_ids=gear_tooth_ids,
            gear_tooth_element_size=gear_tooth_element_size,
            gear_tooth_element_size_min=gear_tooth_element_size_min,
            gear_tooth_element_size_max=gear_tooth_element_size_max,
            gear_tooth_min_element_size=gear_tooth_min_element_size,
            gear_tooth_min_element_size_min=gear_tooth_min_element_size_min,
            gear_tooth_min_element_size_max=gear_tooth_min_element_size_max,
            gear_tooth_feature_angle=gear_tooth_feature_angle,
        )
        one_lines = _unwrap_generated_tcl(script_obj["script"]).splitlines()
        proc_start = next((i for i, line in enumerate(one_lines) if line.startswith("proc mcp_all_elems")), None)
        exec_start = next(
            (
                i
                for i, line in enumerate(one_lines)
                if line.startswith('*currentcollector components')
                or line.startswith('if {![mcp_set_current_component $target_component')
            ),
            None,
        )
        if proc_start is None or exec_start is None:
            raise RuntimeError("Could not compact generated tetra script.")
        if index == 0:
            body_lines.extend(one_lines)
            common_end = exec_start
        else:
            body_lines.extend(one_lines[:proc_start])
            body_lines.extend(one_lines[exec_start:])
        body_lines.extend([
            "incr mcp_tetra_completed",
            'puts "MCP_BT_PROGRESS completed=$mcp_tetra_completed total=$mcp_tetra_batch_count"',
            "catch {update}",
        ])
        if checkpoint_hm_path and checkpoint_every_n_solids > 0:
            body_lines.extend([
                'if {$mcp_tetra_checkpoint_path ne "" && $mcp_tetra_checkpoint_every > 0 && ($mcp_tetra_completed % $mcp_tetra_checkpoint_every) == 0} {',
                '    puts "MCP_BT_CHECKPOINT completed=$mcp_tetra_completed path=$mcp_tetra_checkpoint_path"',
                "    catch {*writefile $mcp_tetra_checkpoint_path 1}",
                "}",
            ])
        body_lines.append("if {$mcp_tetra_pause_ms > 0} {after $mcp_tetra_pause_ms; catch {update}}")

    if output_hm_path:
        body_lines.append(f'*writefile "{_quote_tcl_path(output_hm_path)}" 1')

    body_lines = [
        line.replace("__MCP_TIMEOUT_PLACEHOLDER__", str(max_timeout))
        for line in body_lines
    ]
    compact_saved_lines = max(0, common_end * max(0, len(solids) - 1))
    return {
        "success": True,
        "script": _wrap_generated_tcl("generate_batched_plain_tetra_tcl", "\n".join(body_lines)),
        "solid_count": len(solids),
        "solid_summaries": summaries,
        "recommended_timeout_seconds": max_timeout,
        "compact_saved_lines_estimate": compact_saved_lines,
        "required_next_step": (
            "Execute this script with execute_tcl_gui using at least "
            f"{max_timeout} seconds timeout. Keep high-risk solids in separate batches."
        ),
    }


def _classification_results_dict(classification_results: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    if classification_results is None:
        return dict(_GLOBAL_CLASSIFICATION_RESULTS)
    if isinstance(classification_results, dict) and isinstance(classification_results.get("results"), dict):
        return classification_results["results"]
    if isinstance(classification_results, dict):
        return classification_results
    return {}


@mcp.tool()
def write_gear_tooth_recognition_report(
    classification_results: dict[str, Any] | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Write a compact gear/tooth recognition report for debugging missed or false detections."""
    results = _classification_results_dict(classification_results)
    if output_path:
        path = Path(output_path)
    else:
        RUNS_DIR.mkdir(exist_ok=True)
        path = RUNS_DIR / f"gear_tooth_recognition_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    lines: list[str] = [
        "Gear/tooth recognition diagnostics",
        f"time: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"solid_count: {len(results)}",
        "",
    ]
    for key, item in sorted(results.items(), key=lambda pair: int(pair[0]) if str(pair[0]).isdigit() else str(pair[0])):
        sid = item.get("solid_id", key)
        strategy = item.get("strategy", "")
        dims = item.get("dims", {}) if isinstance(item.get("dims"), dict) else {}
        probe_tooth_ids = item.get("gear_probe_tooth_surface_ids", item.get("gear_tooth_surface_ids", [])) or []
        final_tooth_ids = item.get("gear_tooth_surface_ids", []) or []
        evidence = item.get("evidence", []) or []
        lines.append(f"solid={sid} component={item.get('component_name', '')} strategy={strategy}")
        lines.append(
            "  geometry: "
            f"surf_count={item.get('surf_count', '')} "
            f"dx={dims.get('dx', '')} dy={dims.get('dy', '')} dz={dims.get('dz', '')} "
            f"mn={dims.get('mn', '')} md={dims.get('md', '')} mx={dims.get('mx', '')}"
        )
        lines.append(
            "  source: "
            f"surface={item.get('source_surface_id', '')} "
            f"loops={item.get('source_loop_count', '')} "
            f"inner_loops={item.get('source_inner_loop_count', '')} "
            f"boundary_edges={item.get('source_boundary_edge_count', '')} "
            f"boundary_nodes={item.get('source_boundary_node_count', '')}"
        )
        lines.append(
            "  gear: "
            f"enabled={item.get('gear_detection_enabled', True)} "
            f"axis={item.get('gear_axis', '')} "
            f"reason={item.get('gear_probe_reason', '') or ('accepted' if strategy == 'gear_aware_tetra' else 'not_gear')}"
        )
        lines.append(
            "  tooth_probe: "
            f"count={item.get('gear_probe_tooth_surface_count', len(probe_tooth_ids))} "
            f"ids={','.join(str(value) for value in probe_tooth_ids) if probe_tooth_ids else ''}"
        )
        lines.append(
            "  tooth_final: "
            f"count={item.get('gear_tooth_surface_count', len(final_tooth_ids))} "
            f"ids={','.join(str(value) for value in final_tooth_ids) if final_tooth_ids else ''}"
        )
        if evidence:
            lines.append("  evidence: " + " | ".join(str(value) for value in evidence))
        raw_probe_line = item.get("raw_probe_line", "")
        if raw_probe_line:
            lines.append("  raw_probe: " + str(raw_probe_line))
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "success": True,
        "output_path": str(path),
        "solid_count": len(results),
        "gear_count": sum(1 for item in results.values() if item.get("strategy") == "gear_aware_tetra"),
    }


@mcp.tool()
def generate_gear_tooth_preview_tcl(
    classification_results: dict[str, Any] | None = None,
    element_size: float = 1.6,
    min_element_size: float = 0.3,
    max_deviation: float = 0.1,
    feature_angle: float = 15.0,
    growth_rate: float = 1.23,
    component_name: str = GEAR_TOOTH_PREVIEW_COMPONENT,
    delete_existing_preview: bool = True,
) -> dict[str, Any]:
    """Generate Tcl that meshes only recognized gear tooth surfaces into a temporary preview component."""
    if element_size <= 0:
        raise ValueError("element_size must be > 0.")
    if min_element_size <= 0:
        raise ValueError("min_element_size must be > 0.")
    if feature_angle <= 0:
        raise ValueError("feature_angle must be > 0.")
    results = _classification_results_dict(classification_results)
    gear_items = [
        item for item in results.values()
        if item.get("strategy") == "gear_aware_tetra" and item.get("gear_tooth_surface_ids")
    ]
    preview_comp = _tcl_escape_name(component_name or GEAR_TOOTH_PREVIEW_COMPONENT)
    lines = [
        "# HyperMesh MCP generated gear tooth preview script",
        f"set preview_component {{{preview_comp}}}",
        f"set preview_element_size {float(element_size)}",
        f"set preview_min_size {float(min_element_size)}",
        f"set preview_max_size {max(float(element_size) * 1.35, float(min_element_size) + 0.05)}",
        f"set preview_max_dev {float(max_deviation)}",
        f"set preview_feat_angle {float(feature_angle)}",
        f"set preview_growth {float(growth_rate)}",
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
        "proc mcp_ensure_component {comp color} {",
        "    if {[catch {hm_entityinfo exist comps $comp -byname} exists]} {set exists 0}",
        "    if {!$exists} {catch {*createentity comps name=$comp color=$color}}",
        "}",
        "mcp_ensure_component $preview_component 13",
        "if {" + ("1" if delete_existing_preview else "0") + "} {",
        '    *createmark elems 1 "by comp" "$preview_component"',
        "    set old_preview [hm_getmark elems 1]",
        "    if {[llength $old_preview] > 0} {mcp_delete_elems $old_preview}",
        "    puts \"MCP_GEAR_TOOTH_PREVIEW_CLEANUP removed=[llength $old_preview] component=$preview_component\"",
        "}",
        "*currentcollector components \"$preview_component\"",
        'puts "MCP_GEAR_TOOTH_PREVIEW_BEGIN gear_count=' + str(len(gear_items)) + '"',
    ]
    total_surfaces = 0
    for item in sorted(gear_items, key=lambda value: int(value.get("solid_id", 0) or 0)):
        sid = int(item.get("solid_id", 0) or 0)
        surf_ids = []
        for value in item.get("gear_tooth_surface_ids", []) or []:
            try:
                surf_id = int(value)
            except (TypeError, ValueError):
                continue
            if surf_id > 0:
                surf_ids.append(surf_id)
        if not surf_ids:
            continue
        total_surfaces += len(surf_ids)
        surf_text = " ".join(str(value) for value in surf_ids)
        lines.extend([
            f"set preview_surfs {{{surf_text}}}",
            "set before_preview [mcp_all_elems]",
            "eval *createmark surfaces 1 $preview_surfs",
            "*createarray 3 0 0 0",
            "if {[catch {*defaultmeshsurf_growth 1 $preview_element_size 3 3 2 1 1 1 35 0 $preview_min_size $preview_max_size $preview_max_dev $preview_feat_angle $preview_growth 1 3 1 0} preview_err]} {",
            f"    puts \"MCP_GEAR_TOOTH_PREVIEW_WARN solid={sid} mesh_failed=$preview_err surfaces=[llength $preview_surfs]\"",
            "} else {",
            "    catch {*storemeshtodatabase 1}",
            "}",
            "set after_preview [mcp_all_elems]",
            "set new_preview [mcp_list_subtract $after_preview $before_preview]",
            "if {[llength $new_preview] > 0} {eval *createmark elems 1 $new_preview; catch {*movemark elems 1 \"$preview_component\"}}",
            f"puts \"MCP_GEAR_TOOTH_PREVIEW solid={sid} component={item.get('component_name', '')} surfaces=[llength $preview_surfs] elems=[llength $new_preview]\"",
        ])
    lines.append(f'puts "MCP_GEAR_TOOTH_PREVIEW_DONE gear_count={len(gear_items)} surface_count={total_surfaces} component=$preview_component"')
    return {
        "success": True,
        "script": _wrap_generated_tcl("generate_gear_tooth_preview_tcl", "\n".join(lines)),
        "gear_count": len(gear_items),
        "surface_count": total_surfaces,
        "component_name": component_name,
    }


@mcp.tool()
def generate_delete_gear_tooth_preview_tcl(
    component_name: str = GEAR_TOOTH_PREVIEW_COMPONENT,
) -> dict[str, Any]:
    """Generate Tcl that deletes only the temporary gear-tooth preview mesh component."""
    preview_comp = _tcl_escape_name(component_name or GEAR_TOOTH_PREVIEW_COMPONENT)
    lines = [
        "# HyperMesh MCP generated gear tooth preview cleanup script",
        f"set preview_component {{{preview_comp}}}",
        "proc mcp_delete_elems {ids} {",
        "    if {[llength $ids] == 0} {return}",
        "    eval *createmark elems 1 $ids",
        "    catch {*deletemark elems 1}",
        "    eval *createmark elements 1 $ids",
        "    catch {*deletemark elements 1}",
        "}",
        'if {[catch {hm_entityinfo exist comps $preview_component -byname} exists] || !$exists} {',
        '    puts "MCP_GEAR_TOOTH_PREVIEW_DELETE_DONE component=$preview_component removed=0 exists=0"',
        "} else {",
        '    *createmark elems 1 "by comp" "$preview_component"',
        "    set preview_ids [hm_getmark elems 1]",
        "    set removed [llength $preview_ids]",
        "    if {$removed > 0} {mcp_delete_elems $preview_ids}",
        '    *createmark elems 1 "by comp" "$preview_component"',
        "    if {[hm_marklength elems 1] == 0} {",
        '        *createmark components 1 "$preview_component"',
        "        catch {*deletemark components 1}",
        "    }",
        '    puts "MCP_GEAR_TOOTH_PREVIEW_DELETE_DONE component=$preview_component removed=$removed exists=1"',
        "}",
    ]
    return {
        "success": True,
        "script": _wrap_generated_tcl("generate_delete_gear_tooth_preview_tcl", "\n".join(lines)),
        "component_name": component_name,
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
    drag_aspect_guard: bool = True,
    drag_aspect_threshold: float = 20.0,
    drag_min_layers: int = 3,
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
    if retry_count < 0:
        raise ValueError("retry_count cannot be negative.")
    if seed_balance_ratio_threshold <= 1.0:
        raise ValueError("seed_balance_ratio_threshold must be greater than 1.0.")
    if drag_aspect_threshold <= 0:
        raise ValueError("drag_aspect_threshold must be > 0.")
    if drag_min_layers < 1:
        raise ValueError("drag_min_layers must be >= 1.")
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
        f"set drag_aspect_guard {1 if drag_aspect_guard else 0}",
        f"set drag_aspect_threshold {float(drag_aspect_threshold)}",
        f"set drag_min_layers {int(drag_min_layers)}",
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
        "proc mcp_aspect_bad_count {elems threshold} {",
        "    if {[llength $elems] == 0} {return 0}",
        "    if {[catch {eval *createmark elements 1 $elems; *createmark elements 2; *elementtestaspect elements 1 $threshold 2 2 0 \"  3D Aspect Ratio  \"} err]} {",
        "        puts \"MCP_DRAG_ASPECT_WARN native_failed=$err\"",
        "        return 999999",
        "    }",
        "    if {[catch {hm_marklength elements 2} n]} {",
        "        if {[catch {llength [hm_getmark elements 2]} n]} {return 999999}",
        "    }",
        "    return $n",
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
        "if {$drag_aspect_guard && $drag_layers < $drag_min_layers} {set drag_layers $drag_min_layers}",
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
        "    set final_hex_count 0",
        "    set attempt 0",
        "    set max_attempts [expr {$retry_count + 1}]",
        "    if {$drag_aspect_guard} {set max_attempts 3}",
        "    while {$attempt < $max_attempts && !$hex_success} {",
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
        "        set aspect_bad 0",
        "        if {$drag_aspect_guard} {",
        "            set aspect_bad [mcp_aspect_bad_count $new_elems $drag_aspect_threshold]",
        "            puts \"MCP_DRAG_ASPECT single solid=$target_solid attempt=[expr {$attempt+1}] layers=$drag_layers bad=$aspect_bad threshold=$drag_aspect_threshold\"",
        "        }",
        "        if {[llength $new_elems] > 0 && $hex_count == [llength $new_elems] && $fit_ok && (!$drag_aspect_guard || $aspect_bad == 0)} {",
        "            set hex_success 1",
        "            puts \"MCP_DRAG_RESULT solid=$target_solid ok=1 layers=$drag_layers aspect_bad=$aspect_bad threshold=$drag_aspect_threshold one_layer_fallback=0\"",
        '            puts "MCP guarded drag completed: hex8=$hex_count fit_ok=$fit_ok"',
        "        } else {",
        '            puts "MCP guarded drag invalid: new_elements=[llength $new_elems] hex8=$hex_count fit_ok=$fit_ok aspect_bad=$aspect_bad; cleaning and retrying/falling back."',
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
    element_size_min: float = 0.5,
    element_size_max: float = 1.5,
    drag_aspect_guard: bool = True,
    drag_aspect_threshold: float = 20.0,
    drag_min_layers: int = 3,
    matched_edge_groups: list[list[int]] | None = None,
    pause_seconds_after_each_solid: float = 1.0,
    checkpoint_every_n_solids: int = 0,
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
    if element_size_min <= 0 or element_size_max <= 0 or element_size_min > element_size_max:
        raise ValueError("element_size_min/max must be positive and min <= max.")
    if retry_count < 0:
        raise ValueError("retry_count cannot be negative.")
    if pause_seconds_after_each_solid < 0:
        raise ValueError("pause_seconds_after_each_solid cannot be negative.")
    if checkpoint_every_n_solids < 0:
        raise ValueError("checkpoint_every_n_solids cannot be negative.")
    if drag_aspect_threshold <= 0:
        raise ValueError("drag_aspect_threshold must be > 0.")
    if drag_min_layers < 1:
        raise ValueError("drag_min_layers must be >= 1.")

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
        f"set elem_size_min {float(element_size_min)}",
        f"set elem_size_max {float(element_size_max)}",
        f"set fit_tol_ratio {float(fit_tolerance_ratio)}",
        f"set retry_count {int(retry_count)}",
        f"set drag_aspect_guard {1 if drag_aspect_guard else 0}",
        f"set drag_aspect_threshold {float(drag_aspect_threshold)}",
        f"set drag_min_layers {int(drag_min_layers)}",
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
        "proc b_del {e} { if {[llength $e]>0} { eval *createmark elems 1 $e; catch {*deletemark elems 1} } }",
        "proc b_aspect_bad {e threshold} {",
        "    if {[llength $e]==0} {return 0}",
        "    if {[catch {eval *createmark elements 1 $e; *createmark elements 2; *elementtestaspect elements 1 $threshold 2 2 0 \"  3D Aspect Ratio  \"} err]} {",
        "        puts \"MCP_DRAG_ASPECT_WARN native_failed=$err\"",
        "        return 999999",
        "    }",
        "    if {[catch {hm_marklength elements 2} n]} {",
        "        if {[catch {llength [hm_getmark elements 2]} n]} {return 999999}",
        "    }",
        "    return $n",
        "}",
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
        "    set cs [expr {max($elem_size_min, min($elem_size_max, min($elem_size, $thickness_size, $source_size)))}]",
        '    puts "MCP_DRAG_SIZE solid=$sid thickness=$dd source_minor=$source_minor source_major=$source_major requested=$elem_size thickness_size=$thickness_size source_size=$source_size chosen=$cs limits=$elem_size_min..$elem_size_max"',
        "    set hs 0; set at 0; set one_layer_fallback 0; set final_layers 0; set final_aspect_bad 0",
        "    set max_attempts [expr {$retry_count + 1}]",
        "    if {$drag_aspect_guard} {set max_attempts 3}",
        "    while {$at<$max_attempts&&!$hs} {",
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
        "        catch {update}; after 25",
        "        if {!$drag_aspect_guard} {puts \"MCP_DRAG_ASPECT_CHECK_SKIPPED solid=$sid\"}",
        '        *createmark elems 1 "by surface" $surf',
        '        set source_shells [hm_getmark elems 1]',
        '        puts "MCP_DRAG_SOURCE_SHELLS solid=$sid surf=$surf count=[llength $source_shells]"',
        '        if {[llength $source_shells] == 0} {',
        '            puts "MCP_DRAG_FAIL stage=source_shell_empty solid=$sid surf=$surf"',
        '            set cs [expr {$cs*0.8}]',
        '            if {$cs<$elem_size_min} {set cs $elem_size_min}',
        '            incr at',
        '            continue',
        '        }',
        "        set ss $source_shells; set qc 0",
        "        foreach i $ss { set cf [hm_getvalue elems id=$i dataname=config]; if {$cf==104||$cf==108} {incr qc} }",
        "        if {[llength $ss]==0||$qc!=[llength $ss]} {",
        "            b_del $ss",
            "            puts \"MCP_DRAG_SKIP_TETRA solid=$sid reason=non_quad_source removed_old_tetra_path=1\"",
            "            break",
        "        }",
        "        set dl [expr {max(1,round($dd/$cs))}]",
        "        if {$drag_aspect_guard && $dl < $drag_min_layers} {set dl $drag_min_layers}",
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
        "            catch {update}; after 25",
        "            puts \"MCP_DRAG_FAIL stage=meshdragelements_error solid=$sid error=$_mcp_drag_err\"",
        "            b_del $source_shells",
        "            set cs [expr {$cs*0.8}]",
        "            if {$cs<$elem_size_min} {set cs $elem_size_min}",
        "            incr at",
        "            continue",
        "        }",
        "        catch {update}; after 25",
        "        set after_drag [b_all]",
        "        set new_drag_elems [b_sub $after_drag $be]",
        "        puts \"MCP_DRAG_NEW_ELEMS solid=$sid count=[llength $new_drag_elems]\"",
        "        if {[llength $new_drag_elems] == 0} {",
        "            puts \"MCP_DRAG_FAIL stage=meshdragelements_empty solid=$sid\"",
        "            b_del $source_shells",
        "            set cs [expr {$cs*0.8}]",
        "            if {$cs<$elem_size_min} {set cs $elem_size_min}",
        "            incr at",
        "            continue",
        "        }",
        "        set ne $new_drag_elems; set hc [b_hex $ne]; set fo [b_fit $ne $sid $fit_tol_ratio $cs]",
        "        set aspect_bad 0",
        "        if {$drag_aspect_guard} {",
        "            set aspect_bad [b_aspect_bad $ne $drag_aspect_threshold]",
        "            puts \"MCP_DRAG_ASPECT solid=$sid attempt=[expr {$at+1}] layers=$dl bad=$aspect_bad threshold=$drag_aspect_threshold\"",
        "        }",
        "        if {[llength $ne]>0&&$hc==[llength $ne]&&$fo&&(!$drag_aspect_guard||$aspect_bad==0)} {",
        "            set hs 1; set final_layers $dl; set final_aspect_bad $aspect_bad",
        "        } else {",
        "            b_del $ne",
        "            b_del $source_shells",
        "            set cs [expr {$cs*0.8}]; if {$cs<$elem_size_min} {set cs $elem_size_min}",
        "        }; incr at",
        "    }",
        "    if {!$hs && $drag_aspect_guard} {",
        "        puts \"MCP_DRAG_ONE_LAYER_RETRY solid=$sid reason=aspect_guard_failed attempts=$max_attempts\"",
        "        *createmark surfaces 1 $surf",
        "        *interactiveremeshsurf 1 $cs 1 1 1 1 1",
        "        *set_meshfaceparams 0 5 1 0 0 1 0.5 1 1",
        "        *createmark surfaces 2 $surf",
        "        set bb [hm_getboundingbox surfaces 2 0 0 0]",
        "        set mj [lindex [lsort -real [list [expr {abs([lindex $bb 3]-[lindex $bb 0])}] [expr {abs([lindex $bb 4]-[lindex $bb 1])}] [expr {abs([lindex $bb 5]-[lindex $bb 2])}]]] 2]",
        "        set td [expr {int(round($mj/$cs))}]; if {$td<4} {set td 4}",
        "        foreach e {0 1 2 3} { catch {*set_meshedgeparams $e $td 1 0 0 0 $cs 0 0} }",
        "        *automesh 0 5 1; *storemeshtodatabase 1; *ameshclearsurface",
        "        catch {update}; after 25",
        "        *createmark elems 1 \"by surface\" $surf",
        "        set source_shells [hm_getmark elems 1]",
        "        set ss $source_shells; set qc 0",
        "        foreach i $ss { set cf [hm_getvalue elems id=$i dataname=config]; if {$cf==104||$cf==108} {incr qc} }",
        "        if {[llength $ss]>0&&$qc==[llength $ss]} {",
        "            set be [b_all]",
        "            *createmark surfaces 2 $surf",
        "            set sb [hm_getboundingbox surfaces 2 0 0 0]",
        "            *createmark surfaces 2 \"by solids\" $sid",
        "            set bb [hm_getboundingbox surfaces 2 0 0 0]",
        "            if {$ax eq \"x\"} {set dvx 1; set dvy 0; set dvz 0; set sc [expr {([lindex $sb 0]+[lindex $sb 3])/2.0}]; set smin [lindex $bb 0]; set smax [lindex $bb 3]} else { if {$ax eq \"y\"} {set dvx 0; set dvy 1; set dvz 0; set sc [expr {([lindex $sb 1]+[lindex $sb 4])/2.0}]; set smin [lindex $bb 1]; set smax [lindex $bb 4]} else {set dvx 0; set dvy 0; set dvz 1; set sc [expr {([lindex $sb 2]+[lindex $sb 5])/2.0}]; set smin [lindex $bb 2]; set smax [lindex $bb 5]} }",
        "            if {[expr {$sc-$smin}] > [expr {$smax-$sc}]} { set dvx [expr {-1*$dvx}]; set dvy [expr {-1*$dvy}]; set dvz [expr {-1*$dvz}] }",
        "            *createvector 1 $dvx $dvy $dvz",
        "            eval *createmark elems 1 $source_shells",
        "            if {![catch {*meshdragelements2 1 1 $dd 1 0 0.0 0} _mcp_drag_err]} {",
        "                catch {update}; after 25",
        "                set after_drag [b_all]",
        "                set new_drag_elems [b_sub $after_drag $be]",
        "                set ne $new_drag_elems; set hc [b_hex $ne]; set fo [b_fit $ne $sid $fit_tol_ratio $cs]",
        "                set aspect_bad [b_aspect_bad $ne $drag_aspect_threshold]",
        "                if {[llength $ne]>0&&$hc==[llength $ne]&&$fo} {",
        "                    set hs 1; set one_layer_fallback 1; set final_layers 1; set final_aspect_bad $aspect_bad",
        "                    puts \"MCP_DRAG_ONE_LAYER_FALLBACK solid=$sid component=$dc layers=1 aspect_bad=$aspect_bad threshold=$drag_aspect_threshold\"",
        "                } else {",
        "                    b_del $ne",
        "                    puts \"MCP_DRAG_FAIL stage=one_layer_fallback_quality solid=$sid hex=$hc total=[llength $ne] fit=$fo\"",
        "                }",
        "            } else {",
        "                catch {update}; after 25",
        "                puts \"MCP_DRAG_FAIL stage=one_layer_fallback_error solid=$sid error=$_mcp_drag_err\"",
        "            }",
        "        } else {",
        "            b_del $ss",
        "            puts \"MCP_DRAG_FAIL stage=one_layer_fallback_non_quad_source solid=$sid\"",
        "        }",
        "    }",
        "    if {!$hs} {",
        "        if {[info exists source_shells]&&[llength $source_shells]>0} {b_del $source_shells}",
        "        puts \"MCP_DRAG_SKIP_TETRA solid=$sid reason=drag_failed removed_old_tetra_path=1\"",
        "    }",
        "    if {$hs} {puts \"MCP_DRAG_RESULT solid=$sid ok=1 layers=$final_layers aspect_bad=$final_aspect_bad threshold=$drag_aspect_threshold one_layer_fallback=$one_layer_fallback\"}",
        "    if {$hs&&[info exists source_shells]&&[llength $source_shells]>0} {b_del $source_shells}",
        "    catch {*endhistorystate \"MCP guarded drag hex batch s$sid\"}",
        "    incr completed_drag_solids",
        "    catch {update}",
        "    if {$pause_ms_after_each_solid > 0} {after $pause_ms_after_each_solid}",
        "}",
    ])
    if checkpoint_hm_path and checkpoint_every_n_solids > 0:
        lines[-3:-3] = [
            "    if {$checkpoint_hm_path ne \"\" && $checkpoint_every_n_solids > 0 && ($completed_drag_solids % $checkpoint_every_n_solids) == 0} {",
            "        puts \"MCP_DRAG_CHECKPOINT after_solids=$completed_drag_solids path=$checkpoint_hm_path\"",
            "        catch {*writefile \"$checkpoint_hm_path\" 1}",
            "    }",
        ]
    if output_hm_path:
        lines.append(f'*writefile "{_quote_tcl_path(output_hm_path)}" 1')

    return {
        "success": True,
        "script": _wrap_generated_tcl("generate_batched_drag_hex_tcl", "\n".join(lines)),
        "strategy": "Batched drag-hex: processes multiple solids in one script.",
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
    density: int = 160,
    density_min: int = 60,
    density_max: int | None = None,
    section_element_size_min: float = 0.2,
    section_element_size_max: float = 1.5,
    plane_tolerance: float = 0.02,
    retry_count: int = 1,
    section_edge_seed_counts: list[int] | None = None,
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
    if density_min <= 0:
        raise ValueError("density_min must be greater than 0.")
    if density_max is None:
        density_max = density
    if density_max <= 0:
        raise ValueError("density_max must be greater than 0.")
    density_min = int(density_min)
    density_max = int(density_max)
    if density_min > density_max:
        density_min, density_max = density_max, density_min
    if section_element_size_min <= 0 or section_element_size_max <= 0:
        raise ValueError("section_element_size_min/max must be greater than 0.")
    if section_element_size_min > section_element_size_max:
        section_element_size_min, section_element_size_max = section_element_size_max, section_element_size_min
    if plane_tolerance <= 0:
        raise ValueError("plane_tolerance must be greater than 0.")
    if retry_count < 0:
        raise ValueError("retry_count cannot be negative.")
    retry_count = min(int(retry_count), 2)
    if section_edge_seed_counts is None:
        section_edge_seed_counts = [100, 75]
    section_size_percentages = [int(value) for value in section_edge_seed_counts if int(value) > 0]
    section_size_percentages = section_size_percentages[:2]
    if not section_size_percentages:
        raise ValueError("section_edge_seed_counts must contain at least one positive integer.")
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
    size_factors_text = " ".join(f"{value / 100.0:.6g}" for value in section_size_percentages)
    lines = [
        "# HyperMesh MCP generated cut-section spin-hex script",
        "# Use for stepped/recessed revolved solids where an existing face is not a reliable section.",
        "# The spin axis point must lie on the true rotation axis; the split-plane point alone is not enough.",
        f'set target_component "{comp}"',
        f"set target_solid {int(solid_id)}",
        f"set elem_size {float(element_size)}",
        f"set spin_density_min {int(density_min)}",
        f"set spin_density_max {int(density_max)}",
        f"set section_size_min {float(section_element_size_min)}",
        f"set section_size_max {float(section_element_size_max)}",
        f"set plane_tol {float(plane_tolerance)}",
        f"set retry_count {int(retry_count)}",
        f"set section_size_factors {{{size_factors_text}}}",
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
        f'set spin_axis_key "{axis_key}"',
        "set mcp_ui_yield_ms 75",
        "proc mcp_mark_count {entity mark_id} {",
        "    if {[catch {hm_marklength $entity $mark_id} n]} {return 0}",
        "    return $n",
        "}",
        "proc mcp_all_surfs {} {",
        '    *createmark surfs 1 "all"',
        "    return [hm_getmark surfs 1]",
        "}",
        "proc mcp_all_solids {} {",
        '    *createmark solids 1 "all"',
        "    return [hm_getmark solids 1]",
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
        "proc mcp_ensure_component {comp color} {",
        "    if {[catch {hm_entityinfo exist comps $comp -byname} exists]} {set exists 0}",
        "    if {!$exists} {catch {*createentity comps name=$comp color=$color}}",
        "    return $comp",
        "}",
        "proc mcp_set_current_component {comp color} {",
        "    mcp_ensure_component $comp $color",
        "    if {[catch {*currentcollector components \"$comp\"} err]} {",
        "        puts \"MCP_SPIN_WARN currentcollector_failed component=$comp error=$err\"",
        "        return 0",
        "    }",
        "    return 1",
        "}",
        "proc mcp_hex8_count {elems} {",
        "    set hexes 0",
        "    foreach eid $elems {",
        "        if {[catch {hm_getvalue elems id=$eid dataname=config} cfg]} {continue}",
        "        if {$cfg == 208} {incr hexes}",
        "    }",
        "    return $hexes",
        "}",
        "proc mcp_quad_shell_count {elems} {",
        "    set quads 0",
        "    foreach eid $elems {",
        "        if {[catch {hm_getvalue elems id=$eid dataname=config} cfg]} {continue}",
        "        if {$cfg == 104 || $cfg == 108} {incr quads}",
        "    }",
        "    return $quads",
        "}",
        "proc mcp_nodes_in_elems {elems} {",
        "    array set seen {}",
        "    set nodes {}",
        "    foreach eid $elems {",
        "        if {[catch {hm_getvalue elems id=$eid dataname=nodes} elem_nodes]} {continue}",
        "        foreach nid $elem_nodes {",
        "            if {![info exists seen($nid)]} {set seen($nid) 1; lappend nodes $nid}",
        "        }",
        "    }",
        "    return $nodes",
        "}",
        "proc mcp_merge_coincident_nodes_in_elems {elems tol} {",
        "    if {[llength $elems] == 0 || $tol <= 0} {return 0}",
        "    array set keeper {}",
        "    set merged 0",
        "    set scale [expr {1.0 / $tol}]",
        "    foreach nid [mcp_nodes_in_elems $elems] {",
        "        if {[catch {hm_getvalue nodes id=$nid dataname=x} x]} {continue}",
        "        if {[catch {hm_getvalue nodes id=$nid dataname=y} y]} {continue}",
        "        if {[catch {hm_getvalue nodes id=$nid dataname=z} z]} {continue}",
        "        set key [format \"%d,%d,%d\" [expr {int(floor($x*$scale + 0.5))}] [expr {int(floor($y*$scale + 0.5))}] [expr {int(floor($z*$scale + 0.5))}]]",
        "        if {[info exists keeper($key)]} {",
        "            set keep $keeper($key)",
        "            if {$keep == $nid} {continue}",
        "            if {[catch {*replacenodes $nid $keep 1 0} replace_err]} {",
        "                puts \"MCP cut-section spin node_merge_warn move=$nid keep=$keep err=$replace_err\"",
        "            } else {",
        "                incr merged",
        "            }",
        "        } else {",
        "            set keeper($key) $nid",
        "        }",
        "    }",
        "    return $merged",
        "}",
        "proc mcp_node_plane_dist {nid nx ny nz px py pz} {",
        "    set x [hm_getvalue nodes id=$nid dataname=x]",
        "    set y [hm_getvalue nodes id=$nid dataname=y]",
        "    set z [hm_getvalue nodes id=$nid dataname=z]",
        "    set d [expr {$nx * ($x - $px) + $ny * ($y - $py) + $nz * ($z - $pz)}]",
        "    if {$d < 0} {set d [expr {-$d}]}",
        "    return $d",
        "}",
        "proc mcp_surfs_by_solids {solids} {",
        "    set out {}",
        "    foreach solid_id $solids {",
        "        *createmark surfaces 1 \"by solids\" $solid_id",
        "        set out [mcp_unique_append $out [hm_getmark surfaces 1]]",
        "    }",
        "    return [lsort -integer $out]",
        "}",
        "proc mcp_surface_bbox_plane_maxdist {sid nx ny nz px py pz} {",
        "    *createmark surfaces 2 $sid",
        "    if {[catch {hm_getboundingbox surfaces 2 0 0 0} bb] || [llength $bb] < 6} {",
        "        return 1.0e30",
        "    }",
        "    set x0 [lindex $bb 0]",
        "    set y0 [lindex $bb 1]",
        "    set z0 [lindex $bb 2]",
        "    set x1 [lindex $bb 3]",
        "    set y1 [lindex $bb 4]",
        "    set z1 [lindex $bb 5]",
        "    set maxdist 0.0",
        "    foreach x [list $x0 $x1] {",
        "        foreach y [list $y0 $y1] {",
        "            foreach z [list $z0 $z1] {",
        "                set d [expr {$nx * ($x - $px) + $ny * ($y - $py) + $nz * ($z - $pz)}]",
        "                if {$d < 0} {set d [expr {-$d}]}",
        "                if {$d > $maxdist} {set maxdist $d}",
        "            }",
        "        }",
        "    }",
        "    return $maxdist",
        "}",
        "proc mcp_section_size_info {sid requested_size section_min section_max} {",
        "    *createmark surfaces 2 $sid",
        "    if {[catch {hm_getboundingbox surfaces 2 0 0 0} bb] || [llength $bb] < 6} {",
        "        return [list $requested_size 0.0 0.0 0.0 1]",
        "    }",
        "    set dx [expr {abs([lindex $bb 3] - [lindex $bb 0])}]",
        "    set dy [expr {abs([lindex $bb 4] - [lindex $bb 1])}]",
        "    set dz [expr {abs([lindex $bb 5] - [lindex $bb 2])}]",
        "    set dims [lsort -real [list $dx $dy $dz]]",
        "    set minor [lindex $dims 1]",
        "    set major [lindex $dims 2]",
        "    set diag [expr {sqrt($dx*$dx + $dy*$dy + $dz*$dz)}]",
        "    set chosen [expr {min($requested_size, max($section_min, min($section_max, min($minor/3.0, $major/10.0))))}]",
        "    if {$chosen <= 0} {set chosen $requested_size}",
        "    set area_est [expr {$minor * $major}]",
        "    set min_shells [expr {int(ceil($area_est / max($chosen*$chosen*4.0, 1.0e-9)))}]",
        "    if {$min_shells < 2} {set min_shells 2}",
        "    if {$minor <= 3.0 && $major <= 6.0 && $min_shells > 4} {set min_shells 4}",
        "    if {$min_shells > 30} {set min_shells 30}",
        "    return [list $chosen $minor $major $diag $min_shells]",
        "}",
        "proc mcp_section_area_info {sid} {",
        "    *createmark surfaces 2 $sid",
        "    if {[catch {hm_getboundingbox surfaces 2 0 0 0} bb] || [llength $bb] < 6} {",
        "        return [list 0.0 0.0 0.0]",
        "    }",
        "    set dx [expr {abs([lindex $bb 3] - [lindex $bb 0])}]",
        "    set dy [expr {abs([lindex $bb 4] - [lindex $bb 1])}]",
        "    set dz [expr {abs([lindex $bb 5] - [lindex $bb 2])}]",
        "    set dims [lsort -real [list $dx $dy $dz]]",
        "    set minor [lindex $dims 1]",
        "    set major [lindex $dims 2]",
        "    set area [expr {$minor * $major}]",
        "    return [list $area $minor $major]",
        "}",
        "proc mcp_node_axis_radius {nid axis_key ax ay az} {",
        "    set x [hm_getvalue nodes id=$nid dataname=x]",
        "    set y [hm_getvalue nodes id=$nid dataname=y]",
        "    set z [hm_getvalue nodes id=$nid dataname=z]",
        "    if {$axis_key eq \"x\"} {",
        "        set d1 [expr {$y - $ay}]",
        "        set d2 [expr {$z - $az}]",
        "    } elseif {$axis_key eq \"y\"} {",
        "        set d1 [expr {$x - $ax}]",
        "        set d2 [expr {$z - $az}]",
        "    } else {",
        "        set d1 [expr {$x - $ax}]",
        "        set d2 [expr {$y - $ay}]",
        "    }",
        "    return [expr {sqrt($d1*$d1 + $d2*$d2)}]",
        "}",
        "proc mcp_spin_density_for_shells {shells axis_key ax ay az section_size min_density max_density} {",
        "    set max_radius 0.0",
        "    foreach eid $shells {",
        "        foreach nid [hm_getvalue elems id=$eid dataname=nodes] {",
        "            set r [mcp_node_axis_radius $nid $axis_key $ax $ay $az]",
        "            if {$r > $max_radius} {set max_radius $r}",
        "        }",
        "    }",
        "    if {$section_size <= 0} {set section_size 1.0}",
        "    set density_scale 0.85",
        "    set raw_unscaled [expr {int(ceil((2.0 * 3.141592653589793 * $max_radius) / $section_size))}]",
        "    set raw [expr {int(ceil($raw_unscaled * $density_scale))}]",
        "    set density $raw",
        "    set density_min [expr {int($min_density)}]",
        "    set density_max [expr {int($max_density)}]",
        "    if {$density_min < 1} {set density_min 1}",
        "    if {$density_max < 1} {set density_max 1}",
        "    if {$density_min > $density_max} {set tmp $density_min; set density_min $density_max; set density_max $tmp}",
        "    if {$density < $density_min} {set density $density_min}",
        "    if {$density > $density_max} {set density $density_max}",
        '    puts "MCP spin density radius=$max_radius section_size=$section_size scale=$density_scale raw_unscaled=$raw_unscaled min=$density_min max=$density_max raw=$raw chosen=$density"',
        "    return $density",
        "}",
        "proc mcp_ui_yield {{ms \"\"}} {",
        "    variable mcp_ui_yield_ms",
        "    if {$ms eq \"\"} {set ms $mcp_ui_yield_ms}",
        "    catch {update}",
        "    if {$ms > 0} {after $ms}",
        "}",
        "proc mcp_mesh_true_section {sid elem_size nx ny nz px py pz plane_tol} {",
        "    set size_info [mcp_section_size_info $sid $elem_size $::mcp_section_size_min $::mcp_section_size_max]",
        "    set base_size [lindex $size_info 0]",
        "    set section_minor [lindex $size_info 1]",
        "    set section_major [lindex $size_info 2]",
        "    set section_min_shells [lindex $size_info 4]",
        '    puts "MCP section size surface=$sid requested=$elem_size chosen=$base_size minor=$section_minor major=$section_major min_shells=$section_min_shells"',
        "    set mesh_modes {{1 5} {4 4}}",
        "    foreach mode_pair $mesh_modes {",
        "        set interactive_mode [lindex $mode_pair 0]",
        "        set face_mode [lindex $mode_pair 1]",
        "        foreach size_factor $::mcp_section_size_factors {",
        "            set local_size [expr {$base_size * $size_factor}]",
        "            if {$local_size < $::mcp_section_size_min} {set local_size $::mcp_section_size_min}",
        "            if {$local_size > $::mcp_section_size_max} {set local_size $::mcp_section_size_max}",
        "            if {[catch {",
        "                *createmark surfaces 1 $sid",
        "                catch {*setedgedensitylinkwithaspectratio -1}",
        "                *setedgedensitylink 1",
        "                *interactiveremeshsurf 1 $local_size $interactive_mode $face_mode 2 1 1",
        "                *set_meshfaceparams 0 $face_mode 1 0 0 1 0.5 1 1",
        "                *automesh 0 $face_mode 1",
        "                *storemeshtodatabase 1",
        "                *ameshclearsurface",
        "            } mesh_err]} {",
        '                puts "MCP rejected section surface=$sid mesh_mode=$face_mode size=$local_size automesh_error=$mesh_err"',
        "                catch {*ameshclearsurface}",
        "                mcp_ui_yield 25",
        "                continue",
        "            }",
        "            mcp_ui_yield",
        '            *createmark elems 1 "by surface" $sid',
        "            set shells [hm_getmark elems 1]",
        "            if {[llength $shells] == 0} {mcp_ui_yield 25; continue}",
        "            set quads 0",
        "            set maxdist 0.0",
        "            foreach eid $shells {",
        "                set cfg [hm_getvalue elems id=$eid dataname=config]",
        "                if {$cfg == 104 || $cfg == 108} {incr quads}",
        "                foreach nid [hm_getvalue elems id=$eid dataname=nodes] {",
        "                    set d [mcp_node_plane_dist $nid $nx $ny $nz $px $py $pz]",
        "                    if {$d > $maxdist} {set maxdist $d}",
        "                }",
        "            }",
        "            set enough_section_shells [expr {[llength $shells] >= $section_min_shells || ($quads >= 6 && $maxdist <= $plane_tol)}]",
        "            if {$enough_section_shells && $maxdist <= $plane_tol} {",
        "                set ::mcp_last_section_size $local_size",
        '                puts "MCP accepted true section surface=$sid mesh_mode=$face_mode size=$local_size shells=[llength $shells] quads=$quads min_shells=$section_min_shells maxdist=$maxdist plane_tol=$plane_tol shell_accept=$enough_section_shells"',
        "                mcp_ui_yield",
        "                return $shells",
        "            }",
        '            puts "MCP rejected section surface=$sid mesh_mode=$face_mode size=$local_size shells=[llength $shells] quads=$quads min_shells=$section_min_shells maxdist=$maxdist plane_tol=$plane_tol shell_accept=$enough_section_shells"',
        "            mcp_delete_elems $shells",
        "            mcp_ui_yield 25",
        "        }",
        "    }",
        "    return {}",
        "}",
        'catch {*beginhistorystate "MCP cut-section spin hex"}',
        'if {![mcp_set_current_component $target_component 7]} { puts "MCP_PT_FAIL solid=$target_solid currentcollector_failed component=$target_component"; return }',
        "set ::mcp_section_size_factors $section_size_factors",
        "set ::mcp_section_size_min $section_size_min",
        "set ::mcp_section_size_max $section_size_max",
        "set hex_success 0",
        "set final_hex_count 0",
        "set final_3d_count 0",
        "set final_source_shell_count 0",
        "set final_source_quad_count 0",
        "set final_source_surface -1",
        "set final_spin_density 0",
        "set final_section_size $elem_size",
        "set final_merged_nodes 0",
        "set final_split_solids $target_solid",
        "set ::mcp_last_section_size $elem_size",
        "if {$delete_existing_component_elements} {",
        '    *createmark elems 1 "by comp name" $target_component',
        "    if {[mcp_mark_count elems 1] > 0} {catch {*deletemark elems 1}}",
        "}",
        "mcp_ui_yield",
        "set before_surfs [mcp_all_surfs]",
        "set before_solids [mcp_all_solids]",
        "*createmark solids 1 $target_solid",
        "if {[mcp_mark_count solids 1] == 0} {",
        '    puts "MCP cut-section spin skipped: solid is missing."',
        "} else {",
        "    *createplane 1 $split_nx $split_ny $split_nz $split_px $split_py $split_pz",
        "    if {[catch {*body_splitmerge_with_plane solids 1 1} split_err]} {",
        '        puts "MCP cut-section split failed: $split_err"',
        "    } else {",
        "        mcp_ui_yield",
        "        set after_solids [mcp_all_solids]",
        "        set split_new_solids [lsort -integer [mcp_list_subtract $after_solids $before_solids]]",
        "        set final_split_solids [lsort -integer [mcp_unique_append [list $target_solid] $split_new_solids]]",
        '        puts "MCP cut-section split_solids=$final_split_solids new_solids=$split_new_solids"',
        "        set new_surfs [lsort -integer [mcp_list_subtract [mcp_all_surfs] $before_surfs]]",
        '        puts "MCP cut-section new_surfs=$new_surfs"',
        "        set split_all_surfs [mcp_surfs_by_solids $final_split_solids]",
        "        set candidate_surfs {}",
        "        set bbox_plane_tol [expr {max($plane_tol, $elem_size * 0.05)}]",
        "        set candidate_records {}",
        "        foreach surf_id $split_all_surfs {",
        "            set is_new_split_surface [expr {[llength $new_surfs] == 0 || [lsearch -exact $new_surfs $surf_id] >= 0}]",
        "            if {!$is_new_split_surface} {",
        "                puts \"MCP cut-section reject surface=$surf_id reason=preexisting_surface_not_new_split\"",
        "                continue",
        "            }",
        "            set bbox_plane_dist [mcp_surface_bbox_plane_maxdist $surf_id $split_nx $split_ny $split_nz $split_px $split_py $split_pz]",
        "            if {$bbox_plane_dist <= $bbox_plane_tol} {",
        "                set area_info [mcp_section_area_info $surf_id]",
        "                set area [lindex $area_info 0]",
        "                set minor [lindex $area_info 1]",
        "                set major [lindex $area_info 2]",
        "                lappend candidate_surfs $surf_id",
        "                lappend candidate_records [list $surf_id $area $minor $major $bbox_plane_dist]",
        "                puts \"MCP cut-section candidate surface=$surf_id bbox_plane_dist=$bbox_plane_dist tol=$bbox_plane_tol area=$area minor=$minor major=$major source=new_split_surface\"",
        "            } else {",
        "                puts \"MCP cut-section reject surface=$surf_id bbox_plane_dist=$bbox_plane_dist tol=$bbox_plane_tol reason=not_on_split_plane\"",
        "            }",
        "        }",
        "        if {[llength $candidate_surfs] > 1} {",
        "            set best_surf -1",
        "            set best_area -1.0",
        "            set best_major -1.0",
        "            set duplicate_section_surfs {}",
        "            foreach record $candidate_records {",
        "                set surf_id [lindex $record 0]",
        "                set area [lindex $record 1]",
        "                set major [lindex $record 3]",
        "                if {$area > $best_area || ($area == $best_area && $major > $best_major)} {",
        "                    if {$best_surf > 0} {lappend duplicate_section_surfs $best_surf}",
        "                    set best_surf $surf_id",
        "                    set best_area $area",
        "                    set best_major $major",
        "                } else {",
        "                    lappend duplicate_section_surfs $surf_id",
        "                }",
        "            }",
        "            set candidate_surfs [list $best_surf]",
        "            puts \"MCP cut-section selected_single_section_surface=$best_surf dropped_duplicate_section_surfs=$duplicate_section_surfs reason=largest_new_split_section area=$best_area major=$best_major\"",
        "        }",
        "        if {[llength $candidate_surfs] == 0} {",
        '            puts "MCP cut-section no split-plane section surfaces were found; spin fallback to tetra."',
        '            error "no split-plane section surfaces were found"',
        "        }",
        '        puts "MCP cut-section candidate_surfs=$candidate_surfs"',
        "        mcp_ui_yield",
        "        set attempt 0",
        "        while {$attempt <= $retry_count && !$hex_success} {",
        "            set attempt_size $elem_size",
        "            set effective_plane_tol [expr {max($plane_tol, $attempt_size * 0.05)}]",
        '            puts "MCP cut-section spin attempt=$attempt elem_size=$attempt_size"',
        "            set seed_shells {}",
        "            set seed_surface -1",
        "            foreach sid $candidate_surfs {",
        "                set shells [mcp_mesh_true_section $sid $attempt_size $split_nx $split_ny $split_nz $split_px $split_py $split_pz $effective_plane_tol]",
        "                if {[llength $shells] > 0} {",
        "                    set seed_shells $shells",
        "                    set seed_surface $sid",
        "                    break",
        "                }",
        "            }",
        "            if {[llength $seed_shells] == 0} {",
        '                puts "MCP cut-section spin attempt failed: no section mesh elements within plane tolerance were found."',
        "                incr attempt",
        "                continue",
        "            }",
        "            set before_elems [mcp_all_elems]",
        "            set final_source_shell_count [llength $seed_shells]",
        "            set final_source_quad_count [mcp_quad_shell_count $seed_shells]",
        "            set final_source_surface $seed_surface",
        '            puts "MCP cut-section selected_section_surface=$seed_surface source_shells=$final_source_shell_count source_quads=$final_source_quad_count"',
        "            if {$final_source_shell_count < 4 || $final_source_quad_count < 4} {",
        "                puts \"MCP cut-section spin attempt failed: section mesh too small shells=$final_source_shell_count quads=$final_source_quad_count min_shells=4 min_quads=4\"",
        "                mcp_delete_elems $seed_shells",
        "                incr attempt",
        "                continue",
        "            }",
        "            eval *createmark elems 1 $seed_shells",
        f"            *createplane 1 {snx} {sny} {snz} $axis_px $axis_py $axis_pz",
        "            set actual_spin_density [mcp_spin_density_for_shells $seed_shells $spin_axis_key $axis_px $axis_py $axis_pz $::mcp_last_section_size $spin_density_min $spin_density_max]",
        "            set final_spin_density $actual_spin_density",
        "            set final_section_size $::mcp_last_section_size",
        "            mcp_ui_yield",
        "            if {[catch {*meshspinelements2 1 1 360 $actual_spin_density 1 0.0 0} spin_err]} {",
        '                puts "MCP cut-section spin attempt failed: $spin_err"',
        "            } else {",
        "                mcp_ui_yield",
        "                set new_elems [mcp_list_subtract [mcp_all_elems] $before_elems]",
        "                if {[llength $new_elems] > 0} {",
        "                    eval *createmark elems 1 $new_elems",
        "                    catch {*movemark elems 1 $target_component}",
        "                    set merge_tol [expr {max(1.0e-6, min($::mcp_last_section_size * 1.0e-4, $plane_tol))}]",
        "                    set merged_nodes [mcp_merge_coincident_nodes_in_elems $new_elems $merge_tol]",
        "                    set final_merged_nodes $merged_nodes",
        "                    puts \"MCP cut-section spin node_merge new3d=[llength $new_elems] merged=$merged_nodes tol=$merge_tol\"",
        "                    mcp_ui_yield",
        "                    set hex_count [mcp_hex8_count $new_elems]",
        "                    set hex_success 1",
        "                    set final_hex_count $hex_count",
        "                    set final_3d_count [llength $new_elems]",
        '                    puts "MCP cut-section spin completed: total_3d=[llength $new_elems] hex8=$hex_count"',
        "                } else {",
        '                    puts "MCP cut-section spin invalid: new_elements=[llength $new_elems] hex8=$hex_count; cleaning and retrying/falling back."',
        "                    mcp_delete_elems $new_elems",
        "                }",
        "            }",
        "            mcp_delete_elems $seed_shells",
        "            mcp_ui_yield",
        "            incr attempt",
        "        }",
        "    }",
        "}",
        'if {!$hex_success} { puts "MCP cut-section spin failed: tetra fallback removed_old_tetra_path=1" }',
        'puts "MCP_SPIN_RESULT solid=$target_solid ok=$hex_success total3d=$final_3d_count hex8=$final_hex_count source_shells=$final_source_shell_count source_quads=$final_source_quad_count source_surface=$final_source_surface fallback_tetra=[expr {!$hex_success}] method=cutsection requested_size=$elem_size section_size=$final_section_size section_size_min=$section_size_min section_size_max=$section_size_max spin_density=$final_spin_density density_min=$spin_density_min density_max=$spin_density_max merged_nodes=$final_merged_nodes split_solids=<$final_split_solids>"',
        'catch {*endhistorystate "MCP cut-section spin hex"}',
    ]
    if output_hm_path:
        lines.append(f'*writefile "{_quote_tcl_path(output_hm_path)}" 1')

    return {
        "success": True,
        "script": _wrap_generated_tcl("generate_cutsection_spin_hex_tcl", "namespace eval ::mcp_meshing {\n" + "\n".join(lines) + "\n}\n"),
        "strategy": (
            "Generic cut-section spin: split the actual solid first, select one "
            "new cut-section surface, choose a local 2D size from that section, "
            "then spin the accepted section mesh."
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
    recommended_timeout = _recommended_timeout_from_script(script)
    if recommended_timeout and timeout_seconds < recommended_timeout:
        timeout_seconds = recommended_timeout
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
    recommended_timeout = _recommended_timeout_from_script(script)
    if recommended_timeout and timeout_seconds < recommended_timeout:
        timeout_seconds = recommended_timeout
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

    diag_id = uuid.uuid4().hex[:10]
    _append_gui_realtime_diag(
        "execute_tcl_gui_start",
        diag_id=diag_id,
        host=host,
        port=int(port),
        timeout_seconds=timeout_seconds,
        model_path=str(model) if model else None,
        output_hm_path=output_hm_path,
        **_script_diag_summary(gui_script),
    )
    try:
        result = _run_hypermesh_gui_script(
            script=gui_script,
            host=host,
            port=port,
            timeout_seconds=timeout_seconds,
        )
        _mark_phase2_finalized_from_result(result)
        _append_gui_realtime_diag(
            "execute_tcl_gui_done",
            diag_id=diag_id,
            reason=_gui_response_reason(result),
            success=result.get("success"),
            message=result.get("message"),
            error=result.get("error"),
            response_tail=str(result.get("response", "") or result.get("stdout", ""))[-2500:],
        )
        return result
    except OSError as exc:
        _append_gui_realtime_diag(
            "execute_tcl_gui_exception",
            diag_id=diag_id,
            reason=_gui_response_reason({"success": False, "error": str(exc)}),
            exception_type=type(exc).__name__,
            error=str(exc),
        )
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
def execute_tcl_gui_async(
    script: str,
    host: str = "127.0.0.1",
    port: int = DEFAULT_GUI_PORT,
    model_path: str | None = None,
    output_hm_path: str | None = None,
    log_path: str | None = None,
    enqueue_timeout_seconds: int = 10,
    enforce_meshing_rules: bool = True,
) -> dict[str, Any]:
    """Queue Tcl in the visible GUI listener and return immediately.

    Use this for long HyperMesh operations such as tetra generation. The GUI
    writes command output to log_path; poll with get_gui_async_job_status.
    """
    if not script.strip():
        raise ValueError("script cannot be empty.")
    if enforce_meshing_rules:
        violation = _meshing_rule_violation(script)
        if violation:
            violation["execution_mode"] = "visible_gui_async"
            return violation
        violation = _phase2_finalization_violation(script)
        if violation:
            violation["execution_mode"] = "visible_gui_async"
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

    job_id = f"job_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    log = _normalize_path(log_path) if log_path else (_ensure_runs_dir() / f"{job_id}.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    diag_id = uuid.uuid4().hex[:10]
    _append_gui_realtime_diag(
        "execute_tcl_gui_async_start",
        diag_id=diag_id,
        job_id=job_id,
        log_path=str(log),
        host=host,
        port=int(port),
        enqueue_timeout_seconds=enqueue_timeout_seconds,
        model_path=str(model) if model else None,
        output_hm_path=output_hm_path,
        **_script_diag_summary(gui_script),
    )

    try:
        result = _run_hypermesh_gui_script_async(
            script=gui_script,
            job_id=job_id,
            log_path=str(log),
            host=host,
            port=port,
            timeout_seconds=enqueue_timeout_seconds,
        )
        _append_gui_realtime_diag(
            "execute_tcl_gui_async_enqueued",
            diag_id=diag_id,
            job_id=job_id,
            log_path=str(log),
            reason=_gui_response_reason(result),
            success=result.get("success"),
            response_tail=str(result.get("response", ""))[-2500:],
        )
        return {
            "success": result.get("success", False),
            "queued": result.get("success", False),
            "job_id": job_id,
            "log_path": str(log),
            "host": host,
            "port": int(port),
            "enqueue_response": result.get("response", ""),
            "next_step": "Call get_gui_async_job_status with this job_id/log_path.",
        }
    except OSError as exc:
        _append_gui_realtime_diag(
            "execute_tcl_gui_async_exception",
            diag_id=diag_id,
            job_id=job_id,
            log_path=str(log),
            reason=_gui_response_reason({"success": False, "error": str(exc)}),
            exception_type=type(exc).__name__,
            error=str(exc),
        )
        return {
            "success": False,
            "queued": False,
            "job_id": job_id,
            "log_path": str(log),
            "host": host,
            "port": int(port),
            "message": (
                "Could not connect to the visible HyperMesh GUI listener. "
                "Re-source the Tcl file returned by create_gui_listener_tcl."
            ),
            "error": str(exc),
        }


@mcp.tool()
def get_gui_async_job_status(
    job_id: str,
    log_path: str | None = None,
    host: str = "127.0.0.1",
    port: int = DEFAULT_GUI_PORT,
    tail_chars: int = 8000,
) -> dict[str, Any]:
    """Read async GUI job status and log tail."""
    log = _normalize_path(log_path) if log_path else None
    status_script = f"""
set _mcp_job "{job_id}"
set _mcp_status "unknown"
set _mcp_log ""
if {{[info exists ::mcp_async_status($_mcp_job)]}} {{set _mcp_status $::mcp_async_status($_mcp_job)}}
if {{[info exists ::mcp_async_log($_mcp_job)]}} {{set _mcp_log $::mcp_async_log($_mcp_job)}}
puts "MCP_ASYNC_STATUS job=$_mcp_job status=$_mcp_status log=$_mcp_log"
""".lstrip()
    gui_result = execute_tcl_gui(
        script=status_script,
        host=host,
        port=port,
        timeout_seconds=10,
        enforce_meshing_rules=False,
    )

    log_text = ""
    resolved_log = log
    if not resolved_log and gui_result.get("response"):
        import re
        match = re.search(r"log=([^\r\n]+)", str(gui_result.get("response", "")))
        if match:
            resolved_log = _normalize_path(match.group(1).strip())
    if resolved_log and resolved_log.exists():
        text = resolved_log.read_text(encoding="utf-8", errors="replace")
        log_text = text[-max(1, int(tail_chars)):]

    status = "unknown"
    if "MCP_ASYNC_OK" in log_text:
        status = "ok"
    elif "MCP_ASYNC_ERROR" in log_text:
        status = "error"
    elif "MCP_ASYNC_START" in log_text:
        status = "running"
    elif "MCP_ASYNC_QUEUED" in log_text:
        status = "queued"

    _append_gui_realtime_diag(
        "get_gui_async_job_status",
        job_id=job_id,
        status=status,
        log_path=str(resolved_log) if resolved_log else None,
        gui_reason=_gui_response_reason(gui_result),
        log_tail=log_text[-2500:],
    )

    return {
        "success": gui_result.get("success", False) or bool(log_text),
        "job_id": job_id,
        "status": status,
        "log_path": str(resolved_log) if resolved_log else None,
        "gui_status_response": gui_result.get("response", ""),
        "log_tail": log_text,
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


def _mcp_fmt_int(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except Exception:
        return str(value)


def _mcp_parse_final_counts(response: str | None) -> dict[str, int]:
    import re

    text = str(response or "")
    matches = re.findall(
        r"MCP_FINAL_COUNTS\s+elems=(\d+)\s+shells=(\d+)\s+tet4=(\d+)\s+hex8=(\d+)\s+other=(\d+)",
        text,
    )
    if not matches:
        return {"elems": 0, "shells": 0, "tet4": 0, "hex8": 0, "other": 0}
    elems, shells, tet4, hex8, other = matches[-1]
    return {
        "elems": int(elems),
        "shells": int(shells),
        "tet4": int(tet4),
        "hex8": int(hex8),
        "other": int(other),
    }


def build_chinese_meshing_workflow_report(report_data: dict[str, Any]) -> str:
    """Build a Chinese plain-text meshing report shared by MCP and offline runner."""
    stamp = report_data.get("stamp", "")
    output_hm_path = report_data.get("output_hm_path", "")
    success = bool(report_data.get("success", False))
    classification = report_data.get("classification", {}) if isinstance(report_data.get("classification"), dict) else {}
    strategy_counts = classification.get("strategy_counts", {}) if isinstance(classification.get("strategy_counts"), dict) else {}
    total_solids = int(classification.get("total_solids") or report_data.get("total_solids") or 0)
    drag_count = int(strategy_counts.get("drag_hex") or report_data.get("drag_count") or 0)
    spin_count = int(strategy_counts.get("spin_hex") or 0)
    tetra_count = (
        int(strategy_counts.get("tetra_plain") or 0)
        + int(strategy_counts.get("tetra_surface_deviation_rtrias") or 0)
        + int(strategy_counts.get("surface_tetra") or 0)
    )
    if tetra_count == 0:
        tetra_count = int(report_data.get("tetra_count") or 0)
    other_count = max(0, total_solids - drag_count - spin_count - tetra_count)

    drag_step = report_data.get("drag", {}) if isinstance(report_data.get("drag"), dict) else {}
    spin_step = report_data.get("spin", {}) if isinstance(report_data.get("spin"), dict) else {}
    tetra_step = report_data.get("tetra", {}) if isinstance(report_data.get("tetra"), dict) else {}
    final_save = report_data.get("final_save", {}) if isinstance(report_data.get("final_save"), dict) else {}
    final_counts = report_data.get("final_counts")
    if not isinstance(final_counts, dict):
        final_counts = _mcp_parse_final_counts(str(final_save.get("response", "")))

    repair_summary = report_data.get("repair_summary", {}) if isinstance(report_data.get("repair_summary"), dict) else {}
    repair_agg = repair_summary.get("repair_aggregate", {}) if isinstance(repair_summary.get("repair_aggregate"), dict) else {}
    repair_by_solid = repair_summary.get("repair_by_solid", {}) if isinstance(repair_summary.get("repair_by_solid"), dict) else {}
    errors = report_data.get("errors", []) if isinstance(report_data.get("errors", []), list) else []
    generated_files = report_data.get("generated_files", {}) if isinstance(report_data.get("generated_files"), dict) else {}
    parameters = report_data.get("parameters", {}) if isinstance(report_data.get("parameters"), dict) else {}
    part_parameters = report_data.get("part_parameters", []) if isinstance(report_data.get("part_parameters"), list) else []
    skipped_existing_mesh = (
        report_data.get("skipped_existing_mesh", {})
        if isinstance(report_data.get("skipped_existing_mesh"), dict)
        else {}
    )

    def _extreme_surface_aspect(info: dict[str, Any], suffix: str = "count") -> Any:
        return info.get(f"extreme_surface_aspect_{suffix}", info.get(f"surface_aspect_over_100_{suffix}"))

    def _extreme_surface_aspect_agg(suffix: str = "count") -> Any:
        return repair_agg.get(f"extreme_surface_aspect_{suffix}", repair_agg.get(f"surface_aspect_over_100_{suffix}"))

    lines: list[str] = []
    lines.append("HyperMesh 网格划分报告")
    lines.append("=" * 28)
    if stamp:
        lines.append(f"运行时间戳：{stamp}")
    lines.append(f"流程结果：{'成功' if success else '存在失败或警告'}")
    if output_hm_path:
        lines.append(f"最终模型：{output_hm_path}")
    lines.append("")

    initial_extreme_aspect_total = int(_extreme_surface_aspect_agg("initial_count") or 0)
    extreme_aspect_total = int(_extreme_surface_aspect_agg("count") or 0)
    if initial_extreme_aspect_total > 0 or extreme_aspect_total > 0:
        extreme_aspect_solids = {
            sid: info for sid, info in repair_by_solid.items()
            if int(_extreme_surface_aspect(info, "initial_count") or 0) > 0
            or int(_extreme_surface_aspect(info, "count") or 0) > 0
        }
        lines.append("!!! 严重 2D Aspect 警告 !!!")
        lines.append("-" * 28)
        lines.append(
            f"检测到 aspect > {TETRA_FATAL_SURFACE_ASPECT:g} 的 2D 面单元；这些极端单元未参与自动 2D 修复，"
            "其余普通不合格 2D 单元已继续按原流程修复。"
        )
        lines.append(
            f"涉及实体数量：{_mcp_fmt_int(len(extreme_aspect_solids))}；"
            f"初始极端面单元数量：{_mcp_fmt_int(initial_extreme_aspect_total)}；"
            f"当前保留极端面单元数量：{_mcp_fmt_int(extreme_aspect_total)}"
        )
        for sid in sorted(extreme_aspect_solids, key=lambda value: int(value)):
            info = extreme_aspect_solids[sid]
            lines.append(
                f"  - solid {sid}: 初始 aspect>{TETRA_FATAL_SURFACE_ASPECT:g}="
                f"{_mcp_fmt_int(_extreme_surface_aspect(info, 'initial_count') or 0)}, "
                f"当前 aspect>{TETRA_FATAL_SURFACE_ASPECT:g}="
                f"{_mcp_fmt_int(_extreme_surface_aspect(info, 'count') or 0)}, "
                f"阈值={_extreme_surface_aspect(info, 'threshold') or TETRA_FATAL_SURFACE_ASPECT}"
            )
        lines.append("")

    lines.append("一、模型识别和分类")
    lines.append("-" * 28)
    lines.append(f"检测到的实体数量：{_mcp_fmt_int(total_solids)}")
    lines.append(f"分类为 drag 六面体的实体数量：{_mcp_fmt_int(drag_count)}")
    lines.append(f"分类为 spin 六面体的实体数量：{_mcp_fmt_int(spin_count)}")
    lines.append(f"分类为 tetra 四面体的实体数量：{_mcp_fmt_int(tetra_count)}")
    if other_count:
        lines.append(f"其他策略实体数量：{_mcp_fmt_int(other_count)}")
    if strategy_counts:
        lines.append("分类明细：")
        for name, count in sorted(strategy_counts.items()):
            lines.append(f"  - {name}: {_mcp_fmt_int(count)}")
    if skipped_existing_mesh:
        skipped_solids = skipped_existing_mesh.get("skipped_solids", []) or []
        lines.append(f"已有网格并跳过划分的实体数量：{_mcp_fmt_int(skipped_existing_mesh.get('skipped_count', len(skipped_solids)))}")
        for item in skipped_solids:
            lines.append(
                f"  - solid {item.get('solid_id', '')} "
                f"{item.get('component_name', '')}: 已有单元数量={_mcp_fmt_int(item.get('element_count', 0))}"
            )
    lines.append("")

    lines.append("二、网格生成结果")
    lines.append("-" * 28)
    lines.append(f"drag 六面体实体数量：{_mcp_fmt_int(drag_count)}")
    drag_one_layer = drag_step.get("one_layer_fallback", []) if isinstance(drag_step.get("one_layer_fallback"), list) else []
    if drag_one_layer:
        lines.append(f"drag 三层 aspect 检查后改为一层兜底的实体数量：{_mcp_fmt_int(len(drag_one_layer))}")
        for item in drag_one_layer:
            lines.append(
                f"  - solid {item.get('solid_id', '')} {item.get('component_name', '')}: "
                f"最终层数={item.get('layers', 1)}, "
                f"aspect>{item.get('aspect_threshold', 20)} 数量={item.get('aspect_bad', 0)}"
            )
    lines.append(f"spin 六面体尝试实体数量：{_mcp_fmt_int(spin_step.get('count', 0))}")
    lines.append(f"spin 六面体成功实体数量：{_mcp_fmt_int(len(spin_step.get('completed', []) or []))}")
    lines.append(f"spin 失败后转 tetra 实体数量：{_mcp_fmt_int(len(spin_step.get('fallback_to_tetra', []) or []) + len(spin_step.get('failed', []) or []))}")
    lines.append(f"tetra 批次数量：{_mcp_fmt_int(tetra_step.get('batch_count', 0))}")
    lines.append(f"tetra 成功批次：{_mcp_fmt_int(len(tetra_step.get('completed', []) or []))}")
    lines.append(f"tetra 失败批次：{_mcp_fmt_int(len(tetra_step.get('failed', []) or []))}")
    if repair_summary.get("tetra_attempted_count") is not None:
        lines.append(f"tetra 尝试实体数量：{_mcp_fmt_int(repair_summary.get('tetra_attempted_count', 0))}")
    lines.append(f"tetra 成功实体数量：{_mcp_fmt_int(repair_summary.get('tetra_done_count', 0))}")
    if repair_summary.get("tetra_failed_count") is not None:
        lines.append(f"tetra 失败或退回实体数量：{_mcp_fmt_int(repair_summary.get('tetra_failed_count', 0))}")
    lines.append(f"tet4 单元数量：{_mcp_fmt_int(repair_summary.get('tetra_tet4_total', final_counts.get('tet4', 0)))}")
    lines.append("")

    if parameters:
        lines.append("三、运行参数")
        lines.append("-" * 28)
        parameter_labels = (
            ("drag_element_size", "drag 六面体网格尺寸"),
            ("drag_element_size_min", "drag 六面体网格尺寸下限"),
            ("drag_element_size_max", "drag 六面体网格尺寸上限"),
            ("drag_fit_tolerance_ratio", "drag 贴合容差比例"),
            ("drag_retry_count", "drag 重试次数"),
            ("drag_aspect_guard", "drag 三层 aspect 检查"),
            ("drag_aspect_threshold", "drag hex aspect 阈值"),
            ("drag_min_layers", "drag 最少层数"),
            ("spin_element_size", "spin 截面 2D 请求尺寸"),
            ("spin_retry_count", "spin 重试次数"),
            ("spin_density_min", "spin 旋转份数下限"),
            ("spin_density_max", "spin 旋转份数上限"),
            ("spin_section_element_size_min", "spin 截面 2D 尺寸下限"),
            ("spin_section_element_size_max", "spin 截面 2D 尺寸上限"),
            ("tetra_element_size", "tetra 四面体网格尺寸"),
            ("tetra_element_size_min", "tetra 四面体目标尺寸下限"),
            ("tetra_element_size_max", "tetra 四面体目标尺寸上限"),
            ("tetra_min_element_size", "tetra 最小网格尺寸"),
            ("tetra_min_element_size_min", "tetra 最小网格尺寸下限"),
            ("tetra_min_element_size_max", "tetra 最小网格尺寸上限"),
            ("tetra_max_deviation", "tetra 最大偏差"),
            ("tetra_feature_angle", "tetra 特征角"),
            ("tetra_growth_rate", "tetra 增长率"),
            ("tetra_fit_tolerance_ratio", "tetra 贴合容差比例"),
            ("tetra_chord_dev_degrade_delta", "最大 chord dev 下降值"),
            ("tetra_target_vol_skew", "tetra 生成目标 vol skew"),
            ("tetra_repair_vol_skew", "tetra 修复后 vol skew 上限"),
            ("use_gear_tooth_refinement", "启用齿面加厚/加密模型"),
            ("gear_tooth_element_size", "齿面 tetra 目标尺寸"),
            ("gear_tooth_element_size_min", "齿面 tetra 目标尺寸下限"),
            ("gear_tooth_element_size_max", "齿面 tetra 目标尺寸上限"),
            ("gear_tooth_min_element_size", "齿面 tetra 最小尺寸"),
            ("gear_tooth_min_element_size_min", "齿面 tetra 最小尺寸下限"),
            ("gear_tooth_min_element_size_max", "齿面 tetra 最小尺寸上限"),
            ("gear_tooth_feature_angle", "齿面特征角"),
        )
        for key, label in parameter_labels:
            if key in parameters:
                lines.append(f"{label}：{parameters.get(key)}")
        lines.append("")

    if part_parameters:
        lines.append("四、按部件划分参数明细")
        lines.append("-" * 28)
        for item in sorted(part_parameters, key=lambda value: int(value.get("solid_id", 0) or 0)):
            sid = item.get("solid_id", "")
            comp = item.get("component_name", "")
            strategy = item.get("strategy", "")
            lines.append(f"solid {sid} | component={comp} | strategy={strategy}")
            if str(strategy).startswith("drag"):
                lines.append(
                    "  - 尺寸："
                    f"requested={item.get('requested_element_size', '未记录')}, "
                    f"actual={item.get('actual_element_size', '未记录')}, "
                    f"limit={item.get('element_size_min', '未记录')}..{item.get('element_size_max', '未记录')}"
                )
                lines.append(
                    "  - drag 参数："
                    f"axis={item.get('axis', '未记录')}, "
                    f"distance={item.get('drag_distance', '未记录')}, "
                    f"fit_tolerance_ratio={item.get('fit_tolerance_ratio', '未记录')}, "
                    f"retry_count={item.get('retry_count', '未记录')}"
                )
                if item.get("aspect_guard") is not None:
                    lines.append(
                        "  - drag aspect："
                        f"enabled={item.get('aspect_guard')}, "
                        f"threshold={item.get('aspect_threshold', '未记录')}, "
                        f"min_layers={item.get('min_layers', '未记录')}, "
                        f"actual_layers={item.get('actual_layers', '未记录')}, "
                        f"aspect_bad={item.get('aspect_bad', '未记录')}, "
                        f"one_layer_fallback={item.get('one_layer_fallback', False)}"
                    )
            elif str(strategy).startswith("spin"):
                lines.append(
                    "  - 截面 2D 尺寸："
                    f"requested={item.get('requested_element_size', '未记录')}, "
                    f"actual={item.get('actual_element_size', '未记录')}, "
                    f"limit={item.get('section_element_size_min', '未记录')}..{item.get('section_element_size_max', '未记录')}"
                )
                lines.append(
                    "  - spin 参数："
                    f"axis={item.get('axis', '未记录')}, "
                    f"method={item.get('method', '未记录')}, "
                    f"density_limit={item.get('requested_spin_density_min', '未记录')}..{item.get('requested_spin_density_max', '未记录')}, "
                    f"actual_density={item.get('actual_spin_density', '未记录')}, "
                    f"retry_count={item.get('retry_count', '未记录')}"
                )
                lines.append(
                    "  - spin 结果："
                    f"source_surface={item.get('source_surface_id', '未记录')}, "
                    f"source_shells={item.get('source_shells', '未记录')}, "
                    f"source_quads={item.get('source_quads', '未记录')}, "
                    f"total3d={item.get('total3d', '未记录')}, "
                    f"hex8={item.get('hex8', '未记录')}, "
                    f"merged_nodes={item.get('merged_nodes', '未记录')}, "
                    f"fallback_tetra={item.get('fallback_tetra', False)}"
                )
            else:
                lines.append(
                    "  - 目标尺寸："
                    f"requested={item.get('requested_element_size', '未记录')}, "
                    f"actual={item.get('actual_element_size', '未记录')}, "
                    f"limit={item.get('element_size_min', '未记录')}..{item.get('element_size_max', '未记录')}"
                )
                lines.append(
                    "  - 最小尺寸："
                    f"requested={item.get('requested_min_element_size', '未记录')}, "
                    f"actual={item.get('actual_min_element_size', '未记录')}, "
                    f"limit={item.get('min_element_size_min', '未记录')}..{item.get('min_element_size_max', '未记录')}"
                )
                lines.append(
                    "  - tetra 参数："
                    f"surf_count={item.get('surf_count', '未记录')}, "
                    f"max_deviation={item.get('max_deviation', '未记录')}, "
                    f"feature_angle={item.get('feature_angle', '未记录')}, "
                    f"growth_rate={item.get('growth_rate', '未记录')}, "
                    f"fit_tolerance_ratio={item.get('fit_tolerance_ratio', '未记录')}, "
                    f"chord_dev_degrade_delta={item.get('chord_dev_degrade_delta', '未记录')}, "
                    f"target_vol_skew={item.get('target_vol_skew', '未记录')}, "
                    f"repair_vol_skew={item.get('repair_vol_skew', '未记录')}"
                )
                if strategy == "gear_aware_tetra":
                    tooth_ids = item.get("gear_tooth_surface_ids", []) or []
                    if isinstance(tooth_ids, (list, tuple)):
                        tooth_text = ",".join(str(value) for value in tooth_ids)
                    else:
                        tooth_text = str(tooth_ids)
                    if not tooth_text:
                        tooth_text = "未识别"
                    lines.append(
                        "  - gear 齿面候选："
                        f"axis={item.get('gear_axis', '未记录')}, "
                        f"surface_count={item.get('gear_tooth_surface_count', 0)}, "
                        f"surface_ids={tooth_text}"
                    )
                    lines.append(
                        "  - gear 齿面加密参数："
                        f"enabled={item.get('use_gear_tooth_refinement', '未记录')}, "
                        f"target={item.get('gear_tooth_element_size', '未记录')}, "
                        f"target_limit={item.get('gear_tooth_element_size_min', '未记录')}..{item.get('gear_tooth_element_size_max', '未记录')}, "
                        f"min={item.get('gear_tooth_min_element_size', '未记录')}, "
                        f"min_limit={item.get('gear_tooth_min_element_size_min', '未记录')}..{item.get('gear_tooth_min_element_size_max', '未记录')}, "
                        f"feature_angle={item.get('gear_tooth_feature_angle', '未记录')}"
                    )
        lines.append("")

    lines.append("五、最终质量统计")
    lines.append("-" * 28)
    lines.append(f"总单元数量：{_mcp_fmt_int(final_counts.get('elems', 0))}")
    lines.append(f"残留 shell 面单元数量：{_mcp_fmt_int(final_counts.get('shells', 0))}")
    lines.append(f"tet4 四面体数量：{_mcp_fmt_int(final_counts.get('tet4', 0))}")
    lines.append(f"hex8 六面体数量：{_mcp_fmt_int(final_counts.get('hex8', 0))}")
    lines.append(f"其他单元数量：{_mcp_fmt_int(final_counts.get('other', 0))}")
    lines.append(f"最终保存状态：{'成功' if final_save.get('success') else '失败或未确认'}")
    lines.append("")

    lines.append("六、2D 面网格修复统计")
    lines.append("-" * 28)
    lines.append(f"初始普通 aspect 不合格三角形数量（10 < aspect <= {TETRA_FATAL_SURFACE_ASPECT:g}）：{_mcp_fmt_int(repair_agg.get('initial_bad', 0))}")
    lines.append(f"triangle_cleanup 修复数量：{_mcp_fmt_int(repair_agg.get('triangle_cleanup_repaired', 0))}")
    lines.append(f"smooth_5 修复数量：{_mcp_fmt_int(repair_agg.get('smooth_5_repaired', 0))}")
    lines.append(f"local_remesh 修复数量：{_mcp_fmt_int(repair_agg.get('local_remesh_repaired', 0))}")
    lines.append(f"replace_nodes 修复数量：{_mcp_fmt_int(repair_agg.get('replace_nodes_repaired', 0))}")
    lines.append(f"额外 replace_nodes 修复数量：{_mcp_fmt_int(repair_agg.get('replace_nodes_extra_repaired', 0))}")
    lines.append(
        "贴合度下降后的保守修复数量（不含 replace_nodes）："
        f"{_mcp_fmt_int(repair_agg.get('fit_degrade_final_no_replace_repaired', 0))}"
    )
    lines.append(f"replace_nodes 实际执行次数：{_mcp_fmt_int(repair_agg.get('replace_nodes_changed', 0))}")
    lines.append(f"额外 replace_nodes 实际执行次数：{_mcp_fmt_int(repair_agg.get('replace_nodes_extra_changed', 0))}")
    lines.append(f"replace_nodes 快速通道触发实体数量：{_mcp_fmt_int(repair_agg.get('replace_nodes_fast_path_count', 0))}")
    lines.append(f"replace_nodes 快速通道识别 sliver 数量：{_mcp_fmt_int(repair_agg.get('replace_nodes_fast_path_sliver_count', 0))}")
    lines.append(f"智能跳过低收益修复步骤次数：{_mcp_fmt_int(repair_agg.get('smart_repair_skip_count', 0))}")
    lines.append(f"local_remesh 新生成 shell 数量：{_mcp_fmt_int(repair_agg.get('local_remesh_new_shells', 0))}")
    lines.append(f"初始 aspect > {TETRA_FATAL_SURFACE_ASPECT:g} 面单元数量：{_mcp_fmt_int(_extreme_surface_aspect_agg('initial_count') or 0)}")
    lines.append(f"当前保留 aspect > {TETRA_FATAL_SURFACE_ASPECT:g} 面单元数量：{_mcp_fmt_int(_extreme_surface_aspect_agg('count') or 0)}")
    lines.append(f"最终剩余 aspect 不合格三角形总数（aspect > 10）：{_mcp_fmt_int(repair_agg.get('final_bad', 0))}")
    lines.append("")

    lines.append("七、3D 体网格修复统计")
    lines.append("-" * 28)
    lines.append(f"初始 vol skew 不合格体单元数量：{_mcp_fmt_int(repair_agg.get('vol_skew_initial_bad', 0))}")
    lines.append(f"solid_mesh_optimization 修复数量：{_mcp_fmt_int(repair_agg.get('vol_skew_solid_mesh_optimization_repaired', 0))}")
    lines.append(f"smooth_3 修复数量：{_mcp_fmt_int(repair_agg.get('vol_skew_smooth_3_repaired', 0))}")
    lines.append(f"smooth_8 修复数量：{_mcp_fmt_int(repair_agg.get('vol_skew_smooth_8_repaired', 0))}")
    lines.append(f"smooth_15 修复数量：{_mcp_fmt_int(repair_agg.get('vol_skew_smooth_15_repaired', 0))}")
    lines.append(f"最终剩余 vol skew 不合格体单元数量：{_mcp_fmt_int(repair_agg.get('vol_skew_final_bad', 0))}")
    lines.append(f"因 3D 质量失败退回面网格的实体数量：{_mcp_fmt_int(repair_agg.get('tetra_deleted_keep_surface_shells_count', 0))}")
    lines.append(
        "退回时记录保留 2D 面网格的实体数量："
        f"{_mcp_fmt_int(repair_agg.get('rollback_kept_surface_mesh_count', 0))}"
    )
    lines.append(f"退回前仍不合格体单元数量：{_mcp_fmt_int(repair_agg.get('bad_volume_elements_when_rolled_back', 0))}")
    lines.append(f"因防崩保护跳过 tetra 并保留 2D 面网格的实体数量：{_mcp_fmt_int(repair_agg.get('crash_guard_keep_surface_mesh_count', 0))}")
    lines.append(f"防崩保护触发时剩余 2D aspect 不合格单元数量：{_mcp_fmt_int(repair_agg.get('crash_guard_unfixed_aspect_count', 0))}")
    lines.append(f"因极端 2D aspect 跳过 tetra 并保留 2D 面网格的实体数量：{_mcp_fmt_int(repair_agg.get('extreme_aspect_keep_surface_mesh_count', 0))}")
    lines.append(f"极端 2D aspect 触发时剩余 2D aspect 不合格单元数量：{_mcp_fmt_int(repair_agg.get('extreme_aspect_unfixed_aspect_count', 0))}")
    lines.append(f"因 2D 修复后贴合度下降跳过 tetra 的实体数量：{_mcp_fmt_int(repair_agg.get('surface_fit_degraded_keep_surface_mesh_count', 0))}")
    lines.append(f"贴合度下降触发时剩余 2D aspect 不合格单元数量：{_mcp_fmt_int(repair_agg.get('surface_fit_degraded_unfixed_aspect_count', 0))}")
    lines.append(f"因 2D 修复超时跳过 tetra 的实体数量：{_mcp_fmt_int(repair_agg.get('surface_repair_timeout_keep_surface_mesh_count', 0))}")
    lines.append(f"2D 修复超时时剩余 2D aspect 不合格单元数量：{_mcp_fmt_int(repair_agg.get('surface_repair_timeout_unfixed_aspect_count', 0))}")
    lines.append("")

    lines.append("八、按实体修复过程")
    lines.append("-" * 28)
    active_repairs = {
        sid: info for sid, info in repair_by_solid.items()
        if (
            int(info.get("initial_bad") or 0) > 0
            or int(info.get("final_bad") or 0) > 0
            or int(_extreme_surface_aspect(info, "initial_count") or 0) > 0
            or int(_extreme_surface_aspect(info, "count") or 0) > 0
            or int(info.get("surface_fit_degraded_keep_surface_mesh") or 0) > 0
            or int(info.get("surface_repair_timeout_keep_surface_mesh") or 0) > 0
            or int(info.get("vol_skew_initial_bad") or 0) > 0
            or int(info.get("vol_skew_final_bad") or 0) > 0
        )
    }
    if not active_repairs:
        lines.append("没有检测到需要记录的 2D aspect 修复过程。")
    else:
        for sid in sorted(active_repairs, key=lambda value: int(value)):
            info = active_repairs[sid]
            lines.append(
                f"solid {sid}: 初始普通不合格={_mcp_fmt_int(info.get('initial_bad', 0))}, "
                f"最终不合格总数={_mcp_fmt_int(info.get('final_bad', 0))}"
            )
            if int(_extreme_surface_aspect(info, "initial_count") or 0) > 0 or int(_extreme_surface_aspect(info, "count") or 0) > 0:
                lines.append(
                    "  - 严重 2D aspect 警告："
                    f"初始 aspect>{TETRA_FATAL_SURFACE_ASPECT:g}="
                    f"{_mcp_fmt_int(_extreme_surface_aspect(info, 'initial_count') or 0)}，"
                    f"当前 aspect>{TETRA_FATAL_SURFACE_ASPECT:g}="
                    f"{_mcp_fmt_int(_extreme_surface_aspect(info, 'count') or 0)}；"
                    "这些单元未作为自动修复目标。"
                )
            for step, label in (
                ("triangle_cleanup", "triangle_cleanup"),
                ("smooth_5", "smooth_5"),
                ("local_remesh", "local_remesh"),
                ("replace_nodes", "replace_nodes"),
                ("replace_nodes_extra", "replace_nodes_extra"),
                ("fit_degrade_final_no_replace", "fit_degrade_final_no_replace"),
            ):
                value = info.get(step)
                if isinstance(value, dict):
                    lines.append(
                        f"  - {label}: before={_mcp_fmt_int(value.get('before', value.get('before_count', 0)))}, "
                        f"after={_mcp_fmt_int(value.get('after', '未记录'))}, "
                        f"repaired={_mcp_fmt_int(value.get('repaired', 0))}"
                    )
            if info.get("replace_nodes_changed") is not None:
                lines.append(f"  - replace_nodes_changed={_mcp_fmt_int(info.get('replace_nodes_changed'))}")
            if info.get("replace_nodes_extra_changed") is not None:
                lines.append(f"  - replace_nodes_extra_changed={_mcp_fmt_int(info.get('replace_nodes_extra_changed'))}")
            if isinstance(info.get("replace_nodes_fast_path"), dict):
                value = info.get("replace_nodes_fast_path")
                lines.append(
                    f"  - replace_nodes 快速通道：sliver={_mcp_fmt_int(value.get('sliver_count', 0))}, "
                    f"bad_total={_mcp_fmt_int(value.get('total', 0))}"
                )
            if isinstance(info.get("smart_repair_skips"), list):
                for value in info.get("smart_repair_skips"):
                    lines.append(
                        f"  - 智能跳过：{value.get('skip', '')}, reason={value.get('reason', '')}"
                    )
            if info.get("local_remesh_new_shells") is not None:
                lines.append(f"  - local_remesh_new_shells={_mcp_fmt_int(info.get('local_remesh_new_shells'))}")
            if int(info.get("surface_repair_timeout_keep_surface_mesh") or 0) > 0:
                timeout_seconds = float(info.get("surface_repair_timeout_ms") or 0) / 1000.0
                lines.append(
                    "  - 2D 修复超时保护："
                    f"timeout={timeout_seconds:g}s，"
                    f"shell_count={_mcp_fmt_int(info.get('surface_repair_timeout_shell_count', 0))}，"
                    "已保留当前 2D 面网格并跳过 tetra。"
                )
            if info.get("vol_skew_initial_bad") is not None or info.get("vol_skew_final_bad") is not None:
                lines.append(
                    f"  - 3D vol skew: 初始不合格={_mcp_fmt_int(info.get('vol_skew_initial_bad', 0))}, "
                    f"最终不合格={_mcp_fmt_int(info.get('vol_skew_final_bad', 0))}, "
                    f"threshold={info.get('vol_skew_threshold', '未记录')}"
                )
            vol_repairs = info.get("vol_skew_repairs", {})
            if isinstance(vol_repairs, dict):
                for step in ("solid_mesh_optimization", "smooth_3", "smooth_8", "smooth_15"):
                    value = vol_repairs.get(step)
                    if isinstance(value, dict):
                        lines.append(
                            f"  - 3D {step}: before={_mcp_fmt_int(value.get('before', value.get('before_count', 0)))}, "
                            f"after={_mcp_fmt_int(value.get('after', '未记录'))}, "
                            f"repaired={_mcp_fmt_int(value.get('repaired', 0))}"
                        )
            if int(info.get("tetra_deleted_keep_surface_shells") or 0) > 0:
                lines.append("  - 结果：3D 质量修复后仍有不合格体单元，已删除 tetra 体网格并保留 2D 面网格。")
                if int(info.get("rollback_kept_surface_mesh") or 0) > 0:
                    lines.append(
                        "  - 回退保留：已删除 tetra，并保留当前 2D 面网格，shell_count="
                        f"{_mcp_fmt_int(info.get('rollback_surface_shell_count', info.get('pre_tetra_surface_shell_count', 0)))}"
                    )
                if info.get("bad_volume_elements_when_rolled_back") is not None:
                    lines.append(
                        "  - 退回前仍不合格体单元数量="
                        f"{_mcp_fmt_int(info.get('bad_volume_elements_when_rolled_back'))}"
                    )
            if int(info.get("crash_guard_keep_surface_mesh") or 0) > 0:
                lines.append(
                    "  - 结果：触发 tetra 防崩保护，未进入 3D tetra，已保留修复后的 2D 面网格。"
                )
                lines.append(
                    f"  - 防崩保护：shell_count={_mcp_fmt_int(info.get('crash_guard_shell_count', 0))}, "
                    f"limit={_mcp_fmt_int(info.get('crash_guard_limit', 0))}, "
                    f"剩余 aspect 不合格={_mcp_fmt_int(info.get('final_bad', 0))}, "
                    f"其中极端 aspect>{TETRA_FATAL_SURFACE_ASPECT:g}="
                    f"{_mcp_fmt_int(_extreme_surface_aspect(info, 'count') or 0)}"
                )
            if int(info.get("extreme_aspect_keep_surface_mesh") or 0) > 0:
                lines.append(
                    "  - 结果：修复后仍有极端 2D aspect 单元，未进入 3D tetra，已保留修复后的 2D 面网格。"
                )
                lines.append(
                    f"  - 极端 2D aspect：shell_count={_mcp_fmt_int(info.get('extreme_aspect_shell_count', 0))}, "
                    f"threshold={_extreme_surface_aspect(info, 'threshold') or TETRA_FATAL_SURFACE_ASPECT}, "
                    f"剩余 aspect 不合格={_mcp_fmt_int(info.get('final_bad', 0))}, "
                    f"其中极端={_mcp_fmt_int(_extreme_surface_aspect(info, 'count') or 0)}"
                )
            if int(info.get("surface_fit_degraded_keep_surface_mesh") or 0) > 0:
                lines.append(
                    "  - 结果：2D 修复后贴合度明显下降，已追加一次不含 replace_nodes 的保守修复；"
                    "仍不满足贴合度要求，未进入 3D tetra。"
                )
                reason = str(info.get("surface_fit_degrade_reason") or "")
                detail = (
                    f"  - 最大 chord dev：before={info.get('surface_chord_dev_max_before', 0)}, "
                    f"after={info.get('surface_chord_dev_max_after', 0)}, "
                    f"delta={info.get('surface_chord_dev_delta', 0)}, "
                    f"limit={info.get('surface_chord_dev_delta_limit', 0)}"
                )
                if "bbox" in reason:
                    detail += (
                        f"，bbox_before={info.get('surface_fit_before', 0)}, "
                        f"bbox_after={info.get('surface_fit_after', 0)}"
                    )
                detail += (
                    f"，shell_count={_mcp_fmt_int(info.get('surface_fit_shell_count', 0))}, "
                    f"剩余 aspect 不合格={_mcp_fmt_int(info.get('final_bad', 0))}"
                )
                lines.append(detail)
            if int(info.get("surface_repair_timeout_keep_surface_mesh") or 0) > 0:
                lines.append(
                    "  - 结果：2D 修复超过保护时间，未进入 3D tetra，已保留当前 2D 面网格。"
                )
                lines.append(
                    f"  - 2D 修复超时：timeout={_mcp_fmt_int(info.get('surface_repair_timeout_ms', 0))} ms, "
                    f"shell_count={_mcp_fmt_int(info.get('surface_repair_timeout_shell_count', 0))}, "
                    f"剩余 aspect 不合格={_mcp_fmt_int(info.get('final_bad', 0))}"
                )
    lines.append("")

    lines.append("九、失败和异常记录")
    lines.append("-" * 28)
    if not errors:
        lines.append("没有记录到失败批次。")
    else:
        for item in errors:
            if item.get("status") == "rolled_back_to_surface_mesh":
                lines.append(
                    "- tetra 质量失败："
                    f"{_mcp_fmt_int(item.get('solid_count', 0))} 个实体已退回到 2D 面网格；"
                    f"退回前不合格体单元数={_mcp_fmt_int(item.get('bad_volume_elements', 0))}"
                )
            elif item.get("status") == "crash_guard_keep_surface_mesh":
                lines.append(
                    "- tetra 防崩保护："
                    f"{_mcp_fmt_int(item.get('solid_count', 0))} 个实体未进入 tetra，"
                    f"已保留修复后的 2D 面网格；剩余 aspect 不合格数="
                    f"{_mcp_fmt_int(item.get('unfixed_aspect', 0))}"
                )
            elif item.get("status") == "surface_repair_timeout_keep_surface_mesh":
                lines.append(
                    "- 2D 修复超时保护："
                    f"{_mcp_fmt_int(item.get('solid_count', 0))} 个实体未进入 tetra，"
                    f"已保留当前 2D 面网格；剩余 aspect 不合格数="
                    f"{_mcp_fmt_int(item.get('unfixed_aspect', 0))}"
                )
            else:
                lines.append(f"- step={item.get('step', 'unknown')} batch={item.get('batch', '')} status={item.get('status', '')} solids={item.get('solid_ids', '')}")
    lines.append("")

    if generated_files:
        lines.append("十、相关文件")
        lines.append("-" * 28)
        for name, path in generated_files.items():
            if path:
                lines.append(f"{name}：{path}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


@mcp.tool()
def write_chinese_meshing_workflow_report(
    report_data: dict[str, Any],
    output_path: str | None = None,
) -> dict[str, Any]:
    """Write one Chinese TXT report for a meshing workflow run."""
    text = build_chinese_meshing_workflow_report(report_data)
    if output_path:
        path = _normalize_path(output_path)
    else:
        path = _ensure_runs_dir() / f"meshing_report_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {
        "success": True,
        "report_path": str(path),
        "report_text": text,
    }


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
