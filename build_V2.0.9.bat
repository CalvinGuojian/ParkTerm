@echo off
chcp 65001 >nul
echo ========================================
echo   ParkTerm V2.0.8 - Build Script
echo ========================================
echo.
echo [1/3] Installing dependencies...
python -m pip install "flask>=3.0" "flask-socketio>=5.3" "python-socketio>=5.0" "python-engineio>=4.0" simple-websocket "paramiko>=3.4,<4" "scp>=0.14" appdirs "pyinstaller>=6.0"
if errorlevel 1 (
    echo.
    echo [ERROR] Dependency install failed. Scroll up for details.
    echo If pip is slow, try: python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple [packages]
    pause
    exit /b 1
)
echo.
echo [2/3] Building EXE...
pyinstaller --clean --onefile ^
  --add-data "templates;templates" ^
  --add-data "static;static" ^
  --name "ParkTerm_V2.0.8" ^
  --hidden-import=simple_websocket ^
  --hidden-import=engineio.async_drivers.threading ^
  --hidden-import=scp ^
  --hidden-import=appdirs ^
  --hidden-import=parkterm ^
  --hidden-import=parkterm.config ^
  --hidden-import=parkterm.legacy_ssh ^
  --hidden-import=parkterm.ssh_session ^
  --hidden-import=parkterm.file_manager ^
  --hidden-import=parkterm.capture ^
  --hidden-import=parkterm.server ^
  --hidden-import=parkterm.main ^
  --exclude-module=tkinter ^
  --exclude-module=_tkinter ^
  --exclude-module=setuptools ^
  --exclude-module=pip ^
  --exclude-module=distutils ^
  --exclude-module=unittest ^
  --exclude-module=test ^
  --exclude-module=curses ^
  --exclude-module=ftplib ^
  --exclude-module=imaplib ^
  --exclude-module=smtplib ^
  --exclude-module=telnetlib ^
  --exclude-module=nntplib ^
  --exclude-module=xmlrpc ^
  --exclude-module=pydoc_data ^
  --exclude-module=antigravity ^
  --exclude-module=turtle ^
  --icon=app.ico ^
  app.py
if errorlevel 1 (
    echo.
    echo [ERROR] Build failed. Scroll up for details.
    pause
    exit /b 1
)
echo.
echo [3/3] Build complete!
echo EXE: dist\ParkTerm_V2.0.8.exe
echo.
pause
