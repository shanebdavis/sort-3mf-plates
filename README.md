# sort-3mf-plates

Sort the plates of a Bambu Studio `.3mf` project alphabetically by plate name.

```
$ sort-3mf-plates "Modular Tamper.3mf"
    1  39.5mm Tamp   (was 22)
    2  40mm Tamp   (was 20)
    3  58mm Tamp   (was 14)
    4  58.5mm Tamp   (was 19)
    5  Handle 36mm XS   (was 17)
    6  Handle 38mm S   (was 1)
Wrote Modular Tamper - sorted.3mf
```

The result is written to a new file, `<name> - sorted.3mf`, next to the original. The original is never modified.

## Install

It is a single Python file with no dependencies. Python 3.9 or newer is required.

With [pipx](https://pipx.pypa.io) or [uv](https://docs.astral.sh/uv/):

```
pipx install git+https://github.com/shanebdavis/sort-3mf-plates
uv tool install git+https://github.com/shanebdavis/sort-3mf-plates
```

Or download `sort_3mf_plates.py` and run it directly:

```
python3 sort_3mf_plates.py "My Project.3mf"
```

## Usage

```
sort-3mf-plates [-o OUTPUT] [-f] file
```

| Option | Effect |
|---|---|
| `-o`, `--output` | Write to this path instead of `<name> - sorted.3mf` |
| `-f`, `--force` | Overwrite the output file if it already exists |

## Sort order

- Case is ignored.
- Numbers compare by value, so `58mm` comes before `58.5mm`, and `Exit 60deg` before `Exit 120deg`.
- Unnamed plates go last, in their existing order.
- Plates with the same name keep their existing relative order.

## What moves with a plate

A plate's place in a project is stored in several spots. The tool moves all of them together:

- **Plate settings**: the plate's block in `Metadata/model_settings.config`, including its name, lock state, filament maps and per-plate overrides.
- **Objects**: plates sit on a grid in the 3D scene, so every object on a plate is translated to the plate's new grid cell. Positions relative to the plate are preserved.
- **Thumbnails and slice results**: the numbered files such as `Metadata/plate_N.png`, `top_N.png`, `pick_N.png`, `plate_N.json` and `plate_N.gcode`.
- **Per-plate records**: wipe tower positions, filament sequences, slice info and custom G-code.

Every other file in the archive, including all mesh data, is copied through byte for byte. Objects that are not on any plate stay where they are. The project's cover image is always plate 1's thumbnail, so it becomes the thumbnail of the new first plate.

## Compatibility

Built for and tested with projects saved by Bambu Studio 2.x. OrcaSlicer projects use the same layout and are expected to work, but are untested. Check the sorted file in your slicer before deleting the original.

## Development

```
python3 -m unittest discover tests
```

## License

[MIT](LICENSE)
