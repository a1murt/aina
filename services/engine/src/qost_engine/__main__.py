"""Entry point: ``python -m qost_engine``.

Stage M0 stub (real implementation in M3): validates the plant config and serves
``/healthz`` + ``/readyz`` on port 8120 (``HEALTH_PORT`` overrides).
"""

from twin_core.health import stub_main

SERVICE = "engine"
DEFAULT_PORT = 8120


def main() -> None:
    stub_main(SERVICE, default_port=DEFAULT_PORT)


if __name__ == "__main__":
    main()
