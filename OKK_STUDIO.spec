# Keep FFmpeg/MPV and the 5+ GB effects library beside the executable.
# Only compact UI images belong in the one-file archive.
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH)
dnd_data, dnd_binaries, dnd_hidden = collect_all('tkinterdnd2')
ui_data = [(str(path), str(path.parent.relative_to(root)))
           for path in (root/'assets').rglob('*')
           if path.is_file() and 'effects_library' not in path.relative_to(root/'assets').parts]
a = Analysis([str(root/'rtx5090_exporter.py')], pathex=[str(root)],
    binaries=dnd_binaries, datas=ui_data+dnd_data, hiddenimports=['okk_cli',*dnd_hidden],
    hookspath=[], runtime_hooks=[], excludes=[], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='RTX_Video_Render_Studio',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
    console=False, icon=str(root/'assets/app_icon.ico'))
