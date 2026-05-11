# HyperMesh MCP Server

Local MCP server for driving Altair HyperMesh with generated Tcl scripts.

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
  volume-quality repair.
- `generate_guarded_drag_hex_tcl`: generate guarded drag-hex Tcl.
- `generate_guarded_spin_hex_tcl`: generate guarded spin-hex Tcl for a known true section.
- `get_cutsection_spin_workflow`: explain the generic cut-section spin workflow.
- `generate_cutsection_spin_hex_tcl`: generate cut-section spin Tcl for stepped or recessed revolved solids.

## Generic Strategy Rules

Use `classify_all_solids_from_probe` and geometry facts, not component names.
The intended order is:

1. Try the structured hex route that matches the geometry: drag, spin, or
   cut-section spin.
2. Validate that real 3D hex elements were created. A leftover 2D section mesh
   by itself is a failure.
3. If the hex route fails, clean up temporary/invalid elements and mesh that
   object with tetra.
4. For bearing/ring-like revolved bodies, do not stop after direct spin fails.
   Use a real cut plane through the rotation axis, mesh the true radial section,
   and spin that section before the explicit tetra phase.

### Tetra

Use `tetra_surface_deviation_rtrias` for:

- flanges or flange-like bodies
- bodies with bolt holes, local holes, bosses, protrusions, ribs, grooves, cutouts, or non-sweepable topology
- ambiguous parts where a clean drag/spin source cannot be proven

Required checks:

- create 2D surface-deviation R-trias mesh first
- keep the nominal `element_size`; for high-face-count solids, reduce
  `min_element_size` to capture small faces/features instead of coarsening the
  part
- clean/check 2D aspect issues
- continue to `*tetmesh` even for complex solids, while logging shell-count
  guard warnings for crash diagnosis
- run high-risk tetra solids in isolated Phase 3 batches rather than stopping
  them or making them coarser
- tetramesh per component/object
- check and locally repair/report volume quality

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

Use guarded spin only when the selected source surface is already known to be a
true cross-section of a clean revolved solid.

Preconditions:

- source section is a real cross-section
- source section meshes as 100% quads
- spin result contains hex elements only

Pass `solid_id` when possible so the generated mesh can be checked against the
target solid. Failed fit/non-hex results are cleaned, retried once with the same
element size, then sent to tetra fallback when enabled.

If the solid is stepped, recessed, grooved, or the source section is ambiguous,
use cut-section spin instead.

### Cut-Section Spin Hex

Use `generate_cutsection_spin_hex_tcl` for stepped/recessed/ambiguous revolved
solids.

Workflow:

1. Split the actual solid with `*body_splitmerge_with_plane` using a middle plane.
2. Detect newly created surfaces from the split.
3. Temporarily mesh each new surface.
4. Accept only all-quad surfaces whose shell nodes lie on the split plane.
5. Spin the accepted 2D section shells into 3D hex elements.
6. Delete only the temporary 2D seed shells.

Required inputs:

- `solid_id`
- `component_name`
- split plane normal and point
- spin axis and a point on the spin axis; this is required and must be on the
  real rotation axis, not merely any point on the split plane
- element size and spin density

The split plane must contain the spin axis. In practical terms, the split plane
normal should be nearly perpendicular to the spin axis. If the cut plane is
perpendicular to the axis and creates an annular transverse section, that is a
drag-style source section for a constant-section body, not a spin section.

The generator validates the spin result. If no valid 3D hex8 elements are
created, it deletes temporary section/invalid elements and retries once with the
same requested element size. It does not shrink/refine the hex mesh for the
retry. If the second attempt still fails, the explicit tetra phase handles the
solid.

The cut-section generator also considers existing section surfaces on the target
solid after a split. This helps when a model has already been split or when
HyperMesh does not create new surface IDs. If mapped quads fail, it can try a
quad-only section mesh mode with the same element size before stopping for the explicit tetra phase.

### Gear-Aware Tetra

Use `classify_all_solids_from_probe` from geometry facts only. Do not classify
gear regions from component names, file names, or natural-language labels.
Set one or more of these when geometry inspection shows a gear-like region:
`has_gear_teeth`, `has_helical_teeth`, `has_twisted_tooth_faces`,
`has_many_repeated_radial_teeth`, `has_periodic_outer_radius_variation`,
`has_outer_tooth_band`, `has_repeated_tooth_flanks`, `tooth_count`, or
`outer_radius_variation_ratio`.

## Known Limitations

- Some bearing/ring solids still need the explicit tetra phase even though a human can see
  they should be sweepable by cutting a radial section and spinning it. The
  current `generate_cutsection_spin_hex_tcl` requires HyperMesh to expose a
  usable all-quad true section after `*body_splitmerge_with_plane`; on some
  recessed bearing geometry it only produces invalid/non-quad sections, so the
  guarded workflow correctly stops before tetra. Future work: add a more robust
  profile extraction path that derives ordered radial profile loops from solid
  edges instead of relying only on newly split surfaces.

## Quality Policy

Do not blindly refine the whole mesh to fix quality.

Preferred order:

1. Change strategy if the topology is wrong.
2. Try local 3D smooth/remesh.
3. Try sliver repair where applicable.
4. If bad volume elements remain, keep them and report their IDs.

Do not automatically delete unfixable quality-failed volume elements unless the
user explicitly asks.

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

The runner performs these steps:

1. Probe all solids in the current GUI model.
2. Classify solids from geometry facts.
3. Execute Phase 2 rename/color finalization.
4. Mesh drag-hex solids.
5. Mesh tetra solids in generated batches through the async GUI path.
6. Save once at the end and write JSON summaries into `runs/`.

By default the runner records failed batches and continues so a full-model run
can finish and be diagnosed afterward. Pass `--stop-on-error` to stop at the
first failure.
