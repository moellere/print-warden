import struct
import zipfile

import pytest

from print_warden import checks, threemf

NS = (
    'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
    'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"'
)
CUBE = """<object id="{id}" type="model"><mesh><vertices>
<vertex x="0" y="0" z="0"/><vertex x="{s}" y="0" z="0"/><vertex x="0" y="{s}" z="0"/><vertex x="0" y="0" z="{s}"/>
</vertices><triangles>
<triangle v1="0" v2="2" v3="1"/><triangle v1="0" v2="1" v3="3"/><triangle v1="0" v2="3" v3="2"/><triangle v1="1" v2="2" v3="3"/>
</triangles></mesh></object>"""


def _model(resources: str, build: str, unit: str = "millimeter") -> str:
    return (
        f'<?xml version="1.0"?><model unit="{unit}" {NS}><resources>{resources}</resources>'
        f"<build>{build}</build></model>"
    )


def _write(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return path


def _project(tmp_path, subtypes=("normal_part", "normal_part")):
    """Two objects on two plates; object 2 has two parts (components in a separate file), object 3 is plain."""
    parts = _model(CUBE.format(id=1, s=10) + CUBE.format(id=5, s=4), "")
    root = _model(
        '<object id="2" type="model"><components>'
        '<component p:path="/3D/Objects/a.model" objectid="1"/>'
        '<component p:path="/3D/Objects/a.model" objectid="5" transform="1 0 0 0 1 0 0 0 1 20 0 0"/>'
        "</components></object>" + CUBE.format(id=3, s=30),
        '<item objectid="2" transform="1 0 0 0 1 0 0 0 1 100 100 0"/>'
        '<item objectid="3" transform="1 0 0 0 1 0 0 0 1 400 100 0"/>',
    )
    cfg = (
        '<config><object id="2">'
        f'<part id="1" subtype="{subtypes[0]}"/><part id="5" subtype="{subtypes[1]}"/>'
        "</object>"
        '<plate><metadata key="plater_id" value="1"/><model_instance><metadata key="object_id" value="2"/>'
        '<metadata key="instance_id" value="0"/></model_instance></plate>'
        '<plate><metadata key="plater_id" value="2"/><model_instance><metadata key="object_id" value="3"/>'
        '<metadata key="instance_id" value="0"/></model_instance></plate></config>'
    )
    return _write(
        tmp_path / "proj.3mf",
        {
            "3D/3dmodel.model": root,
            "3D/Objects/a.model": parts,
            "Metadata/model_settings.config": cfg,
            "Metadata/plate_1.gcode": "G28\nM104 S999\n",
        },
    )


def _verts(stl):
    data = stl.read_bytes()
    (n,) = struct.unpack_from("<I", data, 80)
    pts = []
    for i in range(n):
        v = struct.unpack_from("<9f", data, 84 + 50 * i + 12)
        pts += [v[0:3], v[3:6], v[6:9]]
    return n, pts


def test_plates_are_separate_stls(tmp_path):
    src = _project(tmp_path)
    assert threemf.plate_numbers(src) == [1, 2]
    one = threemf.to_stl(src, tmp_path / "p1.stl", 1)
    assert (one.objects, one.triangles, one.plates) == (1, 8, [1, 2])
    assert checks.stl_bounds(one.stl) == pytest.approx((24, 10, 10))
    assert any("plate=N" in w for w in one.warnings)
    two = threemf.to_stl(src, tmp_path / "p2.stl", 2)
    assert two.triangles == 4
    assert checks.stl_bounds(two.stl) == pytest.approx((30, 30, 30))


def test_plate_is_centred_and_on_the_bed(tmp_path):
    res = threemf.to_stl(_project(tmp_path), tmp_path / "p1.stl", 1)
    _, pts = _verts(res.stl)
    xs, ys, zs = zip(*pts, strict=True)
    assert min(zs) == pytest.approx(0)
    assert (min(xs) + max(xs)) / 2 == pytest.approx(0)
    assert (min(ys) + max(ys)) / 2 == pytest.approx(0)


def test_missing_plate_is_refused(tmp_path):
    with pytest.raises(ValueError, match=r"plates \[1, 2\]"):
        threemf.to_stl(_project(tmp_path), tmp_path / "x.stl", 3)


def test_modifier_volumes_are_dropped(tmp_path):
    src = _project(tmp_path, ("normal_part", "modifier_part"))
    res = threemf.to_stl(src, tmp_path / "p1.stl", 1)
    assert res.triangles == 4
    assert any("modifier part" in w for w in res.warnings)


def test_negative_parts_are_refused(tmp_path):
    src = _project(tmp_path, ("normal_part", "negative_part"))
    with pytest.raises(ValueError, match="negative part"):
        threemf.to_stl(src, tmp_path / "p1.stl", 1)


def test_plain_core_3mf_units_and_unprintable_items(tmp_path):
    src = _write(
        tmp_path / "plain.3mf",
        {
            "_rels/.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Target="/3D/model.model" Id="r" '
                'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>'
            ),
            "3D/model.model": _model(
                CUBE.format(id=1, s=1) + CUBE.format(id=2, s=9),
                '<item objectid="1"/><item objectid="2" printable="0"/>',
                unit="inch",
            ),
        },
    )
    res = threemf.to_stl(src, tmp_path / "plain.stl")
    assert (res.plates, res.objects, res.warnings) == ([1], 1, [])
    assert checks.stl_bounds(res.stl) == pytest.approx((25.4, 25.4, 25.4))


def test_rotation_transform(tmp_path):
    rot = "0 1 0 -1 0 0 0 0 1 0 0 0"
    src = _write(
        tmp_path / "r.3mf",
        {
            "3D/3dmodel.model": _model(
                '<object id="1" type="model"><mesh><vertices><vertex x="0" y="0" z="0"/>'
                '<vertex x="10" y="0" z="0"/><vertex x="0" y="2" z="1"/></vertices>'
                '<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>',
                f'<item objectid="1" transform="{rot}"/>',
            )
        },
    )
    res = threemf.to_stl(src, tmp_path / "r.stl")
    assert checks.stl_bounds(res.stl) == pytest.approx((2, 10, 1))


def test_garbage_is_refused(tmp_path):
    bad = tmp_path / "bad.3mf"
    bad.write_bytes(b"not a zip")
    with pytest.raises(ValueError, match="not a 3mf"):
        threemf.to_stl(bad, tmp_path / "x.stl")
    empty = _write(tmp_path / "e.3mf", {"3D/3dmodel.model": _model("", "")})
    with pytest.raises(ValueError, match="no printable geometry"):
        threemf.to_stl(empty, tmp_path / "x.stl")
    dangling = _write(
        tmp_path / "d.3mf",
        {
            "3D/3dmodel.model": _model(
                '<object id="1"><components><component p:path="/3D/none.model" objectid="1"/>'
                "</components></object>",
                '<item objectid="1"/>',
            )
        },
    )
    with pytest.raises(ValueError, match="missing part"):
        threemf.to_stl(dangling, tmp_path / "x.stl")


def test_facets_carry_unit_normals(tmp_path):
    """OrcaSlicer refuses a binary STL whose normals are all zero."""
    res = threemf.to_stl(_project(tmp_path), tmp_path / "p1.stl", 1)
    data = res.stl.read_bytes()
    for i in range(res.triangles):
        nx, ny, nz = struct.unpack_from("<3f", data, 84 + 50 * i)
        assert nx * nx + ny * ny + nz * nz == pytest.approx(1)


def test_project_gcode_and_settings_are_not_used(tmp_path):
    """Only geometry crosses over: the STL holds triangles and a plain header, nothing from Metadata/."""
    res = threemf.to_stl(_project(tmp_path), tmp_path / "p1.stl", 1)
    assert b"M104" not in res.stl.read_bytes()
    assert len(res.stl.read_bytes()) == 84 + 50 * res.triangles


def test_sanitize_strips_gcode_settings_and_embedded_gcode(tmp_path):
    import json

    src = _project(tmp_path)
    with zipfile.ZipFile(src, "a") as z:
        z.writestr(
            "Metadata/project_settings.config",
            json.dumps(
                {
                    "machine_start_gcode": "G28\nM104 S300",
                    "file_start_gcode": "",
                    "filament_start_gcode": ["M109 S290", ""],
                    "post_process": ["/bin/rm -rf /"],
                    "gcode_flavor": "klipper",
                    "emit_machine_limits_to_gcode": "1",
                    "layer_height": "0.2",
                }
            ),
        )
    clean = threemf.sanitize(src, tmp_path / "clean.3mf")
    with zipfile.ZipFile(clean.path) as z:
        names = z.namelist()
        cfg = json.loads(z.read("Metadata/project_settings.config"))
    assert "Metadata/plate_1.gcode" not in names
    assert "3D/Objects/a.model" in names
    assert cfg["machine_start_gcode"] == threemf.SENTINEL + "machine_start_gcode"
    assert (
        cfg["filament_start_gcode"] == [threemf.SENTINEL + "filament_start_gcode"] * 2
    )
    assert cfg["file_start_gcode"] == ""
    assert cfg["post_process"] == []
    assert (
        cfg["gcode_flavor"],
        cfg["emit_machine_limits_to_gcode"],
        cfg["layer_height"],
    ) == ("klipper", "1", "0.2")
    assert "machine_start_gcode" in clean.stripped_keys
    assert threemf.leaked(f"G1 X1\n{threemf.SENTINEL}machine_start_gcode\n") == [
        "machine_start_gcode"
    ]
    assert threemf.leaked("G1 X1\n; machine_start_gcode = G28\n") == []


def test_negative_parts_can_be_left_out_for_bounds(tmp_path):
    src = _project(tmp_path, ("normal_part", "negative_part"))
    res = threemf.to_stl(src, tmp_path / "p1.stl", 1, allow_negative=True)
    assert res.triangles == 4
