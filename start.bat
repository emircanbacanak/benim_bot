@echo off
echo Python versiyonu kontrol ediliyor...
python --version

if %errorlevel% neq 0 (
    echo HATA: Python komutu bulunamadı!
    echo Python'ın PATH'te olduğundan emin olun.
    pause
    exit /b 1
)

echo Uygulama başlatılıyor: python crypto_signal_v2.py
python crypto_signal_v2.py
pause
