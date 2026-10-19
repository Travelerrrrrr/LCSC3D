"""Append native library entries while preserving the original compound storage."""
from __future__ import annotations

import io
import codecs
import hashlib
import json
import re
import struct
from pathlib import Path

import olefile

from lcsc3d.eda.altium import _AnsiString, _block, _component_section, _parameters, _section, _string_block
from lcsc3d.eda.compound_storage import compound_file
from lcsc3d.core.errors import DownloadError
from lcsc3d.core.app_logging import log_event, traced


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
        # Retain the exact ANSI byte identity, including bytes undefined in
        # cp1252. Native AD libraries can use the creator's Windows code page.
        return _AnsiString(payload[1:1 + payload[0]])


PARAMETER_START = re.compile(rb'(?:^|\|)([^\x00-\x1f=|\x7f-\xff]+)=')
INDEX_NAME = re.compile(rb'(?:LIBREF|SECTIONKEY)\d+$')


def mbcs_value_end(block, start, encoding):
    """Find a delimiter at a character boundary, including names like 東A=B."""
    decoder = codecs.getincrementaldecoder(encoding)()
    try:
        for pos in range(start, len(block)):
            if block[pos] == 0:
                decoder.decode(b'', final=True)
                return pos
            if block[pos] == ord('|') and not decoder.getstate()[0] and PARAMETER_START.match(block, pos):
                return pos
            decoder.decode(block[pos:pos + 1])
        decoder.decode(b'', final=True)
    except UnicodeDecodeError:
        return None
    return len(block)


def native_name_values(block, names):
    """Protect complete binary index names before interpreting header fields."""
    raw_names = {name.raw for name in names if isinstance(name, _AnsiString)}
    limit = max(map(len, raw_names), default=0)
    values = {}
    for match in PARAMETER_START.finditer(block):
        key = match[1].upper()
        if re.fullmatch(rb'LIBREF\d+', key):
            for end in range(match.end() + 1, min(match.end() + limit, len(block)) + 1):
                if end == len(block) or block[end] in (0, ord('|')):
                    raw = block[match.end():end]
                    if raw in raw_names:
                        values[key] = raw
    return values


def parameter_spans(block, *, name_encoding=None, native_values=None):
    # Unmodified fields remain byte ranges. Known binary names take precedence;
    # the MBCS fallback is used only for index name fields and must subsequently
    # resolve against real CFB storages before LibraryIndex accepts it.
    starts = list(PARAMETER_START.finditer(block))
    index = 0
    while index < len(starts):
        match = starts[index]
        key = match[1].upper()
        raw = (native_values or {}).get(key)
        protected_end = None
        if raw is not None and block[match.end():match.end() + len(raw)] == raw:
            protected_end = match.end() + len(raw)
        elif name_encoding and INDEX_NAME.fullmatch(key):
            protected_end = mbcs_value_end(block, match.end(), name_encoding)
        index += 1
        while index < len(starts) and protected_end is not None and starts[index].start() < protected_end:
            index += 1
        end = starts[index].start() if index < len(starts) else len(block.rstrip(b'\0'))
        yield match.start(), end, match[1], block[match.end():end]


def parameters(block, *, name_encoding=None, native_values=None):
    result = {}
    for _, _, key, value in parameter_spans(block, name_encoding=name_encoding, native_values=native_values):
        key = key.decode('ascii')
        result[key.removeprefix('%UTF8%').upper()] = (
            value.decode('utf-8') if key.startswith('%UTF8%') else _AnsiString(value))
    return result


def patch_parameters(payload, updates, *, native_values=None):
    """Preserve unknown fields, spelling and duplicate unmodified parameters."""
    replacement = {key.removeprefix('%UTF8%').upper() for key in updates}
    kept, offset = [], 0
    for start, end, key, _ in parameter_spans(payload, native_values=native_values):
        if key.removeprefix(b'%UTF8%').decode('ascii').upper() in replacement:
            kept.append(payload[offset:start])
            offset = end
    kept.append(payload[offset:].rstrip(b'\0'))
    return _block(b''.join(kept) + _parameters(updates)[4:])


def count(value):
    result = int(value)
    if not 0 <= result <= 100000:
        raise DownloadError('库条目数量超出支持范围')
    return result


def indexed_name_count(values, count_key):
    size = count(values.get(count_key, 0))
    actual = {int(key[6:]) for key in values if re.fullmatch(r'LIBREF\d+', key)}
    if actual != set(range(size)):
        raise DownloadError('库名称索引与条目数量不一致，未修改原文件')
    return size


def storage_spellings(value):
    """Possible Unicode spellings of an ANSI index; never rewrite its bytes.

    Resolve against real CFB storage names rather than guessing a code page
    from the machine running this application. A direct spelling wins; a
    legacy fallback must identify exactly one existing component storage.
    """
    yield value, 'cp1252'
    try:
        raw = value.encode('cp1252', errors='surrogateescape')
    except UnicodeEncodeError:
        return
    for encoding in ('utf-8', 'gb18030'):
        try:
            decoded = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if decoded != value:
            yield decoded, encoding


def name_key(name):
    """Case-fold decoded names only; never fold an opaque ANSI byte container."""
    if isinstance(name, _AnsiString):
        if name.encoding is None:
            return ('ansi', name.raw)
        try:
            return name.raw.decode(name.encoding).casefold()
        except UnicodeDecodeError:
            return ('ansi', name.raw)
    return name.casefold()


def raw_name_key(name):
    if isinstance(name, _AnsiString):
        raw = name.raw
    else:
        try:
            raw = name.encode('cp1252')
        except UnicodeEncodeError:
            return ('unicode', name.casefold())
    return ('ansi', raw.lower() if raw.isascii() else raw)


def storage_key(value):
    # CFB names use a case-insensitive directory comparison, not Unicode
    # casefold expansions (which would identify ßA with the distinct ssA).
    return ''.join(upper if len(upper := char.upper()) == 1 else char for char in value)


def resolve_storage(name, section, roots):
    # SectionKeys is authoritative when supplied. Otherwise AD derives a
    # storage name by replacing special characters and truncating to 31 chars.
    value = section if section is not None else name
    spellings = list(storage_spellings(value))
    candidates = [(spelling if section is not None else _section(spelling), encoding)
                  for spelling, encoding in spellings]
    direct = roots.get(storage_key(candidates[0][0]))
    if direct is not None:
        encoding = 'cp1252' if section is None or not section.isascii() else None
        return direct, False, encoding
    # Earlier LCSC3D exports could retain literal '*'/'?' without SectionKeys.
    # Accept those existing spellings too; new exports use AD's normalized form.
    if section is None:
        candidates += [(re.sub(r'[/\\:!]', '_', spelling)[:31], encoding) for spelling, encoding in spellings]
    matches = {roots[storage_key(candidate)] for candidate, _ in candidates[1:] if storage_key(candidate) in roots}
    if len(matches) != 1:
        reason = 'ambiguous_storage' if matches else 'missing_storage'
        log_event('WARNING', 'library.index_resolution_failed', reason=reason, candidate_count=len(matches))
        if matches:
            raise DownloadError('原库名称编码对应多个条目，无法安全追加')
        raise DownloadError('原库索引指向不存在的条目')
    target = matches.pop()
    encodings = {encoding for candidate, encoding in candidates if storage_key(candidate) == storage_key(target)}
    return target, True, next(iter(encodings)) if len(encodings) == 1 else None


class LibraryIndex:
    def __init__(self, content, format):
        try:
            self._read_index(content, format)
        except (DownloadError, KeyError, UnicodeDecodeError):
            if format != 'SCHLIB':
                raise
            # Retry only name-field framing. Other ANSI/UTF-8 metadata keeps
            # its own representation; successful CFB resolution is mandatory.
            self._read_index(content, format, name_encoding='gb18030')

    def _read_index(self, content, format, *, name_encoding=None):
        self.ole = olefile.OleFileIO(io.BytesIO(content), raise_defects=olefile.DEFECT_INCORRECT)
        try:
            self.format = format
            self.roots = {path[0].casefold() for path in self.ole.listdir(streams=True, storages=True)}
            self.sections = {}
            self.raw_names = b''
            self.key_header, self.key_count, self.raw_keys = b'', 0, b''
            self.header_native_values, self.key_native_values = {}, {}
            if format == 'SCHLIB':
                reader = Reader(self.read('FileHeader'))
                flag, self.header = reader.block()
                self.values = parameters(self.header, name_encoding=name_encoding)
                self.binary_index = reader.offset < len(reader.data)
                if self.binary_index:
                    size = count(reader.uint())
                    start = reader.offset
                    self.names = [reader.string() for _ in range(size)]
                    self.raw_names = reader.data[start:reader.offset]
                else:
                    self.names = [self.values[f'LIBREF{i}'] for i in range(indexed_name_count(self.values, 'COMPCOUNT'))]
                self.header_native_values = native_name_values(self.header, self.names)
                self.values = parameters(self.header, native_values=self.header_native_values)
                if flag or 'Schematic Library' not in self.values.get('HEADER', ''):
                    raise DownloadError('所选文件不是受支持的二进制 SchLib')
                keys = self.read('SectionKeys', optional=True)
                if keys:
                    self.key_header = Reader(keys).block()[1]
                    values = parameters(self.key_header, name_encoding=name_encoding,
                                        native_values=native_name_values(self.key_header, self.names))
                    self.key_native_values = {key.encode('ascii'): value.raw for key, value in values.items()
                                              if INDEX_NAME.fullmatch(key.encode('ascii'))
                                              and isinstance(value, _AnsiString)}
                    self.key_count = indexed_name_count(values, 'KEYCOUNT')
                    self.sections = {raw_name_key(values[f'LIBREF{i}']): values[f'SECTIONKEY{i}']
                                     for i in range(self.key_count)}
            else:
                signature = self.read('FileHeader')
                if b'PCB 6.0 Binary Library File' not in signature[:128]:
                    raise DownloadError('所选文件不是受支持的二进制 PcbLib')
                reader = Reader(self.read('Library/Data'))
                flag, self.header = reader.block()
                if flag:
                    raise DownloadError('PcbLib 库头不受支持')
                size = count(reader.uint())
                start = reader.offset
                self.names = [reader.string() for _ in range(size)]
                self.raw_names = reader.data[start:reader.offset]
                keys = self.read('SectionKeys', optional=True)
                if keys:
                    self.raw_keys = keys[4:]
                    r = Reader(keys)
                    self.key_count = count(r.uint())
                    for _ in range(self.key_count):
                        name, section = r.string(), r.string()
                        self.sections[raw_name_key(name)] = section
                    if r.offset != len(keys):
                        raise DownloadError('PcbLib 名称索引尾部格式不受支持')
            data_roots = {storage_key(path[0]): path[0] for path in self.ole.listdir()
                          if len(path) == 2 and path[1].casefold() == 'data'}
            legacy_count = 0
            mappings, self.sections = self.sections, {}
            for name in self.names:
                section, legacy, encoding = resolve_storage(name, mappings.get(raw_name_key(name)), data_roots)
                if isinstance(name, _AnsiString) and not name.raw.isascii():
                    name.encoding = encoding
                self.sections[name_key(name)] = section
                legacy_count += legacy
            self._name_keys = {name_key(name) for name in self.names}
            self._raw_name_index = {raw_name_key(name): name for name in self.names}
            if len(self._name_keys) != len(self.names) or len(self._raw_name_index) != len(self.names):
                raise DownloadError('原库有重复条目名，无法安全追加')
            log_event('DEBUG', 'library.index_resolved', format=format, count=len(self.names),
                      legacy_name_count=legacy_count)
        except Exception:
            self.ole.close()
            raise

    def read(self, path, optional=False):
        if optional and not self.ole.exists(path):
            return b''
        if self.ole.get_size(path) > 64 * 1024 * 1024:
            raise DownloadError('库索引或条目过大')
        return self.ole.openstream(path).read()

    def has_name(self, name):
        if name_key(name) in self._name_keys:
            return True
        other = self._raw_name_index.get(raw_name_key(name))
        # Explicit ASCII aliases (including generated Legacy_* directories)
        # do not establish a code page. Only exact bytes can match those names.
        if other is not None and (isinstance(name_key(name), tuple) or isinstance(name_key(other), tuple)):
            return True
        if other is not None:
            log_event('WARNING', 'library.index_resolution_failed', reason='conflicting_name_encoding')
            raise DownloadError('新旧库的名称字节对应不同条目，无法安全追加')
        return False


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
        model_refs = []
        for path in sorted(streams):
            if not path.startswith(section + '/'):
                continue
            key, payload = path[len(section) + 1:], streams[path]
            if key == 'Data':
                reader = Reader(payload)
                native_name = reader.string()  # The footprint name is not physical geometry.
                records = []
                while reader.offset < len(reader.data):
                    kind = reader.take(1)[0]
                    if kind not in (1, 2, 3, 4, 5, 11, 12):
                        return None
                    blocks = [reader.block() for _ in range(6 if kind == 2 else 2 if kind == 5 else 1)]
                    if kind == 3 and (blocks[0][0] or len(blocks[0][1]) != 31):
                        return None  # Only our verified simple through-via layout.
                    if kind == 2:
                        flag, body = blocks[4]
                        if flag or len(body) != 202:
                            return None
                        # Our native pad layout has two randomly assigned GUIDs.
                        blocks[4] = flag, body[:126] + bytes(32) + body[158:]
                    if kind == 12:
                        flag, body = blocks[0]
                        props = Reader(body[18:])
                        parameter_flag, parameter_body = props.block()
                        if flag or parameter_flag:
                            return None
                        values = parameters(parameter_body)
                        from lcsc3d.eda.altium_models import _pose_key
                        model_refs.append(_pose_key(values,body=True))
                        # The physical identity comes from resolved payload bytes;
                        # display names, GUIDs and weighted checksums are metadata.
                        physical = {k:v for k,v in values.items() if k not in ('MODELID','MODEL.NAME','MODEL.CHECKSUM')}
                        normalized = json.dumps(physical,sort_keys=True,ensure_ascii=True).encode('ascii')
                        blocks[0] = flag,body[:18]+_block(normalized)+body[18+props.offset:]
                    records.append(bytes([kind]) + b''.join(_block(body, flag) for flag, body in blocks))
                payload = b''.join(records)
            elif key in ('Parameters', 'WideStrings'):
                reader = Reader(payload)
                flag, body = reader.block()
                if flag or reader.offset != len(payload):
                    return None
                values = parameters(body, native_values={b'PATTERN': native_name.raw}) if key == 'Parameters' else parameters(body)
                if key == 'Parameters':
                    values = {key: value for key, value in values.items() if key not in ignored}
                payload = json.dumps(values, sort_keys=True, ensure_ascii=True).encode('ascii')
            encoded = key.encode('utf-8')
            digest.update(struct.pack('<I', len(encoded)) + encoded + struct.pack('<I', len(payload)) + payload)
        if model_refs:
            from lcsc3d.eda.altium_models import _model_table, model_payload_identity, _pose_key
            table = _model_table(streams)
            resources = {_pose_key(values):data for values,block,data in table}
            for reference in model_refs:
                data = resources.get(reference)
                if data is None:
                    return None
                digest.update(model_payload_identity(data))
    except (DownloadError, ValueError, KeyError, IndexError, StopIteration, struct.error):
        return None
    return digest.digest()


def rename_footprint(streams, name):
    source = component_section(streams)
    previous_name = Reader(streams[source + '/Data']).string()
    section = _component_section(name)
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
                payload = patch_parameters(body, {'PATTERN': name}, native_values={b'PATTERN': previous_name.raw})
            path = section + '/' + relative
        result[path] = payload
    return result


def indexed_component(index, name):
    section = index.sections[name_key(name)]
    result = {'/'.join(path): index.read(path) for path in index.ole.listdir()
              if path[0].casefold() == section.casefold()}
    if not hasattr(index, '_model_streams'):
        index._model_streams = {'/'.join(path): index.read(path) for path in index.ole.listdir()
                               if len(path)>2 and path[0].casefold()=='library' and path[1].casefold()=='models'}
    result.update(index._model_streams)
    return result


def plan_footprints(entries, original=None, check_cancelled=lambda: None):
    """Prefer an equivalent existing footprint; emit a shared new footprint once."""
    known, reserved = {}, {'library', 'fileheader', 'sectionkeys', 'storage'}
    if original is not None:
        old = LibraryIndex(original, 'PCBLIB')
        try:
            reserved.update(old.roots)
            reserved.update(name_key(name) for name in old.names)
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
        if name_key(target) not in emitted:
            planned.append((identity, target, rename_footprint(streams, target)))
            emitted.add(name_key(target))
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
        added = [name for name in new.names if not old.has_name(name)]
        skipped = duplicates + [name for name in new.names if old.has_name(name)]
        if not added:
            return original, skipped
        streams, roots = {}, set(old.roots)
        added_records = 0
        sections = dict(old.sections)
        offset = count(old.values.get('FONTIDCOUNT', 0)) if format == 'SCHLIB' else 0
        for name in added:
            check_cancelled()
            source = new.sections[name_key(name)]
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
            sections[name_key(name)] = target
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
        if format == 'PCBLIB':
            from lcsc3d.eda.altium_models import aggregate_models
            old_models = {'/'.join(path): old.read(path) for path in old.ole.listdir()
                          if len(path)>2 and path[0].casefold()=='library' and path[1].casefold()=='models'}
            new_models = {'/'.join(path): new.read(path) for path in new.ole.listdir()
                          if len(path)>2 and path[0].casefold()=='library' and path[1].casefold()=='models'}
            model_streams, remapped = aggregate_models(old_models, [{**streams, **new_models}])
            streams = {**streams, **remapped[0], **model_streams}
        names = old.names + added
        if format == 'SCHLIB':
            updates = {'CompCount': len(names), 'Weight': count(old.values.get('WEIGHT', 0)) + added_records,
                       'FontIDCount': offset + count(new.values.get('FONTIDCOUNT', 0))}
            for index, name in enumerate(added, len(old.names)):
                updates[f'LibRef{index}'] = name
                source_index = new.names.index(name)
                for key in ('CompDescr', 'PartCount'):
                    updates[f'{key}{index}'] = new.values.get(f'{key}{source_index}'.upper(), '' if key == 'CompDescr' else '2')
            for i in range(1, count(new.values.get('FONTIDCOUNT', 0)) + 1):
                for key in ('FontName', 'Size'):
                    updates[f'{key}{offset+i}'] = new.values[f'{key}{i}'.upper()]
            header = patch_parameters(old.header, updates, native_values=old.header_native_values)
            if old.binary_index:
                header += struct.pack('<I', len(names)) + old.raw_names + b''.join(_string_block(name) for name in added)
            keys = {'KeyCount': old.key_count + len(added)}
            for index, name in enumerate(added, old.key_count):
                keys.update({f'LibRef{index}': name, f'SectionKey{index}': sections[name_key(name)]})
            streams.update(FileHeader=header, SectionKeys=patch_parameters(old.key_header, keys,
                                                                          native_values=old.key_native_values))
        else:
            streams['Library/Data'] = (_block(old.header) + struct.pack('<I', len(names)) + old.raw_names
                                       + b''.join(_string_block(name) for name in added))
            streams['SectionKeys'] = struct.pack('<I', old.key_count + len(added)) + old.raw_keys + b''.join(
                _string_block(name) + _string_block(sections[name_key(name)]) for name in added)
            toc = old.read('Library/ComponentParamsTOC/Data', optional=True)
            if toc:
                body = Reader(toc).block()[1].rstrip(b'\0')
                if body and not body.endswith(b'\n'):
                    body += b'\r\n'
                for name in added:
                    data = Reader(new.read([new.sections[name_key(name)], 'Data']))
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


@traced('library.read_existing', lambda path, format: {'format': format})
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
