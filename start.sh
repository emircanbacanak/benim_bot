#!/bin/bash

# Python versiyonunu kontrol et
echo "Python versiyonu kontrol ediliyor..."
python --version
python3 --version

# Hangi Python komutunun çalıştığını kontrol et
if command -v python3 &> /dev/null; then
    echo "python3 komutu bulundu, kullanılıyor..."
    PYTHON_CMD="python3"
elif command -v python &> /dev/null; then
    echo "python komutu bulundu, kullanılıyor..."
    PYTHON_CMD="python"
else
    echo "HATA: Python komutu bulunamadı!"
    exit 1
fi

# Uygulamayı çalıştır
echo "Uygulama başlatılıyor: $PYTHON_CMD crypto_signal_v2.py"
exec $PYTHON_CMD crypto_signal_v2.py
