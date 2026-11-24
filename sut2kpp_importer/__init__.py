# Krita plugin entry point. The converter itself is the `sut2kpp` package next to this file; it has no
# dependency on Krita and also runs from a command line (see the README).
try:
    from krita import Krita
except ImportError:  # imported outside Krita (tests, command line)
    Krita = None

if Krita is not None:
    from .importer import SutImporter

    Krita.instance().addExtension(SutImporter(Krita.instance()))
