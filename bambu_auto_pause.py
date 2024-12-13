#!/usr/bin/env python

import re
import os
import sys
import json
import shutil
import hashlib
import tempfile
import itertools
from collections.abc import Iterator
from typing import TypeVar
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile, ZIP_STORED, ZipInfo

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
    # The index of the tool change in the gcode
    index: int

    @staticmethod
    def iter_from_gcode(gcode: list[str], colors: dict[int, str]) -> Iterator['ToolChange']:
        current_filament = None
        current_layer = 0

        for idx, line in enumerate(gcode):
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
            if not match or match.group(1) in ['1000', '255']:
                continue

            next_filament_id = int(match.group(1))
            next_filament = Filament(next_filament_id, colors[next_filament_id])

            yield ToolChange(current_layer, current_filament, next_filament, idx)

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
        plate: int = 1,
        ams_size: int = 4,
        line_separator: str = '\n',
    ) -> None:
        with ZipFile(file_path, 'r') as zf:
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
        last_ams = []
        for toolchange in self.iter_manual_toolchanges(filament_grouping):
            last_ams = toolchange.ams
            if len(last_ams) == self.ams_size:
                return last_ams

        return last_ams


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

    def write(self, modified_file: Path, filament_grouping: FilamentGrouping, log_file: Path) -> None:
        # prepare the modified file:
        data = []
        output = []
        # sorts the toolchanges by their index in the gcode
        manual_toolchanges = list(i for i in sorted(self.iter_manual_toolchanges(filament_grouping), key=lambda x: x.toolchange.index))
        iter_manual_toolchanges = iter(manual_toolchanges)
        manual_toolchange = next(iter_manual_toolchanges, None)
        for idx, line in enumerate(self.gcode):
            # skip all lines until the next manual tool change
            if manual_toolchange is None or idx != manual_toolchange.toolchange.index:
                data.append(line)
                continue

            data.append(pause_gcode)
            data.append(line)

            (current_toolchange, current_state) = (manual_toolchange.toolchange, manual_toolchange.ams)
            colorswap_index = filament_grouping.find_index(current_state, current_toolchange.next_filament)
            if colorswap_index is None:
                raise ValueError(f"Could not find the index for manual tool change: {current_toolchange} in the AMS: {current_state}")

            color_to_swap_with = current_state[colorswap_index]
            
            message = f"Manual filament change required in layer {current_toolchange.layer}: Swap color {color_to_swap_with} with {current_toolchange.next_filament}: {current_state}"
            output.append(message)
            print(message)
            manual_toolchange = next(iter_manual_toolchanges, None)

        print()
        output.append("")
        message = f"Filament change times: {len(self.toolchanges) - 1}"
        print(message)
        output.append(message)
        message = f"Manual filament change times: {len(manual_toolchanges)}"
        print(message)
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
    print(f"Usage: {sys.argv[0]} <3mf gcode file> color:slot [color:slot ...]")
    print(f"For example: {sys.argv[0]} cube.gcode.3mf 5:2 7:3")
    sys.exit(1)

input_file = Path(sys.argv[1])
if not input_file.exists():
    print(f"Error: File {input_file} does not exist.")
    sys.exit(1)

grouped_filaments = []
for arg in sys.argv[2:]:
    grouped_filaments.append([int(i) - 1 for i in arg.split(':')])

gcode = GCode(input_file, input_file.with_name(f"filament_changes.txt"))
if len(grouped_filaments) == 0:
    print("Warning: No color remapping specified. Will now compute the color remapping with the least amount of manual tool changes.")
    mapping = gcode.find_best_mapping(2)
    if mapping is None:
        print("Error: Could not find a mapping.")
        sys.exit(1)
    
    (grouped_filaments, manual_toolchanges, total_toolchanges) = mapping
    print("")
    print(f"Filament change times: {total_toolchanges - 1}")
    print(f"Manual filament change times: {manual_toolchanges}")

    print(f"The best color remapping should be: {grouped_filaments}")
else:
    all_filaments = gcode.all_filaments()
    grouped_filaments = FilamentGrouping.from_list(grouped_filaments, {f.id:f for f in all_filaments})

states = list(gcode.iter_manual_toolchanges(grouped_filaments))
if len(states) == 0:
    print("No manual tool changes are required.")
    sys.exit(0)

print(f"The program assumes that the AMS is loaded initially with the colors: {gcode.find_first_full_ams(grouped_filaments)}")
print(f"The following filaments are grouped together: {grouped_filaments}")

toolchange_conflicts = gcode.list_conflicts(grouped_filaments, states.__iter__())
if len(toolchange_conflicts) > 0:
    print(f"The print order has to be changed in the slicer, so that the following colors are not printed after each other:")

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

    print(f"Layer {first_layer} to {last_layer}: {[f'{a} -> {b}' for (a, b) in set(conflicts)]}")
    first_layer = layer
    last_layer = layer + 1
    conflicts = list(v)

if len(conflicts) > 0:
    print(f"Layer {first_layer} to {last_layer}: {[f'{a} -> {b}' for (a, b) in set(conflicts)]}")

if len(toolchange_conflicts) > 0:
    sys.exit(1)

gcode.write(
    input_file.with_name(f"{input_file.name.split('.')[0]}_with_pauses{''.join(input_file.suffixes)}"),
    grouped_filaments,
    input_file.with_name(f"filament_changes.txt")
)

