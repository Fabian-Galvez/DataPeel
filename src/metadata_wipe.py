"""DataPeel. Show the metadata your pictures carry, then wipe it.

Put this exe in a folder, drop pictures in, run it. It lists the metadata in every
picture, marks the parts that can identify you, and wipes the lot on one click.
JPEG, PNG, GIF and SVG are read and wiped. RAW files are listed and left alone.

Keep the original is OFF by default, so the originals are overwritten and no folder
is made. Tick it and the originals are left alone, with clean copies written to a
"stripped" subfolder instead.

Standard library only. No exiftool, no Pillow, nothing to install.

The file map, top to bottom:
    tag names     the EXIF, GPS and TIFF numbers written out as readable names
    EXIF reading  walk the TIFF block a JPEG or a PNG carries
    JPEG          list the segments, rebuild the file without the metadata ones
    PNG           the same idea over PNG chunks
    GIF           walk the blocks, drop the comment and application blocks
    SVG           text XML, drop the comments, metadata and RDF or XMP elements
    the folder    find the pictures next to the app (or the folder named on the command line), read them, wipe them
    window        the Tkinter window, before on the left, after on the right
    the icon      the window icon, a PNG held as text in this file
"""

import re
import struct
import sys
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont

APP_NAME = "DataPeel"

JPEG_TYPES = {".jpg", ".jpeg", ".jpe"}
PNG_TYPES = {".png"}
GIF_TYPES = {".gif"}
SVG_TYPES = {".svg"}
RAW_TYPES = {".nef", ".cr2", ".cr3", ".arw", ".dng", ".orf", ".raf", ".rw2", ".pef", ".srw"}

# ---------------------------------------------------------------- tag names

TIFF_TAGS = {
    0x010E: "ImageDescription", 0x010F: "Make", 0x0110: "Model",
    0x0112: "Orientation", 0x011A: "XResolution", 0x011B: "YResolution",
    0x0128: "ResolutionUnit", 0x0131: "Software", 0x0132: "ModifyDate",
    0x013B: "Artist", 0x013C: "HostComputer", 0x8298: "Copyright",
    0x0100: "ImageWidth", 0x0101: "ImageHeight", 0x0102: "BitsPerSample",
    0x9C9B: "XPTitle", 0x9C9C: "XPComment", 0x9C9D: "XPAuthor",
    0x9C9E: "XPKeywords", 0x9C9F: "XPSubject",
    0x8769: "ExifOffset", 0x8825: "GPSOffset",
}

EXIF_TAGS = {
    0x829A: "ExposureTime", 0x829D: "FNumber", 0x8827: "ISO",
    0x9000: "ExifVersion", 0x9003: "DateTimeOriginal", 0x9004: "CreateDate",
    0x9010: "OffsetTime", 0x9011: "OffsetTimeOriginal", 0x9012: "OffsetTimeDigitized",
    0x9201: "ShutterSpeedValue", 0x9202: "ApertureValue", 0x9204: "ExposureCompensation",
    0x9207: "MeteringMode", 0x9209: "Flash", 0x920A: "FocalLength",
    0x927C: "MakerNote", 0x9286: "UserComment",
    0x9290: "SubSecTime", 0x9291: "SubSecTimeOriginal", 0x9292: "SubSecTimeDigitized",
    0xA001: "ColorSpace", 0xA002: "ExifImageWidth", 0xA003: "ExifImageHeight",
    0xA005: "InteropOffset", 0xA20E: "FocalPlaneXResolution",
    0xA402: "ExposureMode", 0xA403: "WhiteBalance", 0xA406: "SceneCaptureType",
    0xA430: "CameraOwnerName", 0xA431: "BodySerialNumber",
    0xA432: "LensInfo", 0xA433: "LensMake", 0xA434: "LensModel",
    0xA435: "LensSerialNumber", 0xA420: "ImageUniqueID",
}

GPS_TAGS = {
    0x0000: "GPSVersionID", 0x0001: "GPSLatitudeRef", 0x0002: "GPSLatitude",
    0x0003: "GPSLongitudeRef", 0x0004: "GPSLongitude", 0x0005: "GPSAltitudeRef",
    0x0006: "GPSAltitude", 0x0007: "GPSTimeStamp", 0x0008: "GPSSatellites",
    0x000B: "GPSDOP", 0x000C: "GPSSpeedRef", 0x000D: "GPSSpeed",
    0x0010: "GPSImgDirectionRef", 0x0011: "GPSImgDirection",
    0x0012: "GPSMapDatum", 0x001B: "GPSProcessingMethod",
    0x001D: "GPSDateStamp", 0x001F: "GPSHPositioningError",
}

# Anything here can identify you, your kit, your home or your movements.
DANGEROUS = {
    "Make", "Model", "Software", "HostComputer", "Artist", "Copyright",
    "ImageDescription", "UserComment", "CameraOwnerName", "BodySerialNumber",
    "LensSerialNumber", "LensMake", "LensModel", "ImageUniqueID", "MakerNote",
    "XPTitle", "XPComment", "XPAuthor", "XPKeywords", "XPSubject",
    "ModifyDate", "DateTimeOriginal", "CreateDate",
    "OffsetTime", "OffsetTimeOriginal", "OffsetTimeDigitized",
    "SubSecTime", "SubSecTimeOriginal", "SubSecTimeDigitized",
}

# Whole blocks that are dangerous no matter what is inside them.
# PNG-text is in the list because a text chunk is free-form and routinely carries a
# name, a tool, or where the file came from. GIF-app is an application block this
# app cannot name, and those carry anything the writing tool stored.
# Metadata and RDF are the SVG side: an editor writes the author under dc:creator
# inside a metadata element.
DANGEROUS_GROUPS = {"GPS", "XMP", "IPTC", "Photoshop", "Comment", "PNG-text",
                    "GIF-app", "Metadata", "RDF"}

TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


class Tag:
    """One line of metadata, ready to print."""

    def __init__(self, group, name, value):
        self.group = group
        self.name = name
        self.value = value

    @property
    def risky(self):
        return self.group in DANGEROUS_GROUPS or self.name in DANGEROUS

    def line(self):
        return "%-10s %-24s %s" % (self.group, self.name, self.value)


def flat(text, limit=60):
    """One squeezed line of text, short enough to sit in the list."""
    return " ".join(text.split())[:limit]


# ---------------------------------------------------------------- EXIF reading

def read_value(block, entry_off, endian):
    """Pull one IFD entry out of the TIFF block."""
    tag, typ, count = struct.unpack(endian + "HHL", block[entry_off:entry_off + 8])
    size = TYPE_SIZE.get(typ, 0) * count
    if size == 0 or size > len(block):
        return tag, None

    if size <= 4:
        raw = block[entry_off + 8:entry_off + 8 + size]
    else:
        off = struct.unpack(endian + "L", block[entry_off + 8:entry_off + 12])[0]
        if off + size > len(block):
            return tag, None
        raw = block[off:off + size]

    if typ == 2:
        return tag, raw.split(b"\x00")[0].decode("utf-8", "replace").strip()
    if typ == 7:
        return tag, "<%d bytes>" % len(raw)
    if typ in (5, 10):
        parts = []
        fmt = endian + ("ll" if typ == 10 else "LL")
        for i in range(count):
            num, den = struct.unpack(fmt, raw[i * 8:i * 8 + 8])
            parts.append("%g" % (num / den) if den else "0")
        return tag, " ".join(parts)
    if typ in (3, 8):
        fmt = endian + ("h" if typ == 8 else "H")
        vals = [struct.unpack(fmt, raw[i * 2:i * 2 + 2])[0] for i in range(count)]
        return tag, " ".join(str(v) for v in vals)
    if typ in (4, 9):
        fmt = endian + ("l" if typ == 9 else "L")
        vals = [struct.unpack(fmt, raw[i * 4:i * 4 + 4])[0] for i in range(count)]
        return tag, " ".join(str(v) for v in vals)
    if typ == 1:
        return tag, " ".join(str(b) for b in raw)
    return tag, "<type %d>" % typ


def read_ifd(block, offset, endian, names, group, out, seen):
    """Walk one IFD, and follow the Exif and GPS sub-IFDs it points at."""
    if offset in seen or offset + 2 > len(block):
        return
    seen.add(offset)

    count = struct.unpack(endian + "H", block[offset:offset + 2])[0]
    if count > 512:
        return

    for i in range(count):
        entry = offset + 2 + i * 12
        if entry + 12 > len(block):
            return
        tag, value = read_value(block, entry, endian)
        if value is None:
            continue

        if group == "EXIF" and tag == 0x8769:
            read_ifd(block, int(value), endian, EXIF_TAGS, "EXIF", out, seen)
            continue
        if group == "EXIF" and tag == 0x8825:
            read_ifd(block, int(value), endian, GPS_TAGS, "GPS", out, seen)
            continue
        if tag in (0xA005,):
            continue

        name = names.get(tag) or EXIF_TAGS.get(tag) or "Tag-0x%04X" % tag
        out.append(Tag(group, name, value))


def read_exif(payload):
    """Parse the TIFF block that sits inside an APP1 Exif segment."""
    out = []
    block = payload[6:]
    if len(block) < 8:
        return out
    endian = "<" if block[:2] == b"II" else ">"
    try:
        first = struct.unpack(endian + "L", block[4:8])[0]
        read_ifd(block, first, endian, TIFF_TAGS, "EXIF", out, set())
    except (struct.error, ValueError, IndexError):
        pass
    return out


# ---------------------------------------------------------------- JPEG

def jpeg_segments(data):
    """Yield (marker, start, end) for every marker segment before the scan."""
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker == 0xD8 or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            i += 2
            continue
        if marker == 0xDA:                       # start of scan, image data follows
            yield marker, i, len(data)
            return
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        yield marker, i, i + 2 + length
        i += 2 + length


def read_jpeg(path):
    data = path.read_bytes()
    tags = []
    if data[:2] != b"\xff\xd8":
        return tags

    for marker, start, end in jpeg_segments(data):
        payload = data[start + 4:end]
        if marker == 0xE0:
            tags.append(Tag("JFIF", "JFIFBlock", "<%d bytes>" % len(payload)))
        elif marker == 0xE1:
            if payload[:6] == b"Exif\x00\x00":
                tags.extend(read_exif(payload))
            elif b"ns.adobe.com/xap" in payload[:64]:
                tags.append(Tag("XMP", "XMPBlock", "<%d bytes>" % len(payload)))
            else:
                tags.append(Tag("APP1", "Unknown", "<%d bytes>" % len(payload)))
        elif marker == 0xE2:
            tags.append(Tag("ICC", "ICCProfile", "<%d bytes>" % len(payload)))
        elif marker == 0xED:
            tags.append(Tag("Photoshop", "IPTCBlock", "<%d bytes>" % len(payload)))
        elif marker == 0xEE:
            tags.append(Tag("Adobe", "AdobeBlock", "<%d bytes>" % len(payload)))
        elif 0xE3 <= marker <= 0xEF:
            tags.append(Tag("APP%d" % (marker - 0xE0), "Block", "<%d bytes>" % len(payload)))
        elif marker == 0xFE:
            text = payload.decode("utf-8", "replace").strip()
            tags.append(Tag("Comment", "Comment", text[:60]))
    return tags


def strip_jpeg(data):
    """Rebuild the file with every APPn and comment segment dropped."""
    out = bytearray(b"\xff\xd8")
    for marker, start, end in jpeg_segments(data):
        if 0xE0 <= marker <= 0xEF or marker == 0xFE:
            continue
        out += data[start:end]
    return bytes(out)


# ---------------------------------------------------------------- PNG

PNG_SIG = b"\x89PNG\r\n\x1a\n"
PNG_DROP = {b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME", b"iCCP"}


def png_chunks(data):
    i = 8
    while i + 8 <= len(data):
        length = struct.unpack(">L", data[i:i + 4])[0]
        kind = data[i + 4:i + 8]
        end = i + 12 + length
        if end > len(data):
            return
        yield kind, i, end, data[i + 8:i + 8 + length]
        if kind == b"IEND":
            return
        i = end


def read_png(path):
    data = path.read_bytes()
    tags = []
    if data[:8] != PNG_SIG:
        return tags

    for kind, _s, _e, payload in png_chunks(data):
        name = kind.decode("ascii", "replace")
        if kind in (b"tEXt", b"zTXt"):
            key = payload.split(b"\x00")[0].decode("utf-8", "replace")
            rest = payload.split(b"\x00", 1)[1] if b"\x00" in payload else b""
            value = rest.decode("utf-8", "replace").strip()[:60] if kind == b"tEXt" else "<compressed>"
            tags.append(Tag("PNG-text", key or name, value))
        elif kind == b"iTXt":
            key = payload.split(b"\x00")[0].decode("utf-8", "replace")
            tags.append(Tag("PNG-text", key or name, "<%d bytes>" % len(payload)))
        elif kind == b"eXIf":
            tags.extend(read_exif(b"Exif\x00\x00" + payload))
        elif kind == b"tIME":
            tags.append(Tag("PNG", "ModifyTime", "<%d bytes>" % len(payload)))
        elif kind == b"iCCP":
            key = payload.split(b"\x00")[0].decode("utf-8", "replace")
            tags.append(Tag("ICC", key or "ICCProfile", "<%d bytes>" % len(payload)))
    return tags


def strip_png(data):
    out = bytearray(PNG_SIG)
    for kind, start, end, _p in png_chunks(data):
        if kind in PNG_DROP:
            continue
        out += data[start:end]
    return bytes(out)


# ---------------------------------------------------------------- GIF

GIF_SIGS = (b"GIF87a", b"GIF89a")

# The 11 byte identifier an application extension starts with.
GIF_LOOP = b"NETSCAPE2.0"          # the loop count, a gif requires it to repeat
GIF_XMP = b"XMP DataXMP"           # an XMP packet, the metadata Adobe tools write
GIF_ICC = b"ICCRGBG1012"           # an embedded color profile


def gif_subblocks(data, i):
    """Read a chain of sub-blocks. Returns the joined data and the offset after it.

    A gif stores long data in runs of at most 255 bytes, each run behind a length
    byte, and a zero length byte ends the chain.
    """
    parts = []
    while i < len(data):
        size = data[i]
        if size == 0:
            return b"".join(parts), i + 1
        parts.append(data[i + 1:i + 1 + size])
        i += 1 + size
    return b"".join(parts), len(data)


def gif_blocks(data):
    """Yield (kind, label, start, end, payload) for every block after the header.

    kind is "ext" for an extension, "image" for one frame, "trailer" for the last
    byte. label is the extension label, or None. payload is the sub-block data with
    the length bytes taken out.
    """
    if len(data) < 13 or data[:6] not in GIF_SIGS:
        return
    packed = data[10]
    i = 13
    if packed & 0x80:                            # global color table
        i += 3 * (2 ** ((packed & 7) + 1))

    while i < len(data):
        marker = data[i]
        if marker == 0x3B:                       # trailer, the last byte
            yield "trailer", None, i, i + 1, b""
            return
        if marker == 0x21:                       # extension block
            if i + 2 > len(data):
                return
            label = data[i + 1]
            payload, end = gif_subblocks(data, i + 2)
            yield "ext", label, i, end, payload
            i = end
            continue
        if marker == 0x2C:                       # image descriptor, one frame
            if i + 10 > len(data):
                return
            local = data[i + 9]
            j = i + 10
            if local & 0x80:                     # local color table
                j += 3 * (2 ** ((local & 7) + 1))
            j += 1                               # LZW minimum code size
            _pixels, end = gif_subblocks(data, j)
            yield "image", None, i, end, b""
            i = end
            continue
        return                                   # not a block shape this app handles


def read_gif(path):
    data = path.read_bytes()
    tags = []
    if data[:6] not in GIF_SIGS:
        return tags

    for kind, label, _s, _e, payload in gif_blocks(data):
        if kind != "ext":
            continue
        if label == 0xFE:
            tags.append(Tag("Comment", "Comment", flat(payload.decode("utf-8", "replace"))))
        elif label == 0x01:
            # A plain text extension holds 12 bytes of layout, then the text.
            tags.append(Tag("Comment", "PlainText",
                            flat(payload[12:].decode("utf-8", "replace"))))
        elif label == 0xFF:
            ident = payload[:11]
            name = ident.decode("ascii", "replace")
            rest = payload[11:]
            if ident == GIF_LOOP:
                count = struct.unpack("<H", rest[1:3])[0] if len(rest) >= 3 else 0
                tags.append(Tag("GIF", name, "loop count %d, 0 means forever" % count))
            elif ident == GIF_XMP:
                tags.append(Tag("XMP", name, "<%d bytes>" % len(rest)))
            elif ident == GIF_ICC:
                tags.append(Tag("ICC", name, "<%d bytes>" % len(rest)))
            else:
                tags.append(Tag("GIF-app", name, "<%d bytes>" % len(rest)))
    return tags


def strip_gif(data):
    """Rebuild the gif without its comment, plain text and application blocks.

    NETSCAPE2.0 is kept, because it holds the loop count and a gif without it plays
    once and stops. Graphic control blocks, image descriptors, local color tables
    and image data are copied byte for byte, so the frames and their delays do not
    change.
    """
    if data[:6] not in GIF_SIGS:
        return data
    packed = data[10]
    head = 13
    if packed & 0x80:
        head += 3 * (2 ** ((packed & 7) + 1))

    out = bytearray(data[:head])
    for kind, label, start, end, payload in gif_blocks(data):
        if kind == "ext" and label in (0xFE, 0x01):
            continue
        if kind == "ext" and label == 0xFF and payload[:11] != GIF_LOOP:
            continue
        out += data[start:end]
    if not out.endswith(b"\x3b"):                # a file cut short has no trailer
        out += b"\x3b"
    return bytes(out)


# ---------------------------------------------------------------- SVG

SVG_COMMENT = re.compile(r"<!--.*?-->", re.S)
SVG_METADATA = re.compile(r"<metadata\b[^>]*(?:/>|>.*?</\s*metadata\s*>)", re.S | re.I)
SVG_TITLE = re.compile(r"<(title|desc)\b[^>]*>(.*?)</\s*\1\s*>", re.S | re.I)

# An element holding text and no child element, such as <dc:creator>A Name</dc:creator>.
SVG_LEAF = re.compile(r"<([\w.\-]+:[\w.\-]+)\b[^>]*>([^<]+)</\s*[\w.\-]+:[\w.\-]+\s*>")

SVG_START = re.compile(r"<([\w.\-]+):([\w.\-]+)\b([^>]*?)(/?)>")

# Element prefixes that mean RDF or XMP. Inkscape writes the author under
# dc:creator, Illustrator writes an XMP packet inside x:xmpmeta.
RDF_PREFIXES = {"rdf", "x", "xmp", "xmpMM", "xmpRights", "dc", "cc", "exif",
                "tiff", "photoshop", "pdf", "xap", "xapMM", "Iptc4xmpCore"}


def svg_text(data):
    """Decode for text work in a way that encodes back to the same bytes."""
    return data.decode("utf-8", "surrogateescape")


def svg_rdf_spans(text):
    """Every outermost RDF or XMP element, as (start, end, name)."""
    spans = []
    for match in SVG_START.finditer(text):
        prefix, local, _attrs, closed = match.groups()
        if prefix not in RDF_PREFIXES:
            continue
        if any(s <= match.start() < e for s, e, _n in spans):
            continue                             # already inside one being removed
        name = "%s:%s" % (prefix, local)
        if closed == "/":
            spans.append((match.start(), match.end(), name))
            continue
        close = re.search(r"</\s*%s\s*>" % re.escape(name), text[match.end():])
        end = match.end() + close.end() if close else match.end()
        spans.append((match.start(), end, name))
    return spans


def read_svg(path):
    data = path.read_bytes()
    text = svg_text(data)
    tags = []
    if "<svg" not in text:
        return tags

    for match in SVG_COMMENT.finditer(text):
        tags.append(Tag("Comment", "XMLComment", flat(match.group(0)[4:-3])))

    for match in SVG_METADATA.finditer(text):
        tags.append(Tag("Metadata", "metadata", "<%d bytes>" % len(match.group(0))))

    for start, end, name in svg_rdf_spans(text):
        chunk = text[start:end]
        group = "XMP" if name.split(":")[0] in ("x", "xmp", "xmpMM") else "RDF"
        whole = SVG_LEAF.fullmatch(chunk)
        if whole and flat(whole.group(2)):
            tags.append(Tag(group, name, flat(whole.group(2))))
            continue
        tags.append(Tag(group, name, "<%d bytes>" % len(chunk)))
        for leaf in SVG_LEAF.finditer(chunk):
            value = flat(leaf.group(2))
            if value:
                tags.append(Tag(group, leaf.group(1), value))

    for match in SVG_TITLE.finditer(text):
        tags.append(Tag("SVG", match.group(1).lower(), flat(match.group(2))))
    return tags


def svg_whole_line(text, start, end):
    """If the removed element sits alone on its line, take the line with it."""
    line_start = text.rfind("\n", 0, start) + 1
    if text[line_start:start].strip():
        return start, end
    line_end = text.find("\n", end)
    if line_end == -1 or text[end:line_end].strip():
        return start, end
    return line_start, line_end + 1


def strip_svg(data):
    """Drop the comments, the metadata elements and the RDF or XMP elements.

    Everything else is left byte for byte, including the xml declaration, the
    DOCTYPE, the title, the desc and the whitespace. The document is never written
    back out through an XML library, because that reorders attributes and drops the
    declaration.
    """
    text = svg_text(data)
    spans = [(m.start(), m.end()) for m in SVG_COMMENT.finditer(text)]
    spans += [(m.start(), m.end()) for m in SVG_METADATA.finditer(text)]
    spans += [(s, e) for s, e, _n in svg_rdf_spans(text)]
    if not spans:
        return data

    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    out = []
    at = 0
    for start, end in merged:
        start, end = svg_whole_line(text, start, end)
        start = max(start, at)
        if end <= at:
            continue
        out.append(text[at:start])
        at = end
    out.append(text[at:])
    return "".join(out).encode("utf-8", "surrogateescape")


# ---------------------------------------------------------------- one file

READ_TYPES = JPEG_TYPES | PNG_TYPES | GIF_TYPES | SVG_TYPES


def read_file(path):
    """Read the metadata out of one file, picked by extension."""
    suffix = path.suffix.lower()
    if suffix in JPEG_TYPES:
        return read_jpeg(path)
    if suffix in PNG_TYPES:
        return read_png(path)
    if suffix in GIF_TYPES:
        return read_gif(path)
    if suffix in SVG_TYPES:
        return read_svg(path)
    return []


def strip_file(data, suffix):
    """Return the same picture with its metadata blocks taken out."""
    if suffix in JPEG_TYPES:
        return strip_jpeg(data)
    if suffix in PNG_TYPES:
        return strip_png(data)
    if suffix in GIF_TYPES:
        return strip_gif(data)
    return strip_svg(data)


# ---------------------------------------------------------------- the folder

def app_folder():
    """The folder of pictures to work on.

    A folder given on the command line takes priority. Otherwise the built exe uses the
    folder it sits in (not the temp folder PyInstaller unpacks to), and the
    script run from source uses the current working directory, so
    `python3 src/metadata_wipe.py` works from inside any folder of pictures.
    """
    if len(sys.argv) > 1:
        return Path(sys.argv[1]).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


class Picture:
    def __init__(self, path):
        self.path = path
        self.before = []
        self.after = []
        self.state = "ok"
        self.size = ""

    @property
    def name(self):
        return self.path.name

    def read(self):
        suffix = self.path.suffix.lower()
        kb = self.path.stat().st_size / 1024
        self.size = "%.0f kB" % kb if kb < 1024 else "%.1f MB" % (kb / 1024)
        if suffix in RAW_TYPES:
            self.state = "raw"
            return
        if suffix in READ_TYPES:
            self.before = read_file(self.path)
        else:
            self.state = "unsupported"

    def wipe(self, keep_original, stripped_dir):
        """Wipe the metadata.

        keep_original on:  the original is left in place, the clean copy goes to stripped\\
        keep_original off: the original is overwritten and no folder is made
        """
        if self.state != "ok":
            return
        data = self.path.read_bytes()
        suffix = self.path.suffix.lower()
        cleaned = strip_file(data, suffix)

        if keep_original:
            stripped_dir.mkdir(exist_ok=True)
            target = stripped_dir / self.name
        else:
            target = self.path

        target.write_bytes(cleaned)

        self.after = read_file(target)
        self.state = "wiped"


def find_pictures(folder):
    out = []
    for item in sorted(folder.iterdir()):
        if not item.is_file():
            continue
        suffix = item.suffix.lower()
        if suffix in READ_TYPES or suffix in RAW_TYPES:
            out.append(Picture(item))
    return out


# ---------------------------------------------------------------- window

BG = "#ffffff"
FG = "#1f2328"
HEAD_BG = "#ddf4ff"
RED_FG = "#82071e"
RED_BG = "#ffebe9"
GREEN_FG = "#0a3622"
GREEN_BG = "#e6ffec"
GRAY = "#656d76"


# The window icon, a 64 px PNG written as base64 text. Without it every Tk
# window shows the Tk feather. It is in the source rather than a file beside
# it, so a downloaded metadata_wipe.py still has its icon and the exe needs no
# --add-data flag. Made from icon/datapeel-icon.svg, steps in
# docs/architecture.md.
ICON_PNG = """
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
"""


class Window:
    def __init__(self):
        self.folder = app_folder()
        self.stripped = self.folder / "stripped"
        self.pictures = []
        self.wiped = False
        self.kept = True

        self.root = tk.Tk()
        # The folder NAME, not the path. A path in a title bar carries the
        # account name into every screenshot anyone ever takes of this app.
        self.root.title("%s  -  %s" % (APP_NAME, self.folder.name or str(self.folder)))
        self.root.geometry("1180x720")

        # The image is held on the instance because Tk drops an image
        # nothing refers to, and the icon then goes blank. The except
        # covers Tk 8.5, which cannot read a PNG.
        try:
            self.icon = tk.PhotoImage(data="".join(ICON_PNG.split()))
            self.root.iconphoto(True, self.icon)
        except tk.TclError:
            pass
        self.root.configure(bg=BG)

        mono = tkfont.nametofont("TkFixedFont").copy()
        mono.configure(size=9)

        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill=tk.X, padx=10, pady=(10, 6))

        self.button = tk.Button(bar, text="Wipe all metadata", height=2,
                                font=("Segoe UI", 10, "bold"), command=self.on_wipe)
        self.button.pack(side=tk.LEFT, padx=(0, 12))

        self.keep = tk.BooleanVar(value=False)
        self.keepbox = tk.Checkbutton(
            bar, variable=self.keep, bg=BG, fg=FG, activebackground=BG,
            font=("Segoe UI", 9), anchor="w", justify=tk.LEFT,
            text="Keep the original\nOff means the original is overwritten",
            command=self.set_status)
        self.keepbox.pack(side=tk.LEFT, padx=(0, 16))

        self.status = tk.Label(bar, text="", bg=BG, fg=FG, justify=tk.LEFT,
                               font=("Segoe UI", 9), anchor="w")
        self.status.pack(side=tk.LEFT, fill=tk.X, expand=True)

        panes = tk.Frame(self.root, bg=BG)
        panes.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        panes.columnconfigure(0, weight=1, uniform="p")
        panes.columnconfigure(1, weight=1, uniform="p")
        panes.rowconfigure(1, weight=1)

        tk.Label(panes, text="BEFORE", bg=BG, fg=RED_FG,
                 font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(panes, text="AFTER", bg=BG, fg=GREEN_FG,
                 font=("Segoe UI", 9, "bold")).grid(row=0, column=1, sticky="w")

        self.left = tk.Text(panes, font=mono, wrap="none", bg=BG, fg=FG,
                            borderwidth=1, relief="solid", padx=6, pady=6)
        self.right = tk.Text(panes, font=mono, wrap="none", bg=BG, fg=FG,
                             borderwidth=1, relief="solid", padx=6, pady=6)
        self.left.grid(row=1, column=0, sticky="nsew", padx=(0, 4))
        self.right.grid(row=1, column=1, sticky="nsew", padx=(4, 0))

        bar2 = tk.Scrollbar(panes, orient="vertical", command=self.scroll_both)
        bar2.grid(row=1, column=2, sticky="ns")
        for text in (self.left, self.right):
            text.configure(yscrollcommand=self.on_scroll)
            text.bind("<MouseWheel>", self.on_wheel)
            text.tag_configure("head", background=HEAD_BG, font=(mono.cget("family"), 9, "bold"))
            text.tag_configure("removed", foreground=RED_FG, background=RED_BG)
            text.tag_configure("kept", foreground=GREEN_FG, background=GREEN_BG)
            text.tag_configure("risk", foreground=RED_FG, background=RED_BG,
                               font=(mono.cget("family"), 9, "bold"))
            text.tag_configure("plain", foreground=FG)
            text.tag_configure("quiet", foreground=GRAY)
        self.scrollbar = bar2

        self.load()

    # -------------------------------------------------- scrolling, kept in step
    def scroll_both(self, *args):
        self.left.yview(*args)
        self.right.yview(*args)

    def on_scroll(self, first, last):
        self.scrollbar.set(first, last)
        self.left.yview_moveto(first)
        self.right.yview_moveto(first)

    def on_wheel(self, event):
        step = -1 if event.delta > 0 else 1
        self.left.yview_scroll(step * 3, "units")
        self.right.yview_scroll(step * 3, "units")
        return "break"

    # -------------------------------------------------- drawing
    def write(self, text, line, tag):
        text.insert(tk.END, line + "\n", tag)

    def render(self):
        for text in (self.left, self.right):
            text.configure(state=tk.NORMAL)
            text.delete("1.0", tk.END)

        if not self.pictures:
            self.write(self.left, "No pictures in this folder.", "quiet")
            self.write(self.left, "", "plain")
            self.write(self.left, "Drop .jpg, .png, .gif or .svg files next to the exe.", "quiet")
            for text in (self.left, self.right):
                text.configure(state=tk.DISABLED)
            return

        for pic in self.pictures:
            head = " %s   %s " % (pic.name, pic.size)
            self.write(self.left, head, "head")
            self.write(self.right, head, "head")

            if pic.state == "raw":
                msg = "  RAW file. Not touched, a wipe can break how it renders."
                self.write(self.left, msg, "quiet")
                self.write(self.right, msg, "quiet")
            elif pic.state == "unsupported":
                msg = "  %s files are not supported. Not touched." % pic.path.suffix.upper()
                self.write(self.left, msg, "quiet")
                self.write(self.right, msg, "quiet")
            else:
                left_lines = pic.before
                right_lines = pic.after

                if not self.wiped:
                    if not left_lines:
                        self.write(self.left, "  Already clean. Nothing in this file.", "quiet")
                    for tag in left_lines:
                        style = "risk" if tag.risky else "plain"
                        mark = "  !  " if tag.risky else "     "
                        self.write(self.left, mark + tag.line(), style)
                    self.write(self.right, "  Waiting. Press Wipe all metadata.", "quiet")
                    pad = max(len(left_lines), 1) - 1
                else:
                    for tag in left_lines:
                        self.write(self.left, "  - " + tag.line(), "removed")
                    if not left_lines:
                        self.write(self.left, "  (nothing was in this file)", "quiet")
                    for tag in right_lines:
                        self.write(self.right, "  + " + tag.line(), "kept")
                    if not right_lines:
                        self.write(self.right, "  + clean, no metadata left", "kept")
                    pad = 0

                # Keep the two sides lined up so the next filename sits level.
                lefts = int(self.left.index("end-1c").split(".")[0])
                rights = int(self.right.index("end-1c").split(".")[0])
                for _ in range(lefts - rights):
                    self.write(self.right, "", "plain")
                for _ in range(rights - lefts):
                    self.write(self.left, "", "plain")
                del pad

            self.write(self.left, "", "plain")
            self.write(self.right, "", "plain")

        for text in (self.left, self.right):
            text.configure(state=tk.DISABLED)

    def set_status(self):
        usable = [p for p in self.pictures if p.state in ("ok", "wiped")]
        risky = sum(1 for p in usable for t in p.before if t.risky)
        skipped = len(self.pictures) - len(usable)

        if not self.pictures:
            self.status.configure(text="Folder: %s" % self.folder, fg=GRAY)
            self.button.configure(state=tk.DISABLED)
            return

        if self.wiped:
            removed = sum(len(p.before) for p in usable)
            left = sum(len(p.after) for p in usable)
            where = ("Clean copies are in the stripped folder, originals untouched."
                     if self.kept else "Originals were overwritten.")
            msg = "Wiped %d files. %d entries removed, %d left. %s" % (
                len(usable), removed, left, where)
            self.status.configure(text=msg, fg=GREEN_FG)
        else:
            msg = "%d files. %d entries can identify you, marked with ! in red." % (len(usable), risky)
            if skipped:
                msg += "  %d skipped." % skipped
            if not self.keep.get():
                msg += "\nKeep the original is OFF. The originals will be overwritten."
            self.status.configure(text=msg, fg=RED_FG if risky else FG)

    def load(self):
        self.pictures = find_pictures(self.folder)
        for pic in self.pictures:
            try:
                pic.read()
            except Exception:
                pic.state = "unsupported"
        self.render()
        self.set_status()

    def on_wipe(self):
        self.button.configure(state=tk.DISABLED, text="Working...")
        self.keepbox.configure(state=tk.DISABLED)
        self.root.update()
        self.kept = self.keep.get()
        for pic in self.pictures:
            try:
                pic.wipe(self.kept, self.stripped)
            except Exception:
                pic.state = "unsupported"
        self.wiped = True
        self.button.configure(text="Done")
        self.render()
        self.set_status()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Window().run()
