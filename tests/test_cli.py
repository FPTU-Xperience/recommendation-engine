import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from recommendation_engine.cli import main, read_json, write_atomic
from recommendation_engine.validation import ValidationError

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "docs/contracts/examples/input-v1.json"


class CliTests(unittest.TestCase):
    def test_cli_runs_example_and_writes_private_results(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as logs:
            output = Path(directory) / "result.json"
            self.assertEqual(main(["--input", str(INPUT), "--output", str(output),
                                   "--evaluation-time", "2026-10-27T00:00:00Z"]), 0)
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(data["results"][0]["activities"][0]["id"], 91)
            self.assertNotIn("userId", logs.getvalue())
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_failed_publish_keeps_existing_result_and_cleans_tempfile(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "result.json"
            output.write_text("original", encoding="utf-8")
            with patch("recommendation_engine.cli.os.replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    write_atomic(output, {"new": True})
            self.assertEqual(output.read_text(), "original")
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_invalid_json_never_overwrites_output(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(io.StringIO()) as errors:
            source = Path(directory) / "input.json"
            source.write_text('{"secret":"private-person","schemaVersion":NaN}', encoding="utf-8")
            output = Path(directory) / "result.json"
            output.write_text("old", encoding="utf-8")
            self.assertEqual(main(["--input", str(source), "--output", str(output)]), 1)
            self.assertEqual(output.read_text(), "old")
            self.assertNotIn("private-person", errors.getvalue())

    def test_duplicate_json_keys_and_size_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            source.write_text('{"a":1,"a":2}', encoding="utf-8")
            with self.assertRaises(ValidationError):
                read_json(source)
            with self.assertRaises(ValidationError):
                read_json(source, 4)

    def test_input_cannot_be_replaced_by_output(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--input", str(INPUT), "--output", str(INPUT)]), 1)

    def test_expired_snapshot_does_not_publish(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stderr(io.StringIO()) as errors:
            output = Path(directory) / "result.json"
            self.assertEqual(main(["--input", str(INPUT), "--output", str(output),
                                   "--evaluation-time", "2026-11-01T00:00:00Z"]), 1)
            self.assertFalse(output.exists())
            self.assertIn("SNAPSHOT_EXPIRED", errors.getvalue())

    def test_denied_directory_fails_promptly_without_overwriting(self):
        with patch("recommendation_engine.cli.open", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                write_atomic(Path("denied.json"), {"safe": True})


if __name__ == "__main__":
    unittest.main()
