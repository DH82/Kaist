"""Adapt the captured setup.py for builds without a visible GPU.

Only the image copy is patched; the captured source manifest stays intact.
PyTorch's extension builder emits flags for every TORCH_CUDA_ARCH_LIST entry.
"""
import ast
from pathlib import Path
import sys


def patch(path):
    source = path.read_text()
    if '# KAIST: GPU-independent build' in source:
        return
    tree = ast.parse(source)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == 'get_compute_capability')
    lines = source.splitlines(keepends=True)
    lines[fn.lineno-1:fn.end_lineno] = ['''def get_compute_capability():
    # KAIST: GPU-independent build
    archs = os.environ["TORCH_CUDA_ARCH_LIST"].replace(";", " ").split()
    return min(int(a.removesuffix("+PTX").replace(".", "")) for a in archs)
''']
    source = ''.join(lines)
    old = '    cc_flag.append(f"-arch=sm_{get_compute_capability()}")'
    if source.count(old) != 1:
        raise ValueError('Unexpected selective_scan setup.py; inspect before patching')
    source = source.replace(old, '    # Architecture flags come from TORCH_CUDA_ARCH_LIST.')
    ast.parse(source)
    path.write_text(source)


if __name__ == '__main__':
    patch(Path(sys.argv[1]))
