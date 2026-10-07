"""Select official SVG documents for in-app symbol and footprint previews."""


def svg_entries(data, kind):
    entries = data.get('result') or []
    entries = [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []
    if kind == 'SYMBOL':
        doc_type = 6 if any(entry.get('docType') == 6 for entry in entries) else 2
        return [entry for entry in entries if entry.get('docType') == doc_type]
    return [entry for entry in entries if entry.get('docType') == 4][:1]
