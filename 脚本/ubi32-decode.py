#!/usr/bin/env python3
"""Decode a caller-selected UBI32 firmware byte range without executing it.

Opcode/register definitions derived from ubi32-dis.c:
Copyright (C) 2017 Free Software Foundation, Inc.
Contributed by Michael J. Eager.
https://git.codelinaro.org/clo/external-ubicom/ubi32-binutils-gdb/-/blob/
0a921179d2affaa12c07ba3fa328258cf5e0dbc1/opcodes/ubi32-dis.c
Signed relative encodings independently checked against md_apply_fix in:
https://git.codelinaro.org/clo/external-ubicom/ubi32-binutils-gdb/-/blob/
381ca3a0402a6b03670d6b09833f39c3bb1c221b/gas/config/tc-ubi32.c

This Python port uses native definitions, corrects relative sign extension and
the format-4 data-register bank, and makes unsupported/incomplete results explicit.
SPDX-License-Identifier: GPL-3.0-or-later
This program is free software: you can redistribute it and/or modify it under
the GNU General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version.
This program is distributed WITHOUT ANY WARRANTY; without even the implied
warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See
https://www.gnu.org/licenses/gpl-3.0.html for the full license.
"""

import argparse
import json
import os
from pathlib import Path
import sys

ADDRESS_REGISTERS = ("a0", "a1", "a2", "a3", "a4", "a5", "a6", "sp")
GENERAL_REGISTERS = (
    tuple(f"d{number}" for number in range(16))
    + ("res",) * 16
    + ADDRESS_REGISTERS
    + (
        "acc0_hi",
        "acc0_lo",
        "mac_rc15",
        "source3",
        "inst_cnt",
        "csr",
        "rosr",
        "iread_data",
        "int_mask0",
        "int_mask1",
        "int_mask2",
        "int_mask3",
        "pc",
        "trap_cause",
        "acc1_hi",
        "acc1_lo",
        "previous_pc",
        "ucsr",
        "spsr0",
    )
    + ("res",) * 5
    + (
        "chip_id",
        "int_stat0",
        "int_stat1",
        "int_stat2",
        "int_stat3",
        "int_set0",
        "int_set1",
        "int_set2",
        "int_set3",
        "int_clr0",
        "int_clr1",
        "int_clr2",
        "int_clr3",
        "global_ctrl",
        "mt_active",
        "mt_active_set",
        "mt_active_clr",
        "mt_dbg_active",
        "mt_dbg_active_set",
        "mt_en",
        "mt_hpri",
        "mt_hrt",
        "mt_break",
        "mt_break_clr",
        "mt_single_step",
        "mt_min_delay_en",
        "mt_break_set",
        "perr_addr",
        "dcapt",
        "dcapt_pc",
        "dcapt_tnum",
        "mt_dgb_active_clr",
    )
    + tuple(f"scratchpad{number}" for number in range(8))
    + (
        "cfg",
        "mt_i_blocked",
        "mt_d_blocked",
        "mt_i_blocked_set",
        "mt_d_blocked_set",
        "mt_blocked_clr",
        "mt_trap_en",
        "mt_trap",
        "mt_trap_set",
        "mt_trap_clr",
        "sep",
        "mt_btb_en",
        "btb_ctrl",
        "tnum",
    )
    + ("res",) * 10
    + tuple(f"{kind}_range{number}_{field}" for kind, field in (("i", "hi"), ("i", "lo"), ("i", "en"), ("d", "hi"), ("d", "lo"), ("d", "en"), ("i", "user_en"), ("d", "user_en")) for number in range(8))
    + (None,) * 64
)

# Each format-1 entry supplies its operand roles and their byte widths together.
FORMAT1 = (
    (
        None,
        ("suspend", "zero", 4, 4),
        ("flush", "dest", 4, 4),
        ("sync", "dest", 4, 4),
        ("ret", "source", 4, 4),
        ("prefetch", "dest", 4, 4),
        ("tbtst", "source", 4, 4),
        ("bkpt", "source", 4, 4),
        ("sysret", "both", 4, 4),
        ("syscall", "dest", 4, 4),
        ("not.4", "both", 4, 4),
        ("not.2", "both", 2, 2),
        ("move.4", "both", 4, 4),
        ("move.2", "both", 2, 2),
        ("movea", "both", 4, 4),
        ("move.1", "both", 1, 1),
        ("tbrest", "both", 4, 4),
        ("flushni", "dest", 4, 4),
        ("setcsr", "both", 4, 4),
        ("tbset", "both", 4, 4),
        ("tbclr", "both", 4, 4),
        ("ext.2", "both", 2, 2),
        ("inval", "dest", 4, 4),
        ("ext.1", "both", 1, 1),
        ("swapb.2", "both", 2, 2),
        ("swapb.4", "both", 4, 4),
        ("fxcnv", "both", 4, 4),
        ("fxcnv.t", "both", 4, 4),
        ("lea.4", "both", 4, 4),
        ("lea.2", "both", 2, 4),
        ("pdec", "pdec", 4, 4),
        ("lea.1", "both", 1, 4),
    ),
    (
        ("moveio.4", "both", 4, 4),
        ("moveio.2", "both", 2, 2),
        ("moveio.1", "both", 1, 1),
        ("moveiop.4", "both", 4, 4),
        ("moveiop.2", "both", 2, 2),
        ("moveiop.1", "both", 1, 1),
    )
    + (None,) * 26,
)
# Format-3 entry: extension-0/1 names and their respective operand byte widths.
FORMAT3 = (
    ("and.2", "and.1", 2, 1),
    ("and.4", "and.4", 4, 4),
    ("or.2", "or.1", 2, 1),
    ("or.4", "or.4", 4, 4),
    ("xor.2", "xor.1", 2, 1),
    ("xor.4", "xor.4", 4, 4),
    ("add.2", "add.1", 2, 1),
    ("add.4", "add.4", 4, 4),
    ("addc", "addc", 4, 4),
    ("sub.2", "sub.1", 2, 1),
    ("sub.4", "sub.4", 4, 4),
    ("subc", "subc", 4, 4),
    ("pxblend", "pxblend.t", 4, 4),
    ("pxvi", "pxvi", 4, 4),
    None,
    None,
)
FORMAT4 = (
    ("fxhi", 4),
    ("muls", 4),
    ("fxhi.s", 4),
    ("mulu", 4),
    None,
    ("mulf", 4),
    ("btst", 4),
    None,
    ("crcgen", 4),
    ("mac", 4),
    ("lsl.1", 1),
    ("lsr.1", 1),
    ("asr.1", 1),
    None,
    None,
    None,
    ("lsl.4", 4),
    ("lsl.2", 2),
    ("lsr.4", 4),
    ("lsr.2", 2),
    ("asr.4", 4),
    ("asr.2", 2),
    ("bfextu", 4),
    None,
    ("bfrvrs", 4),
    None,
    ("shftd", 4),
    None,
    ("merge", 4),
    None,
    ("shmrg.2", 2),
    ("shmrg.1", 1),
)
DSP_NAMES = (
    "muls",
    "macs",
    "mulu",
    "macu",
    "mulf",
    "macf",
    "macs.4",
    "macus",
    "muls.4",
    "msuf",
    "mulu.4",
    "macu.4",
    None,
    None,
    None,
    None,
    "madd.4",
    "madd.2",
    "msub.4",
    "msub.2",
) + (None,) * 12
CONDITIONS = ("f", "lo", "hs", "eq", "ge", "gt", "hi", "le", "ls", "lt", "mi", "ne", "pl", "t", "vc", "vs")


def signed(value, bits):
    return value - (1 << bits) if value & (1 << (bits - 1)) else value


def register(number, bank="general"):
    name = f"d{number}" if bank == "data" else GENERAL_REGISTERS[number]
    if name is None:
        text = f"reg0x{number:02x}"
    elif name == "res":
        text = f"res[0x{number:02x}]"
    else:
        text = name
    return {"kind": "reg", "reg": number, "bank": bank, "name": name, "text": text}


def immediate(value, address=False):
    text = f"#0x{value:08x}" if address else f"#{value}"
    return {"kind": "imm", "value": value, "text": text}


def operand(raw, size, pdec=False):
    mode = (raw >> 8) & 7
    if mode == 0:
        return immediate(signed(raw & 255, 8))
    if mode == 1:
        return register(raw & 255)
    areg = (raw >> 5) & 7
    name = ADDRESS_REGISTERS[areg]
    result = {"kind": "mem", "areg": areg, "size": size, "offset": 0, "update": 0, "index": None, "pre": None}
    if mode == 2:
        update = signed(raw & 15, 4) * size
        result.update(update=update, pre=bool(raw & 16))
        result["text"] = f"{update}({name})++" if result["pre"] else f"({name}){update}++"
    elif mode == 3:
        result["index"] = raw & 15
        result["text"] = f"({name},d{result['index']})"
    else:
        offset = (((raw & 0x300) >> 3) + (raw & 31)) * size
        if pdec:
            # PDEC uses the inverse scaled displacement; encoded zero means 512.
            offset = (-offset) & 0x1FC if offset else 512
        result["offset"] = offset
        result["text"] = f"{offset}({name})" if offset else f"({name})"
    return result


def decode(word, pc):
    major = (word >> 27) & 31
    source, dest = word & 0x7FF, (word >> 16) & 0x7FF
    result = {"pc": pc, "word": word, "major": major, "op": "unknown", "args": [], "status": "undefined_opcode"}
    if major in (0, 1):
        ext = (word >> 11) & 31
        result["extension"] = ext
        entry = FORMAT1[major][ext]
        if entry is not None:
            name, form, source_size, dest_size = entry
            result.update(op=name, status="decoded")
            if form in ("dest", "both", "pdec"):
                result["args"].append(operand(dest, dest_size))
            if form in ("source", "both", "pdec"):
                result["args"].append(operand(source, source_size, form == "pdec"))
    elif major == 2:
        ext = (word >> 21) & 31
        result["extension"] = ext
        entry = FORMAT4[ext]
        if entry is not None:
            name, size = entry
            count = (word >> 11) & 31
            # This five-bit field names d0..d31, not a special/general register.
            count_arg = register(count, "data") if word & (1 << 26) else immediate(count)
            result.update(op=name, status="decoded", args=[operand(source, size), count_arg])
            if ext != 6:
                result["args"].insert(0, register((word >> 16) & 15, "data"))
    elif major == 3:
        result.update(extension=(word >> 21) & 31, status="unsupported_fp")
    elif major in (4, 5):
        result.update(op="bset" if major == 4 else "bclr", status="decoded", args=[operand(dest, 4), operand(source, 4), immediate((word >> 11) & 31)])
    elif major == 6:
        ext = (word >> 21) & 31
        result["extension"] = ext
        if DSP_NAMES[ext] is not None:
            result.update(op=DSP_NAMES[ext], status="unsupported_dsp")
    elif 8 <= major <= 23:
        ext = (word >> 15) & 1
        result["extension"] = ext
        entry = FORMAT3[major - 8]
        if entry is not None:
            size = entry[2 + ext]
            result.update(op=entry[ext], status="decoded", args=[operand(dest, size), operand(source, size), register((word >> 11) & 15, "data")])
    elif major == 24:
        result.update(op="cmpi", status="decoded", args=[operand(source, 2), immediate(signed((word >> 11) & 0xFFFF, 16))])
    elif major == 25:
        value = signed(word & 0xFFFF, 16)
        result.update(op="movei", status="decoded", args=[operand(dest, 2), immediate(value)])
        if dest == 0 and value == 0:
            result.update(op="nop", args=[])
    elif major == 26:
        condition = (word >> 23) & 15
        # Extend the field before scaling/addition, then wrap the address to 32 bits.
        target = (pc + signed(word & 0x1FFFFF, 21) * 4) & 0xFFFFFFFF
        result.update(op="jmp" + CONDITIONS[condition], status="decoded", condition=condition, c=(word >> 21) & 1, p=(word >> 22) & 1, target=target, args=[immediate(target, address=True)])
    elif major in (27, 28, 29):
        value = (((word >> 24) & 7) << 21) | (word & 0x1FFFFF)
        destination = register(32 + ((word >> 21) & 7))
        if major == 27:
            target = (pc + signed(value, 24) * 4) & 0xFFFFFFFF
            result.update(op="call", status="decoded", target=target, args=[destination, immediate(target, address=True)])
        else:
            value = ((value << 7) | (0x80000000 if major == 29 else 0)) & 0xFFFFFFFF
            result.update(op="moveai", status="decoded", constant=value, args=[destination, immediate(value, address=True)])
    elif major in (30, 31):
        ext = (word >> 11) & 31
        result["extension"] = ext
        if ext <= 2:
            displacement = (((word >> 24) & 7) << 13) | (((word >> 16) & 31) << 8)
            displacement |= (((word >> 8) & 7) << 5) | (word & 31)
            displacement = signed(displacement * 4, 18)
            areg = (word >> 5) & 7
            name = ADDRESS_REGISTERS[areg]
            memory = {"kind": "mem", "areg": areg, "size": 4, "offset": displacement, "update": 0, "index": None, "pre": None, "text": f"{displacement}({name})"}
            result.update(op=("calli", "leai", "calli.b")[ext], status="decoded", args=[register(32 + ((word >> 21) & 7)), memory])
    text = result["op"]
    if major == 26:
        text += ("" if result["c"] else ".s") + (".t" if result["p"] else ".f")
    if result["status"] in ("unsupported_fp", "unsupported_dsp"):
        text += " [unsupported operands]"
    if result["args"]:
        text += " " + ",".join(arg["text"] for arg in result["args"])
    result["text"] = text
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Offline UBI32 integer/control disassembly of a selected firmware byte range.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Run on the host with Python 3 and its standard library; no router, compiler,
external opcode file or root privilege is needed. IMAGE is a readable, seekable,
blocking saved file. The caller supplies its mapping and byte order. A byte range
is not proof of executable code; no processor-variant eligibility is inferred.

Mapping: --base is the virtual address of the byte at --file-offset. --start and
--end values at least base are virtual addresses; smaller values are relative
byte offsets within that mapped region. End is exclusive. The selected first
byte defines the first word boundary; no implicit alignment or image detection.
Omitted end means the image's remaining bytes, obtained from the opened file.

Output: stdout has address/word/text lines, or one instruction JSON object per
line with --json. JSON retains exact bytes/file_offset/pc and structured operands.
Offsets, updates and memory widths are in bytes. Relative targets wrap at 32 bits;
indirect operands do not claim a known runtime address. General reserved/unnamed
register slots are explicit; format-4 count fields use the data-register bank.
Undefined opcodes are data, not guessed mnemonics. FP and DSP operand decoding
are unsupported (known DSP names are retained). Partial words are not padded.

Exit: 0 selected range processed (including undefined/unsupported words);
1 incomplete selected word or actual input/output failure; 2 argument error;
130 interrupted. SIGINT stops further reads and closes/flushes the owned streams.
Earlier complete lines remain valid after failure/interruption; an output error
may leave a partial final line. Stderr explains operational failures. No file is
modified; redirection owns the saved output, with no temporary files to remove.

Examples (image.bin is your saved firmware; mapping comes from its load layout):
  ubi32-decode.py image.bin --start 0x100 --end 0x140
  ubi32-decode.py image.bin --start 0x40000100 --end 0x40000140
  ubi32-decode.py image.bin --start 0x100 --end 0x140 --json > instructions.jsonl
  ubi32-decode.py image.bin --file-offset 0x40 --base 0x40000000 --end 0x20 --endian big

Definitions: CodeLinaro ubi32-binutils-gdb 0a921179d2affaa12c07ba3fa328258cf5e0dbc1,
opcodes/ubi32-dis.c, FSF 2017 / Michael J. Eager. GPL-3.0-or-later, no warranty;
source URLs and signed-relative reference are in this file's opening notice.
https://www.gnu.org/licenses/gpl-3.0.html
""",
    )
    parser.add_argument("image", type=Path, help="saved firmware file, opened read-only")
    parser.add_argument("--base", default="0x40000000", help="mapped virtual base (default: 0x40000000)")
    parser.add_argument("--file-offset", default="0", help="absolute image byte offset of mapping (default: 0)")
    parser.add_argument("--start", default="0", help="inclusive virtual address or mapped byte offset (default: 0)")
    parser.add_argument("--end", help="exclusive virtual address or mapped byte offset (default: image end)")
    parser.add_argument("--endian", choices=("little", "big"), default="little", help="word byte order (default: little)")
    parser.add_argument("--json", action="store_true", help="emit instruction records as JSON lines")
    args = parser.parse_args()
    for name in ("base", "file_offset", "start", "end"):
        value = getattr(args, name)
        if value is not None:
            try:
                value = int(value, 0)
            except ValueError:
                parser.error(f"--{name.replace('_', '-')} requires an integer")
            if value < 0:
                parser.error(f"--{name.replace('_', '-')} cannot be negative")
            setattr(args, name, value)
    start = args.start - args.base if args.start >= args.base else args.start
    end = args.end
    if end is not None and end >= args.base:
        end -= args.base
    if end is not None and end < start:
        parser.error("--end must not precede --start")
    position, raw, status = start, b"", 0
    try:
        with args.image.open("rb") as image:
            if end is None:
                end = os.fstat(image.fileno()).st_size - args.file_offset
                if end < start:
                    parser.error("selected start is beyond the image's mapped bytes")
            image.seek(args.file_offset + start)
            while position < end:
                requested = min(4, end - position)
                raw = image.read(requested)
                if len(raw) == 4:
                    record = decode(int.from_bytes(raw, args.endian), args.base + position)
                else:
                    reason = "image_eof" if len(raw) < requested else "range_end"
                    record = {
                        "pc": args.base + position,
                        "word": None,
                        "op": None,
                        "args": [],
                        "status": "truncated",
                        "requested_bytes": requested,
                        "available_bytes": len(raw),
                        "missing_word_bytes": 4 - len(raw),
                        "reason": reason,
                        "text": f"truncated: {reason}, {len(raw)}/4 instruction bytes",
                    }
                    status = 1
                record["file_offset"] = args.file_offset + position
                record["bytes"] = raw.hex()
                if args.json:
                    sys.stdout.write(json.dumps(record) + "\n")
                else:
                    word_text = f"{record['word']:08x}" if record["word"] is not None else raw.hex()
                    sys.stdout.write(f"{record['pc']:08x}: {word_text}  {record['text']}\n")
                position += len(raw)
                raw = b""
                if status:
                    break
    except OSError as error:
        print(f"decode failed at requested file offset {args.file_offset + position:#x} " f"(current bytes {raw.hex()}): {error}", file=sys.stderr)
        status = 1
    except KeyboardInterrupt:
        print(f"decode interrupted at requested file offset {args.file_offset + position:#x} " f"(current bytes {raw.hex()})", file=sys.stderr)
        status = 130
    finally:
        # The independent CLI owns stdout through this final flush-and-close.
        try:
            sys.stdout.close()
        except (OSError, KeyboardInterrupt) as error:
            print(f"output close failed: {error or 'interrupted'}", file=sys.stderr)
            if status == 0:
                status = 130 if isinstance(error, KeyboardInterrupt) else 1
    return status


if __name__ == "__main__":
    sys.exit(main())
