"""Minimal PE header reader used to recognise HelixSR and FFX DLLs."""
from pathlib import Path
import re
import struct
from functools import lru_cache


def pe_info(path):
    p = Path(path)
    s = p.stat()
    return _pe_info(str(p), s.st_size, s.st_mtime_ns)


@lru_cache(maxsize=512)
def _pe_info(path, size, modified):
    with Path(path).open('rb') as f:
        data = f.read(4 << 20)
    if data[:2] != b'MZ' or len(data) < 64:
        return dict(arch='not-pe', dx12=False, ffx=False, helix=False)
    offset = struct.unpack_from('<I', data, 0x3c)[0]
    arch = 'unknown'
    if offset + 6 < len(data) and data[offset:offset+4] == b'PE\0\0':
        machine = struct.unpack_from('<H', data, offset + 4)[0]
        arch = 'x64' if machine == 0x8664 else 'unsupported'
    exports, imports = pe_symbols(path, data, offset, size)
    marker = re.search(rb'HelixSR (\d+\.\d+\.\d+)', data)
    return dict(arch=arch, dx12='d3d12.dll' in imports,
                ffx=all(x in exports for x in ['ffxCreateContext', 'ffxDestroyContext', 'ffxDispatch', 'ffxQuery']),
                helix=bool(marker), hlx_fsr=b'HLX FSR 4 (' in data, helix_version=marker.group(1).decode() if marker else None)


def pe_symbols(path, header, offset, size):
    """Read bounded PE tables by RVA, including tables past the initial scan window."""
    exports, imports = set(), set()
    try:
        if offset + 24 > len(header) or header[offset:offset+4] != b'PE\0\0':
            return exports, imports
        sections = struct.unpack_from('<H', header, offset + 6)[0]
        optional_size = struct.unpack_from('<H', header, offset + 20)[0]
        optional = offset + 24
        magic = struct.unpack_from('<H', header, optional)[0]
        dd = 112 if magic == 0x20b else 96 if magic == 0x10b else 0
        table = optional + optional_size
        if not dd or optional_size < dd + 16 or not 1 <= sections <= 96 or table + sections * 40 > len(header):
            return exports, imports
        mappings = []
        for i in range(sections):
            virtual_size, rva, raw_size, raw = struct.unpack_from('<IIII', header, table + i * 40 + 8)
            if raw + raw_size > size:
                return exports, imports
            mappings.append((rva, raw_size, raw))
        with Path(path).open('rb') as stream:
            def read_rva(rva, length):
                for start, count, raw in mappings:
                    if start <= rva and rva + length <= start + count:
                        stream.seek(raw + rva - start)
                        out = stream.read(length)
                        if len(out) != length:
                            raise ValueError('Truncated PE table')
                        return out
                raise ValueError('PE RVA outside mapped file data')

            def string_rva(rva):
                for start, count, raw in mappings:
                    if start <= rva < start + count:
                        out = read_rva(rva, min(512, start + count - rva))
                        if b'\0' not in out:
                            raise ValueError('Unterminated PE symbol')
                        return out.split(b'\0', 1)[0].decode('ascii')
                raise ValueError('PE string RVA outside file')

            export_rva, export_size = struct.unpack_from('<II', header, optional + dd)
            if export_rva and export_size >= 40:
                directory = read_rva(export_rva, 40)
                names_count, names_rva = struct.unpack_from('<II', directory, 24)
                # Offset28 is AddressOfFunctions; AddressOfNames is offset32.
                names_rva = struct.unpack_from('<I', directory, 32)[0]
                if names_count > 8192:
                    raise ValueError('PE export name limit')
                names = read_rva(names_rva, names_count * 4) if names_count else b''
                for i in range(names_count):
                    exports.add(string_rva(struct.unpack_from('<I', names, i * 4)[0]))
            import_rva, import_size = struct.unpack_from('<II', header, optional + dd + 8)
            if import_rva:
                for i in range(min(import_size // 20, 512)):
                    entry = read_rva(import_rva + i * 20, 20)
                    if not any(entry):
                        break
                    imports.add(string_rva(struct.unpack_from('<I', entry, 12)[0]).lower())
    except (ValueError, struct.error, UnicodeError):
        return set(), set()
    return exports, imports
