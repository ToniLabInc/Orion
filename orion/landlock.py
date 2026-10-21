"""The last step inside the sandbox: Landlock, a memory limit, then the program itself.

bubblewrap (``orion.sandbox``) gives a command its own view of the files, processes and, with ``--unshare-net``, the
network. With the network on, what it still shares with the PC are the loopback's ports and the abstract Unix
sockets, which reach Orion's own HUD (whoever connects to its port is taken for the user on this PC) and the
desktop's X server. Landlock closes both without any privilege: this module, run first inside the sandbox
(``python -I -S landlock.py ... -- program args``), restricts the process and everything it starts to

- connecting to any TCP port but the ones named (Linux 6.7, Landlock version 4), so never to Orion's ports,
- connecting to abstract Unix sockets made outside the sandbox, and sending signals out of it (Linux 6.12, version 6),

and sets ``RLIMIT_DATA`` (the memory a program may use) and no core dumps, then ``exec``s the program. A kernel
without those versions can't give the network-on sandbox its guarantees: ``abi`` says what this one offers, and the
sandbox refuses to run with the network on rather than run without them.

It imports nothing of Orion's (it runs with ``-I -S``, where the package isn't on the path) and nothing but the
standard library.
"""

import ctypes
import os
import resource
import sys
from collections.abc import Collection, Sequence

CREATE, ADD_RULE, RESTRICT_SELF = 444, 445, 446  # the Landlock system calls: the same numbers on every architecture
VERSION_FLAG = 1  # landlock_create_ruleset(NULL, 0, VERSION): asks the version
RULE_NET_PORT = 2
CONNECT_TCP = 1 << 1  # the right to connect a TCP socket to a port
SCOPE_ABSTRACT_SOCKET, SCOPE_SIGNAL = 1 << 0, 1 << 1
PR_SET_NO_NEW_PRIVS = 38
NEEDS_NETWORK_RULES = 4  # the first version with network rules
NEEDS_SCOPES = 6  # the first version with scopes
PORTS = range(1, 65536)


class _Ruleset(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64),
                ("scoped", ctypes.c_uint64)]


class _NetPort(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("port", ctypes.c_uint64)]


def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def abi() -> int:
    """The Landlock version this kernel offers: 0 when it has none, or isn't allowing it to this process."""
    return max(0, _libc().syscall(CREATE, None, ctypes.c_size_t(0), ctypes.c_uint32(VERSION_FLAG)))


def restrict(blocked: Collection[int]) -> None:
    """Restrict this process and what it starts for good: connect to any TCP port but ``blocked``, no abstract Unix
    sockets from outside, no signals out.

    Args:
        blocked: the TCP ports it may never connect to (Orion's HUD, the browser and model servers he runs).

    Raises:
        OSError: this kernel has no Landlock network rules and scopes (Linux 6.12), or refused them.
    """
    if (version := abi()) < NEEDS_SCOPES:
        raise OSError(f"this kernel's Landlock is version {version}; the sandbox's network needs {NEEDS_SCOPES} "
                      "(Linux 6.12)")
    libc = _libc()
    attr = _Ruleset(0, CONNECT_TCP, SCOPE_ABSTRACT_SOCKET | SCOPE_SIGNAL)
    fd = libc.syscall(CREATE, ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint32(0))
    if fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
    rule = _NetPort(CONNECT_TCP, 0)
    for port in PORTS:
        if port in blocked:
            continue
        rule.port = port
        if libc.syscall(ADD_RULE, fd, RULE_NET_PORT, ctypes.byref(rule), 0) < 0:
            raise OSError(ctypes.get_errno(), f"landlock_add_rule {port}")
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0:
        raise OSError(ctypes.get_errno(), "prctl NO_NEW_PRIVS")
    if libc.syscall(RESTRICT_SELF, fd, 0) < 0:
        raise OSError(ctypes.get_errno(), "landlock_restrict_self")
    os.close(fd)


def limit(memory_mb: int) -> None:
    """No core dump, and no more memory than ``memory_mb`` (0: no limit) in data, heap and private mappings."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if memory_mb:
        resource.setrlimit(resource.RLIMIT_DATA, (memory_mb << 20, memory_mb << 20))


def main(args: Sequence[str]) -> None:
    """``landlock.py [--memory-mb N] [--block PORT,PORT] -- program args``: set the limits, restrict when ``--block`` is
    given (its ports, possibly none, are the ones the program may not connect to), then exec the program."""
    usage = "usage: landlock.py [--memory-mb N] [--block PORT,PORT] -- program [args...]"
    options: dict[str, str] = {}
    rest = list(args)
    while len(rest) >= 2 and rest[0] != "--":
        name = rest.pop(0)
        options[name] = rest.pop(0)
    if len(rest) < 2 or rest[0] != "--" or not options.keys() <= {"--memory-mb", "--block"}:
        sys.exit(usage)  # a protection that was asked for and not understood must never be dropped quietly
    program = rest[1:]
    try:
        limit(int(options.get("--memory-mb", "0")))
        if "--block" in options:
            restrict({int(port) for port in options["--block"].split(",") if port})
        os.execvp(program[0], program)
    except OSError as e:
        print(f"orion sandbox: {program[0]}: {e}", file=sys.stderr)
        sys.exit(126 if isinstance(e, PermissionError) else 127 if isinstance(e, FileNotFoundError) else 125)


if __name__ == "__main__":
    main(sys.argv[1:])
