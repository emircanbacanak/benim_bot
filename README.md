# Kripto Sinyal Botu

Bu bot, Binance Futures'de kripto para çiftleri için otomatik sinyal üretir ve Telegram üzerinden kullanıcılara gönderir.

## Özellikler

- **4 Kripto Para Desteği**: SOLUSDT, AVAXUSDT, ETHUSDT, ADAUSDT
- **Özel Timeframe Kombinasyonları**: Her kripto için optimize edilmiş timeframe'ler
- **2/2 Sinyal Sistemi**: Belirlenen timeframe'lerde aynı yönde sinyal gerekli
- **15 Dakikalık Mum Onayı**: Sinyal kalitesini artırmak için ek kontrol
- **Telegram Bot Entegrasyonu**: Otomatik sinyal gönderimi
- **MongoDB Veritabanı**: Pozisyon ve sinyal takibi
- **Web Arayüzü**: Port 8000'de web sunucusu

## Kurulum

### Gereksinimler

- Python 3.11+
- MongoDB veritabanı
- Telegram Bot Token
- Binance API anahtarları

### Bağımlılıklar

```bash
pip install -r requirements.txt
```

### Çevre Değişkenleri

`.env` dosyasında aşağıdaki değişkenleri tanımlayın:

```env
TELEGRAM_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id
BOT_OWNER_ID=your_user_id
MONGODB_URI=your_mongodb_connection_string
MONGODB_DB=crypto_signal_bot
MONGODB_COLLECTION=allowed_users
```

## Kullanım

### Yerel Çalıştırma

```bash
python crypto_signal_v2.py
```

### Docker ile Çalıştırma

```bash
# Docker image oluştur
docker build -t crypto-signal-bot .

# Container çalıştır
docker run -p 8000:8000 crypto-signal-bot
```

### Heroku Deployment

```bash
# Heroku app oluştur
heroku create your-app-name

# Deploy et
git push heroku main
```

## Bot Komutları

- `/help` - Yardım menüsü
- `/stats` - Bot istatistikleri
- `/active` - Aktif sinyaller
- `/test` - Test sinyali gönder
- `/adduser <user_id>` - Kullanıcı ekle (Admin)
- `/removeuser <user_id>` - Kullanıcı çıkar (Admin)
- `/listusers` - İzin verilen kullanıcıları listele (Admin)

## Sinyal Sistemi

### Timeframe Kombinasyonları

- **SOLUSDT**: 1h + 2h
- **AVAXUSDT**: 30m + 1h  
- **ETHUSDT**: 30m + 1h
- **ADAUSDT**: 30m + 1h

### Sinyal Kuralları

1. **2/2 Kuralı**: Belirlenen timeframe'lerde aynı yönde sinyal olmalı
2. **Mum Onayı**: 15 dakikalık mum rengi sinyal yönü ile uyumlu olmalı
3. **Hacim Kontrolü**: Yeterli işlem hacmi olmalı
4. **Trend Kontrolü**: EMA 200 üzerinde/altında olma durumu

### Risk Yönetimi

- **Take Profit**: Kripto özel yüzdeler (5%-20%)
- **Stop Loss**: Kripto özel yüzdeler (2.5%-10%)
- **Kaldıraç**: 10x (sabit)

## Teknik Detaylar

- **Web Framework**: aiohttp
- **Telegram Bot**: python-telegram-bot
- **Veritabanı**: MongoDB (PyMongo)
- **Teknik Analiz**: TA-Lib (ta)
- **Veri Kaynağı**: Binance Futures API

## Sorun Giderme

### Container Hataları

Eğer Docker container'da "command not found" hatası alıyorsanız:

1. `start.sh` script'inin çalıştırılabilir olduğundan emin olun
2. Python komutunun PATH'te olduğunu kontrol edin
3. `Procfile` dosyasının doğru formatta olduğunu kontrol edin

### MongoDB Bağlantı Sorunları

1. MongoDB URI'nin doğru olduğunu kontrol edin
2. Network erişimini kontrol edin
3. Veritabanı kullanıcı yetkilerini kontrol edin

## Lisans

Bu proje MIT lisansı altında lisanslanmıştır.
