# HyperMesh MCP Server

Local MCP server for driving Altair HyperMesh with generated Tcl scripts.

This README is agent-facing. Treat it as the current operating contract before
calling tools, generating Tcl, or editing the workflow.

The MCP is intentionally geometry-rule based. It does not assign mesh strategies
by hard-coded component names from one model.

## Execution Modes

- Batch mode: run Tcl through `hmbatch.exe`.
- Visible GUI mode: open HyperMesh, source the generated GUI listener Tcl, then
  send Tcl into the visible session with `execute_tcl_gui`.

Visible GUI mode only changes where Tcl is executed. Strategy selection, Tcl
generation, and input/output paths remain explicit.

Raw meshing Tcl is guarded by default. `execute_tcl` and `execute_tcl_gui` reject
direct meshing commands such as `*meshdragelements*`, `*set_meshedgeparams`,
`*meshspinelements*`, `*defaultmeshsurf_growth`, and `*tetmesh` unless the script
was produced by one of the MCP strategy generators. This prevents agents from
bypassing balanced drag seeding, cut-section validation, and gear-local
refinement rules.

Generated scripts from trusted MCP generators are allowed deterministically when
their `MCP_SCRIPT_BEGIN` / `MCP_SCRIPT_END` markers are intact. For example,
`generate_batched_drag_hex_tcl` is always allowed to contain
`*meshdragelements2` inside its generated block.

## Main Tools

- `locate_hypermesh`: find candidate HyperMesh batch and GUI executables.
- `check_hypermesh_connection`: verify batch startup.
- `create_gui_listener_tcl`: create a Tcl listener for an already opened GUI.
- `start_hypermesh_gui_listener`: try to launch visible HyperMesh with the GUI listener.
- `execute_tcl`: run raw Tcl through batch mode.
- `execute_tcl_gui`: run raw Tcl in the visible GUI listener session.
- `get_hypermesh_meshing_strategy`: return generic meshing rules and workflows.
- `get_meshing_rules`: return structured generic tetra/drag/spin rules.
- `generate_geometry_probe_tcl`: generate a temporary coarse surface-mesh probe
  for all or selected solids; probe elements and nodes are deleted before return.
- `run_geometry_probe_gui`: run that temporary probe in the visible GUI and
  return `MCP_PROBE_SOLID` lines for per-object sizing/strategy planning.
- `recommend_tetra_sizes_from_probe_lines`: turn probe lines into per-solid
  tetra element-size recommendations, reducing size for thin/small bodies.
- `generate_phase2_finalize_tcl`: generate the mandatory Phase 2 rename/color
  Tcl from cached or supplied classification results.
- `generate_surface_automesh_tcl`: generate simple surface automesh Tcl.
- `generate_plain_tetra_tcl`: generate the only supported tetra path:
  surface-deviation R-trias, 2D fit/quality checks, tetra generation, and
  volume-quality repair. The 2D repair rollback checks both bbox fit and
  HyperMesh-native maximum chord dev increase.
- `generate_guarded_drag_hex_tcl`: generate guarded drag-hex Tcl.
- `get_cutsection_spin_workflow`: explain the generic cut-section spin workflow.
- `generate_cutsection_spin_hex_tcl`: generate cut-section spin Tcl for stepped or recessed revolved solids.

## Generic Strategy Rules

Use `classify_all_solids_from_probe` and geometry facts, not component names.
The automatic workflow is probe-driven and uses this conceptual order:

1. Probe all solids with temporary coarse surface mesh.
2. Classify by geometry facts: `gear_aware_tetra`, `drag_hex`, `spin_hex`,
   or `tetra_plain`.
3. Execute mandatory Phase 2 rename/color finalization before meshing.
4. Mesh drag solids first.
5. Mesh spin solids through the real cut-section workflow only.
6. Promote failed drag/spin solids to the explicit tetra phase.
7. Mesh tetra and gear-aware tetra solids in guarded batches.
8. Save once at the end and write reports/diagnostics.

Do not resurrect direct source-surface spin. Current `spin_hex` means:
split through the rotation axis, detect newly created cut-section surfaces,
mesh one true section, and spin that section. If no new cut-section surface is
created or no valid 3D hex mesh results, the solid falls back to tetra.

### Tetra

Use `tetra_surface_deviation_rtrias` for:

- flanges or flange-like bodies
- bodies with bolt holes, local holes, bosses, protrusions, ribs, grooves, cutouts, or non-sweepable topology
- ambiguous parts where a clean drag/spin source cannot be proven

Required checks:

- create 2D surface-deviation R-trias mesh first
- keep target size inside `element_size_min..element_size_max`
- keep min size inside `min_element_size_min..min_element_size_max`
- use HyperMesh native `*elementtestaspect` for 2D aspect checks, with coordinate
  fallback only if the native command fails
- use HyperMesh native `*elementtestchordaldeviation` for chordal deviation
- repair 2D before tetra, but skip high-risk repair modes when the shell mesh
  looks like overlap/broken geometry
- do not enter tetra when the repaired 2D mesh exceeds crash guards, contains
  extreme aspect, or fit/chordal deviation degraded badly
- copy repaired 2D shells to a temporary backup component before tetra
- call tetmesh without updating the shell input
- if 3D quality still fails after repair, delete tetra and restore/keep the
  repaired 2D shell mesh

### Phase 2 Finalization

After `classify_all_solids_from_probe`, Phase 2 is incomplete until components
are renamed and assigned their Phase 2 colors. Execute the returned
`phase2_finalize_script`, or call `generate_phase2_finalize_tcl` and execute its
script. The execution tools block generated meshing scripts after classification
until `MCP_FINALIZE_OK` has been observed.

### Drag Hex

Use guarded drag only for simple straight extrusions or tubes with constant
section.

Preconditions:

- a real source face exists at one end of the extrusion
- corresponding logical edge groups are forced to matched seed counts
- the source face meshes as 100% quads

When a constant-section part is tilted away from global X/Y/Z, the probe can
emit `axis_mode=oblique` with `axis_vec`, paired `target_surf`, and
`drag_distance_hint`. Batched drag uses that vector directly instead of forcing
the extrusion onto a global axis.

Pass `solid_id` when possible. The generator then validates that the generated
hex8 mesh bounding box fits the target solid. If the drag result is missing,
non-hex, or poorly fitted, it deletes invalid elements and retries. Tetra is no
longer hidden inside drag; it is handled only by the explicit tetra phase.

Drag hex sizing is bounded to `0.5..1.5` mm. The initial size is not based only
on extrusion thickness; it also considers the source-face bounding box and uses
the smallest of the requested size, `drag_distance/4`, `source_minor/3`, and
`source_major/8`.

For long drag runs, `generate_batched_drag_hex_tcl` inserts a short pause after
each solid and can checkpoint the `.hm` file every few solids. Tetra work should
be run explicitly in small batches after drag, reducing the chance of a
HyperMesh crash after many consecutive heavy operations.

After `classify_all_solids_from_probe`, use the returned `phase3_tetra_batches`
for tetra work. The batcher is model-agnostic: it preserves solid-id order but
isolates high-risk tetra solids into single-solid batches so they still run
without being placed back-to-back inside one heavy command sequence.

Use `generate_batched_plain_tetra_tcl` for tetra batches of up to four solids.
The generated script defines the shared tetra Tcl helpers once, then runs each
solid with its own size/component parameters. It also embeds
`MCP_RECOMMENDED_TIMEOUT_SECONDS`; `execute_tcl_gui` and `execute_tcl`
automatically honor that value when it is larger than the caller's timeout.

The batched drag workflow does not run hidden tetra fallback during drag batches.
If a drag source is invalid or drag fails, it reports `MCP_DRAG_SKIP_TETRA` and
leaves tetra work to the explicit tetra phase, where shell-count guards are
active.

Seed policy: if inner/outer preview counts or edge lengths differ greatly, pass
`preview_edge_seed_counts` or `source_edge_lengths`. When the largest/smallest
ratio is at least `seed_balance_ratio_threshold` (default 1.6), the generator
uses a balanced common count instead of forcing all source edges up to the
largest outer count.

Do not write naked Tcl with `*set_meshedgeparams` and `*meshdragelements*` for
drag workflows. The execution tools block that path by default. Use
`generate_guarded_drag_hex_tcl`; otherwise the balanced seed policy cannot be
applied.

### Spin Hex

Use `generate_cutsection_spin_hex_tcl` for all current spin work. There is no
separate supported direct-spin entry point in the workflow.

Workflow:

1. Split the actual solid with `*body_splitmerge_with_plane` using a middle plane.
2. Detect newly created surfaces from the split.
3. Temporarily mesh each new surface.
4. Accept a real section when enough shell nodes lie on the split plane. Mixed
   section meshes are allowed; the section is not required to be 100% quads.
5. Spin the accepted 2D section shells into 3D hex elements.
6. Delete only the temporary 2D seed shells.

Required inputs:

- `solid_id`
- `component_name`
- split plane normal and point
- spin axis and a point on the spin axis; this is required and must be on the
  real rotation axis, not merely any point on the split plane
- requested section element size, spin density min, and spin density max

`spin_axis` may be `x`, `y`, `z`, or `vector`. The `vector` mode is used for
oblique axes detected by the probe; radius checks then use node distance to the
arbitrary 3D axis instead of a global-coordinate shortcut.

The split plane must contain the spin axis. In practical terms, the split plane
normal should be nearly perpendicular to the spin axis. If the cut plane is
perpendicular to the axis and creates an annular transverse section, that is a
drag-style source section for a constant-section body, not a spin section.

The generator validates the spin result. If no valid 3D hex8 elements are
created, it deletes temporary section/invalid elements and retries up to the
configured retry count. If it still fails, the explicit tetra phase handles the
solid. It no longer considers existing pre-split solid surfaces as spin section
candidates; no new cut-section surface means tetra fallback.

### Gear-Aware Tetra

Use `classify_all_solids_from_probe` from geometry facts only. Do not classify
gear regions from component names, file names, or natural-language labels.

Gear recognition is based on repeated connected tooth-like faces around a
shared axis. The repeated faces must be numerous, adjacent, curved/flat/oblique
in a gear-like pattern, and arranged as a ring. This rejects housings, covers,
hole arrays, ribs, and cylindrical bands that merely contain repeated features.

When `use_gear_tooth_refinement` is enabled, gear solids become
`gear_aware_tetra`. Tooth surfaces are meshed with smaller target/min sizes and
a smaller feature angle. When disabled, the same solids fall back to normal
tetra behavior.

The workflow can also run tooth-preview mode: rename/color first, mesh only the
recognized tooth surfaces into the preview component, and stop. Use the delete
preview action to remove those tooth preview elements after inspection.

## Known Limitations

- Some revolved bodies still fall back to tetra if HyperMesh cannot create a
  usable new cut-section surface after `*body_splitmerge_with_plane`.
- Tooth recognition is intentionally conservative. If a new gear family is
  missed, inspect `runs/gear_tooth_recognition_<timestamp>.txt` before changing
  thresholds.
- For broken surfaces or overlapping 2D shell meshes, repair may stop early and
  keep repaired 2D instead of entering tetra. This is intentional crash
  prevention.

## Quality Policy

Do not blindly refine the whole mesh to fix quality.

Preferred order:

1. Change strategy if the topology is wrong.
2. Repair 2D with guarded steps and timeout checks.
3. Enter tetra only after 2D crash guards pass.
4. Try local 3D smooth/remesh.
5. If bad volume elements remain, delete tetra and keep the repaired 2D shell
   mesh for that solid.

Do not automatically keep unfixable bad 3D volume elements as final output
unless the user explicitly asks.

## Configuration Example

```json
{
  "mcpServers": {
    "hypermesh": {
      "command": "python",
      "args": ["F:\\mcp\\hypermesh_mcp_server.py"],
      "env": {
        "HYPERMESH_BATCH_EXE": "F:\\Program Files\\Altair\\2020\\hwdesktop\\hw\\bin\\win64\\hmbatch.exe",
        "HYPERMESH_GUI_EXE": "F:\\Program Files\\Altair\\2020\\hwdesktop\\hw\\bin\\win64\\hw.exe"
      }
    }
  }
}
```

Adjust paths for your workstation.

## Run Without an AI Agent

`run_full_meshing_workflow.py` runs the same deterministic workflow that the MCP
tools expose, but as a normal Python command. It still requires a visible
HyperMesh session with the GUI listener Tcl sourced.

Typical use:

```powershell
python run_full_meshing_workflow.py --output outputs/full_mesh.hm
```

Useful options:

```powershell
python run_full_meshing_workflow.py `
  --host 127.0.0.1 `
  --port 47881 `
  --output outputs/full_mesh.hm `
  --tetra-timeout 1800
```

Current important workflow switches:

- `--use-gear-tooth-refinement` is the default. Gear solids become
  `gear_aware_tetra`; recognized tooth surfaces use local smaller tetra
  settings.
- `--no-gear-tooth-refinement` keeps gear-like solids on the ordinary tetra
  path.
- `--gear-tooth-preview-only` runs probe, classification, Phase 2 rename/color,
  then meshes only recognized tooth surfaces for visual checking.
- `--delete-gear-tooth-preview` deletes the temporary tooth-preview mesh.
- `--gear-tooth-element-size-min/max`,
  `--gear-tooth-min-element-size-min/max`, and
  `--gear-tooth-feature-angle` control tooth-local sizing. Defaults are
  `1.2..1.6`, `0.2..0.3`, and `15`.
- `--spin-section-element-size-min/max` controls the cut-section 2D mesh size
  used before spin.
- `--drag-aspect-guard` enables the drag minimum-three-layer/aspect guard from
  the command line. The offline panel enables the same `drag三层` behavior by
  default.

The offline panel in `launch_meshing_workflow_panel.tcl` exposes the same
current switches, including gear refinement, tooth preview/delete, tooth sizing,
spin section sizing, and drag three-layer guarding.

Two background-mode entry points are also available:

- `run_batch_meshing_panel.py` opens a standalone Windows panel. Select a
  `.stp`, `.step`, or `.hm` file, choose the output `.hm`, then run the same
  meshing logic through `hmbatch.exe` without a visible HyperMesh workflow.
- The HyperMesh Tcl panel has a `后台划分` button. It saves the currently open
  HyperMesh model to a temporary `.hm` snapshot under `runs/`, then passes that
  snapshot to `run_full_meshing_workflow_batch.py`.

The runner performs these steps:

1. Probe all solids in the current GUI model.
2. Classify solids from geometry facts.
3. Execute Phase 2 rename/color finalization.
4. Mesh drag-hex solids.
5. Mesh spin-hex solids through cut-section spin.
6. Promote failed drag/spin solids to tetra.
7. Mesh tetra and gear-aware tetra solids in generated batches through the async GUI path.
8. Save once at the end and write reports/diagnostics into `runs/`.

By default the runner records failed batches and continues so a full-model run
can finish and be diagnosed afterward. Pass `--stop-on-error` to stop at the
first failure.
