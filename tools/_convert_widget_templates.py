"""Make the widget templates rebuildable at new addresses.

The three asm templates in build_widget.py are module-level f-strings, so their
`{imm('r0', MEMCPY)}` expressions are frozen at import time with the reference
addresses. To relocate the widget onto a firmware with a different layout we
must re-evaluate them after swapping the symbol values. That means turning each
template into a function whose body re-reads the module globals at call time.

Run once; it is idempotent and refuses to touch an already-converted file.
"""
import re
from pathlib import Path

PATH = Path(__file__).resolve().parent / 'build_widget.py'
src = PATH.read_text(encoding='utf-8')
if 'def _tick():' in src:
    raise SystemExit('已经转换过，无需再跑')

for name in ('TICK', 'ENSURE'):
    pat = re.compile(rf"^{name} = f'''$", re.M)
    if not pat.search(src):
        raise SystemExit(f'找不到 {name} 模板定义')
    src = pat.sub(f"def _{name.lower()}():\n    return f'''", src)

# receive_text 已是函数，但用 f''' 返回；保持原样即可（它每次调用求值）。

anchor = "def build_code(pages):"
if anchor not in src:
    raise SystemExit('找不到 build_code')
helper = '''def visible_symbols():
    """当前生效的厂商地址：默认是参考镜像里的值，可被 relocate() 改写。"""
    names = ('MEMCPY LBL_CREATE LBL_TEXT ALLOC SET_POS SET_SIZE SET_COLOR '
             'SET_FONT GETSCREEN TIMER FONT').split()
    return {n: globals()[n] for n in names}


def relocate(syms=None, free=None):
    """换一套厂商地址（和可选的空闲带起点），返回原来那套，便于还原。"""
    old = visible_symbols()
    if syms:
        for k, v in syms.items():
            if k not in old:
                raise ValueError(f'未知符号 {k}')
            if not isinstance(v, int) or v & 1:
                raise ValueError(f'{k} 地址必须是偶数整数，收到 {v!r}')
            globals()[k] = v
    if free is not None:
        old['FREE'] = FREE
        globals()['FREE'] = free
        globals()['F_RECEIVE'] = free
        globals()['F_TICK'] = free + 0x500
        globals()['F_ENSURE'] = free + 0x700
        globals()['F_DATA'] = free + 0x900
    return old


def restore(old):
    """把 relocate() 返回的旧值装回去。"""
    globals().update({k: v for k, v in old.items() if k != 'FREE'})


'''
src = src.replace(anchor, helper + anchor, 1)

# build_code 里要把 TICK/ENSURE 换成函数调用
src = src.replace("('tick', TICK, F_TICK, F_ENSURE),", "('tick', _tick(), F_TICK, F_ENSURE),")
src = src.replace("('ensure', ENSURE, F_ENSURE, F_DATA),", "('ensure', _ensure(), F_ENSURE, F_DATA),")

PATH.write_text(src, encoding='utf-8')
print('已把 TICK/ENSURE 改成可重定位的函数，并加入 relocate/restore')
