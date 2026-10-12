"""Batch planning and whole-library publication for the KiCad target."""
from pathlib import Path, PurePosixPath

from lcsc3d.core.app_logging import log_event, record_error, safe_part, traced
from lcsc3d.core.errors import Cancelled
from lcsc3d.eda.kicad import FORMATS, FORMAT_LABELS, build_library, conversion_warnings
from lcsc3d.eda.library_files import commit_library


@traced('export.kicad_batch', lambda results, entries, options, *a, **kw: {'target': 'kicad',
        'count': len(entries), 'merge_symbols': options.kicad_merge_symbols,
        'merge_footprints': options.kicad_merge_footprints, 'electrical_policy': options.kicad_electrical_policy}, level='INFO')
def export_batch(results, entries, options, check, *, on_progress=None):
    members = [r for r in results if r.part in entries]
    merged = (options.kicad_merge_symbols and 'KICAD_SYMBOL' in options.formats
              or options.kicad_merge_footprints and 'KICAD_FOOTPRINT' in options.formats)
    groups = [members] if merged else [[r] for r in members]
    for rows in groups:
        if not rows:
            continue
        formats = {r.part: tuple(f for f in r.pending_formats if f in FORMATS) for r in rows}
        root = options.output_root() if merged else entries[rows[0].part][1]
        names = options.kicad_names()
        def libraries(combine, fmt):
            name = names[fmt]
            return {r.part: name if combine else
                (Path(entries[r.part][1]).relative_to(root) / (name + '_' + r.part)).as_posix()
                for r in rows}
        errors = {}
        conversion_errors = set()
        stage = 'convert'
        cancelled = False
        try:
            check()
            if on_progress:
                for row in rows:
                    on_progress(row.part, .85)
            model_assets = {key:payload for r in rows for key,payload in
                            (entries[r.part][2] if len(entries[r.part]) > 2 else {}).items()} if options.bind_3d_models else None
            artifacts, errors = build_library([entries[r.part][0] for r in rows], formats, 'LCSC3D', check,
                electrical_policy=options.kicad_electrical_policy,
                model_assets=model_assets, model_mode=options.kicad_3d_mode,
                symbol_libraries=libraries(options.kicad_merge_symbols, 'KICAD_SYMBOL'),
                footprint_libraries=libraries(options.kicad_merge_footprints, 'KICAD_FOOTPRINT'))
            keep = {fmt for fmt, merge, enabled in (
                ('KICAD_SYMBOL', options.kicad_merge_symbols, options.keep_kicad_symbols),
                ('KICAD_FOOTPRINT', options.kicad_merge_footprints, options.keep_kicad_footprints)) if merge and enabled}
            if keep:
                references = {part: PurePosixPath(a.path).parent.stem + ':' + PurePosixPath(a.path).stem
                              for a in artifacts if a.format == 'KICAD_FOOTPRINT' for part in a.parts}
                separate, failures = build_library([entries[r.part][0] for r in rows],
                    {part: tuple(f for f in selected if f in keep) for part, selected in formats.items()}, 'LCSC3D', check,
                    electrical_policy=options.kicad_electrical_policy,
                    model_assets=model_assets, model_mode=options.kicad_3d_mode,
                    symbol_libraries=libraries(False, 'KICAD_SYMBOL'),
                    footprint_libraries=libraries(False, 'KICAD_FOOTPRINT'), footprint_references=references)
                artifacts.extend(separate)
                errors.update(failures)
                # Publish every footprint before symbols, including retained copies.
                artifacts.sort(key=lambda a: {'KICAD_MODEL':0,'KICAD_FOOTPRINT':1,'KICAD_SYMBOL':2}[a.format])
            conversion_errors = set(errors)
            stage = 'write'
            if on_progress:
                for row in rows:
                    on_progress(row.part, .90)
            written = commit_library(root, artifacts, check, overwrite=True)
            if on_progress:
                for row in rows:
                    on_progress(row.part, .95)
            for artifact, path in written:
                for row in rows:
                    if row.part in artifact.parts:
                        row.files.append(path)
                        if artifact.format in FORMATS and artifact.format not in row.completed_formats:
                            row.completed_formats.append(artifact.format)
                            log_event('INFO', 'download.format_completed', part=safe_part(row.part),
                                      format=artifact.format, bytes=len(artifact.payload))
                log_event('INFO', 'download.library_committed', format=artifact.format, mode='replace',
                          count=len(artifact.parts), bytes=len(artifact.payload))
            for row in rows:
                done = [f for f in FORMATS if f in row.completed_formats]
                if done:
                    folder = Path(entries[row.part][1])
                    row.folder = str(folder if any(Path(path).is_relative_to(folder) for path in row.files) else Path(root))
                    row.message = '；'.join(filter(None, (row.message, '已导出 ' + ' / '.join(FORMAT_LABELS[f] for f in done))))
                    warnings = conversion_warnings(entries[row.part][0], done)
                    if warnings:
                        row.message += '；KiCad：' + '，'.join(warnings)
                        log_event('WARNING', 'export.approximation', part=row.part, target='kicad',
                                  warnings=list(warnings))
        except Exception as exc:
            cancelled = isinstance(exc, Cancelled)
            if not cancelled:
                record_error(exc, 'download.kicad_failed', stage=stage)
            message = '导出已取消' if isinstance(exc, Cancelled) else str(exc)
            for row in rows:
                for fmt in formats[row.part]:
                    errors.setdefault((row.part, fmt), message)
        for row in rows:
            for fmt in formats[row.part]:
                if (row.part, fmt) in errors:
                    log_event('INFO' if cancelled else 'ERROR',
                              'download.format_cancelled' if cancelled else 'download.format_failed',
                              part=safe_part(row.part), format=fmt,
                              stage='convert' if (row.part, fmt) in conversion_errors else stage)
                    message = FORMAT_LABELS[fmt] + '：' + errors[(row.part, fmt)]
                    row.export_errors.append(message)
                    row.message = '；'.join(filter(None, (row.message, message)))
