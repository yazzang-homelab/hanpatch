"""Regression checks for the Python program embedded in the browser worker."""
import os
import re


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, 'web', 'apply', 'worker.js')


def embedded_program():
    source = open(WORKER, encoding='utf-8').read()
    matches = re.findall(r"py\.runPython\(`\n(.*?)\n`\);", source, re.DOTALL)
    assert len(matches) >= 2
    return matches[-1]


def test_layeredfs_zip_keeps_bundle_metadata():
    program = embedded_program()
    start = program.index("if _mode == 'luma':")
    end = program.index('\nelse:', start)
    branch = program[start:end]

    assert "bundle_info = release.inspect" in program
    assert "zip_info = zipfile.ZipInfo" in branch
    assert not re.search(r'^\s*info = zipfile\.ZipInfo', branch, re.MULTILINE)

    compile(program, WORKER, 'exec')
