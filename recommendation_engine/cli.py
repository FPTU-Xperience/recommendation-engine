"""Bounded JSON file CLI. Only publish a complete, validated result."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from .config import Config
from .engine import recommend
from .validation import ValidationError

MAX_INPUT_BYTES = 50 * 1024 * 1024


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("INVALID_JSON", "Duplicate JSON property.")
        result[key] = value
    return result


def _constant(_value):
    raise ValidationError("INVALID_JSON", "Nonfinite JSON number.")


def read_json(path, limit=MAX_INPUT_BYTES):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValidationError("INPUT_TOO_LARGE", "JSON input exceeds its byte limit.")
    return json.loads(data.decode("utf-8-sig"), object_pairs_hook=_pairs, parse_constant=_constant)


def write_atomic(path, result):
    destination = Path(path)
    temporary = None
    try:
        # Fail promptly on permission errors. tempfile's Windows retry loop can
        # mistake a denied directory for a filename collision and retry forever.
        for _ in range(8):
            candidate = destination.with_name(f".recommendations-{uuid4().hex}.tmp")
            try:
                stream = open(candidate, "x", encoding="utf-8", newline="\n",
                              opener=lambda file, flags: os.open(file, flags, 0o600))
            except FileExistsError:
                continue
            temporary = candidate
            break
        else:
            raise FileExistsError("Cannot allocate a unique result file.")
        with stream:
            json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate private club/activity recommendations from a BE JSON snapshot.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--evaluation-time", help="Explicit UTC instant for offline fixture replay; default is current UTC.")
    args = parser.parse_args(argv)
    try:
        if args.output.resolve() in {args.input.resolve(), args.config.resolve() if args.config else args.input.resolve()}:
            raise ValidationError("INVALID_PATH", "Output cannot replace an input/config file.")
        snapshot = read_json(args.input)
        config = Config.from_dict(read_json(args.config, 65536)) if args.config else Config()
        now = args.evaluation_time or datetime.now(timezone.utc).isoformat()
        result = recommend(snapshot, config, evaluated_at=now)
        write_atomic(args.output, result)
        counts = {status: sum(row["status"] == status for row in result["results"])
                  for status in ("READY", "PARTIAL", "UNAVAILABLE")}
        print(json.dumps({"status": "WRITTEN", "students": len(result["results"]), "counts": counts}))
        return 0
    except (ValidationError, OSError, UnicodeError, ValueError, RecursionError) as error:
        # Do not echo raw input, user IDs, student fields or arbitrary OS error paths.
        code = error.code if isinstance(error, ValidationError) else "INPUT_OUTPUT_ERROR"
        print(json.dumps({"status": "FAILED", "code": code}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
