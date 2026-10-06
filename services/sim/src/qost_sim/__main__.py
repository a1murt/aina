"""Entry point: ``python -m qost_sim``.

Stage M0 stub (real implementation in M2): validates the plant config and serves
``/healthz`` + ``/readyz`` on port 8100 (``HEALTH_PORT`` overrides).
"""

from twin_core.health import stub_main

SERVICE = "sim"
DEFAULT_PORT = 8100


def main() -> None:
    stub_main(SERVICE, default_port=DEFAULT_PORT)


if __name__ == "__main__":
    main()
