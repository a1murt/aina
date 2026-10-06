"""Entry point: ``python -m qost_collector``.

Stage M0 stub (real implementation in M3): validates the plant config and serves
``/healthz`` + ``/readyz`` on port 8110 (``HEALTH_PORT`` overrides).
"""

from twin_core.health import stub_main

SERVICE = "collector"
DEFAULT_PORT = 8110


def main() -> None:
    stub_main(SERVICE, default_port=DEFAULT_PORT)


if __name__ == "__main__":
    main()
