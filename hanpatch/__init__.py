"""hanpatch — gate-enforced game localisation pipeline.

The package splits into three layers:

    config/profile   what this title is, where its files live
    core             glossary, translation, layout, audit, manifest, QA gates
                     (no knowledge of any container format)
    adapters         extract/inject/verify for one title on one platform,
                     built on the format readers under platforms/ and formats/

Nothing in the core reads a ROM; nothing in an adapter decides wording.
"""
__version__ = '1.0.0'
