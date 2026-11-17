"""Bounded image previews preserve file scope and delivery-version identity."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from xiaoyu import artifact_images as images
from xiaoyu import serve_artifacts as inspector

def chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)


def png(width=1, height=1, *, pixels=b"\x00\xff\x00\x00\xff", depth=8, interlace=0, extra=b""):
    return (images.PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, 6, 0, 0, interlace))
            + extra + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


def jpeg(width=1, height=1):
    def segment(marker, value):
        return b"\xff" + bytes([marker]) + struct.pack(">H", len(value) + 2) + value
    # One grey block: DC difference zero and AC end-of-block, each code 0.
    quantization = segment(0xdb, b"\0" + bytes([1]) * 64)
    frame = segment(0xc0, struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00")
    counts = bytes([1]) + bytes(15)
    tables = segment(0xc4, b"\x00" + counts + b"\x00" + b"\x10" + counts + b"\x00")
    scan = segment(0xda, b"\x01\x01\x00\x00\x3f\x00")
    return b"\xff\xd8" + quantization + frame + tables + scan + b"\x3f\xff\xd9"


class ImageValidationTests(unittest.TestCase):
    def test_actual_static_png_signature_dimensions_and_digest(self):
        data = png()
        value = images.image_preview(data, "delivered")
        self.assertTrue(value["available"], value)
        self.assertEqual((value["width"], value["height"]), (1, 1))
        self.assertEqual(value["mime_type"], "image/png")
        self.assertEqual(value["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(base64.b64decode(value["data_url"].split(",", 1)[1]), data)

    def test_static_jpeg_has_real_bounds_and_digest(self):
        data = jpeg()
        value = images.image_preview(data, "current_changed")
        self.assertTrue(value["available"], value)
        self.assertEqual(value["mime_type"], "image/jpeg")
        self.assertEqual((value["width"], value["height"]), (1, 1))
        self.assertEqual(value["version"], "current_changed")
        self.assertIn("不代表原交付", value["note"])

    def test_no_svg_html_gif_webp_or_extension_inference(self):
        for data in [b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
                     b"<html><img src=x onerror=alert(1)></html>", b"GIF89a", b"RIFF0000WEBP", b"report.png"]:
            with self.subTest(data=data):
                self.assertFalse(images.image_preview(data, "delivered")["available"])

    def test_size_dimension_and_decompressed_pixel_caps(self):
        for data in [png() + bytes(images.IMAGE_BYTES), png(8193), png(8192, 8192),
                     jpeg(8193), jpeg(0), png(4096, 2048, depth=16), png(pixels=bytes(1024 * 1024))]:
            with self.subTest(size=len(data)):
                self.assertFalse(images.image_preview(data, "delivered")["available"])

    def test_png_crc_truncation_animation_interlace_and_trailing_data(self):
        data = bytearray(png())
        data[-1] ^= 1
        for value in [bytes(data), png()[:-5], png() + b"<html>", png(interlace=1),
                      png(extra=chunk(b"acTL", struct.pack(">II", 2, 0))), png(pixels=b"\x05abcd"),
                      images.PNG_SIGNATURE + chunk(b"IEND", b"")]:
            with self.subTest(size=len(value)):
                self.assertFalse(images.image_preview(value, "delivered")["available"])

    def test_jpeg_truncation_invalid_size_and_tail_rejected(self):
        for value in [jpeg()[:-2], jpeg() + b"<svg/>", b"\xff\xd8\xff\xd9", b"\xff\xd8\xff\xe0\xff\xff",
                      jpeg().replace(b"\xff\xc0", b"\xff\xc3", 1)]:
            self.assertFalse(images.image_preview(value, "delivered")["available"])

    def test_metadata_expansion_and_excessive_container_work_are_rejected(self):
        for kind in (b"iCCP", b"zTXt", b"iTXt"):
            self.assertFalse(images.image_preview(png(extra=chunk(kind, zlib.compress(bytes(1024 * 1024)))), "delivered")["available"])
        self.assertFalse(images.image_preview(png(extra=chunk(b"tEXt", b"a") * 1025), "delivered")["available"])
        excessive = jpeg()[:-2] + (b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\x3f" * 17) + b"\xff\xd9"
        self.assertFalse(images.image_preview(excessive, "delivered")["available"])

    def test_missing_or_unknown_source_never_becomes_delivery(self):
        for version in ["unavailable", "missing", "not-an-inspection-version"]:
            self.assertFalse(images.image_preview(png(), version)["available"])
        self.assertFalse(images.image_preview(None, "delivered")["available"])


@unittest.skipUnless(os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"), "需要安全目录句柄")
class ArtifactImageInspectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.data = png()
        self.path = self.root / "asset.png"
        self.path.write_bytes(self.data)
        # MIME is deliberately false: actual bytes are authoritative.
        self.artifact = {"id": "fixture-image", "path": self.path.name, "mime_type": "text/html",
                         "sha256": hashlib.sha256(self.data).hexdigest(), "size": len(self.data)}

    def test_keyless_image_version_contract(self):
        views = {"delivered": inspector.inspect(self.artifact, self.root, {})["image"]}
        self.path.write_bytes(jpeg())
        views["changed"] = inspector.inspect(self.artifact, self.root, {})["image"]
        self.path.unlink()
        views["missing"] = inspector.inspect(self.artifact, self.root, {})["image"]
        rendered = json.dumps(views, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        expected = Path(__file__).parent / "fixtures" / "artifact_images.expected.json"
        if os.environ.get("XIAOYU_SNAPSHOT") == "refresh":
            expected.write_text(rendered, encoding="utf-8")
        self.assertEqual(rendered, expected.read_text(encoding="utf-8", errors="replace"))

    def test_hash_matched_current_can_be_delivery_without_binary_snapshot(self):
        value = inspector.inspect(self.artifact, self.root, {})
        self.assertEqual(value["version"], "delivered")
        self.assertTrue(value["image"]["available"])
        self.assertEqual(value["image"]["sha256"], self.artifact["sha256"])
        self.assertFalse(value["delivered"]["available"])
        self.assertFalse(value["diff"]["available"])

    def test_changed_image_is_only_current_and_missing_has_no_historical_image(self):
        self.path.write_bytes(jpeg())
        value = inspector.inspect(self.artifact, self.root, {})
        self.assertEqual(value["image"]["version"], "current_changed")
        self.assertEqual(value["image"]["mime_type"], "image/jpeg")
        self.assertNotEqual(value["image"]["sha256"], self.artifact["sha256"])
        self.path.unlink()
        missing = inspector.inspect(self.artifact, self.root, {})
        self.assertEqual(missing["version"], "missing")
        self.assertFalse(missing["image"]["available"])
        self.assertEqual(missing["image"]["data_url"], "")

    def test_symlink_and_scope_do_not_gain_image_access(self):
        self.path.unlink()
        target = self.root / "outside.png"
        target.write_bytes(self.data)
        self.path.symlink_to(target)
        self.assertFalse(inspector.inspect(self.artifact, self.root, {})["image"]["available"])
        for name in ["../outside.png", str(target), ".git/asset.png"]:
            value = inspector.inspect({**self.artifact, "path": name}, self.root, {})
            self.assertFalse(value["image"]["available"])

    def test_text_inspection_unchanged_image_not_available(self):
        self.path.write_bytes(b"plain text\n")
        artifact = {**self.artifact, "sha256": hashlib.sha256(b"plain text\n").hexdigest()}
        value = inspector.inspect(artifact, self.root, {})
        self.assertTrue(value["delivered"]["available"])
        self.assertEqual(value["delivered"]["text"], "plain text\n")
        self.assertFalse(value["image"]["available"])


if __name__ == "__main__":
    unittest.main()
