#!/usr/bin/env python3
"""Sort the plates of a Bambu Studio .3mf project alphabetically by plate name.

Writes "<name> - sorted.3mf" next to the input. The input is never modified.

A plate's position in the project is implied by three things, all of which move together:
  - the order and plater_id of its <plate> block in Metadata/model_settings.config
  - the numbered per-plate files (Metadata/plate_N.png, top_N.png, plate_N.gcode, ...)
  - the world position of its objects, since plates sit on a grid in the 3D scene
Everything else in the archive is copied through byte for byte.
"""

import argparse
import html
import json
import math
import os
import re
import sys
import zipfile
from decimal import Decimal
from pathlib import Path

MODEL = "3D/3dmodel.model"
MODEL_SETTINGS = "Metadata/model_settings.config"
PROJECT_SETTINGS = "Metadata/project_settings.config"
SLICE_INFO = "Metadata/slice_info.config"
CUSTOM_GCODE = "Metadata/custom_gcode_per_layer.xml"
FILAMENT_SEQUENCE = "Metadata/filament_sequence.json"

# Bambu Studio spaces plates one fifth of a bed apart (LOGICAL_PART_PLATE_GAP).
PLATE_STRIDE_FACTOR = Decimal("1.2")

PLATE_BLOCK = re.compile(r"<plate>.*?</plate>", re.S)
PLATE_FILE = re.compile(r"(Metadata/(?:plate_no_light_|plate_|top_|pick_))(\d+)(?=[._])")
PLATER_ID = re.compile(r'key="plater_id" value="(\d+)"')
PLATER_NAME = re.compile(r'key="plater_name" value="([^"]*)"')
SLICE_INDEX = re.compile(r'key="index" value="(\d+)"')
PLATE_INFO_ID = re.compile(r'<plate_info id="(\d+)"')
MODEL_INSTANCE = re.compile(r"<model_instance>(.*?)</model_instance>", re.S)
BUILD_ITEM = re.compile(r"<item\b[^>]*>")
WIPE_TOWER = re.compile(r'("wipe_tower_[xy]":\s*\[)(.*?)(\])', re.S)


def fail(message):
    sys.exit(f"error: {message}")


def natural_key(name):
    """Case-insensitive, with numbers compared by value (58mm, 58.5mm, 240deg)."""
    parts = re.split(r"(\d+(?:\.\d+)?)", name.casefold())
    return [float(p) if i % 2 else p for i, p in enumerate(parts)]


def read_plates(model_settings):
    """Return [(plater_id, name, [(object_id, instance_id), ...])] in file order."""
    plates = []
    for block in PLATE_BLOCK.findall(model_settings):
        plater_id = PLATER_ID.search(block)
        name = PLATER_NAME.search(block)
        if not plater_id:
            fail("found a <plate> without a plater_id in model_settings.config")
        instances = [
            (meta_value(inst, "object_id"), meta_value(inst, "instance_id"))
            for inst in MODEL_INSTANCE.findall(block)
        ]
        plates.append((int(plater_id[1]), html.unescape(name[1]) if name else "", instances))
    return plates


def meta_value(text, key):
    return int(re.search(rf'key="{key}" value="(\d+)"', text)[1])


def renumber_plates(text, id_pattern, new_index):
    """Give each <plate> block its new number and put the blocks in that order."""
    matches = list(PLATE_BLOCK.finditer(text))
    renumbered = []
    for match in matches:
        block = match[0]
        found = id_pattern.search(block)
        if not found or int(found[1]) not in new_index:
            return text
        new = new_index[int(found[1])]
        renumbered.append((new, block[: found.start(1)] + str(new) + block[found.end(1) :]))
    renumbered.sort(key=lambda pair: pair[0])

    out, pos = [], 0
    for match, (_, block) in zip(matches, renumbered):
        out += [text[pos : match.start()], block]
        pos = match.end()
    return "".join(out) + text[pos:]


def remap_plate_files(text, new_index):
    def rename(match):
        new = new_index.get(int(match[2]))
        return match[0] if new is None else f"{match[1]}{new}"

    return PLATE_FILE.sub(rename, text)


def bed_size(project_settings):
    area = json.loads(project_settings).get("printable_area")
    if not area:
        fail("no printable_area in project_settings.config, so the plate grid is unknown")
    points = [tuple(Decimal(v) for v in point.split("x")) for point in area]
    xs, ys = zip(*points)
    return max(xs) - min(xs), max(ys) - min(ys)


def plate_offsets(plate_count, new_index, bed):
    """Map old plate number to the (dx, dy) that carries its objects to the new grid cell."""
    columns = math.isqrt(plate_count - 1) + 1
    stride_x, stride_y = (side * PLATE_STRIDE_FACTOR for side in bed)

    def cell(number):
        return (number - 1) % columns, (number - 1) // columns

    offsets = {}
    for old, new in new_index.items():
        (old_col, old_row), (new_col, new_row) = cell(old), cell(new)
        offsets[old] = ((new_col - old_col) * stride_x, (old_row - new_row) * stride_y)
    return offsets


def move_build_items(model, instance_offsets):
    """Translate each build item that sits on a plate by its plate's offset."""
    seen = {}

    def move(match):
        item = match[0]
        object_id = int(re.search(r'\bobjectid="(\d+)"', item)[1])
        instance_id = seen.get(object_id, 0)
        seen[object_id] = instance_id + 1
        dx, dy = instance_offsets.get((object_id, instance_id), (0, 0))
        if not dx and not dy:
            return item
        transform = re.search(r'\btransform="([^"]*)"', item)
        if not transform:
            fail(f"build item for object {object_id} has no transform to move")
        values = transform[1].split()
        values[9] = f"{float(Decimal(values[9]) + dx):.9g}"
        values[10] = f"{float(Decimal(values[10]) + dy):.9g}"
        return item[: transform.start(1)] + " ".join(values) + item[transform.end(1) :]

    build_start = model.rindex("<build")
    return model[:build_start] + BUILD_ITEM.sub(move, model[build_start:])


def reorder_wipe_towers(project_settings, order):
    """Wipe tower positions are stored as one entry per plate."""

    def reorder(match):
        values = re.findall(r'"[^"]*"', match[2])
        if len(values) != len(order):
            return match[0]
        moved = iter(values[old - 1] for old in order)
        return match[1] + re.sub(r'"[^"]*"', lambda _: next(moved), match[2]) + match[3]

    return WIPE_TOWER.sub(reorder, project_settings)


def remap_filament_sequence(text, new_index):
    def rename(key):
        match = re.fullmatch(r"plate_(\d+)", key)
        new = new_index.get(int(match[1])) if match else None
        return key if new is None else f"plate_{new}"

    data = {rename(key): value for key, value in json.loads(text).items()}
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def sort_plates(source, destination):
    with zipfile.ZipFile(source) as archive:
        names = set(archive.namelist())
        if MODEL_SETTINGS not in names:
            fail(f"{source.name} has no {MODEL_SETTINGS}; is it a Bambu Studio project?")

        def text(name):
            return archive.read(name).decode("utf-8")

        model_settings = text(MODEL_SETTINGS)
        plates = read_plates(model_settings)
        ids = [plater_id for plater_id, _, _ in plates]
        if ids != list(range(1, len(plates) + 1)):
            fail(f"expected plates numbered 1..{len(plates)} in order, found {ids}")

        # Unnamed plates go last. The sort is stable, so ties keep their current order.
        ordered = sorted(plates, key=lambda plate: (plate[1] == "", natural_key(plate[1])))
        order = [plater_id for plater_id, _, _ in ordered]
        new_index = {old: new for new, old in enumerate(order, start=1)}

        for new, (old, name, _) in enumerate(ordered, start=1):
            print(f"  {new:>3}  {name or '(unnamed)'}" + ("" if old == new else f"   (was {old})"))
        if order == ids:
            print("Plates are already sorted. Nothing written.")
            return

        offsets = plate_offsets(len(plates), new_index, bed_size(text(PROJECT_SETTINGS)))
        instance_offsets = {
            instance: offsets[plater_id]
            for plater_id, _, instances in plates
            for instance in instances
        }

        edited = {
            MODEL_SETTINGS: remap_plate_files(
                renumber_plates(model_settings, PLATER_ID, new_index), new_index
            ),
            MODEL: move_build_items(text(MODEL), instance_offsets),
            PROJECT_SETTINGS: reorder_wipe_towers(text(PROJECT_SETTINGS), order),
        }
        if SLICE_INFO in names:
            edited[SLICE_INFO] = renumber_plates(text(SLICE_INFO), SLICE_INDEX, new_index)
        if CUSTOM_GCODE in names:
            edited[CUSTOM_GCODE] = renumber_plates(text(CUSTOM_GCODE), PLATE_INFO_ID, new_index)
        if FILAMENT_SEQUENCE in names:
            edited[FILAMENT_SEQUENCE] = remap_filament_sequence(text(FILAMENT_SEQUENCE), new_index)
        for name in names:
            if name.startswith("Metadata/_rels/"):
                edited[name] = remap_plate_files(text(name), new_index)

        partial = destination.with_name(destination.name + ".partial")
        try:
            with zipfile.ZipFile(partial, "w") as output:
                for info in archive.infolist():
                    data = archive.read(info)
                    if info.filename in edited:
                        data = edited[info.filename].encode("utf-8")
                    entry = zipfile.ZipInfo(
                        remap_plate_files(info.filename, new_index), info.date_time
                    )
                    entry.compress_type = info.compress_type
                    entry.external_attr = info.external_attr
                    output.writestr(entry, data)
            os.replace(partial, destination)
        finally:
            partial.unlink(missing_ok=True)

    print(f"Wrote {destination}")


def main():
    parser = argparse.ArgumentParser(
        description="Sort the plates of a Bambu Studio .3mf alphabetically by plate name."
    )
    parser.add_argument("file", type=Path, help="the .3mf project to sort")
    parser.add_argument(
        "-o", "--output", type=Path, help='output path (default: "<name> - sorted.3mf")'
    )
    parser.add_argument(
        "-f", "--force", action="store_true", help="overwrite the output if it exists"
    )
    args = parser.parse_args()

    source = args.file
    if not source.is_file():
        fail(f"{source} not found")
    destination = args.output or source.with_name(f"{source.stem} - sorted{source.suffix}")
    if destination.resolve() == source.resolve():
        fail("the output would overwrite the input")
    if destination.exists() and not args.force:
        fail(f"{destination} already exists (use --force to overwrite)")

    sort_plates(source, destination)


if __name__ == "__main__":
    main()
