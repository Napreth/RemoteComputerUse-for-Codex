import contextlib
import io
import json
import os
import sys
import time

print(
    json.dumps(
        {
            "ready": True,
            "platform": "remote",
            "backend": "fixture",
            "target": "fixture://computer",
            "width": 640,
            "height": 480,
        }
    ),
    flush=True,
)
namespace = {"os": os, "time": time}
for line in sys.stdin:
    request = json.loads(line)
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            exec(request["code"], namespace)  # noqa: S102 - test-only worker fixture
        output = captured.getvalue()
    except Exception as error:  # noqa: BLE001 - fixture reports arbitrary snippet failures
        output = str(error)
    print(json.dumps({"id": request["id"], "output": [{"type": "input_text", "text": output}]}), flush=True)
