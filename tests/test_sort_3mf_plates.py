import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sort_3mf_plates

# A 100mm bed gives a 120mm plate stride. Four plates sit on a 2x2 grid.
PLATE_NAMES = ["b10", "", "B2", "a"]
PLATE_CENTERS = [(50, 50), (170, 50), (50, -70), (170, -70)]
SORTED_ORIGINAL_IDS = [4, 3, 1, 2]
OFF_PLATE_ITEM = '<item objectid="5" transform="1 0 0 0 1 0 0 0 1 -200 0 3" printable="1"/>'


def plate_block(number, name):
    return f"""  <plate>
    <metadata key="plater_id" value="{number}"/>
    <metadata key="plater_name" value="{name}"/>
    <metadata key="thumbnail_file" value="Metadata/plate_{number}.png"/>
    <metadata key="top_file" value="Metadata/top_{number}.png"/>
    <model_instance>
      <metadata key="object_id" value="{number}"/>
      <metadata key="instance_id" value="0"/>
    </model_instance>
  </plate>
"""


def build_project(path, names=PLATE_NAMES):
    numbered = list(enumerate(names, start=1))
    items = "".join(
        f'  <item objectid="{n}" transform="1 0 0 0 1 0 0 0 1 {x} {y} {n}" printable="1"/>\n'
        for (n, _), (x, y) in zip(numbered, PLATE_CENTERS)
    )
    files = {
        "3D/3dmodel.model": f"<model>\n <resources/>\n <build>\n{items}  {OFF_PLATE_ITEM}\n </build>\n</model>\n",
        "3D/Objects/object_1.model": "<mesh/>",
        "Metadata/model_settings.config": "<config>\n  <object id=\"1\"/>\n"
        + "".join(plate_block(n, name) for n, name in numbered)
        + "  <assemble/>\n</config>\n",
        "Metadata/project_settings.config": json.dumps(
            {
                "printable_area": ["0x0", "100x0", "100x100", "0x100"],
                "wipe_tower_x": [str(n) for n, _ in numbered],
            },
            indent=4,
        ),
        "Metadata/filament_sequence.json": json.dumps(
            {f"plate_{n}": {"sequence": [n]} for n, _ in numbered}
        ),
    }
    for n, _ in numbered:
        files[f"Metadata/plate_{n}.png"] = f"thumb {n}"
        files[f"Metadata/plate_{n}_small.png"] = f"small {n}"
        files[f"Metadata/top_{n}.png"] = f"top {n}"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def run_sort(source, destination):
    with contextlib.redirect_stdout(io.StringIO()):
        sort_3mf_plates.sort_plates(source, destination)


class SortPlatesTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.source = Path(folder.name) / "project.3mf"
        self.destination = Path(folder.name) / "project - sorted.3mf"

    def sorted_archive(self):
        build_project(self.source)
        run_sort(self.source, self.destination)
        archive = zipfile.ZipFile(self.destination)
        self.addCleanup(archive.close)
        return archive

    def test_plates_are_renumbered_in_name_order(self):
        settings = self.sorted_archive().read("Metadata/model_settings.config").decode()
        plates = sort_3mf_plates.read_plates(settings)
        self.assertEqual([name for _, name, _ in plates], ["a", "B2", "b10", ""])
        self.assertEqual([number for number, _, _ in plates], [1, 2, 3, 4])
        self.assertEqual(
            [instances for _, _, instances in plates],
            [[(original, 0)] for original in SORTED_ORIGINAL_IDS],
        )
        self.assertEqual(
            re.findall(r"Metadata/\w+\.png", settings),
            [f"Metadata/{kind}_{n}.png" for n in range(1, 5) for kind in ("plate", "top")],
        )

    def test_numbered_files_follow_their_plate(self):
        archive = self.sorted_archive()
        for new, original in enumerate(SORTED_ORIGINAL_IDS, start=1):
            self.assertEqual(archive.read(f"Metadata/plate_{new}.png"), f"thumb {original}".encode())
            self.assertEqual(archive.read(f"Metadata/plate_{new}_small.png"), f"small {original}".encode())
            self.assertEqual(archive.read(f"Metadata/top_{new}.png"), f"top {original}".encode())

    def test_objects_move_to_the_new_grid_cell(self):
        model = self.sorted_archive().read("3D/3dmodel.model").decode()
        positions = {
            int(object_id): transform.split()[9:]
            for object_id, transform in re.findall(r'objectid="(\d+)" transform="([^"]*)"', model)
        }
        for new, original in enumerate(SORTED_ORIGINAL_IDS, start=1):
            x, y = PLATE_CENTERS[new - 1]
            self.assertEqual(positions[original], [str(x), str(y), str(original)])
        self.assertIn(OFF_PLATE_ITEM, model)

    def test_per_plate_records_are_reordered(self):
        archive = self.sorted_archive()
        settings = json.loads(archive.read("Metadata/project_settings.config"))
        self.assertEqual(settings["wipe_tower_x"], ["4", "3", "1", "2"])
        sequence = json.loads(archive.read("Metadata/filament_sequence.json"))
        self.assertEqual(
            {key: value["sequence"] for key, value in sequence.items()},
            {f"plate_{new}": [original] for new, original in enumerate(SORTED_ORIGINAL_IDS, start=1)},
        )

    def test_everything_else_is_untouched(self):
        archive = self.sorted_archive()
        with zipfile.ZipFile(self.source) as original:
            self.assertEqual(len(archive.namelist()), len(original.namelist()))
            self.assertEqual(archive.read("3D/Objects/object_1.model"), original.read("3D/Objects/object_1.model"))
            before = original.read("Metadata/model_settings.config").decode()
        after = archive.read("Metadata/model_settings.config").decode()
        self.assertEqual(after.split("  <plate>")[0], before.split("  <plate>")[0])
        self.assertEqual(after.split("</plate>")[-1], before.split("</plate>")[-1])

    def test_sorted_project_writes_nothing(self):
        build_project(self.source, names=["a", "b", "c", ""])
        run_sort(self.source, self.destination)
        self.assertFalse(self.destination.exists())


class NaturalKeyTest(unittest.TestCase):
    def test_numbers_compare_by_value_and_case_is_ignored(self):
        names = ["Exit 120deg", "58.5mm", "exit 60deg", "58mm", "Handle", "9mm"]
        self.assertEqual(
            sorted(names, key=sort_3mf_plates.natural_key),
            ["9mm", "58mm", "58.5mm", "exit 60deg", "Exit 120deg", "Handle"],
        )


if __name__ == "__main__":
    unittest.main()
