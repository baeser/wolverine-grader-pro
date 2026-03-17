# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Wolverine Grader Pro 3.0
Produces a macOS .app bundle that teachers can double-click to launch.
"""
import os

block_cipher = None
ROOT = os.path.dirname(os.path.abspath(SPEC))

a = Analysis(
    [os.path.join(ROOT, 'app.py')],
    pathex=[ROOT],
    binaries=[],
    datas=[
        (os.path.join(ROOT, 'templates'), 'templates'),
        (os.path.join(ROOT, 'static'), 'static'),
        (os.path.join(ROOT, 'config.py'), '.'),
        (os.path.join(ROOT, 'grader'), 'grader'),
        (os.path.join(ROOT, 'TreetownAILogoNewest.png'), '.'),
        (os.path.join(ROOT, 'VERSION'), '.'),
    ],
    hiddenimports=[
        'flask',
        'jinja2',
        'anthropic',
        'openai',
        'google.generativeai',
        'docx',
        'pdfplumber',
        'pdfminer',
        'pdfminer.high_level',
        'config',
        'grader',
        'grader.ai_client',
        'grader.canvas_client',
        'grader.extractor',
        'grader.prompt_builder',
        'grader.session_store',
        'grader.license_manager',
        'certifi',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='WolverineGraderPro',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,       # No terminal window
    target_arch=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='WolverineGraderPro',
)

app = BUNDLE(
    coll,
    name='Wolverine Grader Pro.app',
    icon=os.path.join(ROOT, 'build_assets', 'app_icon.icns'),
    bundle_identifier='com.treetownai.wolverinegraderpro',
    info_plist={
        'CFBundleName': 'Wolverine Grader Pro',
        'CFBundleDisplayName': 'Wolverine Grader Pro',
        'CFBundleVersion': '3.1.5',
        'CFBundleShortVersionString': '3.1',
        'NSHighResolutionCapable': True,
    },
)
