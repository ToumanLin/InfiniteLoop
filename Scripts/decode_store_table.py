"""Decode current client BinaryTable scalar and numeric-list columns using Binary/Reader.lua's format.

Input is an extracted TextAsset, not network traffic. Produces authoritative TSV
for the server table generator. Deliberately rejects unsupported column types.
"""
import csv
import struct
import sys
from pathlib import Path


def decode(data):
    pos = 4

    def integer():
        nonlocal pos
        value = shift = 0
        while True:
            byte = data[pos]
            pos += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value if value <= 0x7fffffff else value - 0x100000000
            shift += 7

    def string():
        nonlocal pos
        end = data.index(0, pos)
        value = data[pos:end].decode('utf-8')
        pos = end + 1
        return value

    header_size = struct.unpack_from('<I', data)[0]
    columns = [(integer(), string()) for _ in range(integer())]
    primary_size = 0
    if integer():
        integer()  # primary key column
        primary_size = integer()
    row_size, row_count, content_size = integer(), integer(), integer()
    content = 4 + header_size + primary_size + row_size
    pool = content + content_size
    pool_columns, strings = set(), []
    if pool < len(data):
        pos = pool
        pool_header = integer()
        if pool_header:
            pos = pool + 4
            column_count, string_count, column_size, offset_size = [integer() for _ in range(4)]
            pos = pool + 4 + pool_header
            pool_columns = {integer() for _ in range(column_count)}
            offsets = pool + 4 + pool_header + column_size
            start = offsets + offset_size
            previous = 0
            for i in range(string_count):
                end = struct.unpack_from('<I', data, offsets + i * 4)[0]
                strings.append(data[start + previous:start + end].rstrip(b'\0').decode('utf-8'))
                previous = end
    pos = content
    rows = []
    for _ in range(row_count):
        row = []
        for index, (kind, name) in enumerate(columns):
            if kind == 2:
                value = strings[integer()] if index in pool_columns else string()
            elif kind == 7:
                value = [format(integer() / 10000, ".4f") for _ in range(integer())]
            elif kind == 6:
                value = [integer() for _ in range(integer())]
            elif kind in (1, 23):
                value = data[pos]
                pos += 1
            elif kind == 14:
                value = integer()
            elif kind == 15:
                value = format(integer() / 10000, '.4f')
            else:
                raise ValueError(f'Unsupported column {name}: {kind}')
            row.append(value)
        rows.append(row)
    assert pos == content + content_size, 'Content size mismatch'
    # The C# table generator represents lists as repeated, numbered TSV columns.
    widths = [max(1, max((len(row[i]) for row in rows), default=0)) if kind in (6, 7) else 1
              for i, (kind, _) in enumerate(columns)]
    headers = [f'{name}[{j + 1}]' if kind in (6, 7) else name
               for (kind, name), width in zip(columns, widths) for j in range(width)]
    expanded = []
    for row in rows:
        flattened = []
        for value, width in zip(row, widths):
            flattened.extend(value + [''] * (width - len(value)) if isinstance(value, list) else [value])
        expanded.append(flattened)
    return headers, expanded


if __name__ == '__main__':
    columns, rows = decode(Path(sys.argv[1]).read_bytes())
    output = Path(sys.argv[2])
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream, delimiter='\t', lineterminator='\n')
        writer.writerow(columns)
        writer.writerows(rows)
    print(f'Decoded {len(rows)} rows to {output}')
