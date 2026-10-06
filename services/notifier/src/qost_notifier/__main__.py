"""Entry point: ``python -m qost_notifier``.

Stage M0 stub (real implementation in M8): validates the plant config and serves
``/healthz`` + ``/readyz`` on port 8130 (``HEALTH_PORT`` overrides).
"""

from twin_core.health import stub_main

SERVICE = "notifier"
DEFAULT_PORT = 8130


def main() -> None:
    stub_main(SERVICE, default_port=DEFAULT_PORT)


if __name__ == "__main__":
    main()
