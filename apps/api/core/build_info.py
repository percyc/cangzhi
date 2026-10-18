"""Image provenance, independent of runtime git checkout or network access."""
import json
import os


def build_info() -> dict:
    return {
        "version": os.environ.get("CANGZHI_VERSION", "unversioned"),
        "revision": os.environ.get("CANGZHI_REVISION", "unknown"),
        "built_at": os.environ.get("CANGZHI_BUILD_TIME", "unknown"),
        "dirty": os.environ.get("CANGZHI_BUILD_DIRTY", "true") != "false",
    }


if __name__ == "__main__":
    print(json.dumps(build_info()))
