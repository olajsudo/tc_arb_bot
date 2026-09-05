"""
Ubuntu 22.04+/OpenSSL 3.x moved RIPEMD160 into OpenSSL's "legacy" provider,
which Python's `hashlib` doesn't load by default. `bip32utils` (a dependency
of terra_classic_sdk's MnemonicKey) needs `hashlib.new('ripemd160', ...)`
for standard BIP32 key derivation, so without this it raises:

    ValueError: unsupported hash type ripemd160

Rather than editing system-wide OpenSSL config (requires sudo, affects every
program on the machine), this patches `hashlib.new` in-process to fall back
to pycryptodome's independent RIPEMD160 implementation. Must be imported
before anything that calls hashlib.new('ripemd160', ...) — so it's imported
at the very top of terra_client.py, before terra_classic_sdk.
"""
import hashlib

_original_new = hashlib.new


class _RipeMd160Wrapper:
    """Wraps pycryptodome's RIPEMD160 to match hashlib's object interface
    closely enough for bip32utils' usage (update/digest/hexdigest)."""

    def __init__(self, data: bytes = b""):
        from Crypto.Hash import RIPEMD160
        self._h = RIPEMD160.new()
        if data:
            self._h.update(data)

    def update(self, data: bytes):
        self._h.update(data)

    def digest(self) -> bytes:
        return self._h.digest()

    def hexdigest(self) -> str:
        return self._h.hexdigest()

    @property
    def digest_size(self) -> int:
        return 20

    @property
    def name(self) -> str:
        return "ripemd160"


def _patched_new(name, data=b"", **kwargs):
    if name.lower().replace("-", "").replace("_", "") == "ripemd160":
        return _RipeMd160Wrapper(data)
    return _original_new(name, data, **kwargs)


def apply():
    if hashlib.new is not _patched_new:
        hashlib.new = _patched_new
