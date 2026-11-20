"""Check that a throttled or misbehaving GRIB mirror cannot consume the whole scene job."""
from datetime import datetime, timezone
import io
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fields


class MirrorRecovery(unittest.TestCase):
    def test_503_immediately_yields_to_other_mirror_then_expires(self):
        aws, ecmwf = fields.ROOTS
        error = urllib.error.HTTPError(aws, 503, 'slow down', {}, io.BytesIO())
        with patch.object(fields, '_aws_retry_after', 0), patch.object(fields.time, 'monotonic', return_value=200) as clock:
            with patch.object(fields.urllib.request, 'urlopen', side_effect=error) as get:
                with self.assertRaises(urllib.error.HTTPError):
                    fields._get(aws + '/file')
                self.assertEqual(get.call_count, 1)
            self.assertEqual(fields._mirrors(), [ecmwf, aws])
            clock.return_value = 321
            self.assertEqual(fields._mirrors(), [aws, ecmwf])

    def test_fallback_downloads_requested_message_and_caches_it(self):
        aws, ecmwf = fields.ROOTS
        row = {"param": "msl", "step": "24", "_offset": 10, "_length": 4}
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 206
        response.headers = {"Content-Range": "bytes 10-13/100"}
        response.read.return_value = b"GRIB"
        error = urllib.error.HTTPError(aws, 503, 'slow down', {}, io.BytesIO())
        with tempfile.TemporaryDirectory() as tmp, patch.object(fields, 'CACHE', Path(tmp)), \
             patch.object(fields, 'index_rows', return_value=(aws, [row])), \
             patch.object(fields, '_aws_retry_after', 0), \
             patch.object(fields.urllib.request, 'urlopen', side_effect=[error, response]) as get:
            args = (datetime(2026, 11, 19, 12, tzinfo=timezone.utc), "0p25", "oper", "fc", 24, "msl", "24")
            path = fields.fetch_field(*args)
            self.assertEqual(path.read_bytes(), b"GRIB")
            self.assertEqual(fields.fetch_field(*args), path)
            self.assertEqual(get.call_count, 2)
            for call, root in zip(get.call_args_list, (aws, ecmwf)):
                req = call.args[0]
                self.assertTrue(req.full_url.startswith(root + "/"))
                self.assertEqual(req.get_header("Range"), "bytes=10-13")

    def test_server_ignoring_range_is_closed_before_read(self):
        with patch.object(fields.urllib.request, 'urlopen') as get:
            response = get.return_value.__enter__.return_value
            response.status = 200
            with self.assertRaisesRegex(IOError, 'range'):
                fields._get('https://example.org/file', {'Range': 'bytes=10-19'}, length=10)
            response.read.assert_not_called()
