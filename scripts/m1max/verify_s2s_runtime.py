#!/usr/bin/env python3
"""Fail closed on a managed backend revision or validated dependency mismatch."""

import json
import os
from importlib.metadata import distribution, version


def main():
    expected = {
        "speech-to-speech": os.environ["S2S_BACKEND_VERSION"],
        "mlx": os.environ["S2S_EXPECTED_MLX_VERSION"],
        "mlx-audio": os.environ["S2S_EXPECTED_MLX_AUDIO_VERSION"],
        "mlx-lm": os.environ["S2S_EXPECTED_MLX_LM_VERSION"],
        "mlx-metal": os.environ["S2S_EXPECTED_MLX_METAL_VERSION"],
    }
    for name, wanted in expected.items():
        actual = version(name)
        if actual != wanted:
            raise SystemExit(f"Backend dependency mismatch: {name} expected {wanted}, got {actual}")
    origin = json.loads(distribution("speech-to-speech").read_text("direct_url.json") or "{}")
    sha = origin.get("vcs_info", {}).get("commit_id")
    if sha != os.environ["S2S_BACKEND_FORK_SHA"]:
        raise SystemExit(f"Backend fork revision mismatch: got {sha}")
    print(json.dumps({"backend_revision": sha, "validated_packages": expected}))


if __name__ == "__main__":
    main()
