"""Entry point: ``python -m qost_api`` (uvicorn on :8000)."""

import os

import uvicorn

DEFAULT_PORT = 8000


def main() -> None:
    uvicorn.run(
        "qost_api.app:create_app",
        factory=True,
        host=os.environ.get("API_HOST", "0.0.0.0"),
        port=int(os.environ.get("API_PORT", DEFAULT_PORT)),
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
