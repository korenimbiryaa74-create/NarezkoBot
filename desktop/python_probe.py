"""Installer helper: avoid PowerShell's native -c argument quoting pitfalls."""
import struct
import sys


def acceptable(version, pointer_bytes):
    return tuple(version[:2]) == (3, 15) and pointer_bytes == 8


def main():
    if not acceptable(sys.version_info, struct.calcsize('P')):
        print('Need 64-bit Python 3.15; found %s (%s-bit)' %
              (sys.version.split()[0], struct.calcsize('P') * 8), file=sys.stderr)
        return 1
    print(sys.executable)
    return 0


if __name__ == '__main__':
    sys.exit(main())
