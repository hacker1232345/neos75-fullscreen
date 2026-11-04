import pathlib, sys


def patch(path, pairs, append=None):
    P = pathlib.Path(path)
    d = P.read_bytes()
    crlf = d.count(b'\r\n')
    eol = b'\r\n' if crlf else b'\n'
    for old, new in pairs:
        o = old.replace('\r\n', '\n').encode('utf-8').replace(b'\n', eol)
        n = new.replace('\r\n', '\n').encode('utf-8').replace(b'\n', eol)
        c = d.count(o)
        if c != 1:
            sys.stdout.write('MISS(%d) in %s: %r\n' % (c, path, old[:70].replace('\n', '\\n')))
            sys.exit(1)
        d = d.replace(o, n, 1)
    if append:
        if not d.endswith(eol):
            d += eol
        d += append.replace('\r\n', '\n').encode('utf-8').replace(b'\n', eol)
    P.write_bytes(d)
    sys.stdout.write('patched %s CRLF %d -> %d\n' % (path, crlf, d.count(b'\r\n')))


THEMES_DOC = '''
### 四套配色（对应官方四种颜色）

官方这台键盘有四种颜色：Venus 银 / Selene 灰 / Hades 黑 / Athena 白。渲染器把这四种都做成了主题，
配色只是一套色板，布局与部件完全不变——换主题不会改变任何信息的排布。

```sh
python dashboard/render.py --in state.json --out a.jpg --theme hades    # 黑，原版深色
python dashboard/render.py --in state.json --out b.jpg --theme selene   # 灰，石墨底 + 月光银蓝
python dashboard/render.py --in state.json --out c.jpg --theme venus    # 银，银白底 + 香槟铜
python dashboard/render.py --in state.json --out d.jpg --theme athena   # 白，象牙底 + 墨字柔金
```

桌面程序里在「屏幕预览」上方有一排配色按钮，点一下立刻重画预览，选中的配色会写进 `settings.json`，
下次打开还是它。旧配置没有这一项时自动退回 Hades 黑，不会因为少了字段打不开。

主题表的键名是固定的（`BG / CARD / CARD_HI / TRACK / LINE / TXT / DIM / FAINT / ACCENT / GOOD / WARN / BAD / NA / TOPLINE / WATERMARK`），
新增配色只要往 `THEMES` 里加一条，绘制代码一行都不用动。
'''

patch('README.md', [(
'''预设：`minimal` / `default` / `full` / `performance` / `network`，也可以任意勾选组合。''',
'''预设：`minimal` / `default` / `full` / `performance` / `network`，也可以任意勾选组合。
''' + THEMES_DOC.rstrip('\n')
)])


CHANGELOG = '''
## v1.8.0 — 2026-11-05

四套配色，对应官方四种颜色。这台键盘有 Venus 银 / Selene 灰 / Hades 黑 / Athena 白，
以前屏幕上只有一套深色，银白机身配深黑界面很别扭。

- `dashboard/render.py` 的 13 个硬编码颜色常量换成 `THEMES` 表 + `apply_theme()`。
  绘制代码一行没改，只是颜色从哪来变了：`card/bar/put/_frame/_metric/_text_rows`
  的默认参数从「定义时就绑死颜色」改成 `None`，函数体里再按当前主题取。
  这样切换配色不需要重新 import，也不会出现一半新色一半旧色。
- `--theme {hades,selene,venus,athena}` 加进命令行；`registry_json()` 多返回 `themes` 与 `theme`，
  每个主题带 `swatch`/`swatch2` 两个色值给界面画色块。
- 桌面程序「屏幕预览」上方加一排配色按钮，点一下重画预览并写进 `settings.json`。
  旧配置没有 `theme` 键时退回 Hades 黑，不因为少一个字段就打不开。
- `desktop/dash_bridge.py::render()` 增 `theme` 参数，透传到渲染器。

验证：134 项 Python 测试 + node 12 组全过；四套配色各渲染一张 480×360 实图目视比对；
`--gui-smoke` 里加了三条断言（配色按钮数量等于注册表、当前项被标为选中、两套主题必须渲染出不同像素）。
'''

patch('CHANGELOG.md', [(
'''# 更新记录

## v1.7.0 — 2026-11-04''',
'''# 更新记录
''' + CHANGELOG.rstrip('\n') + '''

## v1.7.0 — 2026-11-04'''
)])
