#!/usr/bin/env python

import re
import os
import sys
import shutil
import hashlib
import tempfile
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

def insert_necessary_pauses(gcode: list[str], remap: dict[int, int], filament_changes_file: Path, slots: list[int] = [1, 2, 3, 4]) -> list[str]:
    # The gcode colors are 0-indexed, the bambu studio colors are 1-indexed
    #
    # For ease of use, the argument is 1-indexed and converted to 0-indexed here.

    # The remap dictionary defines which color should be remapped to which slot.
    # For example, given the remap { 5 : 2 }, it will assume that color 5 shares
    # the AMS slot with color 2.
    remap = {k - 1: v - 1 for k, v in remap.items()}
    if len(remap) == 0:
        print("Warning: No remapping necessary, gcode will be left unchanged.")
        return gcode

    # The slots list defines which colors are currently in which AMS slot.
    # The code will update this list as it encounters new colors.
    slots = [s - 1 for s in slots]

    # Add the default colors to the remap dictionary:
    for i, slot in enumerate(slots):
        remap[slot] = i

    result = []
    toolchange_count = 0
    manual_toolchange_count = 0
    current_slot = None
    current_layer = 0
    toolchange_conflicts = {}
    output = []

    for i, line in enumerate(gcode):
        # This gcode is used to indicate the start of a new layer.
        # It is kept track of to provide context for where tool changes
        # are problematic.
        match = re.match(r'M73 L(\d+)', line)
        if match:
            current_layer = int(match.group(1))
            result.append(line)
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
            result.append(line)
            continue

        next_slot = int(match.group(1))
        toolchange_count += 1

        # The first tool change will be the initial loading of the filament.
        # This will not be a color change, so it is ignored.
        if current_slot is None:
            if next_slot not in slots:
                raise ValueError(f'gcode starts with a color {next_slot} that is not in the AMS: {slots}')

            current_slot = next_slot
            result.append(line)
            continue

        # Check if the next color is in the AMS, if not, we need to insert a pause
        if next_slot not in slots:
            message = f"Manual filament change required in layer {current_layer}: Swap color {slots[remap[next_slot]] + 1} with {next_slot + 1}: {[i + 1 for i in slots]}"
            output.append(message)
            print(message)
            result.append(pause_gcode)
            manual_toolchange_count += 1

            # Assuming that slot 1 is currently printing,
            # and it wants to switch to slot 5 which is remapped to slot 1,
            # then we have a problem.
            #
            # The pause will be inserted before the tool change, but to swap the filament,
            # the printer would have to unload the current filament first.
            # I don't know how to just unload the filament without loading the next one
            # (which would be what the T gcode does).
            #
            # This problem can be solved by manually specifying the color change order.
            if remap[current_slot] == remap[next_slot] and current_slot != next_slot:
                if current_layer not in toolchange_conflicts:
                    toolchange_conflicts[current_layer] = []

                toolchange_conflicts[current_layer].append((current_slot, next_slot))

        current_slot = next_slot
        slots[remap[next_slot]] = next_slot

        result.append(line)

    output.append("")
    print("")
    print(f"Filament change times: {toolchange_count - 1}")
    print(f"Manual filament change times: {manual_toolchange_count}")

    if len(toolchange_conflicts) > 0:
        first_layer = None
        last_layer = None
        conflicts = []
        print("")
        print(f"The print order has to be changed in the slicer, so that the following colors are not printed after each other:")
        for layer, v in toolchange_conflicts.items():
            if first_layer is None or last_layer is None:
                first_layer = layer
                last_layer = layer
                conflicts.extend(v)
                continue

            if all(c in conflicts for c in v) and layer <= last_layer + 1:
                conflicts.extend(v)
                last_layer = layer
                continue

            print(f"Layer {first_layer} to {last_layer}: {[f'{a+1} -> {b+1}' for (a, b) in set(conflicts)]}")
            first_layer = layer
            last_layer = layer + 1
            conflicts = list(v)

        if len(conflicts) > 0:
            print(f"Layer {first_layer} to {last_layer}: {[f'{a+1} -> {b+1}' for (a, b) in set(conflicts)]}")

        raise ValueError("Please fix the filament change order in the slicer.")

    with open(filament_changes_file, 'w') as f:
        f.write('\n'.join(output))

    return result

def read_gcode_file(file_path: Path, plate: int = 1) -> list[str]:
    with ZipFile(file_path, 'r') as zf:
        with zf.open(f'Metadata/plate_{plate}.gcode') as f:
            return f.read().decode('utf-8').splitlines()

def write_gcode_file(file_path: Path, data: list[str], plate: int = 1) -> None:
    with UpdateableZipFile(file_path, 'a') as zf:
        encoded_data = '\n'.join(data).encode('utf-8')
        zf.writestr(f'Metadata/plate_{plate}.gcode', encoded_data)
        zf.writestr(f'Metadata/plate_{plate}.gcode.md5', hashlib.md5(encoded_data).hexdigest().upper())

if len(sys.argv) < 3:
    print(f"Usage: {sys.argv[0]} <3mf gcode file> color:slot [color:slot ...]")
    print(f"For example: {sys.argv[0]} cube.gcode.3mf 5:2 7:3")
    sys.exit(1)

input_file = Path(sys.argv[1])
if not input_file.exists():
    print(f"Error: File {input_file} does not exist.")
    sys.exit(1)

remap = {}
for arg in sys.argv[2:]:
    color, slot = arg.split(':')
    remap[int(color)] = int(slot)

new_gcode = insert_necessary_pauses(read_gcode_file(input_file), remap, input_file.with_name(f"filament_changes.txt"))

# copy the original file to the target file
modified_file = input_file.with_name(f"{input_file.name.split('.')[0]}_with_pauses{''.join(input_file.suffixes)}")
if modified_file.exists():
    modified_file.unlink()

shutil.copy(input_file, modified_file)

write_gcode_file(modified_file, new_gcode)