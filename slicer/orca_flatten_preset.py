#!/usr/bin/env python3
"""
Flatten an OrcaSlicer preset's `inherits` chain into one standalone JSON.

Orca's CLI rejects a user preset that inherits a system preset when both are
passed with --load-settings ("duplicate process config file"), and can't
resolve the parent if only the child is passed. Flattening sidesteps both:
walk the chain from the vendor profile directory, merge parent -> child
(child wins), drop `inherits`, and write a self-contained preset.

Usage:
    orca_flatten_preset.py <vendor_dir> <kind> <preset_name_or_json> <out.json> [override.json]

    vendor_dir   e.g. ~/.local/opt/orcaslicer/squashfs-root/resources/profiles/Artillery
    kind         machine | process | filament
    preset       a preset name ("0.20mm Standard @Artillery X4Plus 0.4 nozzle")
                 or a path to a JSON file (which may itself `inherits`)
    override     optional JSON whose keys are applied last (house rules)
"""
import json
import os
import sys


def load_named(vendor_dir: str, kind: str, name: str) -> dict:
    path = os.path.join(vendor_dir, kind, f"{name}.json")
    if not os.path.exists(path):
        raise SystemExit(f"preset not found: {path}")
    with open(path) as f:
        return json.load(f)


def flatten(vendor_dir: str, kind: str, preset: dict) -> dict:
    parent_name = preset.get("inherits")
    if not parent_name:
        return dict(preset)
    parent = flatten(vendor_dir, kind, load_named(vendor_dir, kind, parent_name))
    merged = dict(parent)
    merged.update({k: v for k, v in preset.items() if k != "inherits"})
    return merged


def main():
    if len(sys.argv) < 5:
        raise SystemExit(__doc__)
    vendor_dir, kind, preset_ref, out_path = sys.argv[1:5]
    override_path = sys.argv[5] if len(sys.argv) > 5 else None

    if os.path.exists(preset_ref):
        with open(preset_ref) as f:
            preset = json.load(f)
    else:
        preset = load_named(vendor_dir, kind, preset_ref)

    flat = flatten(vendor_dir, kind, preset)
    if override_path:
        with open(override_path) as f:
            flat.update(json.load(f))

    # A flattened preset stands alone: mark it instantiable and drop
    # inheritance/compat bookkeeping that would only confuse the CLI.
    flat["instantiation"] = "true"
    flat.pop("inherits", None)
    with open(out_path, "w") as f:
        json.dump(flat, f, indent=2)
    print(f"wrote {out_path} ({len(flat)} keys)")


if __name__ == "__main__":
    main()
