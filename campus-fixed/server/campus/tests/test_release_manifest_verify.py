"""A release archive must verify even when its creator never declared UTF-8.

The deliverable ZIP is produced by whichever archiver the maintainer had to
hand, and many of them store a Chinese filename as UTF-8 bytes while leaving the
entry's UTF-8 flag (bit 0x800) clear. `zipfile` then decodes those bytes as
cp437, so `源码导航.md` arrives as `µ║Éτáüσ»╝Φê¬.md`. `verify()` compared that
mojibake against the manifest and reported the one file as both absent from the
archive and unexpected in it -- two problems, non-zero exit, on an archive that
is perfectly intact. A reviewer running the documented command was told a good
release had failed, which is the worst possible answer: it costs trust in the
artifact and invites "fixing" something that was never broken.

Found on 2026-10-11 by running the documented command against the delivered ZIP
itself. The archive was right; the verifier was wrong.

The cases below build the archive with `zipfile` (which does set the flag) and
then clear the flag in the headers, which is exactly the byte-level difference
between the two kinds of archiver.
"""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.build_release_manifest import archive_entry_name, sha256_file, verify

MANIFEST = "RELEASE_MANIFEST.json"
CJK = "源码导航.md"
UTF8_FLAG = 0x800

CENTRAL_SIGNATURE = b"PK\x01\x02"
CENTRAL_SIZE = 46
END_SIGNATURE = b"PK\x05\x06"
END_SIZE = 22


def clear_utf8_flag(raw: bytes) -> bytes:
    """Clear the UTF-8 flag on every local and central header.

    Walks the structure instead of searching for signatures, because a stored
    payload is free to contain those four bytes by accident and patching random
    data would corrupt the archive. The end-of-central-directory record locates
    the central directory; each central record names the offset of its own local
    header, so both copies of the flag are reachable without guessing.
    """
    if raw[-END_SIZE : -END_SIZE + 4] != END_SIGNATURE:
        raise AssertionError("unexpected archive layout: no trailing EOCD record")

    out = bytearray(raw)
    offset = int.from_bytes(raw[-END_SIZE + 16 : -END_SIZE + 20], "little")

    while out[offset : offset + 4] == CENTRAL_SIGNATURE:
        flags = int.from_bytes(out[offset + 8 : offset + 10], "little")
        name_length = int.from_bytes(out[offset + 28 : offset + 30], "little")
        extra_length = int.from_bytes(out[offset + 30 : offset + 32], "little")
        comment_length = int.from_bytes(out[offset + 32 : offset + 34], "little")
        local = int.from_bytes(out[offset + 42 : offset + 46], "little")

        if out[local : local + 4] != b"PK\x03\x04":
            raise AssertionError("central record does not point at a local header")

        cleared = (flags & ~UTF8_FLAG).to_bytes(2, "little")
        out[offset + 8 : offset + 10] = cleared
        out[local + 6 : local + 8] = cleared

        offset += CENTRAL_SIZE + name_length + extra_length + comment_length

    return bytes(out)


class UndeclaredUtf8ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.payload = "导航\n".encode()
        (self.root / CJK).write_bytes(self.payload)
        (self.root / MANIFEST).write_text(
            json.dumps(
                {
                    "product_version": "test",
                    "contract_version": "test",
                    # verify() echoes the sample hash on success, so the field has to
                    # exist even though this archive has no sample database.
                    "public_sample_sha256": "0" * 64,
                    "file_count": 1,
                    "files": [
                        {
                            "path": CJK,
                            "bytes": len(self.payload),
                            "sha256": sha256_file(self.root / CJK),
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        self.archive = self.root / "release.zip"

    def tearDown(self):
        self.temp.cleanup()

    def build(self, *, declare_utf8: bool) -> Path:
        with zipfile.ZipFile(self.archive, "w", zipfile.ZIP_STORED) as bundle:
            bundle.writestr(f"pkg/{CJK}", self.payload)
            bundle.writestr(f"pkg/{MANIFEST}", (self.root / MANIFEST).read_bytes())
        if not declare_utf8:
            self.archive.write_bytes(clear_utf8_flag(self.archive.read_bytes()))
        return self.archive

    def test_the_two_archives_differ_only_in_the_declared_flag(self):
        declared = self.build(declare_utf8=True).read_bytes()
        self.build(declare_utf8=False)
        undeclared = self.archive.read_bytes()

        self.assertEqual(len(declared), len(undeclared), "clearing the flag must not change the archive's size")

        with zipfile.ZipFile(self.archive) as bundle:
            flags = [info.flag_bits & UTF8_FLAG for info in bundle.infolist()]
        self.assertEqual(flags, [0, 0], "the flag really was cleared")

    def test_an_archive_that_does_not_declare_utf8_still_verifies(self):
        archive = self.build(declare_utf8=False)

        # The defect only exists if zipfile alone really cannot read the name.
        with zipfile.ZipFile(archive) as bundle:
            self.assertNotIn(f"pkg/{CJK}", [info.filename for info in bundle.infolist()])

        self.assertEqual(verify(self.root, archive), 0)

    def test_an_archive_that_declares_utf8_verifies(self):
        # The control: the fix must not break the archives this project writes.
        self.assertEqual(verify(self.root, self.build(declare_utf8=True)), 0)

    def test_a_corrupt_archive_is_still_rejected(self):
        # The fix must not turn the verifier into a rubber stamp. Since T15,
        # the rejection is a printed FAIL and exit 1 -- an uncaught BadZipFile
        # is no longer an acceptable answer, so there is nothing to swallow.
        archive = self.build(declare_utf8=False)
        archive.write_bytes(archive.read_bytes().replace(self.payload, "改动\n".encode()))
        self.assertEqual(verify(self.root, archive), 1)

    def test_a_file_that_is_not_an_archive_fails_cleanly(self):
        # Opening garbage must fail with one line and exit 1, not a traceback.
        archive = self.root / "garbage.zip"
        archive.write_bytes(b"this is not a zip archive, just some bytes")
        self.assertEqual(verify(self.root, archive), 1)

    def test_a_missing_manifest_fails_cleanly(self):
        # Build the archive first: its build() helper embeds the manifest.
        archive = self.build(declare_utf8=True)
        (self.root / MANIFEST).unlink()
        self.assertEqual(verify(self.root, archive), 1)

    def test_an_unparsable_manifest_fails_cleanly(self):
        (self.root / MANIFEST).write_text("{ not json", encoding="utf-8", newline="\n")
        self.assertEqual(verify(self.root, self.build(declare_utf8=True)), 1)

    def test_a_name_that_is_not_utf8_is_left_as_zipfile_read_it(self):
        # cp437 bytes that are not valid UTF-8 must not be mangled further.
        info = zipfile.ZipInfo("caf\xe9.txt")
        info.flag_bits = 0
        self.assertEqual(archive_entry_name(info), "caf\xe9.txt")

    def test_an_ascii_name_is_unchanged(self):
        info = zipfile.ZipInfo("README.md")
        info.flag_bits = 0
        self.assertEqual(archive_entry_name(info), "README.md")


if __name__ == "__main__":
    unittest.main()
