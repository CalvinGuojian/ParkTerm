@echo off
chcp 65001 >nul
echo ========================================
echo   ParkTerm V2.0.12 - Build Script
echo ========================================

pyinstaller ParkTerm_V2.0.12.spec --noconfirm

if exist "dist\ParkTerm_V2.0.12.exe" (
  echo.
  echo ========================================
  echo   Build Successful!
  echo EXE: dist\ParkTerm_V2.0.12.exe
  echo ========================================
) else (
  echo.
  echo ========================================
  echo   Build Failed!
  echo ========================================
)

pause
