"""Evict everything the machine's cache at `/cache` holds, as the step
wrapper of the image's base does when a step needs room: its own
`make_room`, with the cap at nothing and every store entry old enough to
free. Run in an image, with `/cache` mounted."""

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

WRAPPER = "/usr/local/bin/unicon-step"


def main() -> None:
    loader = SourceFileLoader("unicon_step", WRAPPER)
    spec = importlib.util.spec_from_loader("unicon_step", loader)
    assert spec is not None
    wrapper = importlib.util.module_from_spec(spec)
    loader.exec_module(wrapper)
    vars(wrapper).update(
        CACHE_CAP=0, STALE_SECONDS=-60, CACHE_CAP_FILE=Path("/nonexistent")
    )
    wrapper.tidy(Path("/cache"))


if __name__ == "__main__":
    main()
