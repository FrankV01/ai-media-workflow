"""
ai-media-workflow — entry point

Launches the FastAPI application via uvicorn.
Usage:
    python main.py          # dev server with auto-reload
    uvicorn app.main:app    # production-style launch
"""

import uvicorn


def main():
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True,
    )


if __name__ == "__main__":
    main()
