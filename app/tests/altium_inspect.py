"""Decode generated library records for semantic regression assertions.

This reader is test-only and uses olefile, independently of the Windows COM
writer. It never imports the production exporter or its format helpers.
"""
import io
import struct
import zlib

import olefile


class Reader:
    def __init__(self, data):
        self.data, self.offset = data, 0

    def read(self, size):
        result = self.data[self.offset:self.offset + size]
        if len(result) != size:
            raise ValueError('truncated record')
        self.offset += size
        return result

    def unpack(self, fmt):
        return struct.unpack(fmt, self.read(struct.calcsize(fmt)))

    def block(self):
        header, = self.unpack('<I')
        return header >> 24, self.read(header & 0xffffff)

    def string(self):
        length, = self.unpack('<B')
        return self.read(length).decode('cp1252')


def parameters(payload):
    result = {}
    for field in payload.rstrip(b'\0').split(b'|'):
        if b'=' not in field:
            continue
        key, value = field.split(b'=', 1)
        key = key.decode('ascii')
        encoding = 'utf-8' if key.startswith('%UTF8%') else 'cp1252'
        result[key.removeprefix('%UTF8%').upper()] = value.decode(encoding)
    return result


def _section(ole):
    return next(path[0] for path in ole.listdir() if len(path) == 2 and path[1] == 'Data' and path[0] != 'Library')


def merged_pcb_section(data, part):
    with olefile.OleFileIO(io.BytesIO(data)) as ole:
        return next(path[0] for path in ole.listdir() if len(path) == 2 and path[1] == 'Data'
                    and path[0].endswith('_' + part))


def schematic(data, section=None):
    with olefile.OleFileIO(io.BytesIO(data), raise_defects=olefile.DEFECT_INCORRECT) as ole:
        section = section or _section(ole)
        reader = Reader(ole.openstream([section, 'Data']).read())
        records, pins, fractions = [], [], {}
        while reader.offset < len(reader.data):
            flag, block = reader.block()
            if flag == 1:
                p = Reader(block)
                record, unknown, part, mode, inner, outer, inside, outside = p.unpack('<iBhBBBBB')
                assert record == 2
                description = p.string()
                formal, electrical, flags, length, x, y, color = p.unpack('<BBBhhhi')
                name, number = p.string(), p.string()
                pins.append({'number': number, 'name': name, 'electrical': electrical, 'flags': flags,
                             'x': x, 'y': y, 'length': length, 'part': part,
                             'inner': inner, 'outer': outer, 'color': color})
                records.append({'RECORD': '2'})
            else:
                record = parameters(block)
                records.append(record)
                if record.get('RECORD') == '2':
                    pins.append({'number': record['DESIGNATOR'], 'name': record['NAME'],
                                 'x': float(record['LOCATION.X']) + float(record.get('LOCATION.X_FRAC', 0)) / 100000,
                                 'y': float(record['LOCATION.Y']) + float(record.get('LOCATION.Y_FRAC', 0)) / 100000,
                                 'length': float(record['PINLENGTH']) + float(record.get('PINLENGTH_FRAC', 0)) / 100000,
                                 'electrical': int(record['ELECTRICAL']), 'flags': int(record['PINCONGLOMERATE']),
                                 'part': int(record.get('OWNERPARTID', 1)), 'color': int(record.get('COLOR', 0)),
                                 'inner': int(record.get('SYMBOL_INNEREDGE', 0)),
                                 'outer': int(record.get('SYMBOL_OUTEREDGE', 0))})
        if ole.exists([section, 'PinFrac']):
            r = Reader(ole.openstream([section, 'PinFrac']).read())
            r.block()
            while r.offset < len(r.data):
                flag, block = r.block()
                p = Reader(block)
                marker, = p.unpack('<B')
                assert flag == 1 and marker == 0xd0
                pin = int(p.string())
                size, = p.unpack('<I')
                values = struct.unpack('<iii', zlib.decompress(p.read(size)))
                fractions[pin] = values
            for pin, values in fractions.items():
                for key, delta in zip(('x', 'y', 'length'), values):
                    pins[pin][key] += delta / 100000
        header = parameters(Reader(ole.openstream('FileHeader').read()).block()[1])
        return header, records, pins


def pcb(data, section=None):
    with olefile.OleFileIO(io.BytesIO(data), raise_defects=olefile.DEFECT_INCORRECT) as ole:
        section = section or _section(ole)
        reader = Reader(ole.openstream([section, 'Data']).read())
        name = Reader(reader.block()[1]).string()
        pads, other = [], []
        while reader.offset < len(reader.data):
            kind, = reader.unpack('<B')
            primitive_index = len(pads) + len(other)
            if kind == 2:
                number = Reader(reader.block()[1]).string()
                reader.block(); reader.block(); reader.block()
                main = reader.block()[1]
                stack = reader.block()[1]
                x, y, w, h = struct.unpack_from('<iiii', main, 13)
                drill, = struct.unpack_from('<i', main, 45)
                paste, solder = struct.unpack_from('<ii', main, 86)
                slot, angle = struct.unpack_from('<id', stack, 263)
                rotation, = struct.unpack_from('<d', main, 52)
                offset_x, = struct.unpack_from('<i', stack, 275)
                offset_y, = struct.unpack_from('<i', stack, 403)
                pads.append({'number': number, 'x': x, 'y': y, 'width': w, 'height': h, 'drill': drill,
                             'layer': main[0], 'shape': main[49], 'plated': bool(main[60]),
                             'slot': slot, 'hole_type': stack[262], 'hole_rotation': angle, 'rotation': rotation,
                             'paste': paste, 'solder': solder})
                pads[-1]['offset_x'], pads[-1]['offset_y'] = offset_x, offset_y
                pads[-1]['primitive_index'] = primitive_index
            elif kind in (1, 3, 4, 5, 6, 11, 12):
                block = reader.block()[1]
                other.append((kind, block))
                if kind == 5:
                    reader.block()
            else:
                raise ValueError(f'unknown PCB primitive {kind}')
        expected_count, = struct.unpack('<I', ole.openstream([section, 'Header']).read())
        assert expected_count == len(pads) + len(other)
        return name, pads, other


def region(block):
    """Decode region metadata and all contours, independently of the writer."""
    reader = Reader(block)
    layer, = reader.unpack('<B')
    reader.read(12)
    _, holes = reader.unpack('<BH')
    reader.read(2)
    metadata = parameters(reader.block()[1])
    contours = []
    for _ in range(holes + 1):
        count, = reader.unpack('<I')
        contours.append([reader.unpack('<dd') for _ in range(count)])
    assert reader.offset == len(block)
    return layer, metadata, contours
