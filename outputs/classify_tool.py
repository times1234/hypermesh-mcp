
@mcp.tool()
def classify_all_solids_from_probe(probe_lines):
    """MANDATORY Phase 2: auto-classify every probed solid from geometry facts ONLY.

    Takes probe_lines from run_geometry_probe_gui (Phase 1). Classifies each
    solid into drag_hex, gear_aware_tetra, or tetra_plain based purely on
    geometric measurements: surf_count, bbox dimensions, slender ratio,
    and circular symmetry. NO manual flags accepted.

    Returns per-solid: strategy, component_name, element_size, evidence.
    """
    results = {}
    for line in probe_lines:
        if not line.strip().startswith("MCP_PROBE_SOLID"):
            continue
        f = _parse_probe_facts(line)
        sid = f.get("solid_id", 0)
        if sid <= 0 or f.get("dx", 0) <= 0:
            continue

        dx, dy, dz = f["dx"], f["dy"], f["dz"]
        slender = f["slender"]
        sc = f["surf_count"]
        dims = sorted([dx, dy, dz])
        mn, md, mx = dims[0], dims[1], dims[2]
        circular = (dx > 0 and dy > 0 and abs(dx - dy) / max(dx, dy) < 0.25)

        strategy = "tetra_plain"
        evidence = []
        elem_size = max(0.6, mx / 12.0)

        # Gear: high surface count + circular symmetry = teeth
        if sc >= 15 and circular:
            strategy = "gear_aware_tetra"
            evidence.append(
                f"surf_count={sc}>=15 with circular symmetry "
                f"(dx={dx:.1f} dy={dy:.1f}) suggests gear teeth"
            )
            elem_size = max(0.6, mx / 18.0)

        # Drag-hex: simple 6-face block, one dimension is thin extrusion dir
        elif sc == 6 and mx > 0 and mn / mx < 0.35:
            strategy = "drag_hex"
            evidence.append(
                f"6-face block thin_ratio={mn/mx:.2f}<0.35; "
                f"constant-section extrusion candidate"
            )

        # Very thin plate
        elif slender > 15 and mn < 2.0:
            strategy = "tetra_plain"
            evidence.append(
                f"thin plate: slender={slender:.1f}>15, min_dim={mn:.1f}<2"
            )
            elem_size = min(elem_size, max(0.6, mn * 0.3))

        # Thin disc
        elif circular and slender > 8 and mn < 2.5:
            strategy = "tetra_plain"
            evidence.append(
                f"thin disc: circular, slender={slender:.1f}>8, min_dim={mn:.1f}<2.5"
            )
            elem_size = min(elem_size, max(0.6, mn * 0.3))

        # Shaft: one dimension dominates
        elif slender > 4 and mx > 3 * md:
            strategy = "tetra_plain"
            evidence.append(
                f"shaft: slender={slender:.1f}>4, max={mx:.1f}>3*mid={md:.1f}"
            )
            elem_size = max(0.6, md / 6.0)

        # Complex non-gear
        elif sc >= 20:
            strategy = "tetra_plain"
            evidence.append(
                f"complex: surf_count={sc}>=20, no circular tooth pattern"
            )
            elem_size = max(0.6, mx / 20.0)

        else:
            strategy = "tetra_plain"
            evidence.append(f"general: surf_count={sc}, slender={slender:.1f}")

        name = _generate_geometry_component_name(f, strategy, suffix_solid_id=True)

        results[str(sid)] = {
            "solid_id": sid,
            "strategy": strategy,
            "component_name": name,
            "element_size": round(elem_size, 2),
            "evidence": evidence,
            "probe_facts": {
                "dx": round(dx, 2),
                "dy": round(dy, 2),
                "dz": round(dz, 2),
                "slender": round(slender, 2),
                "surf_count": sc,
            },
        }

    counts = {}
    for r in results.values():
        s = r["strategy"]
        counts[s] = counts.get(s, 0) + 1

    return {
        "success": True,
        "phase": "Phase 2 - Classification (MANDATORY after Phase 1 probe)",
        "total_solids": len(results),
        "strategy_counts": counts,
        "execution_order": [
            "Phase 3: Try drag_hex candidates FIRST, validate hex8+bbox",
            "Phase 4a: Then gear_aware_tetra",
            "Phase 4b: Finally tetra_plain (including all hex fallbacks)",
        ],
        "results": results,
    }
