from __future__ import annotations

RUNTIME_VERSION = "0.3.0"

__all__ = ["RUNTIME_VERSION"]


def _main() -> int:
    print(RUNTIME_VERSION)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
