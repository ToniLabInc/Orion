"""ORION, a voice assistant for this Linux PC.

Importing the package caps the thread pools of numpy's maths library and of bundled OpenMP runtimes before numpy
loads, in this process and in every process it starts. ``__version__`` comes from the installed package's metadata
(pyproject.toml is its only source), read the first time it is asked for.
"""

import os

# numpy's maths library otherwise starts a thread per core that spins after every small job. This must happen before
# numpy is first imported; Orion's tool servers inherit it.
for _name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_name, "1")
# Bundled OpenMP runtimes (Whisper's CTranslate2) busy-wait between runs otherwise; they read this when they load.
os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")


def __getattr__(name: str) -> str:
    """Give ``__version__`` on first use (reading package metadata costs tens of milliseconds).

    Args:
        name: the attribute asked for.

    Returns:
        The installed version ("0.0.0" in a source tree that was never installed).

    Raises:
        AttributeError: for any other name.
    """
    if name != "__version__":
        raise AttributeError(f"module 'orion' has no attribute {name!r}")
    from importlib.metadata import PackageNotFoundError, version

    try:
        found = version("orion")
    except PackageNotFoundError:
        found = "0.0.0"
    globals()["__version__"] = found
    return found
