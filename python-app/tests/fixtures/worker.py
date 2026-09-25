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
namespace = {"os": os, "time": time, "connected": True}
for line in sys.stdin:
    request = json.loads(line)
    if request["operation"] == "status":
        print(json.dumps({"id": request["id"], "data": {
            "connected": namespace.get("status_connected", namespace["connected"]),
            "backend": "fixture", "host": "fixture", "port": 3389,
            "width": 640, "height": 480,
        }, "inputHeld": namespace.get("input_held", False)}), flush=True)
        continue
    if request["operation"] == "actions":
        if request.get("actions") == [{"type": "fatal"}]:
            print(json.dumps({"error": "RuntimeError: fixture transport lost"}), flush=True)
            raise SystemExit(1)
        print(
            json.dumps(
                {
                    "id": request["id"],
                    "connected": request.get("actions") != [{"type": "lost_after_validation"}],
                    "error": {
                        "code": "remote_operation_failed",
                        "message": "ValueError: invalid fixture action",
                        "recoverable": True,
                    },
                }
            ),
            flush=True,
        )
        continue
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured):
            exec(request["code"], namespace)  # noqa: S102 - test-only worker fixture
        output = captured.getvalue()
    except Exception as error:  # noqa: BLE001 - fixture reports arbitrary snippet failures
        output = str(error)
    print(json.dumps({"id": request["id"], "output": [{"type": "input_text", "text": output}],
                      "connected": namespace["connected"], "inputHeld": namespace.get("input_held", False)}), flush=True)
