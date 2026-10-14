@echo off
chcp 65001 >nul
echo ========================================
echo   ParkTerm v1.0.4 - Build Script
echo ========================================

pyinstaller ParkTerm_V1.0.4.spec --noconfirm

if exist "dist\ParkTerm_V1.0.4.exe" (
  echo.
  echo ========================================
  echo   Build Successful!
  echo EXE: dist\ParkTerm_V1.0.4.exe
  echo ========================================
) else (
  echo.
  echo ========================================
  echo   Build Failed!
  echo ========================================
)

pause
