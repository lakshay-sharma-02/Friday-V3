import site
import sys
import os

from pathlib import Path

for p in site.getsitepackages():
    target = Path(p) / "misaki" / "espeak.py"
    if target.exists():
        content = """from phonemizer.backend.espeak.wrapper import EspeakWrapper, EspeakAPI
from typing import Tuple
import espeakng_loader
import phonemizer
import re
import tempfile, atexit, shutil, ctypes, pathlib, weakref

EspeakWrapper.set_library(espeakng_loader.get_library_path())

_orig_init = EspeakAPI.__init__
def _patched_init(self, library):
    self._library = None
    try:
        espeak = ctypes.cdll.LoadLibrary(str(library))
        library_path = self._shared_library_path(espeak)
        del espeak
    except OSError as error:
        raise RuntimeError(f'failed to load espeak library: {str(error)}') from None

    self._tempdir = tempfile.mkdtemp()
    weakref.finalize(self, self._delete, self._library, self._tempdir)

    espeak_copy = pathlib.Path(self._tempdir) / library_path.name
    shutil.copy(library_path, espeak_copy, follow_symlinks=False)

    self._library = ctypes.cdll.LoadLibrary(str(espeak_copy))
    try:
        data_path = espeakng_loader.get_data_path().encode("utf-8")
        if self._library.espeak_Initialize(0x02, 0, data_path, 0) <= 0:
            raise RuntimeError('failed to initialize espeak shared library')
    except AttributeError:
        raise RuntimeError('failed to load espeak library') from None

    self._library_path = library_path

EspeakAPI.__init__ = _patched_init

"""
        # Read the rest of original file
        with open(target, 'r') as f:
            lines = f.readlines()
        
        # find class EspeakFallback
        idx = 0
        for i, line in enumerate(lines):
            if "class EspeakFallback:" in line:
                idx = i
                break
        
        new_content = content + "".join(lines[idx:])
        with open(target, 'w') as f:
            f.write(new_content)
        print("Patched!")
        break
