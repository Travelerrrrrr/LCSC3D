"""Append native library entries while preserving the original compound storage."""
from __future__ import annotations

import io
import hashlib
import json
import re
import struct
from pathlib import Path

import olefile

from altium import _block, _parameters, _section, _string_block
from compound_storage import compound_file
from errors import DownloadError
from app_logging import log_event


class Reader:
    def __init__(self, data):
        self.data, self.offset = data, 0

    def take(self, count):
        if count < 0 or self.offset + count > len(self.data):
            raise DownloadError('库索引不完整，未修改原文件')
        value = self.data[self.offset:self.offset + count]
        self.offset += count
        return value

    def uint(self):
        return struct.unpack('<I', self.take(4))[0]

    def block(self):
        size = self.uint()
        return size >> 24, self.take(size & 0xffffff)

    def string(self):
        flag, payload = self.block()
        if flag or not payload or payload[0] + 1 > len(payload):
            raise DownloadError('库名称索引无效')
        return payload[1:1 + payload[0]].decode('cp1252')


def parameters(block):
    result = {}
    for field in block.rstrip(b'\0').split(b'|'):
        key, sep, value = field.partition(b'=')
        if sep:
            key = key.decode('ascii')
            result[key.removeprefix('%UTF8%').upper()] = value.decode('utf-8' if key.startswith('%UTF8%') else 'cp1252')
    return result


def patch_parameters(payload, updates):
    """Preserve unknown fields, spelling and duplicate unmodified parameters."""
    fields = _parameters(updates)[4:].rstrip(b'\0').split(b'|')
    replacement = {field.partition(b'=')[0].removeprefix(b'%UTF8%').upper(): field for field in fields if b'=' in field}
    kept = []
    for field in payload.rstrip(b'\0').split(b'|'):
        key = field.partition(b'=')[0].removeprefix(b'%UTF8%').upper()
        if key not in replacement and field:
            kept.append(field)
    return _block(b'|' + b'|'.join(kept + list(replacement.values())) + b'\0')


def count(value):
    result = int(value)
    if not 0 <= result <= 100000:
        raise DownloadError('库条目数量超出支持范围')
    return result


class LibraryIndex:
    def __init__(self, content, format):
        self.ole = olefile.OleFileIO(io.BytesIO(content), raise_defects=olefile.DEFECT_INCORRECT)
        try:
            self.format = format
            self.roots = {path[0].casefold() for path in self.ole.listdir(streams=True, storages=True)}
            self.sections = {}
            self.key_header, self.key_count, self.raw_keys = b'', 0, b''
            if format == 'SCHLIB':
                reader = Reader(self.read('FileHeader'))
                flag, self.header = reader.block()
                self.values = parameters(self.header)
                if flag or 'Schematic Library' not in self.values.get('HEADER', ''):
                    raise DownloadError('所选文件不是受支持的二进制 SchLib')
                self.binary_index = reader.offset < len(reader.data)
                self.names = ([reader.string() for _ in range(count(reader.uint()))] if self.binary_index else
                              [self.values[f'LIBREF{i}'] for i in range(count(self.values.get('COMPCOUNT', 0)))])
                keys = self.read('SectionKeys', optional=True)
                if keys:
                    self.key_header = Reader(keys).block()[1]
                    values = parameters(self.key_header)
                    self.key_count = count(values.get('KEYCOUNT', 0))
                    self.sections = {values[f'LIBREF{i}'].casefold(): values[f'SECTIONKEY{i}']
                                     for i in range(count(values.get('KEYCOUNT', 0)))}
            else:
                signature = self.read('FileHeader')
                if b'PCB 6.0 Binary Library File' not in signature[:128]:
                    raise DownloadError('所选文件不是受支持的二进制 PcbLib')
                reader = Reader(self.read('Library/Data'))
                flag, self.header = reader.block()
                if flag:
                    raise DownloadError('PcbLib 库头不受支持')
                self.names = [reader.string() for _ in range(count(reader.uint()))]
                keys = self.read('SectionKeys', optional=True)
                if keys:
                    self.raw_keys = keys[4:]
                    r = Reader(keys)
                    self.key_count = count(r.uint())
                    for _ in range(self.key_count):
                        name, section = r.string(), r.string()
                        self.sections[name.casefold()] = section
                    if r.offset != len(keys):
                        raise DownloadError('PcbLib 名称索引尾部格式不受支持')
            if len({name.casefold() for name in self.names}) != len(self.names):
                raise DownloadError('原库有重复条目名，无法安全追加')
            for name in self.names:
                section = self.sections.setdefault(name.casefold(), _section(name))
                if not self.ole.exists([section, 'Data']):
                    raise DownloadError('原库索引指向不存在的条目')
        except Exception:
            self.ole.close()
            raise

    def read(self, path, optional=False):
        if optional and not self.ole.exists(path):
            return b''
        if self.ole.get_size(path) > 64 * 1024 * 1024:
            raise DownloadError('库索引或条目过大')
        return self.ole.openstream(path).read()


def remap_fonts(data, offset):
    reader, records = Reader(data), []
    while reader.offset < len(data):
        flag, payload = reader.block()
        if not flag:
            payload = re.sub(rb'(\|[A-Za-z_]*FontID=)(\d+)',
                             lambda match: match[1] + str(int(match[2]) + offset).encode(), payload, flags=re.I)
        records.append(_block(payload, flag))
    return b''.join(records)


def unique_name(name, reserved, limit=31):
    """Resolve a genuine name collision without adding a supplier number."""
    candidate = name[:limit]
    index = 2
    while candidate.casefold() in reserved:
        suffix = '_' + str(index)
        candidate = name[:limit - len(suffix)] + suffix
        index += 1
    return candidate


def rewrite_symbol_records(data, *, name=None, footprint=None):
    reader, records = Reader(data), []
    while reader.offset < len(data):
        flag, payload = reader.block()
        updates = {}
        if not flag:
            values = parameters(payload)
            if name is not None and values.get('RECORD') == '1':
                updates['LibReference'] = name
            if footprint is not None:
                if values.get('RECORD') == '45' and values.get('MODELTYPE', '').upper() == 'PCBLIB':
                    updates['ModelName'] = footprint
                elif values.get('RECORD') == '41' and values.get('NAME', '').casefold() == 'footprint':
                    updates['Text'] = footprint
        records.append(patch_parameters(payload, updates) if updates else _block(payload, flag))
    return b''.join(records)


def component_section(streams):
    return next(path.split('/')[0] for path in streams if path.endswith('/Data') and not path.startswith('Library/'))


def footprint_signature(streams):
    """Compare native footprint data, excluding names, supplier metadata and pad GUIDs.

    Geometry, pad numbers/stacks/masks, layers, text, height and unknown side streams
    remain significant. Unknown record layouts are never assumed to be equivalent.
    """
    ignored = {'PATTERN', 'DESCRIPTION', 'SUPPLIERPART', 'ITEMGUID', 'REVISIONGUID',
               'SOURCE', 'JLCEDA', 'EASYEDA', 'LIBRARYLICENCE'}
    digest = hashlib.sha256()
    try:
        section = component_section(streams)
        for path in sorted(streams):
            if not path.startswith(section + '/'):
                continue
            key, payload = path[len(section) + 1:], streams[path]
            if key == 'Data':
                reader = Reader(payload)
                reader.string()  # The footprint name is not physical geometry.
                records = []
                while reader.offset < len(reader.data):
                    kind = reader.take(1)[0]
                    if kind not in (1, 2, 4, 5, 11):
                        return None
                    blocks = [reader.block() for _ in range(6 if kind == 2 else 2 if kind == 5 else 1)]
                    if kind == 2:
                        flag, body = blocks[4]
                        if flag or len(body) != 202:
                            return None
                        # Our native pad layout has two randomly assigned GUIDs.
                        blocks[4] = flag, body[:126] + bytes(32) + body[158:]
                    records.append(bytes([kind]) + b''.join(_block(body, flag) for flag, body in blocks))
                payload = b''.join(records)
            elif key in ('Parameters', 'WideStrings'):
                reader = Reader(payload)
                flag, body = reader.block()
                if flag or reader.offset != len(payload):
                    return None
                values = parameters(body)
                if key == 'Parameters':
                    values = {key: value for key, value in values.items() if key not in ignored}
                payload = json.dumps(values, sort_keys=True, ensure_ascii=True).encode('ascii')
            encoded = key.encode('utf-8')
            digest.update(struct.pack('<I', len(encoded)) + encoded + struct.pack('<I', len(payload)) + payload)
    except (DownloadError, ValueError, KeyError, IndexError, StopIteration, struct.error):
        return None
    return digest.digest()


def rename_footprint(streams, name):
    source = component_section(streams)
    section = _section(name)
    result = {}
    for path, payload in streams.items():
        if path.startswith(source + '/'):
            relative = path[len(source) + 1:]
            if relative == 'Data':
                reader = Reader(payload)
                reader.string()
                payload = _string_block(name) + payload[reader.offset:]
            elif relative == 'Parameters':
                flag, body = Reader(payload).block()
                if flag:
                    raise DownloadError('封装参数格式不受支持，无法安全重命名')
                payload = patch_parameters(body, {'PATTERN': name})
            path = section + '/' + relative
        result[path] = payload
    return result


def indexed_component(index, name):
    section = index.sections[name.casefold()]
    return {'/'.join(path): index.read(path) for path in index.ole.listdir()
            if path[0].casefold() == section.casefold()}


def plan_footprints(entries, original=None, check_cancelled=lambda: None):
    """Prefer an equivalent existing footprint; emit a shared new footprint once."""
    known, reserved = {}, {'library', 'fileheader', 'sectionkeys', 'storage'}
    if original is not None:
        old = LibraryIndex(original, 'PCBLIB')
        try:
            reserved.update(old.roots)
            reserved.update(name.casefold() for name in old.names)
            for name in old.names:
                check_cancelled()
                signature = footprint_signature(indexed_component(old, name))
                if signature is not None:
                    known.setdefault(signature, name)
        finally:
            old.ole.close()
    planned, names, emitted, reused, conflicts = [], {}, set(), 0, 0
    for identity, name, streams in entries:
        check_cancelled()
        signature = footprint_signature(streams)
        target = known.get(signature) if signature is not None else None
        if target is not None:
            reused += 1
        else:
            conflicts += name[:31].casefold() in reserved
            target = unique_name(name, reserved)
            reserved.add(target.casefold())
            if signature is not None:
                known[signature] = target
        names[identity] = target
        if target.casefold() not in emitted:
            planned.append((identity, target, rename_footprint(streams, target)))
            emitted.add(target.casefold())
    log_event('INFO', 'library.footprint_reuse', count=len(entries), unique=len(planned), reused=reused,
              name_conflicts=conflicts, existing_library=original is not None)
    return planned, names, reused


def deduplicate_pcblib(content, original=None, check_cancelled=lambda: None):
    index = LibraryIndex(content, 'PCBLIB')
    try:
        entries = [(name, name, indexed_component(index, name)) for name in index.names]
        planned, names, reused = plan_footprints(entries, original, check_cancelled)
        if len(planned) == len(entries) and all(names[name] == name for name in index.names):
            return content, names, []
        sections = {section.casefold() for section in index.sections.values()}
        streams = {'/'.join(path): index.read(path) for path in index.ole.listdir() if path[0].casefold() not in sections}
        for _, _, component in planned:
            streams.update(component)
        streams['Library/Data'] = _block(index.header) + struct.pack('<I', len(planned)) + b''.join(
            _string_block(name) for _, name, _ in planned)
        streams['SectionKeys'] = struct.pack('<I', len(planned)) + b''.join(
            _string_block(name) + _string_block(component_section(component)) for _, name, component in planned)
        # A generated TOC is rebuilt by append_library from the accepted entries.
        streams.pop('Library/ComponentParamsTOC/Header', None)
        streams.pop('Library/ComponentParamsTOC/Data', None)
        skipped = [name for name in index.names if name not in {identity for identity, _, _ in planned}]
        return compound_file(streams, check_cancelled), names, skipped
    finally:
        index.ole.close()


def append_library(original, generated, format, check_cancelled=lambda: None, *, footprint_names=None):
    """Return bytes and skipped names; never alter an existing component."""
    old = new = None
    try:
        duplicates = []
        if format == 'PCBLIB':
            generated, resolved, duplicates = deduplicate_pcblib(generated, original, check_cancelled)
            if footprint_names is not None:
                footprint_names.update(resolved)
        old = LibraryIndex(original, format)
        new = LibraryIndex(generated, format)
        known = {name.casefold() for name in old.names}
        added = [name for name in new.names if name.casefold() not in known]
        skipped = duplicates + [name for name in new.names if name.casefold() in known]
        if not added:
            return original, skipped
        streams, roots = {}, set(old.roots)
        added_records = 0
        sections = dict(old.sections)
        offset = count(old.values.get('FONTIDCOUNT', 0)) if format == 'SCHLIB' else 0
        for name in added:
            check_cancelled()
            source = new.sections[name.casefold()]
            target = source
            if target.casefold() in roots:
                if format == 'PCBLIB':
                    raise DownloadError('原库已有同名存储，无法安全追加封装：' + name)
                index = 2
                while target.casefold() in roots:
                    suffix = '_' + str(index)
                    target = source[:31-len(suffix)] + suffix
                    index += 1
            roots.add(target.casefold())
            sections[name.casefold()] = target
            for path in new.ole.listdir():
                if path[0].casefold() == source.casefold():
                    payload = new.read(path)
                    if format == 'SCHLIB' and path[1:] == ['Data']:
                        records = Reader(payload)
                        while records.offset < len(records.data):
                            records.block()
                            added_records += 1
                        payload = remap_fonts(payload, offset)
                    streams['/'.join([target] + path[1:])] = payload
        names = old.names + added
        if format == 'SCHLIB':
            updates = {'CompCount': len(names), 'Weight': count(old.values.get('WEIGHT', 0)) + added_records,
                       'FontIDCount': offset + count(new.values.get('FONTIDCOUNT', 0))}
            for i, name in enumerate(names):
                updates[f'LibRef{i}'] = name
            for index, name in enumerate(added, len(old.names)):
                source_index = new.names.index(name)
                for key in ('CompDescr', 'PartCount'):
                    updates[f'{key}{index}'] = new.values.get(f'{key}{source_index}'.upper(), '' if key == 'CompDescr' else '2')
            for i in range(1, count(new.values.get('FONTIDCOUNT', 0)) + 1):
                for key in ('FontName', 'Size'):
                    updates[f'{key}{offset+i}'] = new.values[f'{key}{i}'.upper()]
            header = patch_parameters(old.header, updates)
            if old.binary_index:
                header += struct.pack('<I', len(names)) + b''.join(_string_block(name) for name in names)
            keys = {'KeyCount': old.key_count + len(added)}
            for index, name in enumerate(added, old.key_count):
                keys.update({f'LibRef{index}': name, f'SectionKey{index}': sections[name.casefold()]})
            streams.update(FileHeader=header, SectionKeys=patch_parameters(old.key_header, keys))
        else:
            streams['Library/Data'] = _block(old.header) + struct.pack('<I', len(names)) + b''.join(_string_block(name) for name in names)
            streams['SectionKeys'] = struct.pack('<I', old.key_count + len(added)) + old.raw_keys + b''.join(
                _string_block(name) + _string_block(sections[name.casefold()]) for name in added)
            toc = old.read('Library/ComponentParamsTOC/Data', optional=True)
            if toc:
                body = Reader(toc).block()[1].rstrip(b'\0')
                if body and not body.endswith(b'\n'):
                    body += b'\r\n'
                for name in added:
                    data = Reader(new.read([new.sections[name.casefold()], 'Data']))
                    data.string()
                    pads = 0
                    while data.offset < len(data.data):
                        kind = data.take(1)[0]
                        pads += kind == 2
                        for _ in range(6 if kind == 2 else 2 if kind == 5 else 1):
                            data.block()
                    body += f'Name={name}|Pad Count={pads}|Height=0|Description=\r\n'.encode('cp1252')
                streams['Library/ComponentParamsTOC/Data'] = _block(body + b'\0')
        return compound_file(streams, check_cancelled, original=original), skipped
    except (OSError, ValueError, KeyError, IndexError, struct.error) as exc:
        raise DownloadError('无法读取或追加此 AD 库，原文件保留：' + str(exc)) from exc
    finally:
        if old is not None:
            old.ole.close()
        if new is not None:
            new.ole.close()


def read_library(path, format):
    path = Path(path)
    if path.suffix.lower() != '.' + format.lower() or path.stat().st_size > 256 * 1024 * 1024:
        raise DownloadError('请选择对应格式的 AD 库（不超过 256 MiB）')
    payload = path.read_bytes()
    try:
        index = LibraryIndex(payload, format)
        index.ole.close()
    except Exception as exc:
        raise DownloadError('所选 AD 库无法读取：' + str(exc)) from exc
    return payload
