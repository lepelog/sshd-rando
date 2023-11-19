import struct
import tempfile
from constants.itemconstants import ITEM_ITEMFLAGS, ITEM_STORYFLAGS, ITEM_COUNTS
from filepathconstants import (
    ASM_ADDITIONS_DIFFS_PATH,
    ASM_PATCHES_DIFFS_PATH,
    MAIN_NSO_FILE_PATH,
    OUTPUT_ADDITIONAL_SUBSDK,
    OUTPUT_MAIN_NSO,
    STARTFLAGS_FILE_PATH,
    SUBSDK1_FILE_PATH,
)
from io import BytesIO
from pathlib import Path
from collections import Counter

from constants.asmconstants import *

from lz4.block import compress, decompress
from logic.world import World

from patches.asmpatchhelper import NsoOffsets, SegmentHeader
from patches.conditionalpatchhandler import ConditionalPatchHandler

from sslib.fs_helpers import write_bytes, write_u32, write_u8
from sslib.utils import write_bytes_create_dirs
from sslib.yaml import yaml_load, yaml_write


class ASMPatchHandler:
    def compress(self, data: bytes) -> bytes:
        # Uses the lz4 compression.
        return compress(data)[4:]  # trims lz4 junk off the start

    def decompress(self, data: bytes, size: int) -> bytes:
        # Uses the lz4 decompression.
        return decompress(data, size)

    def get_segments(self, nso):
        size = SegmentHeader.SEGMENT_HEADER_SIZE
        nso.seek(size)  # Start of .text SegmentHeader
        text_header = SegmentHeader.bytes_to_segment_header(nso.read(size))
        rodata_header = SegmentHeader.bytes_to_segment_header(nso.read(size))
        data_header = SegmentHeader.bytes_to_segment_header(nso.read(size))

        return text_header, rodata_header, data_header

    def patch_asm(
        self,
        world: World,
        onlyif_handler: ConditionalPatchHandler,
        nso_path: Path,
        asm_diffs_path: Path,
        output_path: Path,
        offsets: NsoOffsets,
        extra_diffs_path: Path | None = None,
    ):
        # Get asm patch diffs.
        asm_patch_diff_paths = tuple(asm_diffs_path.glob("*-diff.yaml"))

        if extra_diffs_path is not None:
            asm_patch_diff_paths += tuple(extra_diffs_path.glob("*-diff.yaml"))

        # Get segment headers.
        nso = BytesIO(nso_path.read_bytes())
        text_header, rodata_header, data_header = self.get_segments(nso)

        nso.seek(text_header.get_file_offset())
        compressed_text = nso.read(
            rodata_header.get_file_offset() - text_header.get_file_offset()
        )
        compressed_rodata = nso.read(
            data_header.get_file_offset() - rodata_header.get_file_offset()
        )
        compressed_data = nso.read()

        # Decompress them.
        text_segment = BytesIO(
            self.decompress(compressed_text, text_header.get_decompressed_size())
        )
        rodata_segment = BytesIO(
            self.decompress(compressed_rodata, rodata_header.get_decompressed_size())
        )
        data_segment = BytesIO(
            self.decompress(compressed_data, data_header.get_decompressed_size())
        )

        for diff_file_name in asm_patch_diff_paths:
            binary_diffs = yaml_load(diff_file_name)

            # Write patch data for each segment.
            for relative_offset, data in binary_diffs.items():
                if type(relative_offset) is not int:
                    if onlyif_handler.evaluate_onlyif(relative_offset):
                        for relative_offset, data2 in data.items():
                            self.write_patch(
                                relative_offset,
                                offsets,
                                text_segment,
                                rodata_segment,
                                data_segment,
                                data2,
                            )
                else:
                    self.write_patch(
                        relative_offset,
                        offsets,
                        text_segment,
                        rodata_segment,
                        data_segment,
                        data,
                    )

                # print(f"data {bytes(data)}")

        new_compressed_text = self.compress(text_segment.getvalue())
        new_compressed_rodata = self.compress(rodata_segment.getvalue())
        new_compressed_data = self.compress(data_segment.getvalue())

        new_text_size_diff = len(new_compressed_text) - len(compressed_text)
        new_rodata_size_diff = len(new_compressed_rodata) - len(compressed_rodata)
        new_data_size_diff = len(new_compressed_data) - len(compressed_data)

        # Update NSO header.
        #
        # Each segment size can change due to the compression.
        # If the new size is smaller, don't bother updating it - there's no point.
        if new_text_size_diff > 0:
            # Update rodata and data segment headers.
            write_u32(
                nso,
                SegmentHeader.SEGMENT_HEADER_SIZE * 2,
                rodata_header.get_file_offset() + new_text_size_diff,
                is_little_endian=True,
            )
            write_u32(
                nso,
                SegmentHeader.SEGMENT_HEADER_SIZE * 3,
                data_header.get_file_offset() + new_text_size_diff,
                is_little_endian=True,
            )

            # Update segment headers in case the rodata size is different too.
            text_header, rodata_header, data_header = self.get_segments(nso)

        if new_rodata_size_diff > 0:
            # Update data segment header.
            write_u32(
                nso,
                SegmentHeader.SEGMENT_HEADER_SIZE * 3,
                data_header.get_file_offset() + new_rodata_size_diff,
                is_little_endian=True,
            )

        if new_data_size_diff > 0:
            # Update .bss size.
            write_u32(
                nso,
                (SegmentHeader.SEGMENT_HEADER_SIZE * 3) + 0xC,
                data_header.get_other() + new_data_size_diff,
                is_little_endian=True,
            )

        # Update segment headers one final time before writing them.
        text_header, rodata_header, data_header = self.get_segments(nso)

        write_bytes(nso, text_header.get_file_offset(), new_compressed_text)
        write_bytes(nso, rodata_header.get_file_offset(), new_compressed_rodata)
        write_bytes(nso, data_header.get_file_offset(), new_compressed_data)

        # Update compressed sizes (each is 4 bytes).
        write_u32(
            nso,
            COMPRESSED_SEGMENT_NSO_OFFSET,
            len(new_compressed_text),
            is_little_endian=True,
        )
        write_u32(
            nso,
            COMPRESSED_SEGMENT_NSO_OFFSET + 4,
            len(new_compressed_rodata),
            is_little_endian=True,
        )
        write_u32(
            nso,
            COMPRESSED_SEGMENT_NSO_OFFSET + 8,
            len(new_compressed_data),
            is_little_endian=True,
        )

        # Patch nso flags to tell consoles not to check the segment hashes.
        # See https://switchbrew.org/wiki/NSO#Flags for more info.
        write_u8(nso, NSO_FLAGS_OFFSET, 0x7, is_little_endian=True)

        write_bytes_create_dirs(output_path, nso.getvalue())

    def write_patch(
        self, relative_offset, offsets, text_segment, rodata_segment, data_segment, data
    ) -> None:
        if relative_offset < offsets.get_rodata_offset():
            file_offset = relative_offset - offsets.get_text_offset()
            write_bytes(text_segment, file_offset, bytes(data))
        elif relative_offset < offsets.get_data_offset():
            file_offset = relative_offset - offsets.get_rodata_offset()
            write_bytes(rodata_segment, file_offset, bytes(data))
        else:
            file_offset = relative_offset - offsets.get_data_offset()
            write_bytes(data_segment, file_offset, bytes(data))

    # Applies both asm patches and additions.
    def patch_all_asm(self, world: World, onlyif_handler: ConditionalPatchHandler):
        print("Applying asm patches")
        self.patch_asm(
            world,
            onlyif_handler,
            MAIN_NSO_FILE_PATH,
            ASM_PATCHES_DIFFS_PATH,
            OUTPUT_MAIN_NSO,
            MAIN_NSO_OFFSETS,
        )

        temp_dir = tempfile.TemporaryDirectory()

        # Keeps the temporary directory only within this with block.
        with temp_dir as temp_dir_name:
            temp_dir_name = Path(temp_dir_name)
            startflags_diff_file_path = temp_dir_name / "startflags-diff.yaml"

            print("Assembling startflags")
            self.patch_startflags(startflags_diff_file_path, world, onlyif_handler)

            print("Applying asm additions")
            self.patch_asm(
                world,
                onlyif_handler,
                SUBSDK1_FILE_PATH,
                ASM_ADDITIONS_DIFFS_PATH,
                OUTPUT_ADDITIONAL_SUBSDK,
                SUBSDK_NSO_OFFSETS,
                extra_diffs_path=temp_dir_name,
            )

    def patch_startflags(
        self, output_path: Path, world: World, onlyif_handler: ConditionalPatchHandler
    ):
        startflags = dict(yaml_load(STARTFLAGS_FILE_PATH))

        storyflags = startflags["Storyflags"]
        sceneflags = startflags["Sceneflags"]
        itemflags = startflags["Itemflags"]
        dungeonflags = startflags["Dungeonflags"]
        start_counts = Counter()

        for item, count in world.starting_item_pool.items():
            item_name = item.name

            if itemflag_data := ITEM_ITEMFLAGS.get(item_name, False):
                if type(itemflag_data) == list:
                    for item_count in range(0, count):
                        itemflags.append(itemflag_data[item_count])
                elif type(itemflag_data) == tuple:
                    for flag in itemflag_data:
                        itemflags.append(flag)
                else:
                    itemflags.append(itemflag_data)

            if storyflag_data := ITEM_STORYFLAGS.get(item_name, False):
                if type(storyflag_data) == list:
                    for item_count in range(count):
                        storyflags.append(storyflag_data[item_count])
                elif type(storyflag_data) == tuple:
                    for flag in storyflag_data:
                        storyflags.append(flag)
                else:
                    storyflags.append(storyflag_data)

            if start_count_data := ITEM_COUNTS.get(item_name, False):
                counter, amount, maximum = start_count_data
                final_count = min(maximum, count)
                start_counts[counter] += amount * final_count

        # Each section is delimited by 0xFFFF
        startflags_data = BytesIO()

        # Storyflags
        for flag in self._get_flags(storyflags, onlyif_handler):
            startflags_data.write(struct.pack("<H", flag))

        startflags_data.write(bytes.fromhex("FFFF"))

        # Sceneflags
        for scene in sceneflags:
            for flag in self._get_flags(sceneflags[scene], onlyif_handler):
                startflags_data.write(
                    struct.pack("<BB", SCENE_NAME_TO_SCENE_INDEX[scene], flag)
                )

        startflags_data.write(bytes.fromhex("FFFF"))

        # Itemflags
        for flag in self._get_flags(itemflags, onlyif_handler):
            startflags_data.write(struct.pack("<H", flag))

        startflags_data.write(bytes.fromhex("FFFF"))

        # Dungeonflags
        for scene in dungeonflags:
            for flag in self._get_flags(dungeonflags[scene], onlyif_handler):
                startflags_data.write(
                    struct.pack("<BB", SCENE_NAME_TO_SCENE_INDEX[scene], flag)
                )

        startflags_data.write(bytes.fromhex("FFFF"))

        start_counts_data = BytesIO()

        # Start counts
        for counter, amount in start_counts.items():
            start_counts_data.write(struct.pack("<HH", counter, amount))

        start_counts_data.write(bytes.fromhex("FFFFFFFF"))

        # Convert startflags_data into a list of bytes.
        startflags_data_bytes = startflags_data.getvalue()
        startflags_data_dict = {
            SUBSDK_STARTFLAG_OFFSET: list(
                struct.unpack("B" * len(startflags_data_bytes), startflags_data_bytes)
            )
        }

        # Same with start_counts_data
        start_counts_data_bytes = start_counts_data.getvalue()
        start_counts_data_dict = {
            SUBSDK_START_COUNTS_OFFSET: list(
                struct.unpack(
                    "B" * len(start_counts_data_bytes), start_counts_data_bytes
                )
            )
        }

        startflags_data_dict.update(start_counts_data_dict)

        yaml_write(output_path, startflags_data_dict)

        # Write the startflag binary to a non-temp file.
        # yaml_write(Path("./test.yaml"), startflags_data_dict)

        # If this fails, the rust struct size will need increasing
        assert len(startflags_data_bytes) < MAX_STARTFLAGS

    def _get_flags(
        self, startflag_section, onlyif_handler: ConditionalPatchHandler
    ) -> tuple:
        flags = []

        for flag in startflag_section:
            if type(flag) is not int:
                condition = tuple(flag.keys())[0]

                if onlyif_handler.evaluate_onlyif(condition):
                    for onlyif_flag in flag[condition]:
                        flags.append(onlyif_flag)
            else:
                flags.append(flag)

        return tuple(flags)
