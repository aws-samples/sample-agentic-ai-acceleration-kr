"""The programs the server runs *inside* a harness sandbox, and how they are wrapped.

Two constraints shape this file. The sandbox has no `find` (its image is minimal:
bash, coreutils, curl, git, awk, python3.12 — no `find`, no `node`, no `which`),
so listing is a python program rather than a shell pipeline. And Korean filenames
are ordinary here, while the command is ultimately a shell string — so **nothing
variable is ever interpolated into it**. Both the program and its arguments are
base64, which contains only `[A-Za-z0-9+/=]`; a filename holding a quote, a space
or `$(…)` cannot change the command's shape.

The program arrives on stdin (`python3 -`) and its arguments as one base64 argv
entry. That keeps the command short enough to stay well clear of any body limit
and removes every quoting question at once.
"""
import base64
import json
from typing import Any, Dict, List

# Paths go out base64-encoded so a name with a quote, a newline or a non-UTF-8
# byte cannot break the JSON the server parses.
LIST_SCRIPT = r"""
import base64, json, os, sys

args = json.loads(base64.b64decode(sys.argv[1]))
extensions = set(args["extensions"])
max_bytes = args["max_file_bytes"]
max_files = args["max_files"]
max_depth = args["max_depth"]

files, dropped = [], []

for root in args["roots"]:
    root = os.path.abspath(root)
    base_depth = root.rstrip("/").count("/")
    for dirpath, dirnames, filenames in os.walk(root):
        # `.agents` holds the harness's own state, and a dot directory is never
        # something the user asked for.
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if dirpath.rstrip("/").count("/") - base_depth >= max_depth:
            dirnames[:] = []
        for name in filenames:
            if name.startswith("."):
                continue
            extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if extension not in extensions:
                continue
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            entry = {
                "p": base64.b64encode(path.encode("utf-8")).decode("ascii"),
                "s": stat.st_size,
                "m": int(stat.st_mtime),
            }
            if stat.st_size > max_bytes:
                dropped.append(dict(entry, why="too_large"))
                continue
            files.append(entry)

files.sort(key=lambda f: f["m"], reverse=True)
if len(files) > max_files:
    dropped.extend(dict(f, why="over_file_limit") for f in files[max_files:])
    files = files[:max_files]

print(json.dumps({"files": files, "dropped": dropped}))
"""

# `Content-Type` is sent explicitly because urllib otherwise supplies
# `application/x-www-form-urlencoded` for any request with a body, which would be
# stored as the object's type. It is deliberately a constant that is *not* part of
# the presigned signature: the real type is applied when the object is read, so a
# header mismatch can never make an upload fail.
UPLOAD_SCRIPT = r"""
import base64, json, sys, urllib.request

args = json.loads(base64.b64decode(sys.argv[1]))
timeout = args.get("timeout") or 60
results = []

for item in args["uploads"]:
    path = base64.b64decode(item["p"]).decode("utf-8")
    try:
        with open(path, "rb") as handle:
            body = handle.read()
        request = urllib.request.Request(item["url"], data=body, method="PUT")
        request.add_header("Content-Type", "application/octet-stream")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            results.append({"p": item["p"], "status": response.status})
    except Exception as exc:
        results.append({"p": item["p"], "error": str(exc)[:200]})

print(json.dumps({"uploads": results}))
"""


def encode_args(payload: Dict[str, Any]) -> str:
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def _command(script: str, payload: Dict[str, Any]) -> str:
    """Wrap a program and its arguments into one shell-safe command string.

    The single quotes around the body are the only quotes in the command, and
    everything inside them is base64 or a fixed word — which is what makes the
    "no interpolation" rule checkable by a test.
    """
    encoded_script = base64.b64encode(script.encode("utf-8")).decode("ascii")
    return (
        f"/bin/bash -c 'echo {encoded_script} | base64 -d | "
        f"python3 - {encode_args(payload)}'"
    )


def list_command(
    roots: List[str],
    extensions: List[str],
    max_file_bytes: int,
    max_files: int,
    max_depth: int = 3,
) -> str:
    return _command(
        LIST_SCRIPT,
        {
            "roots": roots,
            "extensions": [e.lower().lstrip(".") for e in extensions],
            "max_file_bytes": max_file_bytes,
            "max_files": max_files,
            "max_depth": max_depth,
        },
    )


def upload_command(uploads: List[Dict[str, str]], timeout: int = 60) -> str:
    return _command(UPLOAD_SCRIPT, {"uploads": uploads, "timeout": timeout})


def decode_output(stdout: str) -> Dict[str, Any]:
    """Parse the program's JSON result.

    The last non-empty line is taken rather than the whole buffer: a warning on
    stdout from the sandbox's own tooling would otherwise make the parse fail.
    """
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise ValueError(f"No JSON result in sandbox output: {(stdout or '')[:200]!r}")
