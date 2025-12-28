#!/usr/bin/env python

import re
import os
import sys
import json
import shutil
import hashlib
import tempfile
from collections.abc import Iterator
from typing import TypeVar
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile, ZIP_STORED, ZipInfo

def log(*objects, **kwargs):
    print(*objects, **kwargs)
    with open('log.txt', 'a', encoding='utf-8') as fd:
        print(*objects, **kwargs, file=fd)

# This class is taken from https://stackoverflow.com/a/35435548/7766117
class UpdateableZipFile(ZipFile):
    """
    Add delete (via remove_file) and update (via writestr and write methods)
    To enable update features use UpdateableZipFile with the 'with statement',
    Upon  __exit__ (if updates were applied) a new zip file will override the exiting one with the updates
    """

    class DeleteMarker(object):
        pass

    def __init__(self, file, mode="r", compression=ZIP_STORED, allowZip64=False):
        # Init base
        super().__init__(file, mode=mode, compression=compression, allowZip64=allowZip64)
        # track file to override in zip
        self._replace = {}
        # Whether the with statement was called
        self._allow_updates = False

    def writestr(self, zinfo_or_arcname, data, compress_type=None):
        if isinstance(zinfo_or_arcname, ZipInfo):
            name = zinfo_or_arcname.filename
        else:
            name = zinfo_or_arcname
        # If the file exits, and needs to be overridden,
        # mark the entry, and create a temp-file for it
        # we allow this only if the with statement is used
        if self._allow_updates and name in self.namelist():
            temp_file = self._replace[name] = self._replace.get(name,
                                                                tempfile.TemporaryFile())
            if isinstance(data, str):
                data = data.encode('utf-8')
            temp_file.write(data)
        # Otherwise just act normally
        else:
            super(UpdateableZipFile, self).writestr(zinfo_or_arcname,
                                                    data, compress_type=compress_type)

    def write(self, filename, arcname=None, compress_type=None):
        arcname = arcname or filename
        # If the file exits, and needs to be overridden,
        # mark the entry, and create a temp-file for it
        # we allow this only if the with statement is used
        if self._allow_updates and arcname in self.namelist():
            temp_file = self._replace[arcname] = self._replace.get(arcname,
                                                                   tempfile.TemporaryFile())
            with open(filename, "rb") as source:
                shutil.copyfileobj(source, temp_file)
        # Otherwise just act normally
        else:
            super(UpdateableZipFile, self).write(filename, 
                                                 arcname=arcname, compress_type=compress_type)

    def __enter__(self):
        # Allow updates
        self._allow_updates = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # call base to close zip file, organically
        try:
            super(UpdateableZipFile, self).__exit__(exc_type, exc_val, exc_tb)
            if len(self._replace) > 0:
                self._rebuild_zip()
        finally:
            # In case rebuild zip failed,
            # be sure to still release all the temp files
            self._close_all_temp_files()
            self._allow_updates = False

    def _close_all_temp_files(self):
        for temp_file in self._replace.values():
            if hasattr(temp_file, 'close'):
                temp_file.close()

    def remove_file(self, path):
        self._replace[path] = self.DeleteMarker()

    def _rebuild_zip(self):
        tempdir = tempfile.mkdtemp()
        try:
            temp_zip_path = os.path.join(tempdir, 'new.zip')
            with ZipFile(self.filename, 'r') as zip_read:
                # Create new zip with assigned properties
                with ZipFile(temp_zip_path, 'w', compression=self.compression,
                             allowZip64=self._allowZip64) as zip_write:
                    for item in zip_read.infolist():
                        # Check if the file should be replaced / or deleted
                        replacement = self._replace.get(item.filename, None)
                        # If marked for deletion, do not copy file to new zipfile
                        if isinstance(replacement, self.DeleteMarker):
                            del self._replace[item.filename]
                            continue
                        # If marked for replacement, copy temp_file, instead of old file
                        elif replacement is not None:
                            del self._replace[item.filename]
                            # Write replacement to archive,
                            # and then close it (deleting the temp file)
                            replacement.seek(0)
                            data = replacement.read()
                            replacement.close()
                        else:
                            data = zip_read.read(item.filename)
                        zip_write.writestr(item, data)
            # Override the archive with the updated one
            shutil.move(temp_zip_path, self.filename)
        finally:
            shutil.rmtree(tempdir)

pause_gcode = """M400 U1"""

# The default gcode that is used to change the filament in the printer, does not allow for pausing between
# cutting the filament and loading the new filament.
#
# This prevents the user from manually changing the currently printing filament with a different one.
# One can solve this by changing the filament print order in the slicer, but this is manual work, and might
# result in extra filament changes.
#
# The solution is to insert a special filament change gcode for the problematic tool changes. This gcode will
# first unload the filament (like what pressing the unload button in the app would do), then pauses the printer
# and finally loads the new filament from the AMS.
#
# While implementing this, I found this reddit post, which describes the same solution:
# https://www.reddit.com/r/BambuLab/comments/18y6thn/guide_printing_6_colors_on_one_ams_with_custom/
def paused_filament_change(filament_change_gcode: list[str]) -> list[str]:
    # sanity check that the input gcode contains everything expected
    if not filament_change_gcode[0].startswith("; CP TOOLCHANGE START"):
        raise ValueError("The filament change gcode does not start with the expected comment.")
    if not filament_change_gcode[-1].startswith("; CP TOOLCHANGE END"):
        raise ValueError("The filament change gcode does not end with the expected comment.")

    result = []
    next_extruder = None
    for line in filament_change_gcode:
        # replace the M620 S\dA command with the unload indicator
        match = re.match(r'M620 S(\d+)A', line)
        if match:
            next_extruder = int(match.group(1))
            result.append(f"M620 S255")
            continue

        # These lines are only used around the T gcode, which is used to change the filament.
        # Before the T gcode, a few more things are inserted, so the M620.1 lines are skipped,
        # and will be inserted after the pause gcode.
        if line.startswith("M620.1 E F523 T240"):
            continue

        match = re.match(r'T(\d+)', line)
        if match:
            # Sanity check, should not happen.
            if next_extruder is None:
                raise ValueError("The next extruder was not set before the tool change?")

            # Start the unload process (including the cutting of the filament)
            result.append("T255")
            # Indicate that the unload process is done? Not 100% sure what this gcode does.
            result.append("M621 S255")

            # Then pause the printer:
            result.append(pause_gcode)

            # At this point, the toolhead is in the poop chute.
            # The pause gcode has moved the toolhead and the bed, which will be restored by pressing the resume button.
            #
            # The next step is to load the new filament from the AMS. The T gcode will move the toolhead to the cutter,
            # then without cutting, it will move back to the chute. (It is redundant, but there seems to be no way to advoid it.)
            #
            # In a previous test, I did not have the following movements in the gcode, resulting in an awful noises while the
            # toolhead was moving out of the poop chute. I would rather not buy a new poop chute, these movements will move the
            # toolhead to a safe position before it initiates the filament loading.
            result.append("G1 X100 F5000")
            result.append("G1 X165 F15000")
            result.append("G1 Y256")
            # Wait for the movements to complete
            result.append("M400")

            # This inserts the original filament change gcodes, which will load the new filament from the AMS:
            result.append(f"M620 S{next_extruder}A")
            result.append("M620.1 E F523 T240")
            result.append(line)
            result.append("M620.1 E F523 T240")
            continue

        result.append(line)

    return result

T = TypeVar('T')
def unique_k_partition(collection: list[T], k: int, max_group_size: int | None = None) -> Iterator[list[list[T]]]:
    if len(collection) == 1 and k == 1:
        yield [ collection ]
        return
    elif len(collection) == 1:
        return

    first = collection[0]
    for smaller in unique_k_partition(collection[1:], k, max_group_size=max_group_size):
        # insert `first` in each of the subpartition's subsets
        for n, subset in enumerate(smaller):
            # only build partitions where the group size is less than or equal to max_group_size
            if max_group_size is not None and len(subset) + 1 > max_group_size:
                continue 

            yield smaller[:n] + [[first] + subset] + smaller[n + 1:]

    for smaller in unique_k_partition(collection[1:], k - 1, max_group_size=max_group_size):
        # put `first` in its own subset
        yield [[first]] + smaller

class Filament:
    id: int
    color: str

    def __init__(self, id: int, color: str) -> None:
        self.id = id
        self.color = color
    
    def __str__(self) -> str:
        return f"{self.id + 1}"
    
    def __eq__(self, other: object) -> bool:
        if isinstance(other, Filament):
            return self.id == other.id
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.id)

    def __repr__(self) -> str:
        return str(self)

class FilamentGrouping:
    _groups: list[list[Filament]]

    def __init__(self, groups: list[list[Filament]]) -> None:
        seen = set()
        duplicates = set()
        for filament in [x for group in groups for x in group]:
            if filament in seen:
                duplicates.add(filament)
            seen.add(filament)
        
        if len(duplicates) > 0:
            raise ValueError(f"The filaments {list(duplicates)} are in multiple groups.")

        self._groups = sorted([sorted(list(group), key=lambda x: x.id) for group in groups], key=len)

    @staticmethod
    def from_list(groups: list[list[int]], all_filaments: dict[int, Filament]) -> 'FilamentGrouping':
        base = [[all_filaments[i] for i in group] for group in groups]

        # now create single filament groups for all filaments that are not in any group
        used_filaments = set([f for group in base for f in group])

        base.extend([[f] for f in all_filaments.values() if f not in used_filaments])

        return FilamentGrouping(base)

    def is_grouped(self, left: Filament, right: Filament) -> bool:
        for group in self._groups:
            if left in group and right in group:
                return True

        return False
    
    def find_filament_group(self, filament: Filament) -> list[Filament] | None:
        return next((group for group in self._groups if filament in group), None)
    
    def find_index(self, filaments: list[Filament], filament: Filament) -> int | None:
        filament_group = self.find_filament_group(filament)
        if filament_group is None:
            raise ValueError(f"Filament {filament} is not in any group.")

        for idx, current_filament in enumerate(filaments):
            if current_filament in filament_group:
                return idx

        return None
    
    def __str__(self) -> str:
        return ' '.join([':'.join([str(i) for i in g]) for g in self._groups])

@dataclass
class ToolChange:
    # The layer number where the tool change occurs
    layer: int
    # The id of the current filament
    current_filament: Filament | None
    # The id of the next filament
    next_filament: Filament
    # The index of the tool change in the gcode (T gcode)
    index: int
    # The index of the gcode where the toolchange starts (; CP TOOLCHANGE START)
    start_index: int | None
    # The index of the gcode where the toolchange ends (; CP TOOLCHANGE END)
    end_index: int

    @staticmethod
    def iter_from_gcode(gcode: list[str], colors: dict[int, str]) -> Iterator['ToolChange']:
        current_filament = None
        current_layer = 0
        current_start_index = None
        for idx, line in enumerate(gcode):
            if line.startswith("; CP TOOLCHANGE START"):
                current_start_index = idx

            # This gcode is used to indicate the start of a new layer.
            # It is kept track of to provide context for where tool changes
            # are problematic.
            match = re.match(r'M73 L(\d+)', line)
            if match:
                current_layer = int(match.group(1))
                continue

            # The T\d+ gcode indicates a tool change.
            # For bambulab printers, this will be retracting the current filament
            # and loading the next one.
            #
            # The script will insert a pause before the tool change if the next color
            # is a color that is not currently in the AMS (slots list).
            #
            # Note: There seem to be two special tool changes in the gcode:
            # - T1000
            # - T255
            # These will be ignored by the script.
            match = re.match(r'T(\d+)', line)
            if not match or match.group(1) in ['1000', '1100', '255']:
                continue

            next_filament_id = int(match.group(1))
            next_filament = Filament(next_filament_id, colors[next_filament_id])

            end_index = next((idx for idx, line in enumerate(gcode[idx:], start=idx) if line.startswith("; CP TOOLCHANGE END")), None)

            if end_index is None or (current_start_index is not None and gcode[current_start_index] != "; CP TOOLCHANGE START") or gcode[end_index] != "; CP TOOLCHANGE END":
                raise ValueError(f"Toolchange at line {idx} does not have the marker comments. current_start_index: {current_start_index}, end_index: {end_index}")

            yield ToolChange(current_layer, current_filament, next_filament, idx, current_start_index, end_index)

            current_filament = next_filament

    def is_manual(self, ams: list[Filament], filament_grouping: FilamentGrouping) -> bool:
        # A manual tool change is required if the next filament is not in the AMS and there is another
        # filament of the same group as the next one in the ams.
        return self.next_filament not in ams and filament_grouping.find_index(ams, self.next_filament) is not None

    def is_conflict(self, filament_grouping: FilamentGrouping) -> bool:
        # Assuming that slot 1 is currently printing,
        # and it wants to switch to slot 5 which is grouped with slot 1,
        # then we have a problem.
        #
        # The pause will be inserted before the tool change, but to swap the filament,
        # the printer would have to unload the current filament first.
        # I don't know how to just unload the filament without loading the next one
        # (which would be what the T gcode does).
        #
        # This problem can be solved by manually specifying the color change order.
        if self.current_filament is None:
            return False

        return filament_grouping.is_grouped(self.current_filament, self.next_filament)


@dataclass
class ManualToolChange:
    toolchange: ToolChange
    ams: list[Filament]

    def starts_at(self, idx: int) -> bool:
        return self.toolchange.start_index == idx

class GCode:
    file_path: Path
    plate: int
    gcode: list[str]
    filament_changes_file: Path
    plate_metadata: dict
    ams_size: int
    line_separator: str
    toolchanges: list[ToolChange]

    def __init__(
        self,
        file_path: Path,
        filament_changes_file: Path,
        plate: int | None = None,
        ams_size: int = 4,
        line_separator: str = '\n',
    ) -> None:
        with ZipFile(file_path, 'r') as zf:
            if plate is None:
                for name in zf.namelist():
                    match = re.match(r'Metadata/plate_(\d+)\.gcode', name)
                    if match:
                        plate = int(match.group(1))
                        break

            if plate is None:
                raise ValueError("Could not find a plate in the 3mf file.")

            with zf.open(f'Metadata/plate_{plate}.gcode') as f:
                data = f.read().decode('utf-8')
                line_separator = '\n'
                if '\r\n' in data:
                    line_separator = '\r\n'
            
            with zf.open(f'Metadata/plate_{plate}.json') as f:
                metadata = json.load(f)

        self.file_path = file_path
        self.plate = plate
        self.gcode = data.splitlines()
        self.filament_changes_file = filament_changes_file
        self.plate_metadata = metadata
        self.ams_size = ams_size
        self.line_separator = line_separator
        self.toolchanges = list(ToolChange.iter_from_gcode(self.gcode, dict(zip(self.plate_metadata['filament_ids'], self.plate_metadata['filament_colors']))))

    def all_filaments(self) -> list[Filament]:
        return [Filament(id, color) for id, color in zip(self.plate_metadata['filament_ids'], self.plate_metadata['filament_colors'])]
    

    def find_first_full_ams(self, filament_grouping: FilamentGrouping) -> list[Filament]:
        result_ams = []
        last_ams = []
        i = 0
        for toolchange in self.iter_manual_toolchanges(filament_grouping):
            last_ams = toolchange.ams
            if len(last_ams) > i:
                i = len(last_ams)
                # assuming last_ams = [5, 2, 3]
                # and result_ams = [1]
                # then result_ams should be appended with [2, 3]
                result_ams.extend(last_ams[len(result_ams):])

            if len(result_ams) == self.ams_size:
                return result_ams

        return result_ams


    def iter_manual_toolchanges(self, filament_grouping: FilamentGrouping) -> Iterator[ManualToolChange]:
        ams = []
        for toolchange in self.toolchanges:
            # If a tool change occurs, it will switch from the current filament to the next filament.
            # This can either be done automatically or manually.
            # An automatic tool change will only occur if the next filament is already in the AMS.
            # If the next filament is not in the AMS, a manual tool change is required.

            if toolchange.is_manual(ams, filament_grouping):
                yield ManualToolChange(toolchange, list(ams))

            # First find the index of the filament that will be switched to by the tool change or in case of a manual tool change,
            # the index of a filament that is in the same group as the next filament and in the AMS.
            next_filament_index = filament_grouping.find_index(ams, toolchange.next_filament)
            if next_filament_index is None:
                if len(ams) == self.ams_size:
                    raise ValueError(f"Could not find the index of the next filament {toolchange.next_filament} in the AMS: {ams}")
                
                # The next filament is not in the AMS, but there is still space in the AMS.
                # -> Add it to the AMS.
                ams.append(toolchange.next_filament)
            else:
                # The next filament is in the AMS, so swap it with the filament that is currently at the index.
                ams[next_filament_index] = toolchange.next_filament

    def find_best_mapping(self, max_group_size: int | None = None) -> tuple[FilamentGrouping, int, int] | None:
        all_filaments = self.all_filaments()

        best_combination = None
        best_manual_changes = None
        for combination in unique_k_partition(all_filaments, self.ams_size, max_group_size=max_group_size):
            filament_grouping = FilamentGrouping(combination)

            number_of_manual_toolchanges = sum([1 for _ in self.iter_manual_toolchanges(filament_grouping)])
            if best_manual_changes is None or (number_of_manual_toolchanges <= best_manual_changes):
                best_combination = filament_grouping
                best_manual_changes = number_of_manual_toolchanges

        if best_combination is None or best_manual_changes is None:
            return None

        return (best_combination, best_manual_changes, len(self.toolchanges))

    def list_conflicts(self, filament_grouping: FilamentGrouping, iter_manual_changes: Iterator[ManualToolChange]) -> dict[int, list[ToolChange]]:
        result = {}
        for manual_toolchange in iter_manual_changes:
            toolchange = manual_toolchange.toolchange
            if toolchange.is_conflict(filament_grouping):
                if toolchange.layer not in result:
                    result[toolchange.layer] = []

                result[toolchange.layer].append(toolchange)

        return result
    
    def inform_user(self, manual_toolchange: ManualToolChange, filament_grouping: FilamentGrouping, output: list[str]) -> None:
        (current_toolchange, current_state) = (manual_toolchange.toolchange, manual_toolchange.ams)
        colorswap_index = filament_grouping.find_index(current_state, current_toolchange.next_filament)
        if colorswap_index is None:
            raise ValueError(f"Could not find the index for manual tool change: {current_toolchange} in the AMS: {current_state}")

        color_to_swap_with = current_state[colorswap_index]

        message = f"Manual filament change required in layer {current_toolchange.layer}: Swap color {color_to_swap_with} with {current_toolchange.next_filament}: {current_state}"
        output.append(message)
        log(message)

    def write(self, modified_file: Path, filament_grouping: FilamentGrouping, log_file: Path) -> None:
        # prepare the modified file:
        data = []
        output = []
        # sorts the toolchanges by their index in the gcode
        manual_toolchanges = list(i for i in sorted(self.iter_manual_toolchanges(filament_grouping), key=lambda x: x.toolchange.index))
        iter_manual_toolchanges = iter(manual_toolchanges)
        manual_toolchange = next(iter_manual_toolchanges, None)
        skip_until = None
        for idx, line in enumerate(self.gcode):
            if skip_until is not None:
                if idx == skip_until:
                    skip_until = None
                continue

            # This inserts the special filament change gcode for the problematic tool changes.
            if manual_toolchange is not None and manual_toolchange.toolchange.is_conflict(filament_grouping) and manual_toolchange.starts_at(idx):
                data.extend(paused_filament_change(self.gcode[idx:manual_toolchange.toolchange.end_index + 1]))
                # ensure that all lines are skipped until the end of the tool change gcode (to prevent double insertions)
                skip_until = manual_toolchange.toolchange.end_index

                self.inform_user(manual_toolchange, filament_grouping, output)
                manual_toolchange = next(iter_manual_toolchanges, None)
                continue

            # skip all lines until the next manual tool change
            if manual_toolchange is None or idx != manual_toolchange.toolchange.index:
                data.append(line)
                continue

            data.append(pause_gcode)
            data.append(line)

            self.inform_user(manual_toolchange, filament_grouping, output)
            manual_toolchange = next(iter_manual_toolchanges, None)

        log()
        output.append("")
        message = f"Filament change times: {len(self.toolchanges) - 1}"
        log(message)
        output.append(message)
        message = f"Manual filament change times: {len(manual_toolchanges)}"
        log(message)
        output.append(message)

        with open(log_file, 'w') as fd:
            fd.write(self.line_separator.join(output))

        if modified_file.exists():
            modified_file.unlink()

        shutil.copy(self.file_path, modified_file)
        with UpdateableZipFile(modified_file, 'a') as zf:
            encoded_data = self.line_separator.join(data).encode('utf-8')
            zf.writestr(f'Metadata/plate_{self.plate}.gcode', encoded_data)
            zf.writestr(f'Metadata/plate_{self.plate}.gcode.md5', hashlib.md5(encoded_data).hexdigest().upper())

if len(sys.argv) < 2:
    log(f"Usage: {sys.argv[0]} <3mf gcode file> color:slot [color:slot ...]")
    log(f"For example: {sys.argv[0]} cube.gcode.3mf 5:2 7:3")
    sys.exit(1)

input_file = Path(sys.argv[1])
if not input_file.exists():
    log(f"Error: File {input_file} does not exist.")
    sys.exit(1)

grouped_filaments = []
for arg in sys.argv[2:]:
    grouped_filaments.append([int(i) - 1 for i in arg.split(':')])

gcode = GCode(input_file, input_file.with_name(f"filament_changes.txt"))
if len(grouped_filaments) == 0:
    log("Warning: No color remapping specified. Will now compute the color remapping with the least amount of manual tool changes.")
    mapping = gcode.find_best_mapping(2)
    if mapping is None:
        log("Error: Could not find a mapping.")
        sys.exit(1)
    
    (grouped_filaments, manual_toolchanges, total_toolchanges) = mapping
    log("")
    log(f"Filament change times: {total_toolchanges - 1}")
    log(f"Manual filament change times: {manual_toolchanges}")

    log(f"The best color remapping should be: {grouped_filaments}")
else:
    all_filaments = gcode.all_filaments()
    grouped_filaments = FilamentGrouping.from_list(grouped_filaments, {f.id:f for f in all_filaments})

states = list(gcode.iter_manual_toolchanges(grouped_filaments))
if len(states) == 0:
    log("No manual tool changes are required.")
    sys.exit(0)

log(f"The program assumes that the AMS is loaded initially with the colors: {gcode.find_first_full_ams(grouped_filaments)}")
log(f"The following filaments are grouped together: {grouped_filaments}")

toolchange_conflicts = gcode.list_conflicts(grouped_filaments, states.__iter__())
if len(toolchange_conflicts) > 0:
    log(f"The print order has to be changed in the slicer, so that the following colors are not printed after each other:")

first_layer = None
last_layer = None
conflicts = []

def flatten(list: list[list[T]]) -> list[T]:
    return [item for sublist in list for item in sublist]

for (layer, tcs) in toolchange_conflicts.items():
    v = [(tc.current_filament, tc.next_filament) for tc in tcs]
    if first_layer is None or last_layer is None:
        first_layer = layer
        last_layer = layer
        conflicts.extend(v)
        continue

    if all(c in conflicts for c in v) and layer <= last_layer + 1:
        conflicts.extend(v)
        last_layer = layer
        continue

    log(f"Layer {first_layer} to {last_layer}: {[f'{a} -> {b}' for (a, b) in set(conflicts)]}")
    first_layer = layer
    last_layer = layer + 1
    conflicts = list(v)

if len(conflicts) > 0:
    log(f"Layer {first_layer} to {last_layer}: {[f'{a} -> {b}' for (a, b) in set(conflicts)]}")

if len(toolchange_conflicts) > 0:
    log("")
    log("There are conflicts with the current filament printing order.")
    log("You can change the filament order for these layers in the slicer and re-run the script.")
    log("")
    log("This script will now generate a special gcode file where it resolves these conflicts through a special filament change gcode.")
    log("Therefore you don't have to change the filament order in the slicer.")
    log("")
    log("Warning: This script has only been tested on a P1S, it might break stuff on other printers like the A1 or A1 mini!")
    log("         If you don't want to risk it, change the print order in the slicer.")
    # sys.exit(1)

gcode.write(
    input_file.with_name(f"{input_file.name.split('.')[0]}_with_pauses{''.join(input_file.suffixes)}"),
    grouped_filaments,
    input_file.with_name(f"filament_changes.txt")
)

